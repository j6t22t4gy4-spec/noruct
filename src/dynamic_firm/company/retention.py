from __future__ import annotations

from dynamic_firm.runtime.models import utc_now

from .models import (
    HireAssessmentDecision,
    RetentionRecommendationResult,
    RetentionReviewDecision,
    RetentionReviewMode,
    RosterPatchStatus,
    RosterRetentionReview,
    content_digest,
)
from .roster_patch import RosterPatchService
from .store import CompanyStateStore


_AUTO_REVIEW_REASONS = {
    "hire_unused_within_observation_limit",
    "temporary_fallback_repeated",
}


class RosterRetentionService:
    """Translate exact hire assessments into reversible ROSTER changes."""

    def __init__(self, store: CompanyStateStore) -> None:
        self.store = store
        self.patches = RosterPatchService(store)

    def _candidate(self, hire_patch_id: str):
        assessment = self.store.latest_hire_assessment(hire_patch_id)
        if assessment is None:
            raise ValueError("Hire must be assessed before retention review")
        if assessment.decision != HireAssessmentDecision.DORMANCY_CANDIDATE:
            raise ValueError(
                "Retention proposal requires the latest DORMANCY_CANDIDATE assessment"
            )
        contract = self.store.get_hire_observation_contract(hire_patch_id)

        existing_for_assessment = next(
            (
                item
                for item in self.store.list_roster_patches()
                if item.assessment_ids == (assessment.assessment_id,)
            ),
            None,
        )
        if existing_for_assessment is not None:
            return existing_for_assessment, contract, assessment

        open_for_employee = next(
            (
                item
                for item in self.store.list_roster_patches()
                if item.employee_id == contract.employee_id
                and item.status
                in {RosterPatchStatus.PROPOSED, RosterPatchStatus.APPROVED}
                and item.before_employee is not None
                and item.before_employee.get("active") is True
                and item.after_employee.get("active") is False
            ),
            None,
        )
        if open_for_employee is not None:
            raise ValueError(
                "Employee already has an open dormancy Roster Patch: "
                + open_for_employee.patch_id
            )

        candidate = self.patches.propose_set_active(
            contract.employee_id,
            False,
            rationale=(
                "Latest evidence-backed hire assessment recommends reversible dormancy; "
                "no employee data, memory, capability, or credential is deleted."
            ),
            actor="system:retention-recommender",
            assessment_ids=(assessment.assessment_id,),
        )
        return candidate, contract, assessment

    def _review(self, candidate, contract, assessment) -> RosterRetentionReview:
        company = self.store.company()
        mode = self.store.retention_review_mode()
        if mode == RetentionReviewMode.APPROVAL:
            decision = RetentionReviewDecision.PENDING_USER_APPROVAL
            reasons = ("company_policy_requires_user_approval",)
        elif mode == RetentionReviewMode.AUTO_REVIEW:
            if (
                len(assessment.cohort_observation_ids)
                == contract.maximum_observations
                and set(assessment.reasons).issubset(_AUTO_REVIEW_REASONS)
            ):
                decision = RetentionReviewDecision.AUTO_APPROVED
                reasons = ("bounded_full_window_underuse_review_passed",)
            else:
                decision = RetentionReviewDecision.REQUIRES_USER_APPROVAL
                reasons = ("failure_or_safety_dormancy_requires_human_judgment",)
        else:
            decision = RetentionReviewDecision.APPROVAL_BYPASSED
            reasons = (
                "user_selected_always_approve_for_reversible_retention_only",
            )
        immutable = {
            "roster_patch_id": candidate.patch_id,
            "hire_patch_id": contract.patch_id,
            "assessment_id": assessment.assessment_id,
            "company_revision": company.revision,
            "mode": mode,
            "decision": decision,
            "reasons": reasons,
        }
        digest = content_digest(immutable)
        return RosterRetentionReview(
            review_id=f"retention-review-{digest[:24]}",
            **immutable,
            content_hash=digest,
            reviewed_at=utc_now().isoformat(),
        )

    def recommend(self, hire_patch_id: str) -> RetentionRecommendationResult:
        before = self.store.roster().revision
        candidate, contract, assessment = self._candidate(hire_patch_id)
        prior_reviews = self.store.list_retention_reviews(candidate.patch_id)
        if candidate.status == RosterPatchStatus.APPLIED and prior_reviews:
            return RetentionRecommendationResult(
                mode=prior_reviews[-1].mode,
                patch=candidate,
                review=prior_reviews[-1],
                roster_revision_before=before,
                roster_revision_after=before,
                applied=False,
            )

        review = self.store.record_retention_review(
            self._review(candidate, contract, assessment)
        )[0]
        should_apply = review.decision in {
            RetentionReviewDecision.AUTO_APPROVED,
            RetentionReviewDecision.APPROVAL_BYPASSED,
        }
        updated = candidate
        if should_apply:
            actor = (
                "system:deterministic-retention-reviewer"
                if review.decision == RetentionReviewDecision.AUTO_APPROVED
                else "user-policy:retention-always-approve"
            )
            updated = self.patches.approve(candidate.patch_id, actor=actor)
            updated = self.patches.apply(candidate.patch_id, actor=actor)
        after = self.store.roster().revision
        return RetentionRecommendationResult(
            mode=review.mode,
            patch=updated,
            review=review,
            roster_revision_before=before,
            roster_revision_after=after,
            applied=after != before,
        )
