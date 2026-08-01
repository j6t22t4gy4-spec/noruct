from __future__ import annotations

import argparse
import unittest

from dynamic_firm.application.gateway_cli_parser import add_gateway_commands


class GatewayCliParserTests(unittest.TestCase):
    def test_registers_foreground_and_service_gateway_controls(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_gateway_commands(commands)

        run = parser.parse_args(
            ["gateway", "run", "--receiver", "telegram", "--confirm"]
        )
        service = parser.parse_args(
            ["gateway", "service", "logs", "--lines", "12"]
        )

        self.assertEqual((run.command, run.gateway_command), ("gateway", "run"))
        self.assertEqual(run.receiver, ["telegram"])
        self.assertEqual(
            (service.gateway_command, service.gateway_service_command),
            ("service", "logs"),
        )
        self.assertEqual(service.lines, 12)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
