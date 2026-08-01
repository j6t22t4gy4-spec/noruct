from __future__ import annotations

import argparse
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dynamic_firm.application.company_cli import (
    COMPANY_COMMAND_OK,
    run_company_command,
)


class CompanyCliApplicationAdapterTests(unittest.TestCase):
    def test_adapter_projects_organization_metrics_without_cli_or_company_mutation(self) -> None:
        with TemporaryDirectory() as temporary:
            output = io.StringIO()
            result = run_company_command(
                argparse.Namespace(company_command="organization-metrics", json=False),
                state_path=Path(temporary) / "state.db",
                output=output,
            )
        self.assertEqual(result, COMPANY_COMMAND_OK)
        self.assertIn("Organization evidence · episodes=0", output.getvalue())
        self.assertIn("automatic graph, budget, and Patch changes: disabled", output.getvalue())

    def test_adapter_projects_empty_context_bound_organization_outcomes(self) -> None:
        with TemporaryDirectory() as temporary:
            output = io.StringIO()
            result = run_company_command(
                argparse.Namespace(
                    company_command="organization-outcomes",
                    context_fingerprint=None,
                    json=False,
                ),
                state_path=Path(temporary) / "state.db",
                output=output,
            )
        self.assertEqual(result, COMPANY_COMMAND_OK)
        self.assertIn("No organization outcome context", output.getvalue())
        self.assertIn("running Jobs, Graph, budget, and Patch state: unchanged", output.getvalue())

    def test_adapter_preserves_confirm_boundary_before_opening_mutation_lifecycle(self) -> None:
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "requires --confirm"):
                run_company_command(
                    argparse.Namespace(company_command="approve", confirm=False),
                    state_path=Path(temporary) / "state.db",
                    output=io.StringIO(),
                )
