from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.application.schedule_cli import SchedulePorts, run_schedule_command
from dynamic_firm.application.schedule_cli_parser import add_schedule_commands


class ScheduleCliParserTests(unittest.TestCase):
    def test_registers_explicit_cron_and_service_actions(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_schedule_commands(commands)

        cron = parser.parse_args(
            ["schedule", "cron-create", "review", "--cron", "0 9 * * 1", "--confirm"]
        )
        service = parser.parse_args(
            ["schedule", "service", "start", "--confirm"]
        )

        self.assertEqual((cron.command, cron.schedule_command), ("schedule", "cron-create"))
        self.assertEqual(cron.cron, "0 9 * * 1")
        self.assertEqual((service.schedule_command, service.schedule_service_command), ("service", "start"))
        self.assertEqual(service.poll_seconds, 60.0)

    def test_status_dispatch_is_directly_composable_without_cli_import(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_schedule_commands(commands)
        with tempfile.TemporaryDirectory() as temporary:
            args = parser.parse_args(
                ["schedule", "status", "--state", str(Path(temporary) / "runtime.db"), "--json"]
            )
            args.config = Path(temporary) / "noruct.toml"
            output = io.StringIO()
            result = run_schedule_command(
                args,
                {},
                output,
                provider_factory=lambda _config: None,
                ports=SchedulePorts(
                    state_path_for=lambda item, _settings: item.state,
                    run_config_for=lambda *_args: None,
                    provider_config_for=lambda _config: None,
                    run_goal_for=lambda *_args, **_kwargs: None,
                    roster_for=lambda _config: None,
                    log_tail=lambda *_args, **_kwargs: {},
                    company_goal_route=object(),
                    exit_ok=0,
                ),
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["scheduler"], "manual_tick_only")
        self.assertEqual(payload["enabled"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
