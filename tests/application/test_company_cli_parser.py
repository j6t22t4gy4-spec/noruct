from __future__ import annotations

import argparse
import unittest

from dynamic_firm.application.company_cli_parser import add_company_commands


class CompanyCliParserTests(unittest.TestCase):
    def test_registers_governance_and_explicit_mutation_commands(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_company_commands(commands)

        status = parser.parse_args(["company", "status"])
        proposal = parser.parse_args(
            [
                "company",
                "roster-propose",
                "ADD_EMPLOYEE",
                "--employee-id",
                "employee-1",
                "--rationale",
                "coverage gap",
            ]
        )

        self.assertEqual((status.command, status.company_command), ("company", "status"))
        self.assertEqual(
            (proposal.company_command, proposal.operation),
            ("roster-propose", "ADD_EMPLOYEE"),
        )
        self.assertEqual(proposal.employee_id, "employee-1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
