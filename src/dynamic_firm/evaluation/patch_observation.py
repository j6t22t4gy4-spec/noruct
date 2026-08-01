from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    EvidenceSource,
    OrganizationEpisode,
    WorkflowPatchAssessmentDecision,
    WorkflowPatchStatus,
    WorkflowTaskTemplate,
)
from dynamic_firm.compiler import CompilerExecutionProfile


@dataclass(frozen=True, slots=True)
class PatchObservationCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class PatchObservationEvaluationRecord:
    schema_version: str
    noruct_version: str
    evidence_class: str
    external_model_calls: int
    candidate_id: str
    unaligned_cohort_eligible: bool
    two_observation_decision: str
    three_observation_decision: str
    safety_decision: str
    final_patch_status: str
    final_playbook_revision: int
    automatic_rollback: bool
    checks: tuple[PatchObservationCheck, ...]
    passed: bool


def _episode(
    job_id: str,
    *,
    quality: float = 1.0,
    baseline_quality: float | None = 0.7,
    safety_violations: tuple[str, ...] = (),
) -> OrganizationEpisode:
    return OrganizationEpisode.create(
        job_id=job_id,
        source=EvidenceSource.REAL_JOB,
        task_family="evaluation.patch-observation",
        context_fingerprint="offline-contract-workspace",
        execution_profile=CompilerExecutionProfile.SHADOW_CODING.value,
        planning_mode="DYNAMIC",
        plan_template=(
            WorkflowTaskTemplate("inspect", ("repository_analysis",)),
            WorkflowTaskTemplate(
                "implement_change",
                ("implementation",),
                depends_on=("inspect",),
                final=True,
            ),
        ),
        success=True,
        quality_score=quality,
        baseline_quality_score=baseline_quality,
        model_calls=2,
        baseline_model_calls=None if baseline_quality is None else 3,
        employee_count=2,
        maximum_parallelism=1,
        writer_count=1,
        approvals_requested=1,
        approvals_granted=1,
        preapproval_mutations=0,
        validation_attempts=(True,),
        safety_violations=safety_violations,
        ledger_digest=f"offline-contract-{job_id}",
    )


def run_patch_observation_evaluation() -> PatchObservationEvaluationRecord:
    """Exercise the deterministic contract only; no provider, credentials, or quota."""

    with tempfile.TemporaryDirectory(prefix="noruct-patch-observation-") as directory:
        with CompanyStateStore(Path(directory) / "company.db") as store:
            learning = CompanyLearningService(store)
            for job_id in ("seed-one", "seed-two"):
                store.record_episode(_episode(job_id))
            candidate = learning.curate().candidates[0]
            learning.approve(candidate.patch_id, actor="evaluation:fixture")
            learning.apply(candidate.patch_id, actor="evaluation:fixture")

            unaligned, _ = store.record_episode(_episode("unaligned"))
            unaligned_observation = learning.observe(
                candidate.patch_id,
                unaligned,
                prior_exposed=True,
                proposal_aligned=False,
            )

            for job_id in ("aligned-one", "aligned-two"):
                observed, _ = store.record_episode(_episode(job_id))
                learning.observe(
                    candidate.patch_id,
                    observed,
                    prior_exposed=True,
                    proposal_aligned=True,
                )
            two_observation = learning.assess(candidate.patch_id)

            third, _ = store.record_episode(_episode("aligned-three"))
            learning.observe(
                candidate.patch_id,
                third,
                prior_exposed=True,
                proposal_aligned=True,
            )
            three_observation = learning.assess(candidate.patch_id)

            unsafe, _ = store.record_episode(
                _episode(
                    "unsafe",
                    baseline_quality=None,
                    safety_violations=("unexpected_mutation",),
                )
            )
            learning.observe(
                candidate.patch_id,
                unsafe,
                prior_exposed=True,
                proposal_aligned=True,
            )
            safety_assessment = learning.assess(candidate.patch_id)
            final_patch = store.get_patch(candidate.patch_id)
            final_playbook = store.playbook()

            checks = (
                PatchObservationCheck(
                    "exposure_is_not_alignment",
                    not unaligned_observation.cohort_eligible,
                    "exposed prior with a different validated proposal is excluded",
                ),
                PatchObservationCheck(
                    "minimum_cohort_is_bounded",
                    two_observation.decision
                    == WorkflowPatchAssessmentDecision.INSUFFICIENT_OBSERVATION,
                    "two exact measured observations remain insufficient",
                ),
                PatchObservationCheck(
                    "effect_can_be_reproduced",
                    three_observation.decision == WorkflowPatchAssessmentDecision.KEEP,
                    "three exact measured observations reproduce the contract effect",
                ),
                PatchObservationCheck(
                    "safety_is_fail_fast",
                    safety_assessment.decision
                    == WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE,
                    "an attributed safety violation produces a recommendation",
                ),
                PatchObservationCheck(
                    "recommendation_is_not_rollback",
                    final_patch.status == WorkflowPatchStatus.APPLIED
                    and final_playbook.revision == 2,
                    "patch remains applied and PLAYBOOK is unchanged",
                ),
            )
            return PatchObservationEvaluationRecord(
                schema_version="noruct.patch-observation-evaluation.v1",
                noruct_version=__version__,
                evidence_class="offline-contract-only",
                external_model_calls=0,
                candidate_id=candidate.patch_id,
                unaligned_cohort_eligible=unaligned_observation.cohort_eligible,
                two_observation_decision=two_observation.decision.value,
                three_observation_decision=three_observation.decision.value,
                safety_decision=safety_assessment.decision.value,
                final_patch_status=final_patch.status.value,
                final_playbook_revision=final_playbook.revision,
                automatic_rollback=False,
                checks=checks,
                passed=all(item.passed for item in checks),
            )
