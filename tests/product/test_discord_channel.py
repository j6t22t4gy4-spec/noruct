from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.discord_channel import DiscordChannelConfig, deliver_discord_message, write_discord_channel_settings


class _Response:
    def __enter__(self): return self
    def __exit__(self, *_: object) -> None: return None
    def read(self, _limit: int) -> bytes: return b""


class DiscordChannelTests(unittest.TestCase):
    def test_webhook_delivery_disables_mentions_and_hides_url(self) -> None:
        old = os.environ.get("DISCORD_TEST_WEBHOOK")
        os.environ["DISCORD_TEST_WEBHOOK"] = "https://discord.com/api/webhooks/123/secret-token"
        try:
            config = DiscordChannelConfig(webhook_env="DISCORD_TEST_WEBHOOK")
            with patch("dynamic_firm.product.discord_channel.urlopen", return_value=_Response()) as opener:
                result = deliver_discord_message(config, message="@everyone no ping")
            request = opener.call_args.args[0]
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            self.assertTrue(result.delivered)
            self.assertNotIn("secret-token", str(result.to_dict()))
        finally:
            if old is None: os.environ.pop("DISCORD_TEST_WEBHOOK", None)
            else: os.environ["DISCORD_TEST_WEBHOOK"] = old

    def test_cli_configure_is_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            code = main(["--config", str(Path(directory) / "config.toml"), "channel", "discord-configure", "--webhook-env", "DISCORD_TEST_WEBHOOK", "--json"], stdout=output, stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(json.loads(output.getvalue())["configuration_changed"])
