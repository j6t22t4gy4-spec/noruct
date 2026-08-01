from __future__ import annotations

import json
import subprocess
import unittest
import tempfile
import asyncio
from unittest.mock import patch
from pathlib import Path

from dynamic_firm.runtime.models import ActionPolicy, ToolEffect, ToolGrant
from dynamic_firm.runtime.remote_workspace import RemoteWorkspaceTools, RemoteWorkspaceWorkerConfig, remote_worker_config_from_settings, verify_remote_workspace_worker, verify_remote_workspace_worker_content
from dynamic_firm.runtime.tools import ToolExecutionError, ToolExecutor
from dynamic_firm.runtime.ports import CancellationToken, OperationCancelled


class _CancelledProcess:
    pid = None
    returncode = None
    async def communicate(self):
        await asyncio.Event().wait()
        return b"", b""
    def terminate(self): self.returncode = -15
    async def wait(self): return self.returncode


class _FailedProcess:
    pid = None
    returncode = 7
    async def communicate(self): return b"test failed", b""
    async def wait(self): return self.returncode


class RemoteWorkspaceToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definition = RemoteWorkspaceTools(RemoteWorkspaceWorkerConfig(
            target_id="build", host="build.example.test", user="operator", port=22,
            identity_file=None, snapshot_directory="/srv/company/.noruct-remote-snapshots/" + "a" * 64,
            snapshot_sha256="a" * 64, programs={"tests": "/usr/bin/true"},
        )).definition()

    def test_only_allowlisted_program_and_bounded_arguments_are_valid(self) -> None:
        self.assertEqual(self.definition.validator({"program_id":"tests","arguments":["-q"]}), {"program_id":"tests","arguments":("-q",)})
        with self.assertRaisesRegex(Exception, "allowlist"):
            self.definition.validator({"program_id":"shell","arguments":[]})
        with self.assertRaisesRegex(Exception, "at most"):
            self.definition.validator({"program_id":"tests","arguments":["x"] * 17})

    def test_high_risk_remote_tool_requires_approval_and_remote_policy_profile(self) -> None:
        policy = ActionPolicy(tool_grants=(ToolGrant(
            tool_name=self.definition.name, allowed_effects=(ToolEffect.EXECUTE,),
            resource_patterns=("remote-workspace:build:tests:*",), max_calls=1, requires_approval=True,
        ),), sandbox_profile="remote-workspace-approved")
        self.assertIsNone(ToolExecutor._policy_denial(self.definition, policy.tool_grants[0], policy, "remote-workspace:build:tests:" + "a" * 64, 0))
        denied = ActionPolicy(tool_grants=policy.tool_grants, sandbox_profile="none")
        self.assertIn("outside", ToolExecutor._policy_denial(self.definition, denied.tool_grants[0], denied, "remote-workspace:build:tests:" + "a" * 64, 0) or "")
        self.assertTrue(self.definition.requires_approval)

    def test_config_requires_a_verified_strict_host_transfer_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            receipt.write_text(json.dumps({
                "host":"build.example.test", "user":"operator", "port":22,
                "remote_snapshot_directory":"/srv/company/.noruct-remote-snapshots/" + "b" * 64,
                "snapshot_sha256":"b" * 64, "transferred":True,
                "integrity_state":"VERIFIED_REMOTE_SNAPSHOT", "host_key_policy":"STRICT_KNOWN_HOSTS_ONLY",
                "remote_job_execution":"NOT_IMPLEMENTED",
            }), encoding="utf-8")
            config = remote_worker_config_from_settings({"remote_worker":{
                "enabled":True, "target_id":"build", "receipt":str(receipt),
                "programs":{"tests":"/usr/bin/true"},
            }})
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.snapshot_sha256, "b" * 64)
        self.assertEqual(config.programs["tests"], "/usr/bin/true")

    def test_cancellation_terminates_the_remote_ssh_process(self) -> None:
        tools = RemoteWorkspaceTools(RemoteWorkspaceWorkerConfig(
            target_id="build", host="build.example.test", user="operator", port=22,
            identity_file=None, snapshot_directory="/srv/company/.noruct-remote-snapshots/" + "c" * 64,
            snapshot_sha256="c" * 64, programs={"tests":"/usr/bin/true"},
        ))
        token = CancellationToken(); token.cancel("operator cancelled")
        process = _CancelledProcess()
        async def fake_spawn(*command, **kwargs):
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("ForwardAgent=no", command)
            return process
        with patch("dynamic_firm.runtime.remote_workspace.asyncio.create_subprocess_exec", fake_spawn):
            with self.assertRaises(OperationCancelled):
                asyncio.run(tools._execute("tests", (), token))
        self.assertEqual(process.returncode, -15)

    def test_nonzero_remote_exit_is_a_tool_failure_not_a_success_receipt(self) -> None:
        tools = RemoteWorkspaceTools(RemoteWorkspaceWorkerConfig(
            target_id="build", host="build.example.test", user="operator", port=22,
            identity_file=None, snapshot_directory="/srv/company/.noruct-remote-snapshots/" + "d" * 64,
            snapshot_sha256="d" * 64, programs={"tests":"/usr/bin/true"},
        ))
        async def fake_spawn(*command, **kwargs): return _FailedProcess()
        with patch("dynamic_firm.runtime.remote_workspace.asyncio.create_subprocess_exec", fake_spawn):
            with self.assertRaisesRegex(ToolExecutionError, "status 7"):
                asyncio.run(tools._execute("tests", (), CancellationToken()))

    def test_config_rejects_noncanonical_or_untrusted_receipt_target_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            receipt.write_text(json.dumps({
                "host":"build.example.test;not-a-host", "user":"operator", "port":22,
                "remote_snapshot_directory":"/srv/company/.noruct-remote-snapshots/" + "e" * 64 + "/..",
                "snapshot_sha256":"e" * 64, "transferred":True,
                "integrity_state":"VERIFIED_REMOTE_SNAPSHOT", "host_key_policy":"STRICT_KNOWN_HOSTS_ONLY",
                "remote_job_execution":"NOT_IMPLEMENTED",
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid target facts"):
                remote_worker_config_from_settings({"remote_worker":{
                    "enabled":True, "target_id":"build", "receipt":str(receipt),
                    "programs":{"tests":"/usr/bin/true"},
                }})

    def test_fixed_marker_verification_checks_only_the_receipt_bound_snapshot(self) -> None:
        config = RemoteWorkspaceWorkerConfig(
            target_id="build", host="build.example.test", user="operator", port=22,
            identity_file=None, snapshot_directory="/srv/company/.noruct-remote-snapshots/" + "f" * 64,
            snapshot_sha256="f" * 64, programs={"tests": "/usr/bin/true"},
        )
        captured: list[object] = []
        marker = "noruct-remote-worker-v1:present:" + "f" * 64
        def runner(command, **kwargs):
            captured.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, marker, "")
        with patch("dynamic_firm.runtime.remote_workspace.shutil.which", return_value="/usr/bin/ssh"):
            result = verify_remote_workspace_worker(config, runner=runner)
        self.assertTrue(result.reachable); self.assertTrue(result.snapshot_present)
        command, kwargs = captured[0]
        self.assertIn("StrictHostKeyChecking=yes", command); self.assertIn("ForwardAgent=no", command)
        self.assertIn("test -d --", command[-1]); self.assertNotIn("/usr/bin/true", command[-1])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_fixed_marker_verification_distinguishes_a_missing_snapshot_from_unreachable_ssh(self) -> None:
        config = RemoteWorkspaceWorkerConfig(
            target_id="build", host="build.example.test", user="operator", port=22,
            identity_file=None, snapshot_directory="/srv/company/.noruct-remote-snapshots/" + "1" * 64,
            snapshot_sha256="1" * 64, programs={"tests": "/usr/bin/true"},
        )
        missing = "noruct-remote-worker-v1:missing:" + "1" * 64
        with patch("dynamic_firm.runtime.remote_workspace.shutil.which", return_value="/usr/bin/ssh"):
            absent = verify_remote_workspace_worker(config, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, missing, ""))
            unreachable = verify_remote_workspace_worker(config, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 255, "", ""))
        self.assertTrue(absent.reachable); self.assertFalse(absent.snapshot_present)
        self.assertFalse(unreachable.reachable); self.assertFalse(unreachable.snapshot_present)

    def test_fixed_ledger_audit_verifies_transferred_content_without_executing_a_program(self) -> None:
        config = RemoteWorkspaceWorkerConfig(
            target_id="build", host="build.example.test", user="operator", port=22,
            identity_file=None, snapshot_directory="/srv/company/.noruct-remote-snapshots/" + "2" * 64,
            snapshot_sha256="2" * 64, programs={"tests": "/usr/bin/true"},
        )
        marker = "noruct-remote-audit-v1:" + "2" * 64 + ":verified"
        captured: list[object] = []
        def runner(command, **kwargs):
            captured.append((command, kwargs)); return subprocess.CompletedProcess(command, 0, marker, "")
        with patch("dynamic_firm.runtime.remote_workspace.shutil.which", return_value="/usr/bin/ssh"):
            result = verify_remote_workspace_worker_content(config, runner=runner)
        self.assertTrue(result.reachable); self.assertTrue(result.snapshot_present); self.assertTrue(result.content_verified)
        command, kwargs = captured[0]
        self.assertIn("sha256sum -c .noruct-transfer-sha256", command[-1])
        self.assertNotIn("/usr/bin/true", command[-1]); self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_fixed_ledger_audit_reports_remote_content_mismatch(self) -> None:
        config = RemoteWorkspaceWorkerConfig(
            target_id="build", host="build.example.test", user="operator", port=22,
            identity_file=None, snapshot_directory="/srv/company/.noruct-remote-snapshots/" + "3" * 64,
            snapshot_sha256="3" * 64, programs={"tests": "/usr/bin/true"},
        )
        marker = "noruct-remote-audit-v1:" + "3" * 64 + ":mismatch"
        with patch("dynamic_firm.runtime.remote_workspace.shutil.which", return_value="/usr/bin/ssh"):
            result = verify_remote_workspace_worker_content(config, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, marker, ""))
        self.assertTrue(result.reachable); self.assertTrue(result.snapshot_present); self.assertFalse(result.content_verified)
        self.assertEqual(result.integrity_state, "REMOTE_LEDGER_MISMATCH")
