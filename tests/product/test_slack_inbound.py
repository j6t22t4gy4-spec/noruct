from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.slack_inbound import (
    SlackEventReceiver,
    SlackInboundConfig,
    SlackInboundStore,
    remove_slack_inbound_settings,
    run_slack_inbound_channel,
    slack_inbound_config_from_settings,
    slack_inbound_state_path,
    verify_slack_signature,
    write_slack_inbound_settings,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _event(text: str = "Inspect the workspace") -> bytes:
    return json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev0123456789",
            "event": {
                "type": "message",
                "user": "U0123456789",
                "channel": "C0123456789",
                "ts": "171234.0001",
                "text": text,
            },
        }
    ).encode("utf-8")


def _headers(secret: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    signature = "v0=" + hmac.new(
        secret.encode("utf-8"), b"v0:" + stamp.encode("ascii") + b":" + body, hashlib.sha256
    ).hexdigest()
    return {"X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": signature}


class SlackInboundTests(unittest.TestCase):
    def test_signature_verification_rejects_stale_or_changed_payload(self) -> None:
        secret, body, stamp = "signing-secret", _event(), 1_700_000_000
        headers = _headers(secret, body, stamp)
        self.assertTrue(
            verify_slack_signature(
                secret=secret, timestamp=str(stamp), signature=headers["X-Slack-Signature"],
                body=body, skew_seconds=300, now=float(stamp),
            )
        )
        self.assertFalse(
            verify_slack_signature(
                secret=secret, timestamp=str(stamp), signature=headers["X-Slack-Signature"],
                body=body + b"!", skew_seconds=300, now=float(stamp),
            )
        )

    def test_signed_allowlisted_event_dispatches_once_and_keeps_only_hash(self) -> None:
        previous = os.environ.get("SLACK_TEST_SIGNING_SECRET")
        os.environ["SLACK_TEST_SIGNING_SECRET"] = "signing-secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = SlackInboundConfig(
                    workspace=root, allowed_senders=("U0123456789",), allowed_channels=("C0123456789",),
                    signing_secret_env="SLACK_TEST_SIGNING_SECRET", port=_free_port(),
                )
                receiver = SlackEventReceiver(config)
                body = _event("Inspect secret=abc")
                status, _, _ = receiver.accept(path="/slack/events", headers=_headers("signing-secret", body), body=body)
                self.assertEqual(status, 200)
                seen: list[str] = []

                async def dispatch(message):
                    seen.append(message.text)
                    return "job-slack-1", "SUCCEEDED"

                state = root / "runtime.db"
                with SlackInboundStore(slack_inbound_state_path(state)) as store:
                    receipt = asyncio.run(
                        run_slack_inbound_channel(
                            config, store=store, dispatch=dispatch, maximum_seconds=2,
                            maximum_messages=1, receiver=receiver,
                        )
                    )
                    stored = store._conn.execute(
                        "SELECT content_sha256, status, job_id FROM slack_inbound_messages"
                    ).fetchone()
                self.assertEqual(seen, ["Inspect secret=abc"])
                self.assertEqual(receipt.accepted_count, 1)
                self.assertEqual(receipt.dispatches[0].outcome, "DISPATCHED")
                self.assertEqual(stored[1:], ("COMPLETED", "job-slack-1"))
                self.assertNotEqual(stored[0], "Inspect secret=abc")
        finally:
            if previous is None:
                os.environ.pop("SLACK_TEST_SIGNING_SECRET", None)
            else:
                os.environ["SLACK_TEST_SIGNING_SECRET"] = previous

    def test_loopback_http_receiver_verifies_signature_before_queueing(self) -> None:
        previous = os.environ.get("SLACK_TEST_SIGNING_SECRET")
        os.environ["SLACK_TEST_SIGNING_SECRET"] = "signing-secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = SlackInboundConfig(
                    workspace=Path(directory), allowed_senders=("U0123456789",), allowed_channels=("C0123456789",),
                    signing_secret_env="SLACK_TEST_SIGNING_SECRET", port=_free_port(),
                )
                receiver = SlackEventReceiver(config)
                receiver.start()
                try:
                    body = _event()
                    request = Request(
                        f"http://127.0.0.1:{receiver.bound_port}/slack/events", data=body,
                        headers={"Content-Type": "application/json", **_headers("signing-secret", body)}, method="POST",
                    )
                    with urlopen(request, timeout=2) as response:  # nosec B310: test loopback listener
                        self.assertEqual(response.status, 200)
                    self.assertEqual(receiver.get(0.5).sender_id, "U0123456789")
                finally:
                    receiver.close()
        finally:
            if previous is None:
                os.environ.pop("SLACK_TEST_SIGNING_SECRET", None)
            else:
                os.environ["SLACK_TEST_SIGNING_SECRET"] = previous

    def test_configuration_is_non_secret_and_cli_never_binds_a_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, config_path = Path(directory), Path(directory) / "noruct.toml"
            config = SlackInboundConfig(root, ("U0123456789",), ("C0123456789",), signing_secret_env="SLACK_TEST_SIGNING_SECRET")
            write_slack_inbound_settings(config_path, config)
            restored = slack_inbound_config_from_settings(__import__("tomllib").loads(config_path.read_text(encoding="utf-8")))
            self.assertEqual(restored, config)
            self.assertNotIn("signing-secret", config_path.read_text(encoding="utf-8"))
            self.assertTrue(remove_slack_inbound_settings(config_path))
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "--config", str(config_path), "channel", "slack-inbox-configure",
                        "--workspace", str(root), "--allow-sender", "U0123456789", "--allow-channel", "C0123456789",
                    ],
                    stdout=output, stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertIn("Slack inbound receiver: needs signing-secret environment", output.getvalue())
    
