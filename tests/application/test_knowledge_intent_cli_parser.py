from __future__ import annotations

import argparse
import unittest

from dynamic_firm.application.knowledge_intent_cli_parser import (
    add_knowledge_intent_commands,
)


class KnowledgeIntentCliParserTests(unittest.TestCase):
    def test_registers_knowledge_and_intent_plane_controls(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_knowledge_intent_commands(
            commands,
            add_local_knowledge_options=lambda command: command.add_argument(
                "--state", default=None
            ),
            add_execution_options=lambda command: command.add_argument(
                "--workspace", default=None
            ),
        )

        recall = parser.parse_args(["knowledge", "recall", "budget"])
        intent = parser.parse_args(["intent", "run", "intent-1"])
        research = parser.parse_args(["research", "accept", "research-1"])

        self.assertEqual((recall.command, recall.knowledge_command), ("knowledge", "recall"))
        self.assertEqual((intent.command, intent.intent_command), ("intent", "run"))
        self.assertEqual((research.command, research.research_command), ("research", "accept"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
