from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.telegram_channel import (
    TelegramChannelConfig,
    TelegramChannelStore,
    run_telegram_channel,
    telegram_channel_config_from_settings,
    telegram_channel_status,
    telegram_state_path,
    remove_telegram_channel_settings,
    write_telegram_channel_settings,
)


class _FakeTelegramApi:
    def __init__(self, updates):
        self._updates = tuple(updates)
        self.sent = []
        self.calls = 0

    async def get_updates(self, *, offset, timeout_seconds):
        self.calls += 1
        if self.calls == 1:
            return self._updates
        await asyncio.sleep(timeout_seconds)
        return ()

    async def send_message(self, *, chat_id, text, reply_to_message_id):
        self.sent.append((chat_id, text, reply_to_message_id))


def _update(update_id: int = 101, sender: int = 7, text: str = "Inspect the repository"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 22,
            "from": {"id": sender},
            "chat": {"id": 99},
            "text": text,
        },
    }


class TelegramChannelTests(unittest.TestCase):
    def test_configuration_round_trip_keeps_token_as_environment_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            config = TelegramChannelConfig(root, ("7",), token_env="TELEGRAM_TOKEN")
            write_telegram_channel_settings(path, config)
            parsed = telegram_channel_config_from_settings(
                {
                    "telegram_channel": {
                        "enabled": True,
                        "workspace": str(root),
                        "allowed_senders": ["7"],
                        "token_env": "TELEGRAM_TOKEN",
                    }
                }
            )
            assert parsed is not None
            self.assertEqual(parsed.allowed_senders, ("7",))
            self.assertFalse(telegram_channel_status(parsed)["ready"])
            self.assertNotIn("TELEGRAM_TOKEN=", path.read_text(encoding="utf-8"))
            self.assertTrue(remove_telegram_channel_settings(path))

    def test_foreground_poll_dispatches_once_replies_and_persists_no_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TelegramChannelConfig(root, ("7",), poll_timeout_seconds=1)
            api = _FakeTelegramApi((_update(text="change secret=abc"),))
            seen = []

            async def dispatch(message):
                seen.append(message.text)
                return "job-telegram-1", "SUCCEEDED", "Finished token=abc"

            state = root / "runtime.db"
            with TelegramChannelStore(telegram_state_path(state)) as store:
                receipt = asyncio.run(
                    run_telegram_channel(
                        config,
                        store=store,
                        dispatch=dispatch,
                        maximum_seconds=5,
                        maximum_messages=1,
                        client=api,
                    )
                )
                stored = store._conn.execute(
                    "SELECT update_id, content_sha256, status, job_id FROM telegram_channel_messages"
                ).fetchone()
            self.assertEqual(seen, ["change secret=abc"])
            self.assertEqual(receipt.accepted_count, 1)
            self.assertEqual(receipt.highest_offset, 102)
            self.assertEqual(api.sent[0][0], "99")
            self.assertNotIn("abc", api.sent[0][1])
            self.assertEqual(stored[0], 101)
            self.assertNotEqual(stored[1], "change secret=abc")
            self.assertEqual(stored[2:], ("COMPLETED", "job-telegram-1"))

    def test_cli_configuration_succeeds_before_operator_sets_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "noruct.toml"
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "--config", str(config), "channel", "telegram-configure",
                        "--workspace", str(root), "--allow-sender", "7",
                    ],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertIn("Telegram channel: needs token environment", output.getvalue())
            self.assertIn("telegram_channel", config.read_text(encoding="utf-8"))

