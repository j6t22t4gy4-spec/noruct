from __future__ import annotations

import argparse
import unittest
from pathlib import Path

from dynamic_firm.company import GraphMutationPolicy, GraphUserConstraints
from dynamic_firm.product.graph_cli_values import (
    graph_constraints_from_args,
    graph_registry_path,
)


class GraphCliValueTests(unittest.TestCase):
    def test_registry_path_is_adjacent_to_the_company_state_database(self) -> None:
        self.assertEqual(
            graph_registry_path(Path("/tmp/company.runtime.db")),
            Path("/tmp/company.runtime.graph-blueprints.db"),
        )

    def test_explicit_surface_fields_narrow_a_future_job_preference(self) -> None:
        existing = GraphUserConstraints(
            pinned_employee_ids=("employee-generalist",),
            max_cost_usd=8.0,
            mutation_policy=GraphMutationPolicy.PROPOSE,
        )
        args = argparse.Namespace(
            pin_employee=("employee-reviewer",),
            exclude_employee=("employee-generalist",),
            require_independent_review=True,
            max_concurrency=2,
            max_cost_usd=3.5,
            max_wall_time_ms=12_000,
            mutation_policy=GraphMutationPolicy.LOCKED.value,
        )

        constraints = graph_constraints_from_args(args, existing=existing)

        self.assertEqual(constraints.pinned_employee_ids, ("employee-reviewer",))
        self.assertEqual(constraints.excluded_employee_ids, ("employee-generalist",))
        self.assertTrue(constraints.require_independent_review)
        self.assertEqual(constraints.max_concurrency, 2)
        self.assertEqual(constraints.max_cost_usd, 3.5)
        self.assertEqual(constraints.max_wall_time_ms, 12_000)
        self.assertEqual(constraints.mutation_policy, GraphMutationPolicy.LOCKED)

    def test_preview_can_ignore_a_cli_budget_field_without_losing_existing_value(self) -> None:
        existing = GraphUserConstraints(max_cost_usd=8.0)
        args = argparse.Namespace(
            pin_employee=None,
            exclude_employee=None,
            require_independent_review=None,
            max_concurrency=None,
            max_cost_usd=3.5,
            max_wall_time_ms=None,
            mutation_policy=None,
        )

        constraints = graph_constraints_from_args(
            args,
            existing=existing,
            include_budget=False,
        )

        self.assertEqual(constraints.max_cost_usd, 8.0)


if __name__ == "__main__":
    unittest.main()
