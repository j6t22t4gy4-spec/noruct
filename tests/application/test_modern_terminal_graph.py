from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.application.modern_terminal_graph import (
    graph_control_snapshot,
    save_future_graph_constraints,
)
from dynamic_firm.company import (
    GraphBlueprint,
    GraphBlueprintControlService,
    GraphBlueprintOrigin,
    GraphBlueprintTask,
    GraphMutationPolicy,
    GraphUserConstraints,
    SQLiteGraphBlueprintRegistry,
)
from dynamic_firm.product.graph_cli_values import graph_registry_path


class ModernTerminalGraphTests(unittest.TestCase):
    def test_future_constraint_save_preserves_selection_identity_and_never_creates_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            registry = SQLiteGraphBlueprintRegistry(graph_registry_path(state_path))
            try:
                control = GraphBlueprintControlService(registry)
                blueprint = control.save(
                    GraphBlueprint(
                        blueprint_id="fixture-plan",
                        version=1,
                        objective_class="general",
                        execution_profiles=("read_only",),
                        parameters=("objective",),
                        tasks=(
                            GraphBlueprintTask(
                                task_id="final",
                                objective_template="Complete {{objective}}",
                                depends_on=(),
                                required_capabilities=("analysis",),
                                acceptance_templates=("Complete",),
                            ),
                        ),
                        final_task_id="final",
                        origin=GraphBlueprintOrigin.DRAFT,
                    )
                )
                control.select(
                    blueprint.ref,
                    constraints=GraphUserConstraints(
                        pinned_employee_ids=("analyst",),
                        excluded_employee_ids=("writer",),
                        require_independent_review=True,
                        max_concurrency=1,
                        max_cost_usd=1.0,
                        max_wall_time_ms=1_000,
                        mutation_policy=GraphMutationPolicy.LOCKED,
                    ),
                )
            finally:
                registry.close()

            saved = save_future_graph_constraints(
                state_path,
                max_concurrency=3,
                max_cost_usd=2.5,
                max_wall_time_ms=30_000,
                mutation_policy=GraphMutationPolicy.PROPOSE,
            )
            snapshot = graph_control_snapshot(state_path)

            self.assertEqual(saved.blueprint_ref, blueprint.ref)
            self.assertEqual(saved.constraints.pinned_employee_ids, ("analyst",))
            self.assertEqual(saved.constraints.excluded_employee_ids, ("writer",))
            self.assertTrue(saved.constraints.require_independent_review)
            self.assertEqual(saved.constraints.max_concurrency, 3)
            self.assertEqual(saved.constraints.max_cost_usd, 2.5)
            self.assertEqual(saved.constraints.max_wall_time_ms, 30_000)
            self.assertEqual(saved.constraints.mutation_policy, GraphMutationPolicy.PROPOSE)
            self.assertEqual(snapshot["selection"]["max_cost_usd"], 2.5)
            self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
