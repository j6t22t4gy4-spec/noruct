from __future__ import annotations

import argparse
import unittest

from dynamic_firm.application.evolution_cli_parser import add_evolution_commands


class EvolutionCliParserTests(unittest.TestCase):
    def test_registers_local_and_hosted_evolution_lifecycle(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_evolution_commands(commands)

        status = parser.parse_args(["evolution", "status"])
        hosted = parser.parse_args(
            [
                "evolution",
                "network",
                "submit",
                "capsule-1",
                "--endpoint",
                "https://example.test",
                "--confirm",
            ]
        )
        shadow = parser.parse_args(
            [
                "evolution",
                "artifact",
                "shadow-receipts",
                "--scope-key",
                "company_default",
                "--artifact-id",
                "repository_skill",
            ]
        )
        regression = parser.parse_args(
            [
                "evolution", "artifact", "report-regression", "company_default",
                "repository_skill", "--signal-kind", "SAFETY_REGRESSION",
                "--evidence-digest", "a" * 64, "--confirm",
            ]
        )

        self.assertEqual((status.command, status.evolution_command), ("evolution", "status"))
        self.assertEqual(
            (hosted.evolution_command, hosted.evolution_network_command),
            ("network", "submit"),
        )
        self.assertEqual(hosted.capsule_id, "capsule-1")
        self.assertEqual(shadow.evolution_artifact_command, "shadow-receipts")
        self.assertEqual(shadow.scope_key, "company_default")
        self.assertEqual(regression.evolution_artifact_command, "report-regression")
        self.assertTrue(regression.confirm)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
