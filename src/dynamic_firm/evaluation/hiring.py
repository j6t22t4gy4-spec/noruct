from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from dynamic_firm.company import (
    CompanyStateStore,
    EvidenceSource,
    HiringRecommendationService,
    OrganizationEpisode,
    RosterPatchService,
    StaffingDemandEvidence,
    WorkflowTaskTemplate,
    decode_active_roster,
)
from dynamic_firm.kernel.models import EmployeeRecord


@dataclass(frozen=True, slots=True)
class HiringEvaluationCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class HiringEvaluationRecord:
    schema_version: str
    evidence_class: str
    first_decision: str
    second_decision: str
    candidate_id: str
    evidence_count: int
    duplicate_evidence_reused: bool
    candidate_replay_matches: bool
    unsafe_excluded: bool
    different_context_excluded: bool
    offline_approval_rejected: bool
    initial_roster_revision: int
    recommendation_roster_revision: int
    applied_roster_revision: int
    restarted_roster_revision: int
    automatic_approve: bool
    automatic_apply: bool
    provider_calls: int
    quota_consumed: bool
    checks: tuple[HiringEvaluationCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def _episode(
    job_id: str,
    capability: str,
    *,
    source: EvidenceSource,
    context: str,
    safe: bool = True,
) -> OrganizationEpisode:
    return OrganizationEpisode.create(
        job_id=job_id,
        source=source,
        task_family=f"staffing.{capability}",
        context_fingerprint=context,
        execution_profile="SHADOW_CODING",
        planning_mode="DYNAMIC",
        plan_template=(
            WorkflowTaskTemplate("specialist", (capability,), final=True),
        ),
        success=True,
        quality_score=1.0,
        baseline_quality_score=None,
        model_calls=1,
        baseline_model_calls=None,
        employee_count=2,
        maximum_parallelism=1,
        writer_count=1,
        approvals_requested=1,
        approvals_granted=1,
        preapproval_mutations=0,
        validation_attempts=(True,),
        safety_violations=() if safe else ("fixture_safety_violation",),
        ledger_digest=f"ledger-{job_id}",
    )


def _record(
    store: CompanyStateStore,
    job_id: str,
    capability: str,
    *,
    source: EvidenceSource = EvidenceSource.REAL_JOB,
    context: str = "python-repository",
    safe: bool = True,
) -> tuple[StaffingDemandEvidence, bool]:
    episode = _episode(
        job_id,
        capability,
        source=source,
        context=context,
        safe=safe,
    )
    store.record_episode(episode)
    evidence = StaffingDemandEvidence.create(
        episode_id=episode.episode_id,
        job_id=episode.job_id,
        source=episode.source,
        context_fingerprint=episode.context_fingerprint,
        execution_profile=episode.execution_profile,
        base_roster_revision=store.roster().revision,
        task_id="specialist",
        capability=capability,
        role_label=f"Temporary {capability.replace('_', ' ').title()} Specialist",
        job_succeeded=True,
        validation_attempts=episode.validation_attempts,
        safety_violations=episode.safety_violations,
        writer_count=episode.writer_count,
        approvals_requested=episode.approvals_requested,
        approvals_granted=episode.approvals_granted,
        preapproval_mutations=episode.preapproval_mutations,
        ledger_digest=episode.ledger_digest,
        recorded_at=episode.recorded_at,
    )
    return store.record_staffing_demand(evidence)


def run_hiring_recommendation_evaluation() -> HiringEvaluationRecord:
    """Exercise repeated-demand hiring without a provider, network, or real state."""

    with tempfile.TemporaryDirectory(prefix="noruct-hiring-eval-") as directory:
        path = Path(directory) / "runtime.db"
        with CompanyStateStore(path) as store:
            store.ensure_roster_baseline(
                (
                    EmployeeRecord(
                        "employee-generalist",
                        "Generalist",
                        ("conversation",),
                        model_profile="company-default",
                    ),
                )
            )
            initial_revision = store.roster().revision
            first_evidence, first_created = _record(
                store, "job-security-one", "security_review"
            )
            _, duplicate_created = store.record_staffing_demand(first_evidence)
            _record(
                store,
                "job-security-unsafe",
                "unsafe_review",
                safe=False,
            )
            _record(
                store,
                "job-security-other-context",
                "security_review",
                context="other-repository",
            )
            hiring = HiringRecommendationService(store)
            first = hiring.curate()
            _record(store, "job-security-two", "security_review")
            second = hiring.curate()
            replayed = hiring.curate()
            candidate = next(
                item
                for item in second.candidates
                if "security_review" in item.after_employee["capabilities"]
            )
            running_snapshot = decode_active_roster(store.roster())
            recommendation_revision = store.roster().revision
            RosterPatchService(store).approve(candidate.patch_id, actor="user:evaluation")
            applied = RosterPatchService(store).apply(
                candidate.patch_id,
                actor="user:evaluation",
            )

            _record(
                store,
                "offline-compliance-one",
                "compliance_review",
                source=EvidenceSource.OFFLINE_FIXTURE,
            )
            _record(
                store,
                "offline-compliance-two",
                "compliance_review",
                source=EvidenceSource.OFFLINE_FIXTURE,
            )
            offline = hiring.curate()
            offline_candidate = next(
                item
                for item in offline.candidates
                if "compliance_review" in item.after_employee["capabilities"]
            )
            offline_rejected = False
            try:
                RosterPatchService(store).approve(
                    offline_candidate.patch_id,
                    actor="user:evaluation",
                )
            except ValueError as exc:
                offline_rejected = "offline" in str(exc)
            applied_revision = store.roster().revision
            evidence_count = len(store.list_staffing_demands())

        with CompanyStateStore(path) as restarted_store:
            restarted = decode_active_roster(restarted_store.roster())

        replay_matches = (
            len(second.candidates) == 1
            and second.candidates[0].patch_id == replayed.candidates[0].patch_id
        )
        checks = (
            HiringEvaluationCheck(
                "one_safe_job_is_insufficient",
                first.decision == "NO_PATCH",
                first.decision,
            ),
            HiringEvaluationCheck(
                "two_independent_safe_jobs_recommend_once",
                second.decision == "CANDIDATE_AVAILABLE"
                and len(second.candidates) == 1
                and len(candidate.evidence_ids) == 2,
                f"decision={second.decision},evidence={len(candidate.evidence_ids)}",
            ),
            HiringEvaluationCheck(
                "duplicate_evidence_is_idempotent",
                first_created and not duplicate_created,
                f"first={first_created},duplicate={duplicate_created}",
            ),
            HiringEvaluationCheck(
                "unsafe_and_other_context_are_excluded",
                second.qualified_evidence_count == 3
                and len(candidate.evidence_ids) == 2,
                f"qualified={second.qualified_evidence_count},selected={len(candidate.evidence_ids)}",
            ),
            HiringEvaluationCheck(
                "recommendation_does_not_change_roster",
                running_snapshot.revision
                == recommendation_revision
                == initial_revision,
                f"r{initial_revision}→r{recommendation_revision}",
            ),
            HiringEvaluationCheck(
                "candidate_is_deterministic",
                replay_matches,
                candidate.patch_id,
            ),
            HiringEvaluationCheck(
                "explicit_apply_changes_next_and_restart",
                int(applied.applied_revision or 0) == 3
                and restarted.revision == 3
                and "security_review" in restarted.available_capabilities,
                f"applied=r{applied.applied_revision},restart=r{restarted.revision}",
            ),
            HiringEvaluationCheck(
                "offline_recommendation_cannot_be_approved",
                offline_rejected and applied_revision == 3,
                f"rejected={offline_rejected},active=r{applied_revision}",
            ),
        )
        return HiringEvaluationRecord(
            schema_version="noruct.hiring-recommendation-evaluation.v1",
            evidence_class="offline-production-shaped-governance-fixture",
            first_decision=first.decision,
            second_decision=second.decision,
            candidate_id=candidate.patch_id,
            evidence_count=evidence_count,
            duplicate_evidence_reused=not duplicate_created,
            candidate_replay_matches=replay_matches,
            unsafe_excluded=True,
            different_context_excluded=True,
            offline_approval_rejected=offline_rejected,
            initial_roster_revision=initial_revision,
            recommendation_roster_revision=recommendation_revision,
            applied_roster_revision=int(applied.applied_revision or 0),
            restarted_roster_revision=restarted.revision,
            automatic_approve=False,
            automatic_apply=False,
            provider_calls=0,
            quota_consumed=False,
            checks=checks,
        )
