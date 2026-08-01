from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from dynamic_firm.company import (
    AuthoritySnapshotIdentity,
    CompanyOperatingDecision,
    CompanyWorkMode,
    FirmAdmissionController,
    FirmAdmissionStatus,
    GraphUserConstraints,
    InitialCoordinationPolicy,
    OperatingReason,
    RequestedEffect,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
)
from dynamic_firm.kernel.models import (
    EmployeeRecord,
    JobLimits,
    JobTask,
    PlanProposal,
)


def order(mode: CompanyWorkMode = CompanyWorkMode.TEAM_JOB):
    coordination = (
        InitialCoordinationPolicy.DIRECT
        if mode is CompanyWorkMode.DIRECT
        else InitialCoordinationPolicy.PLAN_FIRST
        if mode is CompanyWorkMode.TEAM_JOB
        else InitialCoordinationPolicy.SOLO_FIRST
    )
    return normalize_work_order(
        "Implement and verify the bounded change.",
        work_order_id="work-order-firm-admission",
        authority_snapshot=AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=1,
            roster_revision=2,
            playbook_revision=3,
            action_policy_digest="a" * 64,
        ),
        budget_snapshot=WorkOrderBudgetSnapshot(
            max_model_calls=8,
            max_tool_calls=16,
            max_cost_usd=2.0,
            max_wall_time_ms=30_000,
        ),
        requested_at=datetime(2026, 7, 26, tzinfo=UTC),
        operating_decision=CompanyOperatingDecision(
            work_mode=mode,
            coordination_policy=coordination,
            requested_effect=RequestedEffect.WORKSPACE_CHANGE,
            reason=OperatingReason.STRUCTURED_MULTI_WORKSTREAM,
        ),
    )


def task(task_id: str, *, dependencies=(), capabilities=("engineering",)):
    return JobTask(
        task_id=task_id,
        objective=task_id,
        depends_on=tuple(dependencies),
        required_capabilities=tuple(capabilities),
        acceptance_criteria=("done",),
    )


class FirmAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = (
            EmployeeRecord(
                employee_id="engineer",
                role="Engineer",
                capabilities=("engineering", "general_reasoning"),
            ),
            EmployeeRecord(
                employee_id="reviewer",
                role="Reviewer",
                capabilities=("review", "general_reasoning"),
            ),
        )

    def test_freezes_capability_supply_and_dependency_width(self) -> None:
        proposal = PlanProposal(
            proposal_id="plan-parallel",
            goal="implement",
            tasks=(
                task("implementation"),
                task("review", capabilities=("review",)),
                task(
                    "final",
                    dependencies=("implementation", "review"),
                    capabilities=("general_reasoning",),
                ),
            ),
            final_task_id="final",
        )

        admission = FirmAdmissionController().admit(
            work_order=order(),
            proposal=proposal,
            roster=self.roster,
            limits=JobLimits(max_concurrency=4),
        )

        self.assertTrue(admission.admitted)
        self.assertEqual(admission.status, FirmAdmissionStatus.ADMITTED)
        self.assertEqual(admission.effective_work_mode, "TEAM_JOB")
        self.assertEqual(admission.dependency_width, 2)
        self.assertEqual(admission.concurrency_ceiling, 2)
        self.assertEqual(admission.temporary_role_demand, 0)
        self.assertEqual(admission.uncovered_task_ids, ())
        self.assertEqual(len(admission.content_digest), 64)
        admission.verify()

    def test_deduplicates_missing_capability_into_one_temporary_role(self) -> None:
        proposal = PlanProposal(
            proposal_id="plan-specialist",
            goal="specialist",
            tasks=(
                task("probe-a", capabilities=("forensics",)),
                task("probe-b", capabilities=("forensics",)),
                task(
                    "final",
                    dependencies=("probe-a", "probe-b"),
                    capabilities=("engineering",),
                ),
            ),
            final_task_id="final",
        )

        admission = FirmAdmissionController().admit(
            work_order=order(),
            proposal=proposal,
            roster=self.roster,
            limits=JobLimits(max_temporary_roles=1),
        )

        self.assertTrue(admission.admitted)
        self.assertEqual(admission.temporary_role_demand, 1)
        self.assertEqual(admission.missing_capability_bundles, (("forensics",),))
        self.assertEqual(admission.uncovered_task_ids, ("probe-a", "probe-b"))

    def test_reclassifies_a_multi_task_clone_graph_as_solo_before_dispatch(self) -> None:
        proposal = PlanProposal(
            proposal_id="plan-roleplay-clone",
            goal="same worker twice",
            tasks=(
                task("inspect", capabilities=("engineering",)),
                task(
                    "implement",
                    dependencies=("inspect",),
                    capabilities=("engineering",),
                ),
            ),
            final_task_id="implement",
        )

        admission = FirmAdmissionController().admit(
            work_order=order(),
            proposal=proposal,
            roster=self.roster,
            limits=JobLimits(max_concurrency=2),
        )

        self.assertTrue(admission.admitted)
        self.assertEqual(admission.effective_work_mode, "SOLO_JOB")
        self.assertEqual(admission.distinct_staffing_profile_count, 1)
        self.assertEqual(admission.staffing_difference_dimensions, ())
        self.assertEqual(
            tuple(item.staffing_profile_origin for item in admission.staffing),
            ("PERSISTENT", "PERSISTENT"),
        )

    def test_team_label_requires_static_capability_or_model_difference(self) -> None:
        proposal = PlanProposal(
            proposal_id="plan-heterogeneous",
            goal="specialist evidence then implementation",
            tasks=(
                task("review", capabilities=("review",)),
                task(
                    "implement",
                    dependencies=("review",),
                    capabilities=("engineering",),
                ),
            ),
            final_task_id="implement",
        )

        admission = FirmAdmissionController().admit(
            work_order=order(),
            proposal=proposal,
            roster=self.roster,
            limits=JobLimits(max_concurrency=2),
        )

        self.assertEqual(admission.effective_work_mode, "TEAM_JOB")
        self.assertEqual(admission.distinct_staffing_profile_count, 2)
        self.assertEqual(admission.staffing_difference_dimensions, ("capability_ids",))

    def test_records_capability_candidates_and_task_relevance_without_claiming_dispatch_profile(self) -> None:
        proposal = PlanProposal(
            proposal_id="plan-pre-admission-evidence",
            goal="review then implement",
            tasks=(
                task("review", capabilities=("review",)),
                task("implement", capabilities=("engineering",)),
            ),
            final_task_id="implement",
        )

        admission = FirmAdmissionController().admit(
            work_order=order(),
            proposal=proposal,
            roster=self.roster,
            limits=JobLimits(max_concurrency=2),
            constraints=GraphUserConstraints(pinned_employee_ids=("reviewer",)),
        )

        by_task = {item.task_id: item for item in admission.staffing}
        review = by_task["review"]
        implement = by_task["implement"]
        self.assertEqual(review.candidate_employee_ids, ("reviewer",))
        self.assertEqual(review.selection_reason, "PINNED_CAPABILITY_MATCH")
        self.assertEqual(review.task_relevance, ("REQUIRED_CAPABILITY_COVERAGE",))
        self.assertEqual(implement.candidate_employee_ids, ("engineer",))
        self.assertEqual(implement.selection_reason, "MINIMUM_CAPABILITY_SUPERSET")
        self.assertEqual(implement.task_relevance, ("REQUIRED_CAPABILITY_COVERAGE",))
        admission.verify()

    def test_records_capability_gap_without_fabricating_a_persistent_candidate(self) -> None:
        proposal = PlanProposal(
            proposal_id="plan-pre-admission-gap",
            goal="specialist evidence",
            tasks=(task("forensics", capabilities=("forensics",)),),
            final_task_id="forensics",
        )

        admission = FirmAdmissionController().admit(
            work_order=order(),
            proposal=proposal,
            roster=self.roster,
            limits=JobLimits(max_temporary_roles=1),
        )

        staffing = admission.staffing[0]
        self.assertEqual(staffing.candidate_employee_ids, ())
        self.assertEqual(staffing.selection_reason, "TEMPORARY_CAPABILITY_GAP")
        self.assertEqual(staffing.task_relevance, ("REQUIRED_CAPABILITY_GAP",))
        admission.verify()

    def test_denies_plan_that_exceeds_temporary_role_limit(self) -> None:
        proposal = PlanProposal(
            proposal_id="plan-too-many-gaps",
            goal="specialists",
            tasks=(
                task("a", capabilities=("forensics",)),
                task("b", capabilities=("legal",)),
            ),
            final_task_id="b",
        )

        admission = FirmAdmissionController().admit(
            work_order=order(),
            proposal=proposal,
            roster=self.roster,
            limits=JobLimits(max_temporary_roles=1),
        )

        self.assertFalse(admission.admitted)
        self.assertEqual(admission.reason, "TEMPORARY_ROLE_LIMIT_EXCEEDED")

    def test_denies_task_count_before_building_a_runtime_graph(self) -> None:
        proposal = PlanProposal(
            proposal_id="plan-task-limit",
            goal="too many tasks",
            tasks=(task("a"), task("b")),
            final_task_id="b",
        )
        admission = FirmAdmissionController().admit(
            work_order=order(),
            proposal=proposal,
            roster=self.roster,
            limits=JobLimits(max_tasks=1),
        )
        self.assertFalse(admission.admitted)
        self.assertEqual(admission.reason, "TASK_LIMIT_EXCEEDED")

    def test_direct_mode_never_creates_a_temporary_role(self) -> None:
        proposal = PlanProposal(
            proposal_id="plan-direct",
            goal="answer",
            tasks=(task("answer", capabilities=("unknown-specialty",)),),
            final_task_id="answer",
        )

        admission = FirmAdmissionController().admit(
            work_order=order(CompanyWorkMode.DIRECT),
            proposal=proposal,
            roster=self.roster,
            limits=JobLimits(max_temporary_roles=1),
        )

        self.assertTrue(admission.admitted)
        self.assertEqual(admission.effective_work_mode, "DIRECT")
        self.assertEqual(admission.temporary_role_demand, 0)
        self.assertEqual(admission.uncovered_task_ids, ("answer",))

    def test_rejects_cycles_and_detects_digest_tampering(self) -> None:
        cycle = PlanProposal(
            proposal_id="plan-cycle",
            goal="cycle",
            tasks=(
                task("a", dependencies=("b",)),
                task("b", dependencies=("a",)),
            ),
            final_task_id="b",
        )
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            FirmAdmissionController().admit(
                work_order=order(),
                proposal=cycle,
                roster=self.roster,
                limits=JobLimits(),
            )

        valid = PlanProposal(
            proposal_id="plan-valid",
            goal="valid",
            tasks=(task("a"),),
            final_task_id="a",
        )
        admission = FirmAdmissionController().admit(
            work_order=order(CompanyWorkMode.SOLO_JOB),
            proposal=valid,
            roster=self.roster,
            limits=JobLimits(),
        )
        with self.assertRaisesRegex(ValueError, "digest is invalid"):
            replace(admission, reason="tampered").verify()


if __name__ == "__main__":
    unittest.main()
