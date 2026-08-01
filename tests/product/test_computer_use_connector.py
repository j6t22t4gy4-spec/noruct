from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, _action_policy, _load_config, _run_config, build_parser, main
from dynamic_firm.computer_use_connector import ComputerUseConfig, ComputerUseConnector
from dynamic_firm.product.computer_use_settings import (
    configured_computer_use_policy,
    remove_computer_use_settings,
    write_computer_use_settings,
)
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolValidationError


class ComputerUseConnectorTests(unittest.TestCase):
    def _driver(self, root: Path) -> Path:
        path = root / "fixture-cua-driver"
        path.write_text(
            textwrap.dedent(
                """\
                #!%s
                import json
                import sys
                if sys.argv[1:] == ["--version"]:
                    print("cua-driver fixture 1.0")
                    raise SystemExit(0)
                _, operation, tool, raw = sys.argv
                payload = json.loads(raw)
                if operation != "call":
                    raise SystemExit(2)
                if tool == "list_windows":
                    print(json.dumps({"windows": [{"app_name": "Demo", "pid": 19, "window_id": 29, "title": "Demo Window"}, {"app_name": "Hidden", "pid": 20, "window_id": 30}]}))
                elif tool == "list_apps":
                    print(json.dumps({"apps": [{"app_name": "Demo", "pid": 19}, {"app_name": "Hidden", "pid": 20}]}))
                elif tool == "get_window_state":
                    print(json.dumps({"elements": [{"index": 1, "role": "AXButton", "label": "Continue", "bounds": [1, 2, 30, 12]}], "tree_markdown": "[1] AXButton Continue", "screenshot_file_path": payload.get("screenshot_out_file")}))
                else:
                    print(json.dumps({"completed": tool, "isError": False}))
                """ % os.sys.executable
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _config(self, root: Path) -> ComputerUseConfig:
        return ComputerUseConfig(
            driver_command=self._driver(root),
            allowed_apps=("Demo",),
            allow_control=True,
        )

    def test_capture_filters_desktop_evidence_and_enforces_snapshot_element(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connector = ComputerUseConnector(self._config(Path(directory)))
            definition = connector.definitions()[0]
            capture = asyncio.run(
                definition.handler(
                    definition.validator({"action": "capture", "app": "Demo"}),
                    CancellationToken(),
                )
            )
            payload = json.loads(capture)
            snapshot = payload["result"]
            self.assertEqual(snapshot["app"], "Demo")
            self.assertEqual(snapshot["element_indexes"], [1])
            self.assertFalse(snapshot["screenshot_in_model_context"])
            self.assertNotIn("screenshot_file_path", capture)

            result = asyncio.run(
                definition.handler(
                    definition.validator({"action": "click", "element": 1, "capture_after": True}),
                    CancellationToken(),
                )
            )
            self.assertTrue(json.loads(result)["completed"])
            with self.assertRaises(ToolValidationError):
                definition.validator({"action": "click", "element": 2})

    def test_control_requires_capture_and_blocks_destructive_hotkey(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            definition = ComputerUseConnector(self._config(Path(directory))).definitions()[0]
            with self.assertRaisesRegex(ToolValidationError, "capture"):
                definition.validator({"action": "click", "coordinate": [1, 2]})
            with self.assertRaisesRegex(ToolValidationError, "blocked"):
                definition.validator({"action": "key", "keys": "cmd+shift+backspace"})

    def test_settings_round_trip_and_disable_only_its_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "noruct.toml"
            target.write_text('[provider]\nmodel = "fixture"\n', encoding="utf-8")
            config = self._config(root)
            write_computer_use_settings(target, config)
            restored = configured_computer_use_policy(target)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.allowed_apps, ("Demo",))
            self.assertTrue(remove_computer_use_settings(target))
            self.assertIn("[provider]", target.read_text(encoding="utf-8"))
            self.assertNotIn("[computer_use]", target.read_text(encoding="utf-8"))

    def test_cli_configure_and_status_do_not_install_driver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = self._driver(root)
            config = root / "noruct.toml"
            output = io.StringIO()
            code = main(
                ["--config", str(config), "computer-use", "configure", "--driver-command", str(driver), "--allow-app", "Demo", "--allow-control"],
                stdout=output,
                stderr=io.StringIO(),
            )
            self.assertEqual(code, EXIT_OK)
            self.assertIn("Local computer-use policy: ready", output.getvalue())
            status = io.StringIO()
            self.assertEqual(main(["--config", str(config), "computer-use", "status", "--json"], stdout=status, stderr=io.StringIO()), EXIT_OK)
            self.assertTrue(json.loads(status.getvalue())["driver_ready"])

    def test_cli_preflight_lists_only_allowed_apps_without_a_screen_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = self._driver(root)
            config = root / "noruct.toml"
            self.assertEqual(
                main(["--config", str(config), "computer-use", "configure", "--driver-command", str(driver), "--allow-app", "Demo", "--json"], stdout=io.StringIO(), stderr=io.StringIO()),
                EXIT_OK,
            )
            output = io.StringIO()
            code = main(["--config", str(config), "computer-use", "preflight", "--confirm", "--json"], stdout=output, stderr=io.StringIO())
        payload = json.loads(output.getvalue())
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["operation"], "list_allowed_apps")
        self.assertEqual(payload["result"]["allowed_apps"], [{"app_name": "Demo", "pid": 19}])
        self.assertIn("no_screen_capture", payload["authority"])

    def test_run_configuration_projects_the_tool_only_in_ask_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = self._driver(root)
            config_path = root / "noruct.toml"
            write_computer_use_settings(
                config_path,
                ComputerUseConfig(driver_command=driver, allowed_apps=("Demo",), allow_control=True),
            )
            parser = build_parser()
            args = parser.parse_args(
                [
                    "ask", "inspect demo", "--workspace", str(root), "--provider", "ollama",
                    "--base-url", "http://127.0.0.1:11434/v1", "--model", "fixture", "--no-auth",
                    "--permission-mode", "ask",
                ]
            )
            config = _run_config(args, _load_config(config_path))
            grants = {item.tool_name for item in _action_policy(config).tool_grants}

        self.assertIsNotNone(config.computer_use)
        self.assertIn("computer_use", grants)
