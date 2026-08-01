from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.inbound_channel import (
    INBOUND_CHANNEL_SCHEMA,
    InboundChannelConfig,
    InboundMessageStore,
    consume_inbound_channel,
    inbound_channel_config_from_settings,
    inbound_channel_status,
    inbound_state_path,
    remove_inbound_channel_settings,
    write_inbound_channel_settings,
)
from dynamic_firm.product.routing import InputRoute


def _bridge_args(*records: dict[str, object]) -> tuple[str, ...]:
    lines = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    return ("-c", f"import sys; sys.stdout.write({lines!r}); sys.stdout.flush()")


class InboundChannelTests(unittest.TestCase):
    def _config(self, root: Path, *records: dict[str, object]) -> InboundChannelConfig:
        return InboundChannelConfig(
            source_id="test-bridge",
            command=Path(sys.executable).resolve(),
            workspace=root,
            allowed_senders=("operator-1",),
            args=_bridge_args(*records),
        )

    def test_configuration_round_trip_preserves_outbound_settings_and_never_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config = self._config(root)
            write_inbound_channel_settings(config_path, config)
            parsed = inbound_channel_config_from_settings(
                {
                    "inbound_channel": {
                        "enabled": True,
                        "source_id": "test-bridge",
                        "command": str(Path(sys.executable).resolve()),
                        "workspace": str(root),
                        "allowed_senders": ["operator-1"],
                        "args": list(config.args),
                        "environment": ["BRIDGE_TOKEN"],
                    }
                }
            )
            assert parsed is not None
            self.assertEqual(parsed.source_id, "test-bridge")
            self.assertEqual(parsed.allowed_senders, ("operator-1",))
            self.assertEqual(inbound_channel_status(parsed)["environment_names"], ["BRIDGE_TOKEN"])
            self.assertNotIn("BRIDGE_TOKEN=", config_path.read_text(encoding="utf-8"))
            self.assertTrue(remove_inbound_channel_settings(config_path))

    def test_consumer_accepts_allowed_message_then_deduplicates_it_without_storing_text(self) -> None:
        record = {
            "schema": INBOUND_CHANNEL_SCHEMA,
            "source_id": "test-bridge",
            "message_id": "message-1",
            "sender": "operator-1",
            "text": "inspect repository status",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, record)
            calls: list[str] = []

            async def dispatch(message):
                calls.append(message.text)
                return "job-inbound-1", "SUCCEEDED"

            state = root / "runtime.db"
            with InboundMessageStore(inbound_state_path(state)) as store:
                first = asyncio.run(consume_inbound_channel(config, store=store, maximum_seconds=5, maximum_messages=1, dispatch=dispatch))
                second = asyncio.run(consume_inbound_channel(config, store=store, maximum_seconds=5, maximum_messages=1, dispatch=dispatch))
                row = store._conn.execute("SELECT content_sha256, status, job_id FROM inbound_channel_messages").fetchone()
            self.assertEqual(calls, ["inspect repository status"])
            self.assertEqual(first.accepted_count, 1)
            self.assertEqual(first.dispatches[0].job_id, "job-inbound-1")
            self.assertEqual(second.duplicate_count, 1)
            assert row is not None
            self.assertNotEqual(row[0], "inspect repository status")
            self.assertEqual(row[1:], ("COMPLETED", "job-inbound-1"))

    def test_consumer_rejects_wrong_sender_without_dispatching(self) -> None:
        record = {
            "schema": INBOUND_CHANNEL_SCHEMA,
            "source_id": "test-bridge",
            "message_id": "message-2",
            "sender": "unapproved",
            "text": "do not run",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, record)

            async def dispatch(_message):
                raise AssertionError("unapproved sender must not be dispatched")

            with InboundMessageStore(root / "inbound.db") as store:
                result = asyncio.run(consume_inbound_channel(config, store=store, maximum_seconds=5, maximum_messages=1, dispatch=dispatch))
            self.assertEqual(result.rejected_count, 1)
            self.assertEqual(result.accepted_count, 0)

    def test_cli_foreground_run_uses_normal_company_job_path(self) -> None:
        record = {
            "schema": INBOUND_CHANNEL_SCHEMA,
            "source_id": "test-bridge",
            "message_id": "message-cli-1",
            "sender": "operator-1",
            "text": "hello",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            state = root / "runtime.db"
            config_path.write_text('[provider]\nkind = "ollama"\nmodel = "fixture"\n', encoding="utf-8")
            configured = io.StringIO()
            self.assertEqual(
                main([
                    "--config", str(config_path), "channel", "inbox-configure",
                    "--source-id", "test-bridge", "--command", str(Path(sys.executable).resolve()),
                    "--workspace", str(root), "--allow-sender", "operator-1",
                    "--arg=-c", "--arg", _bridge_args(record)[1],
                ], stdout=configured, stderr=io.StringIO()),
                EXIT_OK,
            )
            result = SimpleNamespace(job_id="job-inbound-cli", status=SimpleNamespace(value="SUCCEEDED"), summary="done")
            fake_run = AsyncMock(return_value=result)
            output = io.StringIO()
            with patch("dynamic_firm.cli.run_goal", fake_run):
                self.assertEqual(
                    main([
                        "--config", str(config_path), "channel", "inbox-run",
                        "--state", str(state), "--max-seconds", "5", "--confirm",
                    ], provider_factory=lambda _config: object(), stdout=output, stderr=io.StringIO()),
                    EXIT_OK,
                )
            self.assertEqual(fake_run.await_count, 1)
            self.assertEqual(
                fake_run.await_args.kwargs["route"],
                InputRoute.CONVERSATION,
            )
            self.assertIn("job-inbound-cli", output.getvalue())
            self.assertIn("No detached gateway", output.getvalue())

    def test_cli_run_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text('[provider]\nkind = "ollama"\nmodel = "fixture"\n', encoding="utf-8")
            write_inbound_channel_settings(config_path, self._config(root))
            stderr = io.StringIO()
            self.assertNotEqual(
                main(["--config", str(config_path), "channel", "inbox-run"], stdout=io.StringIO(), stderr=stderr),
                EXIT_OK,
            )
            self.assertIn("requires --confirm", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
