from __future__ import annotations

import unittest

from dynamic_firm.company import (
    EvidenceSource,
    OrganizationEpisode,
    WorkflowTaskTemplate,
    assess_manager_outcomes,
)


def episode(
    job_id: str,
    *,
    quality: float,
    baseline: float | None,
    calls: int = 3,
    baseline_calls: int | None = 4,
    success: bool = True,
    safe: bool = True,
) -> OrganizationEpisode:
    return OrganizationEpisode.create(
        job_id=job_id,
        source=EvidenceSource.REAL_JOB,
        task_family="company.manager-evaluation",
        context_fingerprint="manager-context",
        execution_profile="READ_ONLY",
        planning_mode="DYNAMIC",
        plan_template=(
            WorkflowTaskTemplate("specialist", ("analysis",)),
            WorkflowTaskTemplate("final", ("integration",), ("specialist",), True),
        ),
        success=success,
        quality_score=quality,
        baseline_quality_score=baseline,
        model_calls=calls,
        baseline_model_calls=baseline_calls,
        employee_count=2,
        maximum_parallelism=1,
        writer_count=1,
        approvals_requested=0,
        approvals_granted=0,
        preapproval_mutations=0,
        validation_attempts=(safe,),
        safety_violations=() if safe else ("validation_failed",),
        ledger_digest=("a" if job_id == "one" else "b") * 64,
        manager_employee_id="manager",
        manager_assignment_digest="c" * 64,
        manager_delegation_digest="d" * 64,
        manager_supervision_count=1,
        temporary_role_count=1,
        graph_patch_count=1,
    )


class ManagerOutcomeTests(unittest.TestCase):
    def test_reproducible_safe_gain_is_kept_under_observation(self) -> None:
        results = assess_manager_outcomes(
            (
                episode("one", quality=1.0, baseline=0.8),
                episode("two", quality=0.95, baseline=0.8),
            )
        )

        self.assertEqual(len(results), 1)
        assessment = results[0]
        self.assertEqual(assessment.decision, "KEEP_UNDER_OBSERVATION")
        self.assertEqual(assessment.specialist_job_count, 2)
        self.assertEqual(assessment.replan_job_count, 2)
        self.assertEqual(assessment.supervised_job_count, 2)
        self.assertFalse(assessment.promotion_allowed)

    def test_negative_transfer_is_not_averaged_away(self) -> None:
        result = assess_manager_outcomes(
            (
                episode("one", quality=1.0, baseline=0.8),
                episode("two", quality=0.6, baseline=0.8),
            )
        )[0]

        self.assertEqual(result.decision, "REVIEW_REQUIRED")
        self.assertEqual(result.negative_transfer_count, 1)
        self.assertIn("negative_quality_transfer_observed", result.reasons)

    def test_missing_baseline_stays_insufficient(self) -> None:
        result = assess_manager_outcomes(
            (episode("one", quality=1.0, baseline=None),)
        )[0]

        self.assertEqual(result.decision, "INSUFFICIENT_EVIDENCE")
        self.assertIn("insufficient_same_budget_baselines", result.reasons)

