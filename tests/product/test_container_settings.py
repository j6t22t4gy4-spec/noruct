from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from dynamic_firm.product.container_settings import (
    ContainerSettings,
    container_status,
    remove_container_settings,
    write_container_settings,
)


class ContainerSettingsTests(unittest.TestCase):
    def test_write_replaces_only_container_table_and_preserves_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[provider]\nmodel = "preserved"\n', encoding="utf-8")
            write_container_settings(path, ContainerSettings(image="python:3.11-alpine", programs={"tests": ("/usr/bin/pytest",)}))
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["provider"]["model"], "preserved")
            self.assertEqual(payload["container"]["programs"]["tests"], ["/usr/bin/pytest"])
            self.assertTrue(remove_container_settings(path))
            self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8")), {"provider": {"model": "preserved"}})

    def test_status_never_claims_network_or_auto_activation(self) -> None:
        config = ContainerSettings(image="python:3.11-alpine", programs={"tests": ("/usr/bin/pytest",)}).validated_runtime_config()
        status = container_status(config)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["network"], "disabled")
        self.assertFalse(status["automatic_activation"])
        self.assertEqual(status["permission_mode_required"], "ask")
