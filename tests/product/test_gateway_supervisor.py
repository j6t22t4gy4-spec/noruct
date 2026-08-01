from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.email_inbound import EmailInboundConfig, write_email_inbound_settings
from dynamic_firm.product.discord_inbound import DiscordInboundConfig, write_discord_inbound_settings
from dynamic_firm.product.telegram_channel import TelegramChannelConfig, write_telegram_channel_settings
from dynamic_firm.product.matrix_inbound import MatrixInboundConfig, write_matrix_inbound_settings
from dynamic_firm.product.mattermost_inbound import MattermostInboundConfig, write_mattermost_inbound_settings
from dynamic_firm.product.slack_inbound import SlackInboundConfig, write_slack_inbound_settings
from dynamic_firm.product.gateway_service import GatewayServiceStore, gateway_service_state_path


class GatewaySupervisorTests(unittest.TestCase):
    def test_status_is_nonmutating_and_reports_configured_receiver_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path = root / "config.toml"
            write_email_inbound_settings(config_path, EmailInboundConfig(root, "company@example.com", "imap.example.com", ("operator@example.com",)))
            output = io.StringIO()
            code = main(["--config", str(config_path), "gateway", "status", "--json"], stdout=output, stderr=io.StringIO())
        payload = json.loads(output.getvalue())
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["gateway"], "foreground_operator_started_only")
        self.assertFalse(payload["background_service"])
        self.assertFalse(payload["receivers"]["email"]["ready"])

    def test_run_requires_confirm_and_uses_configured_email_receiver_only(self) -> None:
        previous = os.environ.get("GATEWAY_IMAP_PASSWORD"); os.environ["GATEWAY_IMAP_PASSWORD"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"
                write_email_inbound_settings(config_path, EmailInboundConfig(root, "company@example.com", "imap.example.com", ("operator@example.com",), password_env="GATEWAY_IMAP_PASSWORD"))
                denied = main(["--config", str(config_path), "gateway", "run", "--receiver", "email", "--max-cycles", "1"], stdout=io.StringIO(), stderr=io.StringIO())
                output = io.StringIO(); fake = AsyncMock(return_value=SimpleNamespace(accepted_count=0, duplicate_count=0, ignored_count=0, dispatches=()))
                with patch("dynamic_firm.cli.run_email_inbound", fake):
                    code = main(["--config", str(config_path), "gateway", "run", "--receiver", "email", "--state", str(state), "--max-cycles", "1", "--confirm", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue())
            self.assertNotEqual(denied, EXIT_OK); self.assertEqual(code, EXIT_OK)
            self.assertEqual(fake.await_count, 1); self.assertEqual(payload["receivers"], ["email"])
            self.assertEqual(payload["cycles"][0]["receivers"][0]["receiver"], "email")
        finally:
            if previous is None: os.environ.pop("GATEWAY_IMAP_PASSWORD", None)
            else: os.environ["GATEWAY_IMAP_PASSWORD"] = previous

    def test_run_uses_configured_telegram_receiver_without_creating_new_gateway_authority(self) -> None:
        previous = os.environ.get("GATEWAY_TELEGRAM_TOKEN"); os.environ["GATEWAY_TELEGRAM_TOKEN"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"
                write_telegram_channel_settings(
                    config_path,
                    TelegramChannelConfig(root, ("12345",), token_env="GATEWAY_TELEGRAM_TOKEN"),
                )
                output = io.StringIO()
                fake = AsyncMock(
                    return_value=SimpleNamespace(
                        accepted_count=0,
                        duplicate_count=0,
                        ignored_count=0,
                        rejected_count=0,
                        dispatches=(),
                    )
                )
                with patch("dynamic_firm.cli.run_telegram_channel", fake):
                    code = main(
                        [
                            "--config", str(config_path), "gateway", "run", "--receiver", "telegram",
                            "--state", str(state), "--max-cycles", "1", "--confirm", "--json",
                        ],
                        stdout=output,
                        stderr=io.StringIO(),
                    )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(fake.await_count, 1)
            self.assertEqual(payload["receivers"], ["telegram"])
            self.assertEqual(payload["cycles"][0]["receivers"][0]["receiver"], "telegram")
        finally:
            if previous is None: os.environ.pop("GATEWAY_TELEGRAM_TOKEN", None)
            else: os.environ["GATEWAY_TELEGRAM_TOKEN"] = previous

    def test_run_uses_configured_discord_receiver_without_a_second_gateway_authority(self) -> None:
        previous = os.environ.get("GATEWAY_DISCORD_TOKEN"); os.environ["GATEWAY_DISCORD_TOKEN"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"
                write_discord_inbound_settings(
                    config_path,
                    DiscordInboundConfig(root, ("12345",), ("54321",), token_env="GATEWAY_DISCORD_TOKEN"),
                )
                output = io.StringIO()
                fake = AsyncMock(return_value=SimpleNamespace(accepted_count=0, duplicate_count=0, ignored_count=0, dispatches=()))
                with patch("dynamic_firm.cli.run_discord_inbound_channel", fake), patch("dynamic_firm.product.discord_inbound.importlib.util.find_spec", return_value=object()):
                    code = main(
                        [
                            "--config", str(config_path), "gateway", "run", "--receiver", "discord",
                            "--state", str(state), "--max-cycles", "1", "--confirm", "--json",
                        ],
                        stdout=output,
                        stderr=io.StringIO(),
                    )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(fake.await_count, 1)
            self.assertEqual(payload["receivers"], ["discord"])
            self.assertEqual(payload["cycles"][0]["receivers"][0]["receiver"], "discord")
        finally:
            if previous is None: os.environ.pop("GATEWAY_DISCORD_TOKEN", None)
            else: os.environ["GATEWAY_DISCORD_TOKEN"] = previous

    def test_run_uses_configured_matrix_and_mattermost_receivers_foreground_only(self) -> None:
        previous_matrix, previous_mattermost = os.environ.get("GATEWAY_MATRIX_TOKEN"), os.environ.get("GATEWAY_MATTERMOST_TOKEN")
        os.environ["GATEWAY_MATRIX_TOKEN"] = "matrix-secret"; os.environ["GATEWAY_MATTERMOST_TOKEN"] = "mattermost-secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"
                write_matrix_inbound_settings(config_path, MatrixInboundConfig(root, "http://127.0.0.1:8008", "!room:example.org", ("@operator:example.org",), token_env="GATEWAY_MATRIX_TOKEN"))
                write_mattermost_inbound_settings(config_path, MattermostInboundConfig(root, "http://127.0.0.1:8065", "channel1", ("operator1",), token_env="GATEWAY_MATTERMOST_TOKEN"))
                output = io.StringIO()
                matrix = AsyncMock(return_value=SimpleNamespace(primed=False, accepted_count=0, duplicate_count=0, ignored_count=0, dispatches=()))
                mattermost = AsyncMock(return_value=SimpleNamespace(primed=False, accepted_count=0, duplicate_count=0, ignored_count=0, dispatches=()))
                with patch("dynamic_firm.cli.run_matrix_inbound", matrix), patch("dynamic_firm.cli.run_mattermost_inbound", mattermost):
                    code = main(["--config", str(config_path), "gateway", "run", "--receiver", "matrix", "--receiver", "mattermost", "--state", str(state), "--max-cycles", "1", "--confirm", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue())
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(matrix.await_count, 1); self.assertEqual(mattermost.await_count, 1)
            self.assertEqual(payload["receivers"], ["matrix", "mattermost"])
        finally:
            if previous_matrix is None: os.environ.pop("GATEWAY_MATRIX_TOKEN", None)
            else: os.environ["GATEWAY_MATRIX_TOKEN"] = previous_matrix
            if previous_mattermost is None: os.environ.pop("GATEWAY_MATTERMOST_TOKEN", None)
            else: os.environ["GATEWAY_MATTERMOST_TOKEN"] = previous_mattermost

    def test_run_uses_configured_slack_receiver_without_claiming_proxy_or_gateway_authority(self) -> None:
        previous = os.environ.get("GATEWAY_SLACK_SIGNING_SECRET"); os.environ["GATEWAY_SLACK_SIGNING_SECRET"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"; output = io.StringIO()
                write_slack_inbound_settings(
                    config_path,
                    SlackInboundConfig(root, ("U123",), ("C123",), signing_secret_env="GATEWAY_SLACK_SIGNING_SECRET"),
                )
                fake = AsyncMock(return_value=SimpleNamespace(accepted_count=0, duplicate_count=0, ignored_count=0, rejected_request_count=0, bound_port=3001, dispatches=()))
                with patch("dynamic_firm.cli.run_slack_inbound_channel", fake):
                    code = main(["--config", str(config_path), "gateway", "run", "--receiver", "slack", "--state", str(state), "--max-cycles", "1", "--confirm", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue())
            self.assertEqual(code, EXIT_OK); self.assertEqual(fake.await_count, 1)
            self.assertEqual(payload["receivers"], ["slack"])
            self.assertEqual(payload["cycles"][0]["receivers"][0]["receiver"], "slack")
        finally:
            if previous is None: os.environ.pop("GATEWAY_SLACK_SIGNING_SECRET", None)
            else: os.environ["GATEWAY_SLACK_SIGNING_SECRET"] = previous

    def test_service_start_records_one_noruct_owned_child_without_enabling_auto_restart(self) -> None:
        previous = os.environ.get("GATEWAY_TELEGRAM_TOKEN"); os.environ["GATEWAY_TELEGRAM_TOKEN"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"
                write_telegram_channel_settings(
                    config_path,
                    TelegramChannelConfig(root, ("12345",), token_env="GATEWAY_TELEGRAM_TOKEN"),
                )
                output = io.StringIO()
                with patch("dynamic_firm.cli.subprocess.Popen", return_value=SimpleNamespace(pid=43210)) as spawn:
                    code = main(
                        [
                            "--config", str(config_path), "gateway", "service", "start", "--receiver", "telegram",
                            "--state", str(state), "--confirm", "--json",
                        ],
                        stdout=output,
                        stderr=io.StringIO(),
                    )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(payload["gateway"], "noruct_owned_local_service")
            self.assertTrue(payload["background_service"])
            self.assertFalse(payload["automatic_restart"])
            self.assertEqual(payload["record"]["pid"], 43210)
            self.assertEqual(len(payload["record"]["receiver_config_digest"]), 64)
            child_command = spawn.call_args.args[0]
            self.assertEqual(child_command[:3], [os.sys.executable, "-m", "dynamic_firm"])
            self.assertIn("gateway", child_command)
            self.assertIn("run", child_command)
        finally:
            if previous is None: os.environ.pop("GATEWAY_TELEGRAM_TOKEN", None)
            else: os.environ["GATEWAY_TELEGRAM_TOKEN"] = previous

    def test_service_status_reports_receiver_configuration_drift_without_restarting_child(self) -> None:
        previous = os.environ.get("GATEWAY_TELEGRAM_TOKEN"); os.environ["GATEWAY_TELEGRAM_TOKEN"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"
                write_telegram_channel_settings(config_path, TelegramChannelConfig(root, ("12345",), token_env="GATEWAY_TELEGRAM_TOKEN"))
                with patch("dynamic_firm.cli.subprocess.Popen", return_value=SimpleNamespace(pid=43214)):
                    started = main([
                        "--config", str(config_path), "gateway", "service", "start", "--receiver", "telegram",
                        "--state", str(state), "--confirm", "--json",
                    ], stdout=io.StringIO(), stderr=io.StringIO())
                self.assertEqual(started, EXIT_OK)
                write_telegram_channel_settings(config_path, TelegramChannelConfig(root, ("99999",), token_env="GATEWAY_TELEGRAM_TOKEN"))
                output = io.StringIO()
                with patch("dynamic_firm.product.gateway_service._is_alive", return_value=True):
                    code = main([
                        "--config", str(config_path), "gateway", "service", "status", "--state", str(state), "--json",
                    ], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue())
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(payload["record"]["state"], "running")
            self.assertEqual(payload["receiver_configuration"]["status"], "DRIFTED_FROM_CURRENT_CONFIGURATION")
            self.assertNotIn("current_digest", payload["receiver_configuration"])
        finally:
            if previous is None: os.environ.pop("GATEWAY_TELEGRAM_TOKEN", None)
            else: os.environ["GATEWAY_TELEGRAM_TOKEN"] = previous

    def test_service_start_accepts_configured_matrix_without_expanding_lifecycle_authority(self) -> None:
        previous = os.environ.get("GATEWAY_MATRIX_SERVICE_TOKEN"); os.environ["GATEWAY_MATRIX_SERVICE_TOKEN"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"; output = io.StringIO()
                write_matrix_inbound_settings(config_path, MatrixInboundConfig(root, "http://127.0.0.1:8008", "!room:example.org", ("@operator:example.org",), token_env="GATEWAY_MATRIX_SERVICE_TOKEN"))
                with patch("dynamic_firm.cli.subprocess.Popen", return_value=SimpleNamespace(pid=43212)) as spawn:
                    code = main(["--config", str(config_path), "gateway", "service", "start", "--receiver", "matrix", "--state", str(state), "--confirm", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue())
            self.assertEqual(code, EXIT_OK); self.assertEqual(payload["record"]["receivers"], ["matrix"])
            self.assertIn("matrix", spawn.call_args.args[0])
            self.assertFalse(payload["automatic_restart"])
        finally:
            if previous is None: os.environ.pop("GATEWAY_MATRIX_SERVICE_TOKEN", None)
            else: os.environ["GATEWAY_MATRIX_SERVICE_TOKEN"] = previous

    def test_service_start_accepts_configured_slack_without_provisioning_a_public_webhook(self) -> None:
        previous = os.environ.get("GATEWAY_SLACK_SERVICE_SECRET"); os.environ["GATEWAY_SLACK_SERVICE_SECRET"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"; output = io.StringIO()
                write_slack_inbound_settings(config_path, SlackInboundConfig(root, ("U123",), ("C123",), signing_secret_env="GATEWAY_SLACK_SERVICE_SECRET"))
                with patch("dynamic_firm.cli.subprocess.Popen", return_value=SimpleNamespace(pid=43213)) as spawn:
                    code = main(["--config", str(config_path), "gateway", "service", "start", "--receiver", "slack", "--state", str(state), "--confirm", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue())
            self.assertEqual(code, EXIT_OK); self.assertEqual(payload["record"]["receivers"], ["slack"])
            self.assertIn("slack", spawn.call_args.args[0]); self.assertFalse(payload["automatic_restart"])
        finally:
            if previous is None: os.environ.pop("GATEWAY_SLACK_SERVICE_SECRET", None)
            else: os.environ["GATEWAY_SLACK_SERVICE_SECRET"] = previous

    def test_service_restart_is_explicit_and_keeps_auto_restart_disabled(self) -> None:
        previous = os.environ.get("GATEWAY_TELEGRAM_TOKEN"); os.environ["GATEWAY_TELEGRAM_TOKEN"] = "secret"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"
                write_telegram_channel_settings(config_path, TelegramChannelConfig(root, ("12345",), token_env="GATEWAY_TELEGRAM_TOKEN"))
                output = io.StringIO()
                with patch("dynamic_firm.cli.subprocess.Popen", return_value=SimpleNamespace(pid=43211)) as spawn:
                    code = main(["--config", str(config_path), "gateway", "service", "restart", "--receiver", "telegram", "--state", str(state), "--confirm", "--json"], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue())
            self.assertEqual(code, EXIT_OK); self.assertEqual(spawn.call_count, 1)
            self.assertEqual(payload["action"], "restart")
            self.assertFalse(payload["automatic_restart"])
            self.assertEqual(payload["record"]["pid"], 43211)
        finally:
            if previous is None: os.environ.pop("GATEWAY_TELEGRAM_TOKEN", None)
            else: os.environ["GATEWAY_TELEGRAM_TOKEN"] = previous

    def test_service_logs_are_bounded_local_and_do_not_control_the_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path = root / "config.toml"; state = root / "state.sqlite3"
            service_state = gateway_service_state_path(state); log_path = service_state.with_suffix(".log")
            log_path.write_text("first\nsecond\nthird\n", encoding="utf-8")
            with GatewayServiceStore(service_state) as store:
                reservation = store.reserve_start(receivers=("telegram",), log_path=log_path)
                store.mark_started(run_id=reservation.run_id or "", pid=os.getpid())
            output = io.StringIO()
            code = main(["--config", str(config_path), "gateway", "service", "logs", "--state", str(state), "--lines", "2", "--json"], stdout=output, stderr=io.StringIO())
        payload = json.loads(output.getvalue())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(payload["log"]["available"])
        self.assertEqual(payload["log"]["lines"], ["second", "third"])
        self.assertEqual(payload["action"], "logs")


if __name__ == "__main__": unittest.main()
