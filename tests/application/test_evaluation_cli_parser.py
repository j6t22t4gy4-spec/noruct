from __future__ import annotations

import argparse
import unittest

from dynamic_firm.application.evaluation_cli_parser import add_evaluation_commands


class EvaluationCliParserTests(unittest.TestCase):
    def test_registers_offline_and_workflow_evaluation_lifecycles(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_evaluation_commands(commands)

        offline = parser.parse_args(["eval", "coding"])
        value = parser.parse_args(["eval", "firm-value-v2"])

        self.assertEqual((offline.command, offline.evaluation), ("eval", "coding"))
        self.assertEqual((value.command, value.evaluation), ("eval", "firm-value-v2"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
