from __future__ import annotations

import argparse
import unittest

from dynamic_firm.application.integration_cli_parser import add_integration_commands


class IntegrationCliParserTests(unittest.TestCase):
    def test_registers_bounded_integration_and_operator_controls(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_integration_commands(commands)

        browser = parser.parse_args(["browser", "status"])
        channel = parser.parse_args(["channel", "status"])

        self.assertEqual((browser.command, browser.browser_command), ("browser", "status"))
        self.assertEqual((channel.command, channel.channel_command), ("channel", "status"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
