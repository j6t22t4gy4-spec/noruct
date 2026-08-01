from __future__ import annotations

import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from dynamic_firm.browser_connector import BrowserReadOnlyConfig
from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.browser_lifecycle import (
    browser_lifecycle_status,
    close_isolated_browser,
    launch_isolated_browser,
)
from dynamic_firm.product.browser_settings import write_browser_settings


class BrowserLifecycleTests(unittest.TestCase):
    def _browser(self, root: Path) -> Path:
        path = root / "fixture-browser"
        path.write_text(
            textwrap.dedent(
                """\
                #!%s
                import http.server
                import sys
                port = int(next(item.split("=", 1)[1] for item in sys.argv if item.startswith("--remote-debugging-port=")))
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        if self.path == "/json/version":
                            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
                        else:
                            self.send_response(404); self.end_headers()
                    def log_message(self, *_args): pass
                http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
                """ % os.sys.executable
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _node(self, root: Path) -> Path:
        path = root / "fixture-node"
        path.write_text(f"#!{os.sys.executable}\nimport sys\nprint('v22.0.0' if sys.argv[1:] == ['--version'] else '')\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_launches_one_isolated_profile_then_closes_and_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "browser-lifecycle.json"
            record = launch_isolated_browser(state_path=state, browser_command=self._browser(root), timeout_seconds=3)
            status = browser_lifecycle_status(state)
            profile = Path(record.profile_directory)

            self.assertTrue(status["running"])
            self.assertTrue(profile.is_dir())
            self.assertTrue(close_isolated_browser(state))
            self.assertFalse(state.exists())
            self.assertFalse(profile.exists())

    def test_cli_launch_rebinds_only_existing_browser_policy_and_close_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "noruct.toml"
            write_browser_settings(
                config,
                BrowserReadOnlyConfig(
                    node_command=self._node(root),
                    cdp_endpoint="http://127.0.0.1:9222",
                ),
            )
            launched = main(
                ["--config", str(config), "browser", "launch", "--browser-command", str(self._browser(root)), "--confirm", "--json"],
                stdout=__import__("io").StringIO(), stderr=__import__("io").StringIO(),
            )
            closed = main(
                ["--config", str(config), "browser", "close", "--confirm", "--json"],
                stdout=__import__("io").StringIO(), stderr=__import__("io").StringIO(),
            )
        self.assertEqual(launched, EXIT_OK)
        self.assertEqual(closed, EXIT_OK)
