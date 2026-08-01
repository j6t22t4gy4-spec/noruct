from __future__ import annotations

from collections import defaultdict

from dynamic_firm.compiler import (
    CompilerExecutionProfile,
    WorkflowPrior,
    WorkflowPriorTask,
)
from dynamic_firm.kernel.workflow_shape import (
    WorkflowShapeError,
    canonical_workflow_shape,
)
from dynamic_firm.runtime.models import utc_now

from .models import (
    CurationResult,
    OrganizationEpisode,
    WorkflowPatchCandidate,
    WorkflowPatchAssessment,
    WorkflowPatchAssessmentDecision,
    WorkflowPatchObservation,
    WorkflowPatchStatus,
    WorkflowPattern,
    content_digest,
)
from .store import CompanyStateStore


MINIMUM_EVIDENCE_COUNT = 2


class CompanyLearningService:
    """Deterministic Workflow Patch lifecycle; never applies a patch automatically."""

    def __init__(self, store: CompanyStateStore) -> None:
        self.store = store

    @staticmethod
    def _group_key(episode: OrganizationEpisode) -> tuple[str, str, str, str]:
        try:
            workflow_identity = canonical_workflow_shape(
                episode.plan_template,
                key_of=lambda task: task.task_key,
                capabilities_of=lambda task: task.required_capabilities,
                dependencies_of=lambda task: task.depends_on,
                final_of=lambda task: task.final,
            )
        except WorkflowShapeError:
            workflow_identity = f"exact-plan-digest:{episode.plan_digest}"
        return (
            episode.task_family,
            episode.context_fingerprint,
            episode.execution_profile,
            workflow_identity,
        )

    @staticmethod
    def _pattern(episodes: tuple[OrganizationEpisode, ...]) -> WorkflowPattern:
        first = episodes[0]
        pattern_identity = {
            "task_family": first.task_family,
            "context_fingerprint": first.context_fingerprint,
            "execution_profile": first.execution_profile,
            "plan_digest": first.plan_digest,
        }
        return WorkflowPattern(
            pattern_id=f"workflow-{content_digest(pattern_identity)[:24]}",
            task_family=first.task_family,
            context_fingerprint=first.context_fingerprint,
            execution_profile=first.execution_profile,
            plan_digest=first.plan_digest,
            tasks=first.plan_template,
            maximum_parallelism=max(item.maximum_parallelism for item in episodes),
            writer_count=max(item.writer_count for item in episodes),
            evidence_count=len(episodes),
            rationale=(
                "Repeated ledger episodes showed the same safe workflow shape with a "
                "measured quality or model-call benefit. This is an advisory Compiler prior."
            ),
        )

    @classmethod
    def _candidate(
        cls,
        episodes: tuple[OrganizationEpisode, ...],
        *,
        base_playbook_revision: int,
    ) -> WorkflowPatchCandidate:
        pattern = cls._pattern(episodes)
        quality_gains = tuple(
            round(item.quality_score - float(item.baseline_quality_score), 6)
            for item in episodes
            if item.baseline_quality_score is not None
        )
        model_savings = tuple(
            item.baseline_model_calls - item.model_calls
            for item in episodes
            if item.baseline_model_calls is not None
        )
        eligible = all(item.production_eligible for item in episodes)
        reasons = () if eligible else ("synthetic_or_offline_evidence_present",)
        immutable = {
            "base_playbook_revision": base_playbook_revision,
            "pattern": pattern,
            "evidence_episode_ids": tuple(item.episode_id for item in episodes),
            "expected_quality_gain": min(quality_gains),
            "expected_model_call_savings": min(model_savings) if model_savings else 0,
            "confidence": round(min(0.95, 0.6 + len(episodes) * 0.1), 2),
            "eligible_for_apply": eligible,
            "ineligibility_reasons": reasons,
        }
        digest = content_digest(immutable)
        now = utc_now().isoformat()
        return WorkflowPatchCandidate(
            patch_id=f"workflow-patch-{digest[:24]}",
            status=WorkflowPatchStatus.PROPOSED,
            base_playbook_revision=base_playbook_revision,
            pattern=pattern,
            evidence_episode_ids=immutable["evidence_episode_ids"],
            expected_quality_gain=float(immutable["expected_quality_gain"]),
            expected_model_call_savings=int(immutable["expected_model_call_savings"]),
            confidence=float(immutable["confidence"]),
            eligible_for_apply=eligible,
            ineligibility_reasons=reasons,
            content_hash=digest,
            created_at=now,
            updated_at=now,
        )

    def curate(self) -> CurationResult:
        episodes = self.store.list_episodes()
        groups: dict[tuple[str, str, str, str], list[OrganizationEpisode]] = defaultdict(list)
        qualified = tuple(
            item for item in episodes if item.safety_passed and item.effect_passed
        )
        for episode in qualified:
            groups[self._group_key(episode)].append(episode)

        active_pattern_ids = {item.pattern_id for item in self.store.playbook().patterns}
        candidates: list[WorkflowPatchCandidate] = []
        reasons: list[str] = []
        for items in groups.values():
            ordered = tuple(sorted(items, key=lambda item: (item.recorded_at, item.episode_id)))
            if len(ordered) < MINIMUM_EVIDENCE_COUNT:
                reasons.append("insufficient_repeated_evidence")
                continue
            production = tuple(item for item in ordered if item.production_eligible)
            selected = (
                production
                if len(production) >= MINIMUM_EVIDENCE_COUNT
                else ordered
            )
            pattern = self._pattern(selected)
            if pattern.pattern_id in active_pattern_ids:
                reasons.append("matching_workflow_pattern_already_active")
                continue
            existing = self.store.find_open_patch_for_pattern(pattern.pattern_id)
            proposed = self._candidate(
                selected,
                base_playbook_revision=self.store.playbook().revision,
            )
            if existing is not None and (
                existing.eligible_for_apply or not proposed.eligible_for_apply
            ):
                candidates.append(existing)
                continue
            stored, _ = self.store.create_candidate(proposed)
            if stored.status in {
                WorkflowPatchStatus.PROPOSED,
                WorkflowPatchStatus.APPROVED,
            }:
                candidates.append(stored)
            else:
                reasons.append("matching_candidate_was_previously_closed")

        if not episodes:
            reasons.append("no_organization_episodes")
        elif not qualified:
            reasons.append("no_episode_passed_repetition_effect_and_safety_gates")
        decision = "CANDIDATE_AVAILABLE" if candidates else "NO_PATCH"
        return CurationResult(
            decision=decision,
            candidates=tuple(candidates),
            considered_episode_count=len(episodes),
            qualified_episode_count=len(qualified),
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def preview(self, patch_id: str) -> WorkflowPatchCandidate:
        return self.store.get_patch(patch_id)

    def approve(self, patch_id: str, *, actor: str) -> WorkflowPatchCandidate:
        return self.store.approve_patch(patch_id, actor)

    def apply(self, patch_id: str, *, actor: str) -> WorkflowPatchCandidate:
        return self.store.apply_patch(patch_id, actor)

    def reject(
        self, patch_id: str, *, actor: str, reason: str
    ) -> WorkflowPatchCandidate:
        return self.store.reject_patch(patch_id, actor, reason)

    def rollback(self, patch_id: str, *, actor: str) -> WorkflowPatchCandidate:
        return self.store.rollback_patch(patch_id, actor)

    def replay(self, patch_id: str) -> bool:
        candidate = self.store.get_patch(patch_id)
        episodes = tuple(
            self.store.get_episode(episode_id)
            for episode_id in candidate.evidence_episode_ids
        )
        from .promotion import (
            promotion_envelope_from_events,
            replay_promoted_candidate,
        )

        try:
            promotion = promotion_envelope_from_events(
                self.store.list_patch_events(patch_id)
            )
        except ValueError:
            return False
        if promotion is not None:
            return replay_promoted_candidate(candidate, episodes, promotion)
        if len(episodes) < MINIMUM_EVIDENCE_COUNT:
            return False
        if not all(item.safety_passed and item.effect_passed for item in episodes):
            return False
        replayed = self._candidate(
            episodes,
            base_playbook_revision=candidate.base_playbook_revision,
        )
        return (
            replayed.content_hash == candidate.content_hash
            and replayed.pattern == candidate.pattern
            and replayed.evidence_episode_ids == candidate.evidence_episode_ids
        )

    def observe(
        self,
        patch_id: str,
        episode: OrganizationEpisode,
        *,
        prior_exposed: bool,
        proposal_aligned: bool,
    ) -> WorkflowPatchObservation:
        """Record attribution facts; never assess or roll back in the job path."""

        if proposal_aligned and not prior_exposed:
            raise ValueError("A workflow proposal cannot align with an unexposed prior")
        contract = self.store.get_observation_contract(patch_id)
        existing = next(
            (
                item
                for item in self.store.list_observations(patch_id)
                if item.episode_id == episode.episode_id
            ),
            None,
        )
        if existing is not None:
            if (
                existing.prior_exposed != prior_exposed
                or existing.proposal_aligned != proposal_aligned
            ):
                raise ValueError(
                    "Workflow patch episode already has different attribution evidence"
                )
            return existing

        attribution_reasons: list[str] = []
        if not prior_exposed:
            attribution_reasons.append("prior_not_exposed")
        if not proposal_aligned:
            attribution_reasons.append("proposal_not_aligned")
        if not episode.production_eligible:
            attribution_reasons.append("non_production_evidence")
        if episode.execution_profile != contract.execution_profile:
            attribution_reasons.append("execution_profile_mismatch")
        if episode.context_fingerprint != contract.context_fingerprint:
            attribution_reasons.append("context_fingerprint_mismatch")
        attribution_eligible = not attribution_reasons

        reasons = list(attribution_reasons)
        quality_gain = (
            None
            if episode.baseline_quality_score is None
            else round(episode.quality_score - episode.baseline_quality_score, 6)
        )
        model_call_savings = (
            None
            if episode.baseline_model_calls is None
            else episode.baseline_model_calls - episode.model_calls
        )
        if quality_gain is None:
            reasons.append("quality_baseline_missing")
        if model_call_savings is None:
            reasons.append("model_call_baseline_missing")
        cohort_count = sum(
            item.cohort_eligible for item in self.store.list_observations(patch_id)
        )
        if cohort_count >= contract.maximum_observations:
            reasons.append("observation_limit_reached")
        immutable = {
            "patch_id": patch_id,
            "episode_id": episode.episode_id,
            "prior_exposed": prior_exposed,
            "proposal_aligned": proposal_aligned,
            "attribution_eligible": attribution_eligible,
            "cohort_eligible": not reasons,
            "ineligibility_reasons": tuple(reasons),
            "quality_gain": quality_gain,
            "model_call_savings": model_call_savings,
        }
        digest = content_digest(immutable)
        observation = WorkflowPatchObservation(
            observation_id=f"workflow-observation-{digest[:24]}",
            **immutable,
            content_hash=digest,
            recorded_at=utc_now().isoformat(),
        )
        return self.store.record_observation(observation)[0]

    @staticmethod
    def _has_safety_failure(episode: OrganizationEpisode) -> bool:
        return bool(
            episode.safety_violations
            or episode.preapproval_mutations
            or episode.approvals_granted != episode.approvals_requested
            or not episode.validation_attempts
            or not all(episode.validation_attempts)
            or episode.writer_count > 1
        )

    def assess(self, patch_id: str) -> WorkflowPatchAssessment:
        """Append a deterministic recommendation; never changes the PLAYBOOK."""

        contract = self.store.get_observation_contract(patch_id)
        observations = self.store.list_observations(patch_id)
        attributable = tuple(item for item in observations if item.attribution_eligible)
        cohort = tuple(item for item in observations if item.cohort_eligible)
        attributable_episodes = tuple(
            self.store.get_episode(item.episode_id) for item in attributable
        )

        decision = WorkflowPatchAssessmentDecision.INSUFFICIENT_OBSERVATION
        reasons: tuple[str, ...]
        if any(not item.success for item in attributable_episodes):
            decision = WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE
            reasons = ("attributed_job_failure",)
        elif contract.fail_on_safety_violation and any(
            self._has_safety_failure(item) for item in attributable_episodes
        ):
            decision = WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE
            reasons = ("attributed_safety_violation",)
        elif len(cohort) < contract.minimum_observations:
            reasons = ("minimum_observation_count_not_reached",)
        else:
            quality_gains = tuple(
                float(item.quality_gain)
                for item in cohort
                if item.quality_gain is not None
            )
            model_savings = tuple(
                int(item.model_call_savings)
                for item in cohort
                if item.model_call_savings is not None
            )
            mean_quality = round(sum(quality_gains) / len(quality_gains), 6)
            mean_savings = round(sum(model_savings) / len(model_savings), 6)
            effect_reproduced = mean_quality >= contract.minimum_quality_gain - 1e-9 or (
                mean_quality >= -1e-9
                and mean_savings >= contract.minimum_model_call_savings
            )
            if effect_reproduced:
                decision = WorkflowPatchAssessmentDecision.KEEP
                reasons = ("measured_effect_reproduced",)
            elif mean_quality < -1e-9 or mean_savings < -1e-9:
                decision = WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE
                reasons = ("measured_effect_regressed",)
            elif len(cohort) >= contract.maximum_observations:
                decision = WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE
                reasons = ("effect_not_reproduced_within_observation_limit",)
            else:
                reasons = ("effect_not_yet_reproduced",)

        quality_values = tuple(
            float(item.quality_gain) for item in cohort if item.quality_gain is not None
        )
        savings_values = tuple(
            int(item.model_call_savings)
            for item in cohort
            if item.model_call_savings is not None
        )
        mean_quality_gain = (
            round(sum(quality_values) / len(quality_values), 6)
            if quality_values
            else None
        )
        mean_model_call_savings = (
            round(sum(savings_values) / len(savings_values), 6)
            if savings_values
            else None
        )
        immutable = {
            "patch_id": patch_id,
            "decision": decision,
            "reasons": reasons,
            "attributable_observation_ids": tuple(
                item.observation_id for item in attributable
            ),
            "cohort_observation_ids": tuple(item.observation_id for item in cohort),
            "mean_quality_gain": mean_quality_gain,
            "mean_model_call_savings": mean_model_call_savings,
        }
        digest = content_digest(immutable)
        latest = self.store.latest_assessment(patch_id)
        assessment = WorkflowPatchAssessment(
            assessment_id=f"workflow-assessment-{digest[:24]}",
            seq=1 if latest is None else latest.seq + 1,
            **immutable,
            content_hash=digest,
            assessed_at=utc_now().isoformat(),
        )
        return self.store.record_assessment(assessment)[0]

    def compiler_priors(
        self,
        execution_profile: CompilerExecutionProfile,
        *,
        context_fingerprint: str | None = None,
        limit: int = 8,
    ) -> tuple[WorkflowPrior, ...]:
        if not 1 <= limit <= 8:
            raise ValueError("Compiler prior limit must be between 1 and 8")
        selected = (
            pattern
            for pattern in self.store.playbook().patterns
            if pattern.execution_profile == execution_profile.value
            and (
                context_fingerprint is None
                or pattern.context_fingerprint == context_fingerprint
            )
        )
        return tuple(
            WorkflowPrior(
                pattern_id=pattern.pattern_id,
                task_family=pattern.task_family,
                context_fingerprint=pattern.context_fingerprint,
                execution_profile=execution_profile,
                rationale=pattern.rationale,
                tasks=tuple(
                    WorkflowPriorTask(
                        task_key=task.task_key,
                        required_capabilities=task.required_capabilities,
                        depends_on=task.depends_on,
                        final=task.final,
                    )
                    for task in pattern.tasks
                ),
                evidence_count=pattern.evidence_count,
            )
            for pattern in list(selected)[:limit]
        )
