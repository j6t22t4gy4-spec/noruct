"""Evidence-gated admission for automatic Company coordination.

The Front Door may identify a *possible* team, replica, or Manager-shaped Job,
but a possible topology is not evidence that it improves an outcome.  This
module is the narrow bridge from replayable OrganizationEpisode observations to
the next Job's automatic coordination admission.  It owns no authority,
budget, graph mutation, or employee selection.

Explicit user-required independent review remains an instruction, not an
automatic optimization claim.  The evidence gate only collapses automatic
``PLAN_FIRST`` expansion that was suggested for performance reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Iterable

from dynamic_firm.kernel.models import ExecutionReplicaPreference

from .models import OrganizationEpisode
from .operating import (
    CompanyOperatingDecision,
    CompanyWorkMode,
    InitialCoordinationPolicy,
)


class OrganizationEvidenceDecision(StrEnum):
    """What local outcome evidence permits for one exact workflow context."""

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SOLO_REQUIRED = "SOLO_REQUIRED"
    TEAM_ELIGIBLE = "TEAM_ELIGIBLE"
    REPLICA_ELIGIBLE = "REPLICA_ELIGIBLE"


@dataclass(frozen=True, slots=True)
class OrganizationOutcomeAssessment:
    """Content-free result of a same-context automatic staffing assessment."""

    context_fingerprint: str
    decision: OrganizationEvidenceDecision
    observed_episode_ids: tuple[str, ...]
    production_episode_count: int
    baselined_team_episode_count: int
    baselined_replica_episode_count: int
    lower_decile_quality_delta: float | None
    median_model_call_delta: int | None
    reasons: tuple[str, ...]


def assess_organization_outcomes(
    episodes: Iterable[OrganizationEpisode],
    *,
    context_fingerprint: str,
) -> OrganizationOutcomeAssessment:
    """Assess whether a context earned automatic TEAM or replica reuse.

    Two independent production episodes with an explicit same-budget baseline
    are the minimum.  An unsafe, failed, or negative-transfer team result
    denies automatic expansion rather than being averaged away.  A replica is
    stricter still: it must independently meet the same rule and record an
    actual bounded replica group in the immutable runtime ledger.
    """

    scoped = tuple(
        sorted(
            (
                item
                for item in episodes
                if item.production_eligible
                and item.context_fingerprint == context_fingerprint
            ),
            key=lambda item: (item.recorded_at, item.episode_id),
        )
    )
    teams = tuple(item for item in scoped if _is_material_team(item))
    replicas = tuple(item for item in teams if item.execution_replica_count >= 2)
    team_result = _cohort_result(teams)
    replica_result = _cohort_result(replicas)

    if replica_result.eligible:
        decision = OrganizationEvidenceDecision.REPLICA_ELIGIBLE
    elif team_result.eligible:
        decision = OrganizationEvidenceDecision.TEAM_ELIGIBLE
    elif len(scoped) < 2:
        decision = OrganizationEvidenceDecision.INSUFFICIENT_EVIDENCE
    else:
        decision = OrganizationEvidenceDecision.SOLO_REQUIRED

    reasons = list(team_result.reasons)
    if decision is OrganizationEvidenceDecision.REPLICA_ELIGIBLE:
        reasons.append("replica_value_reproduced")
    elif replicas and not replica_result.eligible:
        reasons.extend(
            reason
            for reason in replica_result.reasons
            if reason not in reasons
        )
    if not teams:
        reasons.append("no_material_team_episode")
    return OrganizationOutcomeAssessment(
        context_fingerprint=context_fingerprint,
        decision=decision,
        observed_episode_ids=tuple(item.episode_id for item in scoped),
        production_episode_count=len(scoped),
        baselined_team_episode_count=len(team_result.baselined),
        baselined_replica_episode_count=len(replica_result.baselined),
        lower_decile_quality_delta=team_result.lower_decile_quality_delta,
        median_model_call_delta=team_result.median_model_call_delta,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def apply_organization_evidence_gate(
    decision: CompanyOperatingDecision,
    assessment: OrganizationOutcomeAssessment,
) -> CompanyOperatingDecision:
    """Collapse an automatic topology candidate when outcome proof is absent.

    A user can explicitly require independent review.  That is preserved so
    the Kernel can validate the requested separation.  In every other case a
    planning opportunity is only a candidate until the exact context has a
    qualified outcome cohort.
    """

    if (
        decision.coordination_policy is not InitialCoordinationPolicy.PLAN_FIRST
        or decision.requires_independent_review
    ):
        return decision
    if assessment.decision in {
        OrganizationEvidenceDecision.TEAM_ELIGIBLE,
        OrganizationEvidenceDecision.REPLICA_ELIGIBLE,
    }:
        if assessment.decision is OrganizationEvidenceDecision.TEAM_ELIGIBLE:
            return replace(
                decision,
                execution_replica_preference=ExecutionReplicaPreference.DISABLED,
                suggested_execution_replica_strategy=None,
            )
        return decision
    return replace(
        decision,
        work_mode=CompanyWorkMode.SOLO_JOB,
        coordination_policy=InitialCoordinationPolicy.SOLO_FIRST,
        execution_replica_preference=ExecutionReplicaPreference.DISABLED,
        suggested_execution_replica_strategy=None,
    )


@dataclass(frozen=True, slots=True)
class _CohortResult:
    baselined: tuple[OrganizationEpisode, ...]
    lower_decile_quality_delta: float | None
    median_model_call_delta: int | None
    eligible: bool
    reasons: tuple[str, ...]


def _cohort_result(items: tuple[OrganizationEpisode, ...]) -> _CohortResult:
    baselined = tuple(item for item in items if item.baseline_quality_score is not None)
    quality_deltas = tuple(
        round(item.quality_score - float(item.baseline_quality_score), 6)
        for item in baselined
    )
    model_deltas = tuple(
        int(item.baseline_model_calls) - item.model_calls
        for item in baselined
        if item.baseline_model_calls is not None
    )
    p10 = _lower_decile(quality_deltas)
    median_calls = _median_int(model_deltas)
    reasons: list[str] = []
    if len(items) < 2:
        reasons.append("insufficient_independent_production_episodes")
    if len(baselined) < 2:
        reasons.append("insufficient_same_budget_baselines")
    if not model_deltas:
        reasons.append("model_call_baseline_missing")
    if any(not item.safety_passed for item in items):
        reasons.append("unsafe_or_failed_organization_episode")
    if any(delta < -1e-9 for delta in quality_deltas):
        reasons.append("negative_quality_transfer_observed")
    if not reasons and not (
        p10 is not None
        and (p10 >= 0.1 or (p10 >= 0 and median_calls is not None and median_calls >= 1))
    ):
        reasons.append("no_reproducible_quality_or_cost_gain")
    return _CohortResult(
        baselined=baselined,
        lower_decile_quality_delta=p10,
        median_model_call_delta=median_calls,
        eligible=not reasons,
        reasons=tuple(reasons),
    )


def _is_material_team(item: OrganizationEpisode) -> bool:
    # A label, role prompt, or employee count alone does not establish a
    # heterogeneous team.  The accepted graph must show at least two distinct
    # capability requirements and more than one selected execution identity.
    capability_sets = {
        tuple(sorted(task.required_capabilities)) for task in item.plan_template
    }
    return item.employee_count >= 2 and len(capability_sets) >= 2


def _lower_decile(values: tuple[float, ...]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, (len(ordered) + 9) // 10 - 1)]


def _median_int(values: tuple[int, ...]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]
