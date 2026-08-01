from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.ntfy_channel import (
    NtfyChannelConfig,
    deliver_ntfy_message,
    ntfy_channel_config_from_settings,
    remove_ntfy_channel_settings,
    write_ntfy_channel_settings,
)
from dynamic_firm.product.inbound_channel import InboundMessageStore, inbound_state_path
from dynamic_firm.product.ntfy_inbound import NtfyInboundConfig, run_ntfy_inbound


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return b'{"event":"message","id":"fixture"}'


class NtfyChannelTests(unittest.TestCase):
    def test_settings_are_separate_and_publish_keeps_token_out_of_receipt(self) -> None:
        previous = os.environ.get("NTFY_TEST_TOKEN")
        os.environ["NTFY_TEST_TOKEN"] = "ntfy-secret-value"
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"
                path.write_text('[provider]\nmodel = "fixture"\n', encoding="utf-8")
                config = NtfyChannelConfig(
                    topic="private_topic",
                    token_env="NTFY_TEST_TOKEN",
                    server_url="http://127.0.0.1:8080",
                    markdown=True,
                )
                write_ntfy_channel_settings(path, config)
                restored = ntfy_channel_config_from_settings(__import__("tomllib").loads(path.read_text(encoding="utf-8")))
                self.assertEqual(restored, config)
                with patch("dynamic_firm.product.ntfy_channel.urlopen", return_value=_Response()) as opener:
                    result = deliver_ntfy_message(config, message="hello", title="Noruct test")
                request = opener.call_args.args[0]
                self.assertEqual(request.full_url, "http://127.0.0.1:8080/private_topic")
                self.assertEqual(request.get_header("Authorization"), "Bearer ntfy-secret-value")
                self.assertEqual(request.get_header("Markdown"), "yes")
                self.assertTrue(result.delivered)
                self.assertNotIn("ntfy-secret-value", str(result.to_dict()))
                self.assertTrue(remove_ntfy_channel_settings(path))
                self.assertIn("[provider]", path.read_text(encoding="utf-8"))
        finally:
            if previous is None:
                os.environ.pop("NTFY_TEST_TOKEN", None)
            else:
                os.environ["NTFY_TEST_TOKEN"] = previous

    def test_cli_configuration_is_local_only_and_capabilities_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            output = io.StringIO()
            code = main(
                [
                    "--config", str(path), "channel", "ntfy-configure", "--topic", "private_topic",
                    "--token-env", "NTFY_TEST_TOKEN", "--json",
                ],
                stdout=output,
                stderr=io.StringIO(),
            )
            payload = json.loads(output.getvalue())
            capabilities = io.StringIO()
            capability_code = main(["--config", str(path), "capabilities", "status", "--json"], stdout=capabilities, stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(payload["configuration_changed"])
        self.assertEqual(capability_code, EXIT_OK)
        self.assertTrue(json.loads(capabilities.getvalue())["ntfy_channel"]["enabled"])

    def test_external_publish_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            write_ntfy_channel_settings(path, NtfyChannelConfig(topic="private_topic"))
            code = main(
                ["--config", str(path), "channel", "ntfy-test", "--message", "hello"],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertNotEqual(code, EXIT_OK)

    def test_foreground_inbound_dispatches_each_topic_message_once(self) -> None:
        class Client:
            def read(self, _seconds: float, _limit: int):
                return (
                    {"event": "message", "topic": "private_topic", "id": "message-1", "message": "inspect"},
                    {"event": "message", "topic": "private_topic", "id": "message-1", "message": "inspect"},
                    {"event": "message", "topic": "other", "id": "message-2", "message": "ignore"},
                )
        async def dispatch(_message): return ("job-1", "SUCCEEDED")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = NtfyInboundConfig(workspace, "private_topic")
            with InboundMessageStore(inbound_state_path(workspace / "state.sqlite3")) as store:
                import asyncio
                result = asyncio.run(run_ntfy_inbound(config, store=store, dispatch=dispatch, maximum_seconds=2, client=Client()))
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.ignored_count, 1)

    def test_cli_inbound_configuration_appears_in_capability_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"; output = io.StringIO()
            code = main(["--config", str(path), "channel", "ntfy-inbox-configure", "--workspace", directory, "--topic", "private_topic", "--json"], stdout=output, stderr=io.StringIO())
            capabilities = io.StringIO()
            status = main(["--config", str(path), "capabilities", "status", "--json"], stdout=capabilities, stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK); self.assertEqual(status, EXIT_OK)
        self.assertTrue(json.loads(capabilities.getvalue())["ntfy_inbound"]["enabled"])


if __name__ == "__main__":
    unittest.main()
