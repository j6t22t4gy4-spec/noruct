from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.teams_channel import (
    TeamsChannelConfig,
    deliver_teams_message,
    remove_teams_channel_settings,
    teams_channel_config_from_settings,
    write_teams_channel_settings,
)


class _Response:
    def __enter__(self): return self
    def __exit__(self, *_): return None
    def read(self, _): return b"accepted"


class TeamsChannelTests(unittest.TestCase):
    def test_settings_and_webhook_delivery_hide_url_secret(self) -> None:
        old = os.environ.get("TEAMS_TEST_WEBHOOK")
        os.environ["TEAMS_TEST_WEBHOOK"] = "https://example.test/workflows/trigger?sig=secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"
                config = TeamsChannelConfig("TEAMS_TEST_WEBHOOK")
                write_teams_channel_settings(path, config)
                self.assertEqual(teams_channel_config_from_settings(__import__("tomllib").loads(path.read_text(encoding="utf-8"))), config)
                with patch("dynamic_firm.product.teams_channel.urlopen", return_value=_Response()) as opener:
                    result = deliver_teams_message(config, message="hello")
                request = opener.call_args.args[0]
                self.assertEqual(request.full_url, os.environ["TEAMS_TEST_WEBHOOK"])
                self.assertEqual(json.loads(request.data.decode()), {"text": "hello"})
                self.assertTrue(result.delivered)
                self.assertNotIn("sig=", str(result.to_dict()))
                self.assertTrue(remove_teams_channel_settings(path))
        finally:
            if old is None: os.environ.pop("TEAMS_TEST_WEBHOOK", None)
            else: os.environ["TEAMS_TEST_WEBHOOK"] = old

    def test_cli_configure_and_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"; output = io.StringIO()
            configured = main(["--config", str(path), "channel", "teams-configure", "--json"], stdout=output, stderr=io.StringIO())
            capabilities = io.StringIO()
            status = main(["--config", str(path), "capabilities", "status", "--json"], stdout=capabilities, stderr=io.StringIO())
            denied = main(["--config", str(path), "channel", "teams-test", "--message", "hello"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(configured, EXIT_OK)
        self.assertTrue(json.loads(output.getvalue())["configuration_changed"])
        self.assertEqual(status, EXIT_OK)
        self.assertTrue(json.loads(capabilities.getvalue())["teams_channel"]["enabled"])
        self.assertNotEqual(denied, EXIT_OK)
