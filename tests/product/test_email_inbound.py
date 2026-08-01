from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.email_inbound import (
    EmailInboundConfig,
    EmailInboundMessage,
    ImapUnreadClient,
    email_inbound_config_from_settings,
    remove_email_inbound_settings,
    run_email_inbound,
    write_email_inbound_settings,
)
from dynamic_firm.product.inbound_channel import InboundMessageStore, inbound_state_path


class _Client:
    def unread(self, _limit: int):
        return (
            EmailInboundMessage("1", "operator@example.com", "inspect"),
            EmailInboundMessage("1", "operator@example.com", "inspect"),
            EmailInboundMessage("2", "not-allowed@example.com", "ignore"),
        )


class EmailInboundTests(unittest.TestCase):
    def test_foreground_run_allowlists_and_deduplicates_uid(self) -> None:
        async def dispatch(_message: EmailInboundMessage): return ("job-1", "SUCCEEDED")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = EmailInboundConfig(workspace, "company@example.com", "imap.example.com", ("operator@example.com",))
            with InboundMessageStore(inbound_state_path(workspace / "state.sqlite3")) as store:
                result = asyncio.run(run_email_inbound(config, store=store, dispatch=dispatch, maximum_seconds=2, client=_Client()))
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.ignored_count, 1)
        self.assertEqual(result.dispatches[0]["job_id"], "job-1")

    def test_settings_are_nonsecret_and_removable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"; path.write_text('[provider]\nmodel = "fixture"\n', encoding="utf-8")
            config = EmailInboundConfig(Path(directory).resolve(), "company@example.com", "imap.example.com", ("operator@example.com",), password_env="IMAP_PASSWORD")
            write_email_inbound_settings(path, config)
            restored = email_inbound_config_from_settings(__import__("tomllib").loads(path.read_text(encoding="utf-8")))
            self.assertEqual(restored, config)
            self.assertNotIn("password =", path.read_text(encoding="utf-8"))
            self.assertTrue(remove_email_inbound_settings(path))
            self.assertIn("[provider]", path.read_text(encoding="utf-8"))

    def test_imap_client_reads_plaintext_with_peek_and_marks_message_seen(self) -> None:
        class Imap:
            last = None
            def __init__(self, *_args, **_kwargs): self.calls = []; Imap.last = self
            def login(self, user, password): self.calls.append(("login", user, password))
            def select(self, folder, readonly=False): self.calls.append(("select", folder, readonly)); return ("OK", [b""])
            def uid(self, command, *args):
                self.calls.append((command, *args))
                if command == "search": return ("OK", [b"42"])
                if command == "fetch": return ("OK", [(b"42 (RFC822 {1})", b"From: operator@example.com\r\nSubject: hello\r\nContent-Type: text/plain; charset=utf-8\r\n\r\ninspect")])
                return ("OK", [b""])
            def logout(self): self.calls.append(("logout",))
        previous = __import__("os").environ.get("IMAP_TEST_PASSWORD")
        __import__("os").environ["IMAP_TEST_PASSWORD"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = EmailInboundConfig(Path(directory), "company@example.com", "imap.example.com", ("operator@example.com",), password_env="IMAP_TEST_PASSWORD")
                with patch("dynamic_firm.product.email_inbound.imaplib.IMAP4_SSL", Imap):
                    messages = ImapUnreadClient(config).unread(4)
            self.assertEqual(messages, (EmailInboundMessage("42", "operator@example.com", "inspect"),))
            self.assertIn(("fetch", b"42", "(BODY.PEEK[])"), Imap.last.calls)
            self.assertIn(("store", b"42", "+FLAGS.SILENT", "(\\Seen)"), Imap.last.calls)
        finally:
            if previous is None: __import__("os").environ.pop("IMAP_TEST_PASSWORD", None)
            else: __import__("os").environ["IMAP_TEST_PASSWORD"] = previous

    def test_cli_configuration_appears_in_capabilities_and_run_requires_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"; output = io.StringIO()
            code = main(["--config", str(path), "channel", "email-inbox-configure", "--workspace", directory, "--mailbox", "company@example.com", "--imap-host", "imap.example.com", "--allow-sender", "operator@example.com", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue()); capabilities = io.StringIO()
            capability_code = main(["--config", str(path), "capabilities", "status", "--json"], stdout=capabilities, stderr=io.StringIO())
            no_confirm = main(["--config", str(path), "channel", "email-inbox-run"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK); self.assertTrue(payload["configuration_changed"])
        self.assertEqual(capability_code, EXIT_OK); self.assertTrue(json.loads(capabilities.getvalue())["email_inbound"]["enabled"])
        self.assertNotEqual(no_confirm, EXIT_OK)


if __name__ == "__main__":
    unittest.main()
