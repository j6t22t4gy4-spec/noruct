from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from dynamic_firm.product.remote_worker_settings import (
    RemoteWorkerSettings,
    remove_remote_worker_settings,
    remote_worker_status,
    write_remote_worker_settings,
)


class RemoteWorkerSettingsTests(unittest.TestCase):
    def _receipt(self, root: Path) -> Path:
        receipt = root / "verified-receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "host": "build.example.test",
                    "user": "operator",
                    "port": 22,
                    "remote_snapshot_directory": "/srv/company/.noruct-remote-snapshots/" + "a" * 64,
                    "snapshot_sha256": "a" * 64,
                    "transferred": True,
                    "integrity_state": "VERIFIED_REMOTE_SNAPSHOT",
                    "host_key_policy": "STRICT_KNOWN_HOSTS_ONLY",
                    "remote_job_execution": "NOT_IMPLEMENTED",
                }
            ),
            encoding="utf-8",
        )
        return receipt

    def test_write_replaces_only_remote_worker_tables_and_preserves_other_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text('[provider]\nmodel = "preserved"\n', encoding="utf-8")
            target = write_remote_worker_settings(
                config_path,
                RemoteWorkerSettings(
                    target_id="build",
                    receipt=self._receipt(root),
                    programs={"tests": "/usr/bin/pytest", "format": "/usr/bin/ruff"},
                ),
            )
            parsed = tomllib.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(parsed["provider"]["model"], "preserved")
            self.assertEqual(parsed["remote_worker"]["programs"], {"format": "/usr/bin/ruff", "tests": "/usr/bin/pytest"})
            self.assertTrue(remove_remote_worker_settings(target))
            remaining = tomllib.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(remaining, {"provider": {"model": "preserved"}})

    def test_status_exposes_only_target_facts_and_no_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RemoteWorkerSettings(
                target_id="build",
                receipt=self._receipt(root),
                programs={"tests": "/usr/bin/pytest"},
            ).validated_runtime_config()
        status = remote_worker_status(config)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["permission_mode_required"], "ask")
        self.assertFalse(status["automatic_activation"])
        self.assertFalse(status["reverse_sync"])
        self.assertNotIn("identity_file", status)

