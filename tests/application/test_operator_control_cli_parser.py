from __future__ import annotations

import argparse
import unittest
from pathlib import Path

from dynamic_firm.application.operator_control_cli_parser import (
    add_operator_control_commands,
)


class OperatorControlCliParserTests(unittest.TestCase):
    def test_registers_network_and_local_operator_controls(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_operator_control_commands(
            commands,
            default_state_path=Path("runtime.db"),
            provider_cli_choices=lambda: ("codex",),
            add_execution_options=lambda command: command.add_argument(
                "--workspace", default=None
            ),
        )

        network = parser.parse_args(["network", "source", "list"])
        provider = parser.parse_args(["provider", "status"])

        self.assertEqual(
            (network.command, network.network_command, network.network_source_command),
            ("network", "source", "list"),
        )
        self.assertEqual((provider.command, provider.provider_command), ("provider", "status"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
