from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.matrix_channel import MatrixChannelConfig, deliver_matrix_message, matrix_channel_config_from_settings, remove_matrix_channel_settings, write_matrix_channel_settings

class _Response:
    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def read(self, _limit): return b'{"event_id":"$fixture"}'

class MatrixChannelTests(unittest.TestCase):
    def test_settings_are_nonsecret_and_delivery_uses_fixed_room_endpoint(self) -> None:
        previous = os.environ.get("MATRIX_TEST_TOKEN"); os.environ["MATRIX_TEST_TOKEN"] = "secret-value"
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"; path.write_text('[provider]\nmodel = "fixture"\n', encoding="utf-8")
                config = MatrixChannelConfig("https://matrix.example.com", "!room123:example.com", "MATRIX_TEST_TOKEN")
                write_matrix_channel_settings(path, config); self.assertNotIn("secret-value", path.read_text(encoding="utf-8"))
                self.assertEqual(matrix_channel_config_from_settings(__import__("tomllib").loads(path.read_text(encoding="utf-8"))), config)
                with patch("dynamic_firm.product.matrix_channel.urlopen", return_value=_Response()) as opener:
                    result = deliver_matrix_message(config, message="hello")
                request = opener.call_args.args[0]
                self.assertTrue(request.full_url.startswith("https://matrix.example.com/_matrix/client/v3/rooms/%21room123%3Aexample.com/send/m.room.message/"))
                self.assertEqual(request.get_method(), "PUT"); self.assertEqual(request.get_header("Authorization"), "Bearer secret-value")
                self.assertEqual(json.loads(request.data.decode("utf-8")), {"msgtype": "m.text", "body": "hello"})
                self.assertTrue(result.delivered); self.assertNotIn("secret-value", str(result.to_dict()))
                self.assertTrue(remove_matrix_channel_settings(path)); self.assertIn("[provider]", path.read_text(encoding="utf-8"))
        finally:
            if previous is None: os.environ.pop("MATRIX_TEST_TOKEN", None)
            else: os.environ["MATRIX_TEST_TOKEN"] = previous

    def test_cli_requires_confirm_and_capabilities_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"; output = io.StringIO()
            code = main(["--config", str(path), "channel", "matrix-configure", "--homeserver-url", "https://matrix.example.com", "--room-id", "!room123:example.com", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue()); capabilities = io.StringIO(); status = main(["--config", str(path), "capabilities", "status", "--json"], stdout=capabilities, stderr=io.StringIO())
            denied = main(["--config", str(path), "channel", "matrix-test", "--message", "hello"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK); self.assertTrue(payload["configuration_changed"]); self.assertEqual(status, EXIT_OK)
        self.assertTrue(json.loads(capabilities.getvalue())["matrix_channel"]["enabled"]); self.assertNotEqual(denied, EXIT_OK)

if __name__ == "__main__": unittest.main()
