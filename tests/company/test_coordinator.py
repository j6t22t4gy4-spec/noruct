from __future__ import annotations

import unittest
from datetime import UTC, datetime

import dynamic_firm.company as company_package
from dynamic_firm.company import (
    AuthoritySnapshotIdentity,
    ManagerProposalAdapter,
    FirmCoordinatorAction,
    GraphBlueprint,
    GraphBlueprintOrigin,
    GraphBlueprintRegistry,
    GraphBlueprintTask,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
)
from dynamic_firm.company.coordinator import FirmCoordinator
from dynamic_firm.compiler import CompilerExecutionProfile, CompilerRequest
from dynamic_firm.compiler.replanner import ManagerFollowUpReplanner, SemanticSignalReplanner
from dynamic_firm.kernel.models import JobLimits


def _order(goal: str):
    return normalize_work_order(
        goal,
        work_order_id="work-order-test",
        authority_snapshot=AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=1,
            roster_revision=1,
            playbook_revision=1,
            action_policy_digest="policy-digest",
        ),
        budget_snapshot=WorkOrderBudgetSnapshot(
            max_model_calls=8,
            max_tool_calls=8,
            max_cost_usd=1.0,
            max_wall_time_ms=30_000,
        ),
        requested_at=datetime.now(UTC),
    )


class FirmCoordinatorTests(unittest.TestCase):
    def test_product_adapter_is_the_single_planning_adapter(self) -> None:
        self.assertIs(FirmCoordinator, ManagerProposalAdapter)

    def test_historical_alias_is_not_a_package_level_product_export(self) -> None:
        self.assertNotIn("FirmCoordinator", company_package.__all__)
        self.assertFalse(hasattr(company_package, "FirmCoordinator"))

    @staticmethod
    def _request(goal: str) -> CompilerRequest:
        return CompilerRequest(
            request_id="compiler-request",
            goal=goal,
            workspace_manifest=(),
            available_capabilities=("analysis",),
            model_profile="test",
            execution_profile=CompilerExecutionProfile.READ_ONLY,
        )

    def test_direct_input_skips_manager_model_call_and_has_no_authority(self) -> None:
        decision = FirmCoordinator().initial_decision(_order("hello"))

        self.assertEqual(decision.action, FirmCoordinatorAction.SKIP_DIRECT)
        self.assertFalse(decision.model_call_allowed)
        self.assertFalse(decision.authority_granted)
        decision.verify()

    def test_ordinary_managed_goal_starts_with_one_solo_probe(self) -> None:
        work_order = _order("이 저장소 구조를 분석해줘")
        decision = FirmCoordinator().initial_decision(work_order)

        self.assertEqual(decision.action, FirmCoordinatorAction.RUN_SOLO_PROBE)
        self.assertFalse(decision.model_call_allowed)
        self.assertIsNotNone(
            FirmCoordinator().runtime_replanner(
                work_order,
                FirmCoordinator().runtime_decision(work_order),
                managed_job=True,
            )
        )

    def test_manager_capable_job_uses_bounded_follow_up_adapter(self) -> None:
        work_order = _order("이 저장소 구조를 분석해줘")
        replanner = FirmCoordinator().runtime_replanner(
            work_order,
            FirmCoordinator().runtime_decision(work_order),
            managed_job=True,
            manager_employee_id="employee-manager",
        )

        self.assertIsInstance(replanner, SemanticSignalReplanner)
        assert replanner is not None
        self.assertIsInstance(replanner.delegate, ManagerFollowUpReplanner)

    def test_cross_functional_goal_allows_one_bounded_plan_proposal(self) -> None:
        decision = FirmCoordinator().initial_decision(
            _order(
                "Research the issue, then design a fix, and finally implement and test it"
            )
        )

        self.assertEqual(
            decision.action,
            FirmCoordinatorAction.REQUEST_PLAN_PROPOSAL,
        )
        self.assertTrue(decision.model_call_allowed)
        self.assertFalse(decision.authority_granted)

    def test_direct_decision_cannot_enable_runtime_replanning(self) -> None:
        work_order = _order("hello")

        self.assertIsNone(
            FirmCoordinator().runtime_replanner(
                work_order,
                FirmCoordinator().runtime_decision(work_order),
                managed_job=True,
            )
        )

    def test_local_blueprint_is_bound_before_a_new_planning_call(self) -> None:
        order = _order("이 저장소 구조를 분석해줘")
        registry = GraphBlueprintRegistry()
        registry.save(
            GraphBlueprint(
                blueprint_id="repository-analysis",
                version=1,
                objective_class="general",
                execution_profiles=("read_only",),
                parameters=("objective",),
                tasks=(
                    GraphBlueprintTask(
                        task_id="final",
                        objective_template="Analyze {{objective}}",
                        depends_on=(),
                        required_capabilities=("analysis",),
                        acceptance_templates=("A concise repository assessment",),
                    ),
                ),
                final_task_id="final",
                origin=GraphBlueprintOrigin.VERIFIED_PLAYBOOK,
            )
        )
        coordinator = FirmCoordinator(graph_blueprints=registry)
        decision = coordinator.initial_decision(order)

        resolution = coordinator.resolve_initial_blueprint(
            order,
            decision,
            self._request(order.objective),
            limits=JobLimits(max_tasks=2),
        )

        self.assertTrue(resolution.hit)
        assert resolution.binding is not None
        self.assertEqual(resolution.binding.proposal.final_task_id, "final")


if __name__ == "__main__":
    unittest.main()
