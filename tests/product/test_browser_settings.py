from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.browser_connector import BrowserReadOnlyConfig
from dynamic_firm.product.browser_settings import configured_browser_policy, remove_browser_settings, write_browser_settings


class BrowserSettingsTests(unittest.TestCase):
    def _config(self) -> BrowserReadOnlyConfig:
        return BrowserReadOnlyConfig(
            node_command=Path(sys.executable).resolve(),
            cdp_endpoint="http://127.0.0.1:9222",
            timeout_seconds=5.0,
            max_result_bytes=12_000,
        )

    def test_atomic_replace_preserves_unrelated_tables_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            target.write_text('[provider]\nmodel = "fixture"\n\n[mcp]\nenabled = false\n', encoding="utf-8")
            write_browser_settings(target, self._config())
            content = target.read_text(encoding="utf-8")
            parsed = configured_browser_policy(target)

        self.assertIn('[provider]\nmodel = "fixture"', content)
        self.assertIn("[mcp]", content)
        self.assertIn("[browser]", content)
        assert parsed is not None
        self.assertEqual(parsed.cdp_endpoint, "http://127.0.0.1:9222")
        self.assertEqual(parsed.max_result_bytes, 12_000)

    def test_disable_removes_only_browser_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            target.write_text('[provider]\nmodel = "fixture"\n', encoding="utf-8")
            write_browser_settings(target, self._config())
            self.assertTrue(remove_browser_settings(target))
            content = target.read_text(encoding="utf-8")

        self.assertIn("[provider]", content)
        self.assertNotIn("[browser]", content)

    def test_capture_directory_round_trips_only_when_explicitly_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as root:
            target = Path(root) / "noruct.toml"
            config = BrowserReadOnlyConfig(
                node_command=Path(sys.executable).resolve(),
                cdp_endpoint="http://127.0.0.1:9222",
                allow_control=True,
                capture_directory=Path(directory),
            )
            write_browser_settings(target, config)
            parsed = configured_browser_policy(target)
            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertEqual(parsed.capture_directory.resolve(), Path(directory).resolve())

    def test_control_opt_in_round_trips_as_a_non_secret_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            config = BrowserReadOnlyConfig(
                node_command=Path(sys.executable).resolve(),
                cdp_endpoint="http://127.0.0.1:9222",
                allow_control=True,
            )
            write_browser_settings(target, config)
            parsed = configured_browser_policy(target)
        assert parsed is not None
        self.assertTrue(parsed.allow_control)
