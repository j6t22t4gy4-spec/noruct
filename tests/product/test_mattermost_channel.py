from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.mattermost_channel import MattermostChannelConfig, deliver_mattermost_message, mattermost_channel_config_from_settings, remove_mattermost_channel_settings, write_mattermost_channel_settings


class _Response:
    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def read(self, _limit): return b'{}'


class MattermostChannelTests(unittest.TestCase):
    def test_settings_are_nonsecret_and_delivery_posts_only_to_configured_channel(self) -> None:
        previous = os.environ.get("MATTERMOST_TEST_TOKEN"); os.environ["MATTERMOST_TEST_TOKEN"] = "secret-value"
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"; path.write_text('[provider]\nmodel = "fixture"\n', encoding="utf-8")
                config = MattermostChannelConfig("https://mattermost.example.com", "channel123", "MATTERMOST_TEST_TOKEN")
                write_mattermost_channel_settings(path, config)
                self.assertNotIn("secret-value", path.read_text(encoding="utf-8"))
                self.assertEqual(mattermost_channel_config_from_settings(__import__("tomllib").loads(path.read_text(encoding="utf-8"))), config)
                with patch("dynamic_firm.product.mattermost_channel.urlopen", return_value=_Response()) as opener:
                    result = deliver_mattermost_message(config, message="hello")
                request = opener.call_args.args[0]
                self.assertEqual(request.full_url, "https://mattermost.example.com/api/v4/posts")
                self.assertEqual(request.get_header("Authorization"), "Bearer secret-value")
                self.assertEqual(json.loads(request.data.decode("utf-8")), {"channel_id": "channel123", "message": "hello"})
                self.assertTrue(result.delivered); self.assertNotIn("secret-value", str(result.to_dict()))
                self.assertTrue(remove_mattermost_channel_settings(path)); self.assertIn("[provider]", path.read_text(encoding="utf-8"))
        finally:
            if previous is None: os.environ.pop("MATTERMOST_TEST_TOKEN", None)
            else: os.environ["MATTERMOST_TEST_TOKEN"] = previous

    def test_cli_requires_confirm_and_capabilities_reports_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"; output = io.StringIO()
            code = main(["--config", str(path), "channel", "mattermost-configure", "--base-url", "https://mattermost.example.com", "--channel-id", "channel123", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue()); capabilities = io.StringIO()
            status = main(["--config", str(path), "capabilities", "status", "--json"], stdout=capabilities, stderr=io.StringIO())
            no_confirm = main(["--config", str(path), "channel", "mattermost-test", "--message", "hello"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK); self.assertTrue(payload["configuration_changed"]); self.assertEqual(status, EXIT_OK)
        self.assertTrue(json.loads(capabilities.getvalue())["mattermost_channel"]["enabled"]); self.assertNotEqual(no_confirm, EXIT_OK)


if __name__ == "__main__": unittest.main()
