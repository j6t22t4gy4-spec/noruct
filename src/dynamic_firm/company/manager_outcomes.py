"""Deterministic, read-only outcome assessment for persistent Managers.

This is deliberately an assessment, not a manager ranking system and not a
promotion engine.  It consumes already immutable Organization Episodes and
returns a bounded explanation of whether a Manager-led coordination shape has
enough evidence to be kept under observation.  It never alters a Manager,
ROSTER, Skill, workflow, authority, or runtime budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from .models import OrganizationEpisode


class ManagerOutcomeDecision(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    KEEP_UNDER_OBSERVATION = "KEEP_UNDER_OBSERVATION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class ManagerOutcomeAssessment:
    """One content-free deterministic assessment for a Manager/context lane."""

    manager_employee_id: str
    context_fingerprint: str
    decision: ManagerOutcomeDecision
    observed_episode_ids: tuple[str, ...]
    production_episode_count: int
    baseline_episode_count: int
    succeeded_count: int
    safety_passed_count: int
    effect_passed_count: int
    specialist_job_count: int
    replan_job_count: int
    supervised_job_count: int
    negative_transfer_count: int
    p10_quality_delta: float | None
    median_model_call_delta: int | None
    reasons: tuple[str, ...]
    promotion_allowed: bool = False


def assess_manager_outcomes(
    episodes: Iterable[OrganizationEpisode],
    *,
    manager_employee_id: str | None = None,
    context_fingerprint: str | None = None,
) -> tuple[ManagerOutcomeAssessment, ...]:
    """Assess exact Manager/context cohorts without inventing a baseline.

    Only episodes with a persistent Manager identity participate.  A cohort
    needs two independent production episodes with an explicit baseline before
    a positive KEEP decision is possible.  Any failed, unsafe, or quality
    negative member is exposed as REVIEW_REQUIRED rather than averaged away.
    """

    groups: dict[tuple[str, str], list[OrganizationEpisode]] = {}
    for episode in episodes:
        if not episode.manager_employee_id:
            continue
        if manager_employee_id and episode.manager_employee_id != manager_employee_id:
            continue
        if context_fingerprint and episode.context_fingerprint != context_fingerprint:
            continue
        groups.setdefault(
            (episode.manager_employee_id, episode.context_fingerprint), []
        ).append(episode)

    return tuple(
        _assess_group(manager_id, context, items)
        for (manager_id, context), items in sorted(groups.items())
    )


def _assess_group(
    manager_id: str,
    context: str,
    episodes: Iterable[OrganizationEpisode],
) -> ManagerOutcomeAssessment:
    ordered = tuple(sorted(episodes, key=lambda item: (item.recorded_at, item.episode_id)))
    production = tuple(item for item in ordered if item.production_eligible)
    baselined = tuple(
        item for item in production if item.baseline_quality_score is not None
    )
    quality_deltas = tuple(
        round(item.quality_score - float(item.baseline_quality_score), 6)
        for item in baselined
    )
    model_deltas = tuple(
        int(item.baseline_model_calls) - item.model_calls
        for item in baselined
        if item.baseline_model_calls is not None
    )
    negative_transfer = sum(
        1
        for item in baselined
        if (item.quality_score - float(item.baseline_quality_score)) < -1e-9
    )
    failed_or_unsafe = any(
        not item.success or not item.safety_passed for item in production
    )
    reasons: list[str] = []
    if len(production) < 2:
        reasons.append("insufficient_independent_production_episodes")
    if len(baselined) < 2:
        reasons.append("insufficient_same_budget_baselines")
    if not model_deltas:
        reasons.append("model_call_baseline_missing")
    if failed_or_unsafe:
        reasons.append("failed_or_unsafe_manager_episode")
    if negative_transfer:
        reasons.append("negative_quality_transfer_observed")

    p10 = _lower_decile(quality_deltas)
    median_calls = _median_int(model_deltas)
    if reasons:
        decision = (
            ManagerOutcomeDecision.REVIEW_REQUIRED
            if failed_or_unsafe or negative_transfer
            else ManagerOutcomeDecision.INSUFFICIENT_EVIDENCE
        )
    elif p10 is not None and (p10 >= 0.1 or (p10 >= 0 and median_calls is not None and median_calls >= 1)):
        decision = ManagerOutcomeDecision.KEEP_UNDER_OBSERVATION
        reasons.append("bounded_quality_or_cost_gain_reproduced")
    else:
        decision = ManagerOutcomeDecision.REVIEW_REQUIRED
        reasons.append("no_reproducible_quality_or_cost_gain")

    return ManagerOutcomeAssessment(
        manager_employee_id=manager_id,
        context_fingerprint=context,
        decision=decision,
        observed_episode_ids=tuple(item.episode_id for item in ordered),
        production_episode_count=len(production),
        baseline_episode_count=len(baselined),
        succeeded_count=sum(item.success for item in production),
        safety_passed_count=sum(item.safety_passed for item in production),
        effect_passed_count=sum(item.effect_passed for item in baselined),
        specialist_job_count=sum(item.temporary_role_count > 0 for item in production),
        replan_job_count=sum(item.graph_patch_count > 0 for item in production),
        supervised_job_count=sum(item.manager_supervision_count > 0 for item in production),
        negative_transfer_count=negative_transfer,
        p10_quality_delta=p10,
        median_model_call_delta=median_calls,
        reasons=tuple(reasons),
    )


def _lower_decile(values: tuple[float, ...]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # Conservative lower decile: for a two-item cohort this is the minimum.
    return ordered[max(0, (len(ordered) + 9) // 10 - 1)]


def _median_int(values: tuple[int, ...]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]
