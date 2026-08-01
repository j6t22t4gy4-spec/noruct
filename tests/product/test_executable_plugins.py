from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
import io
import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, _action_policy, _run_config, build_parser, main
from dynamic_firm.product.executable_plugins import ExecutablePluginStore, PluginGitUpdateReview, PluginLifecycleError
from dynamic_firm.runtime.models import ToolEffect
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolExecutionError


class ExecutablePluginTests(unittest.TestCase):
    def _source(self, root: Path, *, version: str = "1.0.0", dependency_lock: bool = False) -> Path:
        source = root / f"example-plugin-{version}"
        source.mkdir()
        (source / "noruct-plugin.json").write_text(
            json.dumps(
                {
                    "schema": "noruct.executable-plugin.v1",
                    "plugin_id": "echo",
                    "version": version,
                    "description": "A fixture plugin.",
                    "command": ["host.py"],
                    "environment": [],
                    "timeout_seconds": 5,
                    "tools": [
                        {
                            "name": "plugin_echo_reply",
                            "description": "Return one bounded echo response.",
                            "input_schema": {
                                "type": "object",
                                "properties": {"text": {"type": "string", "maxLength": 32}},
                                "required": ["text"],
                                "additionalProperties": False,
                            },
                        }
                    ],
                    **({"dependency_lock": "requirements.lock"} if dependency_lock else {}),
                }
            ),
            encoding="utf-8",
        )
        host = source / "host.py"
        host.write_text(
            "#!/usr/bin/env python3\nimport json,sys\nv=json.load(sys.stdin)\njson.dump({'schema':'noruct.executable-plugin-response.v1','ok':True,'result':{'text':v['arguments']['text']}},sys.stdout)\n",
            encoding="utf-8",
        )
        host.chmod(host.stat().st_mode | stat.S_IXUSR)
        if dependency_lock:
            (source / "requirements.lock").write_text(
                "# Exact wheel hashes are required for real dependency builds.\n",
                encoding="utf-8",
            )
        return source

    def test_install_enable_and_out_of_process_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ExecutablePluginStore(root / "managed")
            source = self._source(root)
            installed = store.install(source)
            self.assertFalse(installed.enabled)
            self.assertEqual(store.active(), ())
            enabled = store.set_enabled("echo", True)
            self.assertTrue(enabled.enabled)
            definition = store.active()[0].definitions()[0]
            self.assertEqual(definition.effect, ToolEffect.EXECUTE)
            self.assertTrue(definition.requires_approval)
            arguments = definition.validator({"text": "hello"})
            raw = asyncio.run(definition.handler(arguments, CancellationToken()))
            self.assertEqual(json.loads(raw)["result"], {"text": "hello"})
            with self.assertRaises(Exception):
                definition.validator({"text": "x" * 33})

    def test_cli_lifecycle_and_runtime_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            config_path = root / "config.toml"
            stdout, stderr = io.StringIO(), io.StringIO()
            self.assertEqual(
                main(["--config", str(config_path), "plugin", "install", str(source), "--confirm", "--json"], stdout=stdout, stderr=stderr),
                EXIT_OK,
            )
            self.assertEqual(main(["--config", str(config_path), "plugin", "enable", "echo", "--confirm"], stdout=stdout, stderr=stderr), EXIT_OK)
            import tomllib
            settings = tomllib.loads(config_path.read_text(encoding="utf-8"))
            args = type("Args", (), {
                "goal": "inspect", "workspace": root, "state": root / "state.db", "provider_kind": "openai_api", "base_url": "http://127.0.0.1:1", "model": "fixture", "codex_command": None, "external_command": None, "api_key_env": None, "no_auth": True, "request_timeout": 1.0, "max_wall_time": 1.0, "max_model_calls": 1, "max_tool_calls": 4, "max_cost_usd": 1.0, "cost_mode": "standard", "permission_mode": "ask", "employee_runtime": "noruct", "runtime_python": None, "skills_dir": None,
            })()
            config = _run_config(args, settings)
            self.assertEqual(len(config.executable_plugins.plugins), 1)
            policy = _action_policy(config)
            grant = next(item for item in policy.tool_grants if item.tool_name == "plugin_echo_reply")
            self.assertEqual(grant.allowed_effects, (ToolEffect.EXECUTE,))
            self.assertTrue(grant.requires_approval)
            self.assertIn("plugin_echo_reply", policy.auto_approved_tool_names)

    def test_install_is_inactive_reports_exact_closure_and_never_mutates_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root, dependency_lock=True)
            source_before = {
                path.relative_to(source).as_posix(): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }
            config_path = root / "config.toml"
            output = io.StringIO()

            self.assertEqual(
                main(
                    [
                        "--config",
                        str(config_path),
                        "plugin",
                        "install",
                        str(source),
                        "--confirm",
                        "--json",
                    ],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )

            record = json.loads(output.getvalue())
            installed = record["installed"]
            self.assertEqual(installed["state"], "INSTALLED_INACTIVE")
            self.assertFalse(installed["enabled"])
            self.assertFalse(installed["activation_requested"])
            self.assertRegex(installed["package_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(installed["source"], {"kind": "local_directory"})
            self.assertEqual(
                installed["dependency_closure"]["lock_file"],
                "requirements.lock",
            )
            self.assertRegex(
                installed["dependency_closure"]["lock_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertFalse(installed["dependency_closure"]["environment_ready"])
            self.assertIn("--version 1.0.0 --confirm", installed["next_action"])
            self.assertEqual(record["active_count"], 0)
            self.assertEqual(
                source_before,
                {
                    path.relative_to(source).as_posix(): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                },
            )

    def test_install_commands_have_no_combined_activation_flag(self) -> None:
        parser = build_parser()
        cases = (
            ("plugin", "install", "/tmp/plugin", "--confirm", "--enable"),
            (
                "plugin",
                "install-git",
                "--url",
                "https://example.test/plugin.git",
                "--commit",
                "a" * 40,
                "--confirm",
                "--enable",
            ),
            (
                "plugin",
                "catalog-install",
                "official",
                "echo",
                "--confirm",
                "--enable",
            ),
        )
        for arguments in cases:
            with self.subTest(command=arguments[1]), self.assertRaises(SystemExit):
                with redirect_stderr(io.StringIO()):
                    parser.parse_args(arguments)

    def test_manifest_escape_drift_missing_binary_timeout_and_crash_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            escaped = self._source(root, version="escape")
            manifest = json.loads(
                (escaped / "noruct-plugin.json").read_text(encoding="utf-8")
            )
            manifest["command"] = ["../outside-host"]
            (escaped / "noruct-plugin.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(PluginLifecycleError, "escapes"):
                ExecutablePluginStore(root / "escape-store").install(escaped)

            store = ExecutablePluginStore(root / "managed")
            installed = store.install(self._source(root, version="drift"))
            store.activate(installed.plugin_id, version=installed.version)
            definition = store.active()[0].definitions()[0]
            installed.command_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            with self.assertRaisesRegex(ToolExecutionError, "changed"):
                asyncio.run(definition.handler({"text": "blocked"}, CancellationToken()))
            installed.command_path.unlink()
            with self.assertRaisesRegex(ToolExecutionError, "unavailable"):
                asyncio.run(definition.handler({"text": "blocked"}, CancellationToken()))

            crash_store = ExecutablePluginStore(root / "crash-store")
            crash_source = self._source(root, version="crash")
            (crash_source / "host.py").write_text(
                "#!/bin/sh\nexit 7\n", encoding="utf-8"
            )
            crash = crash_store.install(crash_source)
            crash_store.activate(crash.plugin_id, version=crash.version)
            crash_definition = crash_store.active()[0].definitions()[0]
            with self.assertRaisesRegex(ToolExecutionError, "process failed"):
                asyncio.run(
                    crash_definition.handler(
                        {"text": "blocked"}, CancellationToken()
                    )
                )

            timeout_store = ExecutablePluginStore(root / "timeout-store")
            timeout_source = self._source(root, version="timeout")
            timeout_manifest = json.loads(
                (timeout_source / "noruct-plugin.json").read_text(encoding="utf-8")
            )
            timeout_manifest["timeout_seconds"] = 1
            (timeout_source / "noruct-plugin.json").write_text(
                json.dumps(timeout_manifest), encoding="utf-8"
            )
            (timeout_source / "host.py").write_text(
                "#!/bin/sh\nsleep 5\n", encoding="utf-8"
            )
            timeout = timeout_store.install(timeout_source)
            timeout_store.activate(timeout.plugin_id, version=timeout.version)
            timeout_definition = timeout_store.active()[0].definitions()[0]
            with self.assertRaisesRegex(ToolExecutionError, "timed out"):
                asyncio.run(
                    timeout_definition.handler(
                        {"text": "blocked"}, CancellationToken()
                    )
                )

    def test_oversized_plugin_response_is_terminated_before_unbounded_buffering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ExecutablePluginStore(root / "managed")
            source = self._source(root, version="oversized")
            (source / "host.py").write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdout.write('x' * 70000)\n"
                "sys.stdout.flush()\n"
                "import time\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            host = source / "host.py"
            host.chmod(host.stat().st_mode | stat.S_IXUSR)
            installed = store.install(source)
            store.activate(installed.plugin_id, version=installed.version)
            definition = store.active()[0].definitions()[0]
            with self.assertRaisesRegex(ToolExecutionError, "output limit"):
                asyncio.run(
                    definition.handler({"text": "blocked"}, CancellationToken())
                )

    def test_rejects_symlink_and_unbounded_plugin_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            (source / "linked").symlink_to(source / "host.py")
            with self.assertRaises(PluginLifecycleError):
                ExecutablePluginStore(root / "managed").install(source)

    def test_rejects_symbolic_link_directory_before_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            outside = root / "outside"
            outside.mkdir()
            (outside / "unreviewed.py").write_text("value = 1\n", encoding="utf-8")
            (source / "linked-directory").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(PluginLifecycleError, "symbolic links"):
                ExecutablePluginStore(root / "managed").install(source)

    def test_git_intake_requires_https_and_an_exact_commit_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = ExecutablePluginStore(Path(temp) / "managed")
            with self.assertRaisesRegex(PluginLifecycleError, "HTTPS"):
                store.install_git("git@github.com:example/plugin.git", "a" * 40)
            with self.assertRaisesRegex(PluginLifecycleError, "40-character"):
                store.install_git("https://github.com/example/plugin.git", "main")

    def test_git_intake_stages_the_pinned_checkout_and_records_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            commit = "a" * 40
            store = ExecutablePluginStore(root / "managed")

            def fake_git(command: tuple[str, ...]) -> str:
                if command[1] == "clone":
                    shutil.copytree(source, Path(command[-1]))
                    return ""
                if command[-2:] == ("rev-parse", "HEAD"):
                    return commit + "\n"
                return ""

            with patch("dynamic_firm.product.executable_plugins.shutil.which", return_value="git"), patch.object(ExecutablePluginStore, "_git", side_effect=fake_git):
                installed = store.install_git("https://github.com/example/plugin.git", commit)

            self.assertFalse(installed.enabled)
            registry = json.loads(store.registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["receipts"]["echo@1.0.0"], {
                "kind": "git", "repository_url": "https://github.com/example/plugin.git",
                "commit": commit, "subdirectory": ".",
            })

    def test_catalog_provenance_is_retained_in_the_safe_git_source_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            commit = "a" * 40
            store = ExecutablePluginStore(root / "managed")

            def fake_git(command: tuple[str, ...]) -> str:
                if command[1] == "clone":
                    shutil.copytree(source, Path(command[-1])); return ""
                if command[-2:] == ("rev-parse", "HEAD"):
                    return commit + "\n"
                return ""

            with patch("dynamic_firm.product.executable_plugins.shutil.which", return_value="git"), patch.object(ExecutablePluginStore, "_git", side_effect=fake_git):
                store.install_git(
                    "https://github.com/example/plugin.git", commit,
                    catalog_provenance={"catalog_id": "official", "catalog_digest": "b" * 64},
                )
            self.assertEqual(store.source_receipt("echo", version="1.0.0"), {
                "kind": "git", "repository_url": "https://github.com/example/plugin.git", "commit": commit,
                "subdirectory": ".", "catalog_id": "official", "catalog_digest": "b" * 64,
            })

    def test_pre_receipt_registry_remains_listable_as_legacy_unrecorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ExecutablePluginStore(root / "managed")
            source = self._source(root)
            installed = store.install(source)
            registry = json.loads(store.registry_path.read_text(encoding="utf-8"))
            registry.pop("receipts")
            registry.pop("environments")
            store.registry_path.write_text(json.dumps(registry), encoding="utf-8")
            self.assertEqual(store.source_receipt(installed.plugin_id, version=installed.version), {"kind": "legacy_unrecorded"})

    def test_git_update_review_resolves_one_ref_without_mutating_the_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            installed_commit, candidate_commit = "a" * 40, "b" * 40
            store = ExecutablePluginStore(root / "managed")

            def fake_git(command: tuple[str, ...]) -> str:
                if command[1] == "clone":
                    shutil.copytree(source, Path(command[-1])); return ""
                if command[-2:] == ("rev-parse", "HEAD"):
                    return installed_commit + "\n"
                if command[1] == "ls-remote":
                    self.assertEqual(command[-1], "refs/heads/main")
                    return f"{candidate_commit}\trefs/heads/main\n"
                return ""

            with patch("dynamic_firm.product.executable_plugins.shutil.which", return_value="git"), patch.object(ExecutablePluginStore, "_git", side_effect=fake_git):
                store.install_git("https://github.com/example/plugin.git", installed_commit)
                before = store.registry_path.read_bytes()
                review = store.review_git_update("echo", ref="refs/heads/main")
                after = store.registry_path.read_bytes()

            self.assertTrue(review.update_available)
            self.assertEqual(review.candidate_commit, candidate_commit)
            self.assertEqual(before, after)

    def test_git_update_review_rejects_mutable_or_local_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); store = ExecutablePluginStore(root / "managed")
            store.install(self._source(root))
            with self.assertRaisesRegex(PluginLifecycleError, "exact Git receipt"):
                store.review_git_update("echo", ref="refs/heads/main")

    def test_versioned_activation_and_rollback_keep_an_installed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ExecutablePluginStore(root / "managed")
            store.install(self._source(root, version="1.0.0"))
            store.install(self._source(root, version="2.0.0"))
            self.assertEqual(store.activate("echo", version="1.0.0").version, "1.0.0")
            self.assertEqual(store.activate("echo", version="2.0.0").version, "2.0.0")
            rolled_back = store.rollback("echo")
            self.assertEqual(rolled_back.version, "1.0.0")
            self.assertEqual([(item.version, item.enabled) for item in store.list()], [("1.0.0", True), ("2.0.0", False)])
            history = store.lifecycle_receipts("echo", limit=10)
            self.assertEqual(
                [item["action"] for item in history],
                [
                    "ROLLED_BACK_FUTURE_JOB",
                    "ACTIVATED_FUTURE_JOB",
                    "ACTIVATED_FUTURE_JOB",
                    "INSTALLED_INACTIVE",
                    "INSTALLED_INACTIVE",
                ],
            )
            self.assertEqual([item["sequence"] for item in reversed(history)], [1, 2, 3, 4, 5])
            self.assertEqual(history[0]["versions"], ["1.0.0"])
            self.assertRegex(str(history[0]["package_sha256"][0]), r"^[0-9a-f]{64}$")

    def test_lifecycle_history_survives_withdrawal_without_source_or_package_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ExecutablePluginStore(root / "managed")
            source = self._source(root)
            installed = store.install(source)
            store.activate("echo", version="1.0.0")
            store.set_enabled("echo", False)
            self.assertTrue(store.remove("echo"))

            history = store.lifecycle_receipts(limit=20)
            self.assertEqual(
                [item["action"] for item in history],
                [
                    "WITHDRAWN_FUTURE_JOB",
                    "DISABLED_FUTURE_JOB",
                    "ACTIVATED_FUTURE_JOB",
                    "INSTALLED_INACTIVE",
                ],
            )
            rendered = json.dumps(history, sort_keys=True)
            self.assertNotIn(str(source), rendered)
            self.assertNotIn(str(installed.package_path), rendered)
            self.assertNotIn("A fixture plugin.", rendered)
            self.assertEqual(history[0]["versions"], ["1.0.0"])

    def test_malformed_lifecycle_history_fails_closed_before_plugin_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ExecutablePluginStore(root / "managed")
            store.install(self._source(root))
            registry = json.loads(store.registry_path.read_text(encoding="utf-8"))
            registry["lifecycle_receipts"][0]["package_sha256"] = ["not-a-digest"]
            store.registry_path.write_text(json.dumps(registry), encoding="utf-8")

            with self.assertRaisesRegex(PluginLifecycleError, "lifecycle receipts"):
                store.list()

    def test_cli_history_is_read_only_and_works_after_runtime_disable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.toml"
            managed = root / "managed"
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "--config", str(config_path), "plugin", "install", str(self._source(root)),
                        "--root", str(managed), "--confirm",
                    ],
                    stdout=io.StringIO(), stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertEqual(
                main(
                    ["--config", str(config_path), "plugin", "runtime-disable"],
                    stdout=io.StringIO(), stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            before = (managed / "registry.json").read_bytes()
            self.assertEqual(
                main(
                    [
                        "--config", str(config_path), "plugin", "history", "echo", "--root",
                        str(managed), "--limit", "1", "--json",
                    ],
                    stdout=output, stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertEqual(before, (managed / "registry.json").read_bytes())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["receipt_count"], 1)
            self.assertEqual(payload["receipts"][0]["action"], "INSTALLED_INACTIVE")
            self.assertIn("no_host_load_or_execution", payload["authority"])

    def test_cli_rollback_activates_the_previous_installed_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); config_path = root / "config.toml"; stdout = io.StringIO()
            self.assertEqual(main(["--config", str(config_path), "plugin", "install", str(self._source(root, version="1.0.0")), "--confirm"], stdout=stdout, stderr=io.StringIO()), EXIT_OK)
            self.assertEqual(main(["--config", str(config_path), "plugin", "install", str(self._source(root, version="2.0.0")), "--confirm"], stdout=stdout, stderr=io.StringIO()), EXIT_OK)
            self.assertEqual(main(["--config", str(config_path), "plugin", "enable", "echo", "--version", "2.0.0", "--confirm"], stdout=stdout, stderr=io.StringIO()), EXIT_OK)
            output = io.StringIO()
            self.assertEqual(main(["--config", str(config_path), "plugin", "rollback", "echo", "--confirm", "--json"], stdout=output, stderr=io.StringIO()), EXIT_OK)
            self.assertEqual(json.loads(output.getvalue())["version"], "1.0.0")

    def test_remove_withdraws_future_registry_without_breaking_assembled_definition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ExecutablePluginStore(root / "managed")
            source = self._source(root)
            installed = store.install(source)
            store.activate(installed.plugin_id, version=installed.version)
            definition = store.active()[0].definitions()[0]
            self.assertTrue(store.remove("echo"))
            self.assertEqual(store.list(), ())
            self.assertTrue(installed.package_path.is_dir())
            result = asyncio.run(
                definition.handler(definition.validator({"text": "still-pinned"}), CancellationToken())
            )
            self.assertEqual(json.loads(result)["result"], {"text": "still-pinned"})
            reinstalled = store.install(source)
            self.assertFalse(reinstalled.enabled)

    def test_cli_update_check_only_reports_a_pinned_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); config_path = root / "config.toml"; stdout = io.StringIO()
            self.assertEqual(main(["--config", str(config_path), "plugin", "install", str(self._source(root)), "--confirm"], stdout=stdout, stderr=io.StringIO()), EXIT_OK)
            review = PluginGitUpdateReview(
                plugin_id="echo", installed_version="1.0.0", installed_commit="a" * 40,
                repository_url="https://github.com/example/plugin.git", subdirectory=".",
                ref="refs/heads/main", candidate_commit="b" * 40,
            )
            output = io.StringIO()
            with patch.object(ExecutablePluginStore, "review_git_update", return_value=review):
                self.assertEqual(
                    main(["--config", str(config_path), "plugin", "update-check", "echo", "--ref", "refs/heads/main", "--confirm", "--json"], stdout=output, stderr=io.StringIO()),
                    EXIT_OK,
                )
            record = json.loads(output.getvalue())
            self.assertFalse(record["configuration_changed"])
            self.assertTrue(record["update_available"])
            self.assertIn("install-git", record["next_action"])

    def test_hash_locked_dependency_environment_must_be_explicitly_built(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ExecutablePluginStore(root / "managed")
            store.install(self._source(root, dependency_lock=True))
            store.activate("echo", version="1.0.0")
            self.assertEqual(store.active(), ())
            definition = store.list()[0].definitions()[0]
            arguments = definition.validator({"text": "hello"})
            with self.assertRaisesRegex(ToolExecutionError, "environment is not built"):
                asyncio.run(definition.handler(arguments, CancellationToken()))

            def fake_dependency_command(command: tuple[str, ...], *, timeout: float) -> None:
                if command[1:3] == ("-m", "venv"):
                    environment = Path(command[-1])
                    executable = environment / ("Scripts/python.exe" if __import__("os").name == "nt" else "bin/python")
                    executable.parent.mkdir(parents=True)
                    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            with patch.object(ExecutablePluginStore, "_run_dependency_command", side_effect=fake_dependency_command):
                built = store.build_dependency_environment("echo")

            self.assertTrue(built.dependency_environment_ready)
            self.assertTrue(store.active()[0].dependency_environment_ready)
            registry = json.loads(store.registry_path.read_text(encoding="utf-8"))
            self.assertIn("echo@1.0.0", registry["environments"])

    def test_cli_dependency_environment_build_requires_confirmation_and_reports_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.toml"
            stdout, stderr = io.StringIO(), io.StringIO()
            source = self._source(root, dependency_lock=True)
            self.assertEqual(main(["--config", str(config_path), "plugin", "install", str(source), "--confirm"], stdout=stdout, stderr=stderr), EXIT_OK)
            self.assertNotEqual(main(["--config", str(config_path), "plugin", "environment-build", "echo"], stdout=stdout, stderr=io.StringIO()), EXIT_OK)

            case = self

            def fake_build(store: ExecutablePluginStore, plugin_id: str, *, version: str | None = None, python_command: str | None = None):
                case.assertEqual(plugin_id, "echo")
                return store.list()[0]

            output = io.StringIO()
            with patch.object(ExecutablePluginStore, "build_dependency_environment", new=fake_build):
                self.assertEqual(main(["--config", str(config_path), "plugin", "environment-build", "echo", "--confirm", "--json"], stdout=output, stderr=io.StringIO()), EXIT_OK)
            record = json.loads(output.getvalue())
            self.assertTrue(record["environment_changed"])
            self.assertEqual(record["dependency_lock"], "requirements.lock")
