from __future__ import annotations

import unittest

from dynamic_firm.company import (
    CompanyWorkMode,
    EvidenceSource,
    OrganizationEpisode,
    OrganizationEvidenceDecision,
    WorkflowTaskTemplate,
    apply_organization_evidence_gate,
    assess_organization_outcomes,
    classify_company_input,
)


def episode(
    job_id: str,
    *,
    quality: float = 1.0,
    baseline: float | None = 0.8,
    safe: bool = True,
    replica_count: int = 0,
) -> OrganizationEpisode:
    return OrganizationEpisode.create(
        job_id=job_id,
        source=EvidenceSource.REAL_JOB,
        task_family="company.organization-evaluation",
        context_fingerprint="same-workspace-context",
        execution_profile="READ_ONLY",
        planning_mode="DYNAMIC",
        plan_template=(
            WorkflowTaskTemplate("analysis", ("analysis",)),
            WorkflowTaskTemplate("final", ("implementation",), ("analysis",), True),
        ),
        success=safe,
        quality_score=quality,
        baseline_quality_score=baseline,
        model_calls=3,
        baseline_model_calls=4,
        employee_count=2,
        maximum_parallelism=2,
        writer_count=1,
        approvals_requested=0,
        approvals_granted=0,
        preapproval_mutations=0,
        validation_attempts=(safe,),
        safety_violations=() if safe else ("validation_failed",),
        ledger_digest=("a" if job_id == "one" else "b") * 64,
        execution_replica_count=replica_count,
        replica_group_count=1 if replica_count else 0,
    )


class OrganizationOutcomeTests(unittest.TestCase):
    def test_missing_context_evidence_collapses_automatic_team_to_solo(self) -> None:
        candidate = classify_company_input(
            "이 저장소의 모든 파일을 분석하고 결과를 종합해줘"
        )
        assessment = assess_organization_outcomes(
            (), context_fingerprint="same-workspace-context"
        )

        admitted = apply_organization_evidence_gate(candidate, assessment)

        self.assertEqual(
            assessment.decision,
            OrganizationEvidenceDecision.INSUFFICIENT_EVIDENCE,
        )
        self.assertEqual(admitted.work_mode, CompanyWorkMode.SOLO_JOB)
        self.assertEqual(admitted.execution_replica_preference.value, "DISABLED")

    def test_reproducible_safe_heterogeneous_gain_admits_team_not_replica(self) -> None:
        assessment = assess_organization_outcomes(
            (episode("one"), episode("two", quality=0.95)),
            context_fingerprint="same-workspace-context",
        )

        self.assertEqual(
            assessment.decision, OrganizationEvidenceDecision.TEAM_ELIGIBLE
        )
        candidate = classify_company_input(
            "이 저장소의 모든 파일을 분석하고 결과를 종합해줘"
        )
        admitted = apply_organization_evidence_gate(candidate, assessment)
        self.assertEqual(admitted.work_mode, CompanyWorkMode.TEAM_JOB)
        self.assertEqual(admitted.execution_replica_preference.value, "DISABLED")

    def test_reproducible_replica_evidence_admits_replica(self) -> None:
        assessment = assess_organization_outcomes(
            (
                episode("one", replica_count=2),
                episode("two", quality=0.95, replica_count=2),
            ),
            context_fingerprint="same-workspace-context",
        )
        self.assertEqual(
            assessment.decision, OrganizationEvidenceDecision.REPLICA_ELIGIBLE
        )

    def test_unsafe_or_negative_team_never_becomes_automatic_default(self) -> None:
        assessment = assess_organization_outcomes(
            (
                episode("one"),
                episode("two", quality=0.6, safe=False),
            ),
            context_fingerprint="same-workspace-context",
        )
        self.assertEqual(assessment.decision, OrganizationEvidenceDecision.SOLO_REQUIRED)
        self.assertIn("unsafe_or_failed_organization_episode", assessment.reasons)

    def test_explicit_independent_review_is_not_rewritten_as_automatic_policy(self) -> None:
        candidate = classify_company_input("독립 검토를 붙여서 이 변경을 검증해줘")
        assessment = assess_organization_outcomes(
            (), context_fingerprint="same-workspace-context"
        )

        admitted = apply_organization_evidence_gate(candidate, assessment)

        self.assertTrue(admitted.requires_independent_review)
        self.assertEqual(admitted.work_mode, candidate.work_mode)
