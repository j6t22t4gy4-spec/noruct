from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.discord_inbound import (
    DiscordInboundConfig,
    DiscordInboundStore,
    discord_inbound_config_from_settings,
    discord_inbound_status,
    discord_inbound_state_path,
    run_discord_inbound_channel,
)


def _message(message_id: str, sender_id: str, channel_id: str, text: str, *, bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=sender_id, bot=bot),
        channel=SimpleNamespace(id=channel_id),
        content=text,
    )


class _FakeIntents:
    message_content = False
    guild_messages = False
    dm_messages = False

    @classmethod
    def default(cls) -> "_FakeIntents":
        return cls()


class _FakeDiscord:
    Intents = _FakeIntents


class _FakeClient:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = messages
        self.closed = False
        self.on_message = None

    def event(self, callback):
        self.on_message = callback
        return callback

    async def start(self, _token: str) -> None:
        assert self.on_message is not None
        for message in self.messages:
            await self.on_message(message)
            if self.closed:
                break

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class DiscordInboundTests(unittest.TestCase):
    def test_foreground_inbound_dispatches_only_allowlisted_deduplicated_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = DiscordInboundConfig(
                workspace=root,
                allowed_senders=("111",),
                allowed_channels=("222",),
                token_env="DISCORD_TEST_TOKEN",
                max_messages_per_run=2,
            )
            messages = [
                _message("1", "111", "222", "first"),
                _message("1", "111", "222", "duplicate"),
                _message("2", "999", "222", "ignored"),
                _message("3", "111", "222", "second"),
            ]
            client = _FakeClient(messages)
            received = []

            async def dispatch(message):
                received.append(message.text)
                return f"job-{message.message_id}", "SUCCEEDED"

            with patch.dict(os.environ, {"DISCORD_TEST_TOKEN": "token"}, clear=False), DiscordInboundStore(discord_inbound_state_path(root / "state.sqlite3")) as store:
                result = asyncio.run(
                    run_discord_inbound_channel(
                        config,
                        store=store,
                        dispatch=dispatch,
                        maximum_seconds=1,
                        discord_module=_FakeDiscord,
                        client_factory=lambda _intents: client,
                    )
                )

            self.assertEqual(received, ["first", "second"])
            self.assertEqual(result.accepted_count, 2)
            self.assertEqual(result.duplicate_count, 1)
            self.assertEqual(result.ignored_count, 1)
            self.assertTrue(client.closed)
            self.assertEqual([item.job_status for item in result.dispatches], ["SUCCEEDED", "SUCCEEDED"])

    def test_cli_configures_and_reports_optional_dependency_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.toml"
            output = io.StringIO()
            code = main(
                [
                    "--config", str(config_path), "channel", "discord-inbox-configure",
                    "--workspace", str(root), "--allow-sender", "111", "--allow-channel", "222", "--json",
                ],
                stdout=output,
                stderr=io.StringIO(),
            )
            self.assertEqual(code, EXIT_OK)
            import tomllib
            config = discord_inbound_config_from_settings(tomllib.loads(config_path.read_text(encoding="utf-8")))
            self.assertIsNotNone(config)
            assert config is not None
            status = discord_inbound_status(config)
            self.assertTrue(status["enabled"])
            self.assertEqual(status["allowed_sender_count"], 1)
            self.assertEqual(status["allowed_channel_count"], 1)

    def test_requires_a_token_before_starting_a_gateway_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = DiscordInboundConfig(workspace=root, allowed_senders=("111",), allowed_channels=("222",), token_env="MISSING_DISCORD_TOKEN")
            with DiscordInboundStore(discord_inbound_state_path(root / "state.sqlite3")) as store:
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(ValueError, "token environment"):
                        asyncio.run(
                            run_discord_inbound_channel(
                                config,
                                store=store,
                                dispatch=lambda _message: None,
                                maximum_seconds=1,
                                discord_module=_FakeDiscord,
                                client_factory=lambda _intents: _FakeClient([]),
                            )
                        )
