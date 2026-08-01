from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.slack_channel import (
    SlackChannelConfig,
    deliver_slack_message,
    remove_slack_channel_settings,
    slack_channel_config_from_settings,
    write_slack_channel_settings,
)


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


class SlackChannelTests(unittest.TestCase):
    def test_settings_are_separate_and_delivery_never_exposes_token(self) -> None:
        previous = os.environ.get("SLACK_TEST_TOKEN")
        os.environ["SLACK_TEST_TOKEN"] = "xoxb-secret-value"
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"
                path.write_text('[provider]\nmodel = "fixture"\n', encoding="utf-8")
                config = SlackChannelConfig(channel_id="C01234567", token_env="SLACK_TEST_TOKEN")
                write_slack_channel_settings(path, config)
                restored = slack_channel_config_from_settings(__import__("tomllib").loads(path.read_text(encoding="utf-8")))
                self.assertEqual(restored, config)
                with patch("dynamic_firm.product.slack_channel.urlopen", return_value=_Response(b'{"ok": true}')) as opener:
                    result = deliver_slack_message(config, message="hello")
                request = opener.call_args.args[0]
                self.assertEqual(request.full_url, "https://slack.com/api/chat.postMessage")
                self.assertTrue(result.delivered)
                self.assertNotIn("xoxb-secret-value", str(result.to_dict()))
                self.assertTrue(remove_slack_channel_settings(path))
                self.assertIn("[provider]", path.read_text(encoding="utf-8"))
        finally:
            if previous is None:
                os.environ.pop("SLACK_TEST_TOKEN", None)
            else:
                os.environ["SLACK_TEST_TOKEN"] = previous

    def test_cli_configuration_does_not_contact_slack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            code = main([
                "--config", str(Path(directory) / "config.toml"), "channel", "slack-configure",
                "--channel-id", "C01234567", "--token-env", "SLACK_TEST_TOKEN", "--json",
            ], stdout=output, stderr=io.StringIO())
        payload = json.loads(output.getvalue())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(payload["configuration_changed"])
        self.assertNotIn("xoxb", str(payload))
