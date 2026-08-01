from __future__ import annotations

import unittest
from datetime import UTC, datetime

from dynamic_firm.company import (
    AuthoritySnapshotIdentity,
    ManagerDelegation,
    ManagerAssignmentMode,
    PersistentExecutiveManager,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
)
from dynamic_firm.company.direct import DirectCompanyExecutor
from dynamic_firm.kernel.models import EmployeeRecord, JobTask, PlanProposal
from dynamic_firm.runtime.models import ActionPolicy, ToolEffect, ToolGrant


def _order(goal: str):
    return normalize_work_order(
        goal,
        work_order_id="work-order-manager",
        authority_snapshot=AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=1,
            roster_revision=3,
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


class PersistentExecutiveManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = EmployeeRecord(
            "employee-manager",
            "Executive Manager",
            ("company_management",),
            model_profile="fixture",
        )
        self.specialist = EmployeeRecord(
            "employee-analyst",
            "Analyst",
            ("analysis",),
            model_profile="fixture",
        )

    def test_direct_work_is_bound_to_one_persistent_manager_identity(self) -> None:
        manager = PersistentExecutiveManager.from_roster(
            (self.manager, self.specialist),
            roster_revision=3,
        )
        order = _order("hello")
        assignment = manager.initial_assignment(order, session_key="session-a")

        self.assertEqual(assignment.mode, ManagerAssignmentMode.DIRECT_RESPONSE)
        self.assertEqual(assignment.manager_employee_id, self.manager.employee_id)
        self.assertEqual(
            assignment.session_key,
            "manager:employee-manager:session-a",
        )
        self.assertFalse(assignment.authority_granted)
        manager.validate_assignment(assignment, order)

    def test_managed_work_creates_delegation_assignment_without_authority(self) -> None:
        manager = PersistentExecutiveManager.from_roster(
            (self.manager, self.specialist),
            roster_revision=3,
        )
        order = _order("이 저장소를 분석해줘")
        assignment = manager.initial_assignment(order, session_key="")

        self.assertEqual(assignment.mode, ManagerAssignmentMode.DELEGATE)
        self.assertEqual(assignment.reason, "MANAGER_SPECIALIST_DELEGATION_REQUIRED")
        self.assertFalse(assignment.authority_granted)
        manager.validate_assignment(assignment, order)

        proposal = PlanProposal(
            proposal_id="proposal-manager",
            goal=order.objective,
            tasks=(
                JobTask(
                    task_id="research",
                    objective="Research the evidence",
                    depends_on=(),
                    required_capabilities=("analysis",),
                    acceptance_criteria=("Return cited findings",),
                ),
                JobTask(
                    task_id="report",
                    objective="Integrate the findings",
                    depends_on=("research",),
                    required_capabilities=("analysis",),
                    acceptance_criteria=("Return one report",),
                ),
            ),
            final_task_id="report",
        )
        delegation = ManagerDelegation.from_proposal(assignment, proposal)

        delegation.verify(proposal)
        self.assertFalse(delegation.authority_granted)
        self.assertEqual(delegation.manager_employee_id, self.manager.employee_id)
        self.assertEqual(tuple(item.task_id for item in delegation.tasks), ("research", "report"))
        research, report = delegation.tasks
        self.assertEqual(research.context_lane.value, "WORK_ORDER_BRIEF")
        self.assertEqual(research.dependency_artifact_ids, ())
        self.assertEqual(research.deliverable_kind, "SPECIALIST_ARTIFACT")
        self.assertIn("task-acceptance-v1", research.validator_ids)
        self.assertEqual(report.context_lane.value, "DEPENDENCY_ARTIFACTS")
        self.assertEqual(report.dependency_artifact_ids, ("research",))
        self.assertEqual(report.deliverable_kind, "USER_REPORT")

    def test_delegation_freezes_bounded_task_contract_not_employee_chat(self) -> None:
        manager = PersistentExecutiveManager.from_roster(
            (self.manager, self.specialist),
            roster_revision=3,
        )
        assignment = manager.initial_assignment(_order("review the repository"), session_key="s")
        proposal = PlanProposal(
            proposal_id="proposal-bounded-handoff",
            goal="review the repository",
            tasks=(
                JobTask(
                    "inspect",
                    "Inspect the implementation and produce cited findings.",
                    (),
                    ("analysis",),
                    ("Name evidence.",),
                ),
                JobTask(
                    "review",
                    "Validate the findings and identify unresolved conflicts.",
                    ("inspect",),
                    ("review",),
                    ("State disagreements.",),
                ),
                JobTask(
                    "report",
                    "Integrate the bounded artifacts for the user.",
                    ("review",),
                    ("Preserve uncertainty.",),
                    ("Return one report.",),
                ),
            ),
            final_task_id="report",
        )
        delegation = ManagerDelegation.from_proposal(assignment, proposal)

        self.assertEqual(delegation.tasks[0].context_lane.value, "WORK_ORDER_BRIEF")
        self.assertEqual(delegation.tasks[1].context_lane.value, "DEPENDENCY_ARTIFACTS")
        self.assertEqual(delegation.tasks[1].dependency_artifact_ids, ("inspect",))
        self.assertIn("independent-review-v1", delegation.tasks[1].validator_ids)
        self.assertEqual(delegation.tasks[2].deliverable_kind, "USER_REPORT")
        self.assertEqual(delegation.tasks[2].objective, "Integrate the bounded artifacts for the user.")

    def test_manager_records_performance_replica_proposal_without_authority(self) -> None:
        manager = PersistentExecutiveManager.from_roster(
            (self.manager, self.specialist),
            roster_revision=3,
        )
        order = _order("여러 후보안을 만들고 비교해서 가장 좋은 안을 골라줘")

        assignment = manager.initial_assignment(order, session_key="session-replica")

        self.assertEqual(assignment.mode, ManagerAssignmentMode.DELEGATE)
        self.assertEqual(
            assignment.reason,
            "MANAGER_PERFORMANCE_REPLICA_PROPOSAL",
        )
        self.assertFalse(assignment.authority_granted)
        manager.validate_assignment(assignment, order)

    def test_existing_roster_without_manager_stays_explicitly_pre_m2(self) -> None:
        self.assertIsNone(
            PersistentExecutiveManager.optional_from_roster(
                (self.specialist,),
                roster_revision=3,
            )
        )

    def test_manager_direct_selection_preserves_frozen_tool_authority(self) -> None:
        employee, reason = DirectCompanyExecutor._select_employee(
            JobTask(
                task_id="final",
                objective="hello",
                depends_on=(),
                required_capabilities=(),
                acceptance_criteria=(),
            ),
            (self.manager, self.specialist),
            manager_employee_id=self.manager.employee_id,
        )
        self.assertEqual(employee.employee_id, self.manager.employee_id)
        self.assertEqual(reason, "PERSISTENT_MANAGER_DIRECT")

        policy = ActionPolicy(
            tool_grants=(
                ToolGrant("read", (ToolEffect.READ,)),
                ToolGrant("write", (ToolEffect.WRITE,), requires_approval=True),
                ToolGrant("run", (ToolEffect.EXECUTE,), requires_approval=True),
            ),
            filesystem_policy="WORKSPACE_WRITE",
            sandbox_profile="host-workspace-approved",
        )
        projected = DirectCompanyExecutor._action_policy_for_employee(
            policy,
            employee_id=self.manager.employee_id,
            manager_employee_id=self.manager.employee_id,
        )
        self.assertEqual(projected, policy)

        manager_policy = ActionPolicy(
            tool_grants=(
                *policy.tool_grants,
                ToolGrant("manager_inspect_company", (ToolEffect.READ,)),
            ),
            filesystem_policy=policy.filesystem_policy,
            sandbox_profile=policy.sandbox_profile,
        )
        specialist_projection = DirectCompanyExecutor._action_policy_for_employee(
            manager_policy,
            employee_id=self.specialist.employee_id,
            manager_employee_id=self.manager.employee_id,
        )
        self.assertNotIn(
            "manager_inspect_company",
            tuple(grant.tool_name for grant in specialist_projection.tool_grants),
        )


if __name__ == "__main__":
    unittest.main()
