from __future__ import annotations

import argparse
import unittest

from dynamic_firm.application.session_cli_parser import add_session_commands


class SessionCliParserTests(unittest.TestCase):
    def test_registers_search_and_explicit_rewind_controls(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_session_commands(commands)

        search = parser.parse_args(["session", "search", "budget"])
        rewind = parser.parse_args(["session", "rewind", "session-1", "3", "--confirm"])

        self.assertEqual((search.command, search.session_command), ("session", "search"))
        self.assertEqual(search.query, "budget")
        self.assertEqual((rewind.session_id, rewind.through_message), ("session-1", 3))
        self.assertTrue(rewind.confirm)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
