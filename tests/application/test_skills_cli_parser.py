from __future__ import annotations

import argparse
import unittest
from pathlib import Path

from dynamic_firm.application.skills_cli_parser import add_skills_commands


class SkillsCliParserTests(unittest.TestCase):
    def test_register_schema_preserves_explicit_mutation_inputs(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_skills_commands(commands)

        parsed = parser.parse_args(
            [
                "skills",
                "package",
                "register",
                "--skills-root",
                "managed-skills",
                "--name",
                "reviewer",
                "--artifact-id",
                "reviewer-package",
                "--version",
                "1.0.0",
                "--skill-key",
                "reviewer.v1",
                "--applies-to",
                "analyst",
                "--step",
                "Inspect bounded evidence.",
                "--confirm",
            ]
        )

        self.assertEqual(parsed.command, "skills")
        self.assertEqual(parsed.skills_command, "package")
        self.assertEqual(parsed.skills_root, Path("managed-skills"))
        self.assertEqual(parsed.applies_to, ["analyst"])
        self.assertTrue(parsed.confirm)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
