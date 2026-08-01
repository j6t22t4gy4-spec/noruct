from __future__ import annotations

import json
import stat
import tarfile
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.product.execution_environment import (
    execution_environment_status,
    inspect_workspace_snapshot_manifest,
    probe_ssh_environment,
    run_ssh_operator_command,
    transfer_workspace_snapshot,
    write_workspace_snapshot_manifest,
)


class ExecutionEnvironmentTests(unittest.TestCase):
    def test_status_is_read_only_and_reports_the_closed_remote_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            record = execution_environment_status(workspace).to_dict()
        self.assertEqual(record["schema"], "noruct.execution-environment.v1")
        self.assertTrue(record["workspace"]["is_directory"])
        self.assertEqual(record["local_execution"], "AVAILABLE_PER_ACTION_APPROVAL")
        self.assertEqual(
            record["remote_job_execution"],
            "DISABLED_UNTIL_EXPLICIT_OPERATOR_CONFIGURATION",
        )
        self.assertEqual(record["os_sandbox"], "NOT_CLAIMED")

    def test_ssh_probe_uses_a_fixed_marker_with_strict_host_key_policy(self) -> None:
        captured: list[object] = []

        def runner(command, **kwargs):
            captured.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "noruct-remote-probe-v1", "")

        with patch(
            "dynamic_firm.product.execution_environment.shutil.which",
            return_value="/usr/bin/ssh",
        ):
            record = probe_ssh_environment(host="build.example.test", user="operator", runner=runner)
        self.assertTrue(record.reachable)
        self.assertEqual(record.authentication, "CONFIRMED_BY_FIXED_MARKER")
        self.assertEqual(record.remote_job_execution, "NOT_IMPLEMENTED")
        command, kwargs = captured[0]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("ForwardAgent=no", command)
        self.assertIn("noruct-remote-probe-v1", command)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_ssh_probe_rejects_shell_like_host_input_before_running_anything(self) -> None:
        with patch(
            "dynamic_firm.product.execution_environment.shutil.which",
            return_value="/usr/bin/ssh",
        ):
            with self.assertRaisesRegex(ValueError, "hostname"):
                probe_ssh_environment(host="host;whoami", user="operator")

    def test_ssh_probe_rejects_a_declared_identity_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "key"
            key.write_text("not a key", encoding="utf-8")
            alias = Path(temporary) / "key-alias"
            alias.symlink_to(key)
            with self.assertRaisesRegex(ValueError, "non-symbolic-link"):
                probe_ssh_environment(
                    host="build.example.test",
                    user="operator",
                    identity_file=alias,
                )

    def test_explicit_operator_command_is_quoted_and_remains_outside_company_jobs(self) -> None:
        captured: list[object] = []

        def runner(command, **kwargs):
            captured.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "remote-ok", "")

        with patch(
            "dynamic_firm.product.execution_environment.shutil.which",
            return_value="/usr/bin/ssh",
        ):
            record = run_ssh_operator_command(
                host="build.example.test",
                user="operator",
                remote_workspace="/srv/company workspace",
                program="/usr/local/bin/check",
                arguments=("--label", "safe value"),
                runner=runner,
            )
        self.assertTrue(record.completed)
        self.assertEqual(record.remote_job_execution, "NOT_IMPLEMENTED")
        self.assertIn("no_company_job", record.authority)
        command, kwargs = captured[0]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("cd -- '/srv/company workspace'", command[-1])
        self.assertIn("exec env -i PATH=/usr/bin:/bin", command[-1])
        self.assertIn("'safe value'", command[-1])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_operator_command_rejects_non_normalized_paths_before_ssh(self) -> None:
        with self.assertRaisesRegex(ValueError, "normalized POSIX"):
            run_ssh_operator_command(
                host="build.example.test",
                user="operator",
                remote_workspace="/srv/../secret",
                program="/usr/local/bin/check",
            )

    def test_workspace_snapshot_is_bounded_secret_aware_and_never_enables_remote_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            (workspace / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (workspace / ".env").write_text("secret=do-not-hash", encoding="utf-8")
            (workspace / ".git").mkdir()
            (workspace / ".git" / "config").write_text("private", encoding="utf-8")
            output = Path(temporary) / "snapshot.json"
            record = write_workspace_snapshot_manifest(workspace=workspace, output_path=output)
            payload = json.loads(output.read_text(encoding="utf-8"))
            output_mode = stat.S_IMODE(output.stat().st_mode)
        self.assertEqual(record.file_count, 1)
        self.assertEqual(record.remote_job_execution, "NOT_IMPLEMENTED")
        self.assertEqual(payload["schema"], "noruct.remote-workspace-snapshot.v1")
        self.assertEqual([entry["path"] for entry in payload["entries"]], ["app.py"])
        self.assertEqual(payload["snapshot_sha256"], record.snapshot_sha256)
        self.assertEqual(output_mode, 0o600)

    def test_workspace_snapshot_refuses_a_nonexcluded_symlink_before_writing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            target = Path(temporary) / "outside.txt"
            target.write_text("outside", encoding="utf-8")
            (workspace / "link.txt").symlink_to(target)
            output = Path(temporary) / "snapshot.json"
            with self.assertRaisesRegex(ValueError, "symbolic-link"):
                write_workspace_snapshot_manifest(workspace=workspace, output_path=output)
            self.assertFalse(output.exists())

    def test_workspace_snapshot_refuses_an_output_inside_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            (workspace / "note.txt").write_text("safe", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside"):
                write_workspace_snapshot_manifest(
                    workspace=workspace,
                    output_path=workspace / "snapshot.json",
                )

    def test_workspace_snapshot_inspection_detects_tampering_without_reading_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            (workspace / "note.txt").write_text("safe", encoding="utf-8")
            manifest = Path(temporary) / "snapshot.json"
            receipt = write_workspace_snapshot_manifest(workspace=workspace, output_path=manifest)
            valid = inspect_workspace_snapshot_manifest(manifest)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["workspace"] = r"C:\\Users\\operator\\workspace"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            windows_path_valid = inspect_workspace_snapshot_manifest(manifest)
            payload["entries"][0]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            invalid = inspect_workspace_snapshot_manifest(manifest)
        self.assertTrue(valid.valid)
        self.assertEqual(valid.snapshot_sha256, receipt.snapshot_sha256)
        self.assertEqual(valid.remote_job_execution, "NOT_IMPLEMENTED")
        self.assertTrue(windows_path_valid.valid)
        self.assertFalse(invalid.valid)
        self.assertEqual(invalid.integrity_state, "INVALID_MANIFEST")

    def test_verified_workspace_transfer_streams_only_manifest_bound_files_to_isolated_staging(self) -> None:
        captured: list[object] = []

        def runner(command, **kwargs):
            captured.append((command, kwargs))
            marker = "noruct-transfer-ok:" + receipt.snapshot_sha256
            return subprocess.CompletedProcess(command, 0, "note.txt: OK\n" + marker, "")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "note.txt").write_text("safe", encoding="utf-8")
            (workspace / ".env").write_text("secret", encoding="utf-8")
            manifest = root / "snapshot.json"
            receipt = write_workspace_snapshot_manifest(workspace=workspace, output_path=manifest)
            with patch("dynamic_firm.product.execution_environment.shutil.which", return_value="/usr/bin/ssh"):
                transfer = transfer_workspace_snapshot(
                    workspace=workspace,
                    snapshot_manifest=manifest,
                    host="build.example.test",
                    user="operator",
                    remote_workspace="/srv/company-workspace",
                    runner=runner,
                )
        self.assertTrue(transfer.transferred)
        self.assertEqual(transfer.integrity_state, "VERIFIED_REMOTE_SNAPSHOT")
        self.assertEqual(transfer.remote_job_execution, "NOT_IMPLEMENTED")
        self.assertIn("no_company_job", transfer.authority)
        self.assertTrue(transfer.remote_snapshot_directory.endswith(receipt.snapshot_sha256))
        command, kwargs = captured[0]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertEqual(kwargs["input"][:2], b"\x1f\x8b")
        self.assertIn("noruct-transfer-sha256", command[-1])
        self.assertIn("sha256sum", command[-1])
        self.assertNotIn("rm -f -- .noruct-transfer-sha256", command[-1])
        self.assertIn(".noruct-remote-snapshots", command[-1])
        self.assertTrue(command[-1].startswith("sh -ceu '"))
        with tempfile.TemporaryDirectory() as unpacked:
            archive = Path(unpacked) / "transfer.tar.gz"
            archive.write_bytes(kwargs["input"])
            with tarfile.open(archive, "r:gz") as contents:
                self.assertEqual(sorted(contents.getnames()), [".noruct-transfer-sha256", "note.txt"])

    def test_verified_workspace_transfer_rejects_manifest_workspace_drift_before_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "note.txt").write_text("before", encoding="utf-8")
            manifest = root / "snapshot.json"
            write_workspace_snapshot_manifest(workspace=workspace, output_path=manifest)
            (workspace / "note.txt").write_text("after", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed since"):
                transfer_workspace_snapshot(
                    workspace=workspace,
                    snapshot_manifest=manifest,
                    host="build.example.test",
                    user="operator",
                    remote_workspace="/srv/company-workspace",
                )
