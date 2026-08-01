from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.runtime.container_workspace import (
    ContainerWorkspaceConfig,
    ContainerWorkspaceTools,
    container_config_from_settings,
    verify_container_workspace,
)
from dynamic_firm.runtime.models import ActionPolicy, ToolEffect, ToolGrant
from dynamic_firm.runtime.ports import CancellationToken, OperationCancelled
from dynamic_firm.runtime.tools import ToolExecutionError, ToolExecutor


class _Process:
    pid = None
    returncode = 0

    async def communicate(self):
        return b"ok", b""

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


class _CancelledProcess(_Process):
    returncode = None

    async def communicate(self):
        await asyncio.Event().wait()
        return b"", b""


class ContainerWorkspaceToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ContainerWorkspaceConfig(
            image="python:3.11-alpine", programs={"tests": ("/usr/bin/pytest",)}
        )
        self.tools = ContainerWorkspaceTools(self.config, Path.cwd())
        self.definition = self.tools.definition()

    def test_config_accepts_only_explicit_program_arrays_and_bounded_limits(self) -> None:
        config = container_config_from_settings({"container": {"enabled": True, "image": "python:3.11-alpine", "programs": {"tests": ["/usr/bin/pytest"]}}})
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.programs["tests"], ("/usr/bin/pytest",))
        with self.assertRaisesRegex(ValueError, "programs"):
            container_config_from_settings({"container": {"enabled": True, "image": "python:3.11", "programs": {"shell": "/bin/sh"}}})

    def test_definition_rejects_generic_shell_and_policy_requires_dynamic_approval(self) -> None:
        self.assertEqual(self.definition.validator({"program_id": "tests", "arguments": ["-q"]}), {"program_id": "tests", "arguments": ("-q",)})
        with self.assertRaisesRegex(Exception, "allowlist"):
            self.definition.validator({"program_id": "shell", "arguments": []})
        grant = ToolGrant(tool_name=self.definition.name, allowed_effects=(ToolEffect.EXECUTE,), resource_patterns=("container-workspace:python:3.11-alpine:*",), max_calls=1, requires_approval=True)
        policy = ActionPolicy(tool_grants=(grant,), sandbox_profile="host-workspace-approved")
        self.assertIsNone(ToolExecutor._policy_denial(self.definition, grant, policy, "container-workspace:python:3.11-alpine:tests", 0))
        denied = ActionPolicy(tool_grants=(grant,), sandbox_profile="none")
        self.assertIn("outside", ToolExecutor._policy_denial(self.definition, grant, denied, "container-workspace:python:3.11-alpine:tests", 0) or "")
        self.assertTrue(self.definition.requires_approval)

    def test_hardened_command_disables_network_and_privilege_paths(self) -> None:
        command = self.tools._command("tests", ("-q",))
        self.assertIn("--network", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("--cap-drop", command)
        self.assertIn("--read-only", command)
        self.assertIn("no-new-privileges", command)
        self.assertNotIn("--privileged", command)

    def test_execution_returns_bounded_receipt_without_docker_shell(self) -> None:
        process = _Process()

        async def spawn(*command, **kwargs):
            self.assertEqual(command[0], "docker")
            self.assertNotIn("/bin/sh", command)
            return process

        with patch("dynamic_firm.runtime.container_workspace.asyncio.create_subprocess_exec", spawn):
            receipt = json.loads(asyncio.run(self.tools._execute("tests", (), CancellationToken())))
        self.assertEqual(receipt["program_id"], "tests")
        self.assertEqual(receipt["output"], "ok")

    def test_cancellation_terminates_container_process(self) -> None:
        token = CancellationToken()
        token.cancel("operator cancelled")
        process = _CancelledProcess()

        async def spawn(*command, **kwargs):
            return process

        with patch("dynamic_firm.runtime.container_workspace.asyncio.create_subprocess_exec", spawn):
            with self.assertRaises(OperationCancelled):
                asyncio.run(self.tools._execute("tests", (), token))
        self.assertEqual(process.returncode, -15)

    def test_metadata_preflight_never_starts_or_pulls_a_container_image(self) -> None:
        captured: list[object] = []
        def runner(command, **kwargs):
            captured.append((command, kwargs))
            output = "25.0.0" if command[1] == "version" else "sha256:" + "a" * 64
            return subprocess.CompletedProcess(command, 0, output, "")
        with patch("dynamic_firm.runtime.container_workspace.shutil.which", return_value="/usr/bin/docker"):
            result = verify_container_workspace(self.config, runner=runner)
        self.assertTrue(result.runtime_available); self.assertTrue(result.image_present)
        self.assertFalse(result.image_reference_pinned)
        self.assertEqual([item[0][1:3] for item in captured], [["version", "--format"], ["image", "inspect"]])
        self.assertNotIn("run", captured[0][0]); self.assertNotIn("pull", captured[1][0])

    def test_metadata_preflight_distinguishes_engine_unavailable_from_missing_image(self) -> None:
        with patch("dynamic_firm.runtime.container_workspace.shutil.which", return_value="/usr/bin/docker"):
            absent = verify_container_workspace(self.config, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0 if command[1] == "version" else 1, "25.0" if command[1] == "version" else "", ""))
            unavailable = verify_container_workspace(self.config, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", ""))
        self.assertTrue(absent.runtime_available); self.assertFalse(absent.image_present)
        self.assertFalse(unavailable.runtime_available); self.assertFalse(unavailable.image_present)
