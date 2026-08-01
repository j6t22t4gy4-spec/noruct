from __future__ import annotations

import os
import json
import stat
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.product.release_update import (
    activate_installed_release,
    release_installation_status,
)


@unittest.skipIf(os.name == "nt", "Unix command-link behavior is covered in this test environment")
class ReleaseUpdateTests(unittest.TestCase):
    def _install(self, root: Path, version: str) -> Path:
        binary = root / "versions" / version / "venv" / "bin" / "noruct"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return binary

    def _receipt(self, root: Path, version: str) -> None:
        path = root / "versions" / version / "release-receipt.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "noruct.release-installation-receipt.v1",
                    "version": version,
                    "target": "darwin-arm64",
                    "manifest_url": "https://downloads.example.test/releases/" + version + ".json",
                    "manifest_sha256": "a" * 64,
                    "wheel_sha256": "b" * 64,
                    "employee_runtime_sha256": "c" * 64,
                }
            ),
            encoding="utf-8",
        )

    def test_status_and_explicit_activation_only_select_existing_managed_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "releases"
            bin_dir = Path(temporary) / "bin"
            old = self._install(root, "0.0.79")
            latest = self._install(root, "0.0.80")
            self._receipt(root, "0.0.79")
            self._receipt(root, "0.0.80")
            initial = release_installation_status(install_root=root, bin_dir=bin_dir)
            self.assertEqual(initial.installed_versions, ("0.0.79", "0.0.80"))
            self.assertEqual(initial.command_state, "NOT_INSTALLED")
            self.assertEqual(initial.install_root_state, "MISSING_MANAGED_MARKER")
            (root / ".noruct-install-root-v1").write_text("noruct-install-root-v1\n", encoding="utf-8")

            activated = activate_installed_release("0.0.80", install_root=root, bin_dir=bin_dir)
            self.assertEqual(activated.active_version, "0.0.80")
            self.assertEqual(activated.install_root_state, "MANAGED_MARKER_VALID")
            self.assertEqual(activated.active_receipt_state, "VALID")
            self.assertEqual(activated.verified_receipt_versions, ("0.0.79", "0.0.80"))
            command = bin_dir / "noruct"
            self.assertTrue(command.is_symlink())
            self.assertEqual(command.resolve(), latest.resolve())

            rolled_back = activate_installed_release("0.0.79", install_root=root, bin_dir=bin_dir)
            self.assertEqual(rolled_back.active_version, "0.0.79")
            self.assertEqual(command.resolve(), old.resolve())
            self.assertFalse(rolled_back.local_state_touched)
            self.assertFalse(rolled_back.network_accessed)

    def test_invalid_or_missing_receipt_never_prevents_rollback_but_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "releases"
            bin_dir = Path(temporary) / "bin"
            self._install(root, "0.0.80")
            activated = activate_installed_release("0.0.80", install_root=root, bin_dir=bin_dir)
            self.assertEqual(activated.active_receipt_state, "MISSING")
            path = root / "versions" / "0.0.80" / "release-receipt.json"
            path.write_text("not JSON", encoding="utf-8")
            status = release_installation_status(install_root=root, bin_dir=bin_dir)
            self.assertEqual(status.active_receipt_state, "INVALID")

    def test_activation_refuses_an_unmanaged_command_or_missing_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "releases"
            bin_dir = Path(temporary) / "bin"
            self._install(root, "0.0.80")
            bin_dir.mkdir()
            (bin_dir / "noruct").write_text("operator command", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not managed"):
                activate_installed_release("0.0.80", install_root=root, bin_dir=bin_dir)
            with self.assertRaisesRegex(ValueError, "installed managed release"):
                activate_installed_release("0.0.81", install_root=root, bin_dir=Path(temporary) / "other-bin")
