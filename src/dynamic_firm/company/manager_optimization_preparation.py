"""Provider-free preparation contracts for the D11 Manager optimization gate.

This module records a bounded proposal; it does not select a provider, run a
Manager, alter a budget, or promote a candidate.  All objects are immutable so
the preparation can be reviewed independently of runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Iterable

from .manager_fallback_policy import (
    ManagerTerminalEvidence,
    project_manager_fallback,
)
from ..evaluation.organization_comparison_v9 import (
    ORGANIZATION_ARMS,
    V9_REPORT_SCHEMA,
    OrganizationComparisonV9ArmReport,
    OrganizationComparisonV9Report,
)


class ManagerFailureStage(StrEnum):
    PLANNING = "planning"
    STAFFING = "staffing"
    DECOMPOSITION = "decomposition"
    HANDOFF = "handoff"
    INTEGRATION = "integration"
    VERIFICATION = "verification"


FIXED_FAILURE_STAGES = tuple(ManagerFailureStage)


class ManagerOptimizationDecision(StrEnum):
    SOLO_REQUIRED = "SOLO_REQUIRED"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ManagerOptimizationPreparationError(ValueError):
    """A proposed change is outside the bounded D11 contract."""


class MatchedReportError(ValueError):
    """A report is malformed for the matched before/after comparison."""


def _text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManagerOptimizationPreparationError(f"{name}_MUST_BE_NON_EMPTY")
    return value


def _stage(value: ManagerFailureStage | str) -> ManagerFailureStage:
    try:
        return value if isinstance(value, ManagerFailureStage) else ManagerFailureStage(value)
    except (TypeError, ValueError) as exc:
        raise ManagerOptimizationPreparationError("FAILURE_STAGE_NOT_SUPPORTED") from exc


def _nonnegative_int(name: str, value: int) -> int:
    if type(value) is not int or value < 0:
        raise ManagerOptimizationPreparationError(f"{name}_MUST_BE_NON_NEGATIVE_INTEGER")
    return value


def _finite_number(name: str, value: float | int) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ManagerOptimizationPreparationError(f"{name}_MUST_BE_FINITE_NUMBER")
    return value


@dataclass(frozen=True, slots=True)
class FailureStageAttribution:
    """One content-free, immutable attribution for a release workload."""

    workload_id: str
    stage: ManagerFailureStage
    evidence_id: str
    reason_code: str
    complete_failure: bool

    def __post_init__(self) -> None:
        _text("WORKLOAD_ID", self.workload_id)
        _text("EVIDENCE_ID", self.evidence_id)
        _text("REASON_CODE", self.reason_code)
        _stage(self.stage)
        if type(self.complete_failure) is not bool:
            raise ManagerOptimizationPreparationError("COMPLETE_FAILURE_MUST_BE_BOOLEAN")


def attribute_failure_stage(
    *,
    workload_id: str,
    stage: ManagerFailureStage | str,
    evidence_id: str,
    reason_code: str,
    complete_failure: bool,
) -> FailureStageAttribution:
    """Create the only accepted form of failure-stage attribution."""

    return FailureStageAttribution(
        workload_id=workload_id,
        stage=_stage(stage),
        evidence_id=evidence_id,
        reason_code=reason_code,
        complete_failure=complete_failure,
    )


@dataclass(frozen=True, slots=True)
class OneStageAblation:
    """A single stage change with all comparison controls held constant."""

    stage: ManagerFailureStage
    change_code: str
    held_constant: tuple[str, ...] = (
        "prompt",
        "model_access",
        "authority",
        "total_budget",
        "evaluator",
    )
    provider_calls: int = 0

    def __post_init__(self) -> None:
        _stage(self.stage)
        _text("CHANGE_CODE", self.change_code)
        if self.held_constant != (
            "prompt",
            "model_access",
            "authority",
            "total_budget",
            "evaluator",
        ):
            raise ManagerOptimizationPreparationError("ABLATION_CONTROLS_MUST_BE_HELD_CONSTANT")
        _nonnegative_int("PROVIDER_CALLS", self.provider_calls)
        if self.provider_calls != 0:
            raise ManagerOptimizationPreparationError("ABLATION_MUST_BE_PROVIDER_FREE")


@dataclass(frozen=True, slots=True)
class OneStageAblationPlan:
    """Exactly one provider-free ablation for each accepted failure stage."""

    ablations: tuple[OneStageAblation, ...]

    def __post_init__(self) -> None:
        stages = tuple(item.stage for item in self.ablations)
        if stages != FIXED_FAILURE_STAGES:
            raise ManagerOptimizationPreparationError(
                "ABLATION_PLAN_MUST_CHANGE_EACH_FIXED_STAGE_ONCE"
            )


def build_one_stage_ablation_plan() -> OneStageAblationPlan:
    return OneStageAblationPlan(
        ablations=tuple(
            OneStageAblation(stage=stage, change_code=f"ablate_{stage.value}")
            for stage in FIXED_FAILURE_STAGES
        )
    )


@dataclass(frozen=True, slots=True)
class RoleTierRoute:
    role: str
    model_tier: str

    def __post_init__(self) -> None:
        _text("ROLE", self.role)
        _text("MODEL_TIER", self.model_tier)


@dataclass(frozen=True, slots=True)
class RoleSensitiveRoutingProposal:
    """A role-to-tier proposal that cannot change access or authority."""

    routes: tuple[RoleTierRoute, ...]
    model_access_unchanged: bool = True
    authority_unchanged: bool = True
    budget_unchanged: bool = True
    evaluator_unchanged: bool = True

    def __post_init__(self) -> None:
        if not self.routes or len({route.role for route in self.routes}) != len(self.routes):
            raise ManagerOptimizationPreparationError("ROUTING_ROLES_MUST_BE_UNIQUE")
        if not all(
            (
                self.model_access_unchanged,
                self.authority_unchanged,
                self.budget_unchanged,
                self.evaluator_unchanged,
            )
        ):
            raise ManagerOptimizationPreparationError("ROUTING_MUST_PRESERVE_COMPARISON_CONTROLS")


def default_role_sensitive_routing() -> RoleSensitiveRoutingProposal:
    return RoleSensitiveRoutingProposal(
        routes=(
            RoleTierRoute("manager", "coordination"),
            RoleTierRoute("specialist", "execution"),
            RoleTierRoute("integrator", "integration"),
            RoleTierRoute("verifier", "verification"),
        )
    )


@dataclass(frozen=True, slots=True)
class BoundedOptimizationCandidate:
    """One stage-bounded candidate; this object never grants promotion."""

    candidate_id: str
    stage: ManagerFailureStage
    ablation: OneStageAblation
    routing: RoleSensitiveRoutingProposal
    additional_model_calls: int = 0
    prompt_changed: bool = False
    budget_changed: bool = False
    authority_changed: bool = False
    evaluator_changed: bool = False
    promotion_allowed: bool = False

    def __post_init__(self) -> None:
        _text("CANDIDATE_ID", self.candidate_id)
        if _stage(self.stage) != self.ablation.stage:
            raise ManagerOptimizationPreparationError("CANDIDATE_STAGE_MUST_MATCH_ABLATION")
        _nonnegative_int("ADDITIONAL_MODEL_CALLS", self.additional_model_calls)
        if self.additional_model_calls != 0:
            raise ManagerOptimizationPreparationError("CANDIDATE_MUST_KEEP_TOTAL_BUDGET")
        if any(
            (
                self.prompt_changed,
                self.budget_changed,
                self.authority_changed,
                self.evaluator_changed,
                self.promotion_allowed,
            )
        ):
            raise ManagerOptimizationPreparationError(
                "CANDIDATE_REJECTS_PROMPT_BUDGET_AUTHORITY_EVALUATOR_OR_PROMOTION_CHANGE"
            )


def build_bounded_candidate(
    stage: ManagerFailureStage | str,
    *,
    candidate_id: str = "d11-stage-bounded-candidate",
) -> BoundedOptimizationCandidate:
    fixed_stage = _stage(stage)
    ablation = OneStageAblation(stage=fixed_stage, change_code=f"ablate_{fixed_stage.value}")
    return BoundedOptimizationCandidate(
        candidate_id=candidate_id,
        stage=fixed_stage,
        ablation=ablation,
        routing=default_role_sensitive_routing(),
    )


def strong_solo_fallback(*, negative_transfer: bool = True) -> ManagerTerminalEvidence:
    """Return the existing terminal fallback contract without executing it."""

    evidence = project_manager_fallback(negative_transfer=negative_transfer)
    if not evidence.terminal or evidence.retry_allowed or evidence.loop_allowed:
        raise ManagerOptimizationPreparationError("SOLO_FALLBACK_MUST_BE_TERMINAL")
    return evidence


@dataclass(frozen=True, slots=True)
class MatchedReportGuardResult:
    decision: ManagerOptimizationDecision
    matching_release_workloads: int
    complete_failure_rate_before: float
    complete_failure_rate_after: float
    lower_tail_improved: bool
    review_rework_improved: bool
    reasons: tuple[str, ...]
    promotion_allowed: bool = False


def _report_arm(
    report: OrganizationComparisonV9Report,
    arm_name: str,
) -> OrganizationComparisonV9ArmReport:
    if not isinstance(report, OrganizationComparisonV9Report):
        raise MatchedReportError("REPORT_MUST_USE_V9_TYPE")
    if report.schema_version != V9_REPORT_SCHEMA:
        raise MatchedReportError("REPORT_SCHEMA_MISMATCH")
    names = tuple(arm.arm for arm in report.arms)
    if names != ORGANIZATION_ARMS:
        raise MatchedReportError("REPORT_MUST_CONTAIN_FOUR_ORDERED_ARMS")
    for arm in report.arms:
        if arm.arm == arm_name:
            return arm
    raise MatchedReportError("REPORT_ARM_MISSING")


def _rate(arm: OrganizationComparisonV9ArmReport) -> float:
    slots = arm.complete_safety_failure.slot_count
    complete = arm.complete_safety_failure.complete_failure_count
    if type(slots) is not int or slots <= 0 or type(complete) is not int or not 0 <= complete <= slots:
        raise MatchedReportError("REPORT_COMPLETE_FAILURE_COUNTS_INVALID")
    return complete / slots


def _numeric_review_values(arm: OrganizationComparisonV9ArmReport) -> tuple[float, ...] | None:
    values = (
        arm.review_rework.review_wait_ms,
        arm.review_rework.reopened_evidence_count,
        arm.review_rework.unused_subartifact_rate,
        arm.review_rework.rework_count,
        arm.review_rework.approval_friction_count,
    )
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
        return None
    result = tuple(float(value) for value in values)
    if not all(isfinite(value) and value >= 0 for value in result):
        raise MatchedReportError("REPORT_REVIEW_REWORK_VALUES_INVALID")
    return result


def guard_matched_reports(
    before: OrganizationComparisonV9Report,
    after: OrganizationComparisonV9Report,
    *,
    matching_release_workloads: int,
    candidate: BoundedOptimizationCandidate | None = None,
) -> MatchedReportGuardResult:
    """Gate a candidate on matched strong-SOLO/Manager-led report facts.

    The gate only returns review eligibility.  It never returns promotion
    eligibility, and it requires two matching release workloads before any
    status can leave ``SOLO_REQUIRED``.
    """

    _nonnegative_int("MATCHING_RELEASE_WORKLOADS", matching_release_workloads)
    if candidate is not None and not isinstance(candidate, BoundedOptimizationCandidate):
        raise MatchedReportError("CANDIDATE_MUST_USE_BOUNDED_TYPE")
    if before.benchmark_id != after.benchmark_id or before.manifest_content_hash != after.manifest_content_hash:
        raise MatchedReportError("REPORTS_ARE_NOT_MATCHED")

    solo_before = _report_arm(before, "strong-solo")
    manager_after = _report_arm(after, "manager-led-graph")
    before_rate = _rate(solo_before)
    after_rate = _rate(manager_after)

    before_cost = solo_before.cost_time
    after_cost = manager_after.cost_time
    costs_match = before_cost == after_cost
    lower_tail_improved = (
        solo_before.lower_decile_quality.lower_decile_quality is not None
        and manager_after.lower_decile_quality.lower_decile_quality is not None
        and manager_after.lower_decile_quality.lower_decile_quality
        > solo_before.lower_decile_quality.lower_decile_quality
    )
    before_review = _numeric_review_values(solo_before)
    after_review = _numeric_review_values(manager_after)
    review_rework_improved = False
    review_rework_nonworse = False
    if before_review is not None and after_review is not None:
        review_rework_nonworse = all(after_value <= before_value for before_value, after_value in zip(before_review, after_review))
        review_rework_improved = review_rework_nonworse and any(
            after_value < before_value for before_value, after_value in zip(before_review, after_review)
        )

    reasons: list[str] = []
    if matching_release_workloads < 2:
        reasons.append("fewer_than_two_matching_release_workloads")
    if after_rate > before_rate:
        reasons.append("complete_failure_rate_worsened")
    if not costs_match:
        reasons.append("matched_model_review_cost_missing")
    if not lower_tail_improved and not review_rework_improved:
        reasons.append("no_prescribed_lower_tail_or_review_rework_improvement")

    if matching_release_workloads < 2:
        decision = ManagerOptimizationDecision.SOLO_REQUIRED
    elif reasons:
        decision = ManagerOptimizationDecision.OBSERVE_ONLY
    else:
        decision = ManagerOptimizationDecision.REVIEW_REQUIRED
        reasons.append("bounded_candidate_ready_for_independent_review")

    return MatchedReportGuardResult(
        decision=decision,
        matching_release_workloads=matching_release_workloads,
        complete_failure_rate_before=before_rate,
        complete_failure_rate_after=after_rate,
        lower_tail_improved=lower_tail_improved,
        review_rework_improved=review_rework_improved,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class ManagerOptimizationPreparation:
    attribution: FailureStageAttribution
    ablation_plan: OneStageAblationPlan
    candidate: BoundedOptimizationCandidate
    routing: RoleSensitiveRoutingProposal
    solo_fallback: ManagerTerminalEvidence


def prepare_manager_optimization(
    attribution: FailureStageAttribution,
) -> ManagerOptimizationPreparation:
    """Assemble the bounded D11 output without touching Manager runtime."""

    plan = build_one_stage_ablation_plan()
    candidate = build_bounded_candidate(attribution.stage)
    return ManagerOptimizationPreparation(
        attribution=attribution,
        ablation_plan=plan,
        candidate=candidate,
        routing=candidate.routing,
        solo_fallback=strong_solo_fallback(),
    )


__all__ = [
    "BoundedOptimizationCandidate",
    "FIXED_FAILURE_STAGES",
    "FailureStageAttribution",
    "ManagerFailureStage",
    "ManagerOptimizationDecision",
    "ManagerOptimizationPreparation",
    "ManagerOptimizationPreparationError",
    "MatchedReportError",
    "MatchedReportGuardResult",
    "OneStageAblation",
    "OneStageAblationPlan",
    "RoleSensitiveRoutingProposal",
    "RoleTierRoute",
    "attribute_failure_stage",
    "build_bounded_candidate",
    "build_one_stage_ablation_plan",
    "default_role_sensitive_routing",
    "guard_matched_reports",
    "prepare_manager_optimization",
    "strong_solo_fallback",
]
