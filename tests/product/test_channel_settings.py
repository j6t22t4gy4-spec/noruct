from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.product.channel_settings import (
    ChannelConfig,
    ChannelJobSummary,
    channel_config_from_settings,
    channel_status,
    deliver_channel_test,
    deliver_terminal_job_summary,
    remove_channel_settings,
    write_channel_settings,
)
from dynamic_firm.product.setup import SetupConfig, write_setup_config


class ChannelSettingsTests(unittest.TestCase):
    def _config(self) -> ChannelConfig:
        return ChannelConfig(command=Path(sys.executable).resolve(), args=("-c", "import sys; sys.stdin.read()"))

    def test_configure_preserves_provider_setup_and_never_persists_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            config = ChannelConfig(
                command=Path(sys.executable).resolve(),
                args=("-c", "import sys; sys.stdin.read()"),
                environment_names=("CHANNEL_TOKEN",),
            )
            write_channel_settings(target, config)
            write_setup_config(target, SetupConfig(base_url="http://localhost:11434/v1", model="local"), overwrite=True)
            text = target.read_text(encoding="utf-8")
            self.assertIn("[provider]", text)
            self.assertIn("[channel]", text)
            self.assertIn('environment = ["CHANNEL_TOKEN"]', text)
            self.assertNotIn("secret-value", text)
            self.assertTrue(remove_channel_settings(target))
            self.assertNotIn("[channel]", target.read_text(encoding="utf-8"))

    def test_explicit_test_delivery_uses_stdin_and_reports_no_automatic_delivery(self) -> None:
        result = deliver_channel_test(self._config(), message="hello channel")
        self.assertTrue(result.delivered)
        self.assertEqual(result.message_bytes, len(b"hello channel"))
        self.assertFalse(result.automatic_delivery)

    def test_config_from_settings_and_status_only_expose_environment_names(self) -> None:
        config = channel_config_from_settings(
            {
                "channel": {
                    "enabled": True,
                    "command": str(Path(sys.executable).resolve()),
                    "args": ["-c", "import sys; sys.stdin.read()"],
                    "environment": ["MISSING_CHANNEL_TOKEN"],
                }
            }
        )
        assert config is not None
        previous = os.environ.pop("MISSING_CHANNEL_TOKEN", None)
        try:
            status = channel_status(config)
        finally:
            if previous is not None:
                os.environ["MISSING_CHANNEL_TOKEN"] = previous
        self.assertTrue(status["enabled"])
        self.assertEqual(status["missing_environment_names"], ["MISSING_CHANNEL_TOKEN"])
        self.assertFalse(status["automatic_delivery"])

    def test_configuration_rejects_a_declared_command_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            alias = Path(temporary) / "sender"
            alias.symlink_to(Path(sys.executable).resolve())
            with self.assertRaisesRegex(ValueError, "non-symbolic-link"):
                ChannelConfig(command=alias).validate()

    def test_terminal_job_summary_is_explicitly_delivered_without_job_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            capture = Path(temporary) / "sender-input.json"
            config = ChannelConfig(
                command=Path(sys.executable).resolve(),
                args=(
                    "-c",
                    "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')",
                    str(capture),
                ),
            )
            result = deliver_terminal_job_summary(
                config,
                summary=ChannelJobSummary(
                    job_id="job-20260721",
                    job_status="SUCCEEDED",
                    audit_status="TERMINAL",
                    attempt_count=3,
                    mutation_count=1,
                    final_graph_version=2,
                ),
            )
            envelope = json.loads(capture.read_text(encoding="utf-8"))
        self.assertTrue(result.delivered)
        self.assertEqual(result.payload_kind, "operator_confirmed_terminal_job_summary")
        self.assertEqual(result.job_id, "job-20260721")
        self.assertFalse(result.automatic_delivery)
        self.assertEqual(envelope["title"], "Noruct terminal Job summary")
        self.assertIn("Job: job-20260721", envelope["message"])
        self.assertNotIn("goal", envelope["message"].lower())
        self.assertNotIn("workspace", envelope["message"].lower())
