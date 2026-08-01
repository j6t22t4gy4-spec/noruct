from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from dynamic_firm.company import (
    CompanyStateStore,
    HireObservationService,
    HiringRecommendationService,
    RetentionReviewMode,
    RosterPatchService,
    RosterRetentionService,
    decode_active_roster,
)
from dynamic_firm.evaluation.hire_observation import _observe, _record_demand
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.runtime.store import RunStore


@dataclass(frozen=True, slots=True)
class RetentionReviewEvaluationCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class RetentionReviewEvaluationRecord:
    schema_version: str
    evidence_class: str
    manual_decision: str
    auto_review_decision: str
    auto_review_safety_decision: str
    always_approve_decision: str
    running_snapshot_revision: int
    next_snapshot_revision: int
    stale_apply_rejected: bool
    provider_calls: int
    quota_consumed: bool
    checks: tuple[RetentionReviewEvaluationCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def _prepare(path: Path, *, safety_failure: bool):
    store = CompanyStateStore(path)
    runtime = RunStore(path)
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
    _record_demand(store, f"{path.stem}-demand-one")
    _record_demand(store, f"{path.stem}-demand-two")
    hire = HiringRecommendationService(store).curate().candidates[0]
    patches = RosterPatchService(store)
    patches.approve(hire.patch_id, actor="user:evaluation")
    patches.apply(hire.patch_id, actor="user:evaluation")
    contract = store.get_hire_observation_contract(hire.patch_id)
    if safety_failure:
        _observe(
            store,
            runtime,
            patch_id=hire.patch_id,
            employee_id=contract.employee_id,
            job_id=f"{path.stem}-safety",
            validation=(),
            violations=("no_validation_evidence",),
        )
    else:
        for index in range(contract.maximum_observations):
            _observe(
                store,
                runtime,
                patch_id=hire.patch_id,
                employee_id=f"temporary-{path.stem}-{index}",
                job_id=f"{path.stem}-fallback-{index}",
                temporary=True,
            )
    assessment = HireObservationService(store).assess(hire.patch_id)
    return store, runtime, hire, contract, assessment


def run_retention_review_evaluation() -> RetentionReviewEvaluationRecord:
    """Exercise all three retention review modes without a provider or network."""

    with tempfile.TemporaryDirectory(prefix="noruct-retention-review-") as directory:
        root = Path(directory)

        manual_store, manual_runtime, manual_hire, _, manual_assessment = _prepare(
            root / "manual.db",
            safety_failure=False,
        )
        try:
            manual = RosterRetentionService(manual_store).recommend(
                manual_hire.patch_id
            )
            manual_replay = RosterRetentionService(manual_store).recommend(
                manual_hire.patch_id
            )
            manual_revision = manual_store.roster().revision
        finally:
            manual_runtime.close()
            manual_store.close()

        auto_store, auto_runtime, auto_hire, auto_contract, _ = _prepare(
            root / "auto.db",
            safety_failure=False,
        )
        try:
            auto_store.set_retention_review_mode(
                RetentionReviewMode.AUTO_REVIEW,
                actor="user:evaluation",
            )
            running_snapshot = decode_active_roster(auto_store.roster())
            auto = RosterRetentionService(auto_store).recommend(auto_hire.patch_id)
            next_snapshot = decode_active_roster(auto_store.roster())
            auto_employee_in_running = any(
                item.employee_id == auto_contract.employee_id
                for item in running_snapshot.employees
            )
            auto_employee_in_next = any(
                item.employee_id == auto_contract.employee_id
                for item in next_snapshot.employees
            )
        finally:
            auto_runtime.close()
            auto_store.close()
        with CompanyStateStore(root / "auto.db") as restarted:
            restart_mode = restarted.retention_review_mode()
            restart_snapshot = decode_active_roster(restarted.roster())
            auto_employee_in_restart = any(
                item.employee_id == auto_contract.employee_id
                for item in restart_snapshot.employees
            )

        review_store, review_runtime, review_hire, _, _ = _prepare(
            root / "auto-safety.db",
            safety_failure=True,
        )
        try:
            review_store.set_retention_review_mode(
                RetentionReviewMode.AUTO_REVIEW,
                actor="user:evaluation",
            )
            auto_safety = RosterRetentionService(review_store).recommend(
                review_hire.patch_id
            )
            auto_safety_revision = review_store.roster().revision
        finally:
            review_runtime.close()
            review_store.close()

        always_store, always_runtime, always_hire, _, _ = _prepare(
            root / "always.db",
            safety_failure=True,
        )
        try:
            always_store.set_retention_review_mode(
                RetentionReviewMode.ALWAYS_APPROVE,
                actor="user:evaluation",
            )
            always = RosterRetentionService(always_store).recommend(
                always_hire.patch_id
            )
            always_revision = always_store.roster().revision
        finally:
            always_runtime.close()
            always_store.close()

        stale_store, stale_runtime, stale_hire, stale_contract, _ = _prepare(
            root / "stale.db",
            safety_failure=True,
        )
        try:
            stale = RosterRetentionService(stale_store).recommend(stale_hire.patch_id)
            _observe(
                stale_store,
                stale_runtime,
                patch_id=stale_hire.patch_id,
                employee_id=stale_contract.employee_id,
                job_id="stale-new-observation",
            )
            try:
                RosterPatchService(stale_store).approve(
                    stale.patch.patch_id,
                    actor="user:evaluation",
                )
                stale_rejected = False
            except ValueError:
                stale_rejected = True
            stale_revision = stale_store.roster().revision
        finally:
            stale_runtime.close()
            stale_store.close()

    checks = (
        RetentionReviewEvaluationCheck(
            "approval_mode_proposes_without_apply",
            manual.review.decision.value == "PENDING_USER_APPROVAL"
            and not manual.applied
            and manual_revision == 3,
            f"decision={manual.review.decision.value},ROSTER=r{manual_revision}",
        ),
        RetentionReviewEvaluationCheck(
            "manual_replay_is_idempotent",
            manual.patch.patch_id == manual_replay.patch.patch_id
            and manual.review.review_id == manual_replay.review.review_id
            and manual.patch.assessment_ids == (manual_assessment.assessment_id,),
            f"patch={manual.patch.patch_id},review={manual.review.review_id}",
        ),
        RetentionReviewEvaluationCheck(
            "auto_review_applies_full_window_underuse",
            auto.review.decision.value == "AUTO_APPROVED"
            and auto.applied
            and auto.roster_revision_after == 4,
            f"decision={auto.review.decision.value},ROSTER=r{auto.roster_revision_after}",
        ),
        RetentionReviewEvaluationCheck(
            "auto_review_escalates_safety_failure",
            auto_safety.review.decision.value == "REQUIRES_USER_APPROVAL"
            and not auto_safety.applied
            and auto_safety_revision == 3,
            f"decision={auto_safety.review.decision.value},ROSTER=r{auto_safety_revision}",
        ),
        RetentionReviewEvaluationCheck(
            "always_approve_keeps_hard_validation_but_skips_semantic_review",
            always.review.decision.value == "APPROVAL_BYPASSED"
            and always.applied
            and always_revision == 4,
            f"decision={always.review.decision.value},ROSTER=r{always_revision}",
        ),
        RetentionReviewEvaluationCheck(
            "running_snapshot_is_frozen_next_and_restart_are_inactive",
            auto_employee_in_running
            and not auto_employee_in_next
            and not auto_employee_in_restart
            and restart_mode == RetentionReviewMode.AUTO_REVIEW,
            f"running=r{running_snapshot.revision},next=r{next_snapshot.revision}",
        ),
        RetentionReviewEvaluationCheck(
            "new_observation_blocks_stale_apply_in_every_mode",
            stale_rejected and stale_revision == 3,
            f"rejected={stale_rejected},ROSTER=r{stale_revision}",
        ),
    )
    return RetentionReviewEvaluationRecord(
        schema_version="noruct.retention-review-evaluation.v1",
        evidence_class="offline-production-shaped-policy-evaluation",
        manual_decision=manual.review.decision.value,
        auto_review_decision=auto.review.decision.value,
        auto_review_safety_decision=auto_safety.review.decision.value,
        always_approve_decision=always.review.decision.value,
        running_snapshot_revision=running_snapshot.revision,
        next_snapshot_revision=next_snapshot.revision,
        stale_apply_rejected=stale_rejected,
        provider_calls=0,
        quota_consumed=False,
        checks=checks,
    )
