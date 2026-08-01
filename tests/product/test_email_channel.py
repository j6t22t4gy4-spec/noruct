from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.email_channel import (
    EmailChannelConfig,
    deliver_email_message,
    email_channel_config_from_settings,
    remove_email_channel_settings,
    write_email_channel_settings,
)


class _Smtp:
    instances: list["_Smtp"] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.message = None
        self.quit_called = False
        self.closed = False
        self.__class__.instances.append(self)

    def starttls(self, *, context: object) -> None:
        self.started_tls = context is not None

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message: object, **_kwargs: object) -> None:
        self.message = message

    def quit(self) -> None:
        self.quit_called = True

    def close(self) -> None:
        self.closed = True


class EmailChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        _Smtp.instances.clear()

    def test_settings_keep_credentials_out_and_starttls_delivery_is_allowlisted(self) -> None:
        previous = os.environ.get("EMAIL_TEST_PASSWORD")
        os.environ["EMAIL_TEST_PASSWORD"] = "smtp-secret-value"
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"
                path.write_text('[provider]\nmodel = "fixture"\n', encoding="utf-8")
                config = EmailChannelConfig(
                    sender="agent@example.com", recipients=("operator@example.com",),
                    smtp_host="smtp.example.com", password_env="EMAIL_TEST_PASSWORD",
                )
                write_email_channel_settings(path, config)
                self.assertNotIn("smtp-secret-value", path.read_text(encoding="utf-8"))
                restored = email_channel_config_from_settings(__import__("tomllib").loads(path.read_text(encoding="utf-8")))
                self.assertEqual(restored, config)
                with patch("dynamic_firm.product.email_channel.smtplib.SMTP", _Smtp):
                    result = deliver_email_message(config, subject="Noruct test", message="hello")
                client = _Smtp.instances[0]
                self.assertTrue(client.started_tls)
                self.assertEqual(client.login_args, ("agent@example.com", "smtp-secret-value"))
                self.assertEqual(client.message["To"], "operator@example.com")
                self.assertTrue(result.delivered)
                self.assertNotIn("smtp-secret-value", str(result.to_dict()))
                self.assertTrue(remove_email_channel_settings(path))
                self.assertIn("[provider]", path.read_text(encoding="utf-8"))
        finally:
            if previous is None:
                os.environ.pop("EMAIL_TEST_PASSWORD", None)
            else:
                os.environ["EMAIL_TEST_PASSWORD"] = previous

    def test_port_465_uses_implicit_tls_and_header_injection_is_rejected(self) -> None:
        previous = os.environ.get("EMAIL_TEST_PASSWORD")
        os.environ["EMAIL_TEST_PASSWORD"] = "smtp-secret-value"
        try:
            config = EmailChannelConfig("agent@example.com", ("operator@example.com",), "smtp.example.com", smtp_port=465, password_env="EMAIL_TEST_PASSWORD")
            with patch("dynamic_firm.product.email_channel.smtplib.SMTP_SSL", _Smtp):
                result = deliver_email_message(config, subject="TLS", message="hello")
            self.assertTrue(result.delivered)
            self.assertFalse(_Smtp.instances[0].started_tls)
            with self.assertRaisesRegex(ValueError, "single-line"):
                deliver_email_message(config, subject="bad\nBcc: attacker@example.com", message="hello")
        finally:
            if previous is None:
                os.environ.pop("EMAIL_TEST_PASSWORD", None)
            else:
                os.environ["EMAIL_TEST_PASSWORD"] = previous

    def test_cli_is_local_until_explicit_confirm_and_capabilities_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"; output = io.StringIO()
            code = main(["--config", str(path), "channel", "email-configure", "--sender", "agent@example.com", "--to", "operator@example.com", "--smtp-host", "smtp.example.com", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue())
            capabilities = io.StringIO()
            capability_code = main(["--config", str(path), "capabilities", "status", "--json"], stdout=capabilities, stderr=io.StringIO())
            not_confirmed = main(["--config", str(path), "channel", "email-test", "--message", "hello"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(payload["configuration_changed"])
        self.assertEqual(capability_code, EXIT_OK)
        self.assertTrue(json.loads(capabilities.getvalue())["email_channel"]["enabled"])
        self.assertNotEqual(not_confirmed, EXIT_OK)


if __name__ == "__main__":
    unittest.main()
