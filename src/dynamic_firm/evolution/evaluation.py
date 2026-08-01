from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from dynamic_firm.company.models import content_digest

from .score_contract import evolution_content_digest


BLUEPRINT_ADMISSION_SCHEMA = "noruct.blueprint-admission-report.v1"
BLUEPRINT_DELTA_HOLDOUT_SCHEMA = "noruct.blueprint-delta-holdout-report.v1"
BLUEPRINT_DELTA_HOLDOUT_SUITE_SCHEMA = "noruct.blueprint-delta-holdout-suite-report.v1"


class BlueprintAdmissionDecision(StrEnum):
    REJECTED = "REJECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NO_EXECUTABLE_DELTA = "NO_EXECUTABLE_DELTA"


class BlueprintDeltaHoldoutDecision(StrEnum):
    REJECTED = "REJECTED"
    NO_IMPROVEMENT = "NO_IMPROVEMENT"
    REGRESSION = "REGRESSION"
    ELIGIBLE_FOR_MANUAL_REVIEW = "ELIGIBLE_FOR_MANUAL_REVIEW"


@dataclass(frozen=True, slots=True)
class BlueprintAdmissionReport:
    schema: str
    candidate_blueprint_id: str
    candidate_version: str
    capsule_digests: tuple[str, ...]
    capsule_count: int
    matched_capability_count: int
    successful_capsule_count: int
    distinct_task_context_count: int
    evaluator_kinds: tuple[str, ...]
    mean_quality_score: float | None
    decision: BlueprintAdmissionDecision
    promotion_allowed: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BlueprintDeltaHoldoutReport:
    schema: str
    fixture_id: str
    fixture_digest: str
    blueprint_id: str
    base_version: str
    candidate_version: str
    delta_digest: str
    baseline_passed: int
    candidate_passed: int
    total_cases: int
    positive_case_gain: int
    negative_case_regression_count: int
    decision: BlueprintDeltaHoldoutDecision
    automatic_promotion_allowed: bool
    manual_review_eligible: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BlueprintDeltaHoldoutSuiteReport:
    schema: str
    fixture_count: int
    fixture_digests: tuple[str, ...]
    reports: tuple[BlueprintDeltaHoldoutReport, ...]
    decision: BlueprintDeltaHoldoutDecision
    automatic_promotion_allowed: bool
    manual_review_eligible: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


# Public, content-free fixture. It only tests capability routing; it is not an
# employee-run benchmark and must never be presented as production quality.
_CAPABILITY_ALIAS_HOLDOUT = (
    ("direct_declared_capability", "repository_analysis", True),
    ("alias_repository_inspection", "repository_inspection", True),
    ("alias_repository_inventory", "repository_inventory", True),
    ("unknown_capability_denied", "external_write", False),
)
_CAPABILITY_ALIAS_SAFETY_HOLDOUT = (
    ("alias_repeat", "repository_inspection", True),
    ("unknown_external_communication_denied", "external_communication", False),
    ("unknown_secret_access_denied", "secret_access", False),
)


def _capability_route(
    declared: frozenset[str], aliases: Mapping[str, str], requested: str
) -> bool:
    if requested in declared:
        return True
    target = aliases.get(requested)
    return target in declared if target is not None else False


def evaluate_blueprint_admission(
    blueprint: Mapping[str, Any], capsules: Sequence[Mapping[str, Any]]
) -> BlueprintAdmissionReport:
    """Assess evidence alignment without fabricating an executable improvement.

    A minimized Learning Capsule is deliberately not an executable Skill or
    Workflow Patch.  EN-2 must therefore block every promotion until a later,
    separately audited evaluator measures an explicit, reversible Blueprint
    delta against a holdout fixture.
    """

    blueprint_capabilities = frozenset(str(item) for item in blueprint["capabilities"])
    matched = tuple(
        capsule
        for capsule in capsules
        if str(capsule["capability"]) in blueprint_capabilities
    )
    incompatible = len(matched) != len(capsules)
    successful = tuple(
        capsule for capsule in matched if capsule["outcome"]["status"] == "SUCCEEDED"
    )
    contexts = {
        (str(capsule["task_schema"]["domain"]), str(capsule["task_schema"]["operation"]))
        for capsule in successful
    }
    evaluators = tuple(
        sorted({str(capsule["outcome"]["evaluator_kind"]) for capsule in successful})
    )
    quality_scores = tuple(float(capsule["outcome"]["quality_score"]) for capsule in successful)
    mean_quality = sum(quality_scores) / len(quality_scores) if quality_scores else None
    blockers: list[str] = []
    if incompatible:
        blockers.append("CAPSULE_CAPABILITY_NOT_DECLARED_BY_BLUEPRINT")
    if len(successful) < 2:
        blockers.append("REQUIRES_AT_LEAST_TWO_SUCCESSFUL_CAPSULES")
    if len(contexts) < 2:
        blockers.append("REQUIRES_TWO_DISTINCT_TASK_CONTEXTS")
    if mean_quality is None or mean_quality < 0.75:
        blockers.append("MEAN_QUALITY_BELOW_SYNTHETIC_SCREEN")
    if blockers:
        decision = (
            BlueprintAdmissionDecision.REJECTED
            if incompatible
            else BlueprintAdmissionDecision.INSUFFICIENT_EVIDENCE
        )
    else:
        blockers.extend(
            (
                "CAPSULE_SCHEMA_HAS_NO_EXECUTABLE_SKILL_OR_WORKFLOW_DELTA",
                "NO_HOLDOUT_EXECUTION_OF_A_CANDIDATE_BLUEPRINT",
            )
        )
        decision = BlueprintAdmissionDecision.NO_EXECUTABLE_DELTA
    return BlueprintAdmissionReport(
        schema=BLUEPRINT_ADMISSION_SCHEMA,
        candidate_blueprint_id=str(blueprint["blueprint_id"]),
        candidate_version=str(blueprint["version"]),
        capsule_digests=tuple(evolution_content_digest(capsule) for capsule in capsules),
        capsule_count=len(capsules),
        matched_capability_count=len(matched),
        successful_capsule_count=len(successful),
        distinct_task_context_count=len(contexts),
        evaluator_kinds=evaluators,
        mean_quality_score=mean_quality,
        decision=decision,
        promotion_allowed=False,
        blockers=tuple(blockers),
    )


def evaluate_blueprint_delta_holdout(
    blueprint: Mapping[str, Any], delta: Mapping[str, Any]
) -> BlueprintDeltaHoldoutReport:
    """Run the one public/synthetic Delta holdout without touching runtime state."""

    blockers: list[str] = []
    if delta["blueprint_id"] != blueprint["blueprint_id"]:
        blockers.append("DELTA_BLUEPRINT_ID_DOES_NOT_MATCH_BASE")
    if delta["base_version"] != blueprint["version"]:
        blockers.append("DELTA_BASE_VERSION_DOES_NOT_MATCH_BASE")
    declared = frozenset(str(item) for item in blueprint["capabilities"])
    if delta["target_capability"] not in declared:
        blockers.append("DELTA_TARGET_CAPABILITY_NOT_DECLARED_BY_BASE")
    if delta["alias"] in declared:
        blockers.append("DELTA_ALIAS_ALREADY_IS_A_DECLARED_CAPABILITY")
    fixture_digest = content_digest(_CAPABILITY_ALIAS_HOLDOUT)
    if blockers:
        return BlueprintDeltaHoldoutReport(
            schema=BLUEPRINT_DELTA_HOLDOUT_SCHEMA,
            fixture_id="public-synthetic-capability-alias-v1",
            fixture_digest=fixture_digest,
            blueprint_id=str(blueprint["blueprint_id"]),
            base_version=str(blueprint["version"]),
            candidate_version=str(delta["candidate_version"]),
            delta_digest=content_digest(delta),
            baseline_passed=0,
            candidate_passed=0,
            total_cases=len(_CAPABILITY_ALIAS_HOLDOUT),
            positive_case_gain=0,
            negative_case_regression_count=0,
            decision=BlueprintDeltaHoldoutDecision.REJECTED,
            automatic_promotion_allowed=False,
            manual_review_eligible=False,
            blockers=tuple(blockers),
        )
    aliases = {str(delta["alias"]): str(delta["target_capability"])}
    baseline_passed = 0
    candidate_passed = 0
    positive_gain = 0
    negative_regressions = 0
    for _, requested, expected in _CAPABILITY_ALIAS_HOLDOUT:
        baseline_result = _capability_route(declared, {}, requested)
        candidate_result = _capability_route(declared, aliases, requested)
        baseline_passed += baseline_result == expected
        candidate_passed += candidate_result == expected
        if expected and candidate_result and not baseline_result:
            positive_gain += 1
        if not expected and candidate_result:
            negative_regressions += 1
    if negative_regressions:
        decision = BlueprintDeltaHoldoutDecision.REGRESSION
        blockers.append("NEGATIVE_HOLDOUT_CASE_BECAME_ALLOWED")
    elif candidate_passed <= baseline_passed or positive_gain == 0:
        decision = BlueprintDeltaHoldoutDecision.NO_IMPROVEMENT
        blockers.append("CANDIDATE_DOES_NOT_IMPROVE_PUBLIC_HOLDOUT")
    else:
        decision = BlueprintDeltaHoldoutDecision.ELIGIBLE_FOR_MANUAL_REVIEW
        blockers.extend(
            (
                "PUBLIC_SYNTHETIC_HOLDOUT_ONLY",
                "NO_AUTOMATIC_CATALOG_OR_NETWORK_PROMOTION",
                "REQUIRES_SIGNED_PROVENANCE_AND_OPERATOR_REVIEW",
            )
        )
    return BlueprintDeltaHoldoutReport(
        schema=BLUEPRINT_DELTA_HOLDOUT_SCHEMA,
        fixture_id="public-synthetic-capability-alias-v1",
        fixture_digest=fixture_digest,
        blueprint_id=str(blueprint["blueprint_id"]),
        base_version=str(blueprint["version"]),
        candidate_version=str(delta["candidate_version"]),
        delta_digest=content_digest(delta),
        baseline_passed=baseline_passed,
        candidate_passed=candidate_passed,
        total_cases=len(_CAPABILITY_ALIAS_HOLDOUT),
        positive_case_gain=positive_gain,
        negative_case_regression_count=negative_regressions,
        decision=decision,
        automatic_promotion_allowed=False,
        manual_review_eligible=(
            decision == BlueprintDeltaHoldoutDecision.ELIGIBLE_FOR_MANUAL_REVIEW
        ),
        blockers=tuple(blockers),
    )


def evaluate_blueprint_delta_holdout_suite(
    blueprint: Mapping[str, Any], delta: Mapping[str, Any]
) -> BlueprintDeltaHoldoutSuiteReport:
    """Require gain plus independent negative-boundary preservation.

    The second fixture has a distinct digest and safety cases. It is still
    public/synthetic, so a passing suite remains review-only.
    """
    primary = evaluate_blueprint_delta_holdout(blueprint, delta)
    declared = frozenset(str(item) for item in blueprint["capabilities"])
    aliases = {str(delta["alias"]): str(delta["target_capability"])}
    safety_baseline = 0
    safety_candidate = 0
    safety_gain = 0
    safety_regression = 0
    for _, requested, expected in _CAPABILITY_ALIAS_SAFETY_HOLDOUT:
        baseline = _capability_route(declared, {}, requested)
        candidate = _capability_route(declared, aliases, requested)
        safety_baseline += baseline == expected
        safety_candidate += candidate == expected
        safety_gain += int(expected and candidate and not baseline)
        safety_regression += int(not expected and candidate)
    safety_decision = (
        BlueprintDeltaHoldoutDecision.REGRESSION
        if safety_regression
        else BlueprintDeltaHoldoutDecision.ELIGIBLE_FOR_MANUAL_REVIEW
        if safety_candidate >= safety_baseline
        else BlueprintDeltaHoldoutDecision.NO_IMPROVEMENT
    )
    safety = BlueprintDeltaHoldoutReport(
        schema=BLUEPRINT_DELTA_HOLDOUT_SCHEMA,
        fixture_id="public-synthetic-capability-alias-safety-v1",
        fixture_digest=content_digest(_CAPABILITY_ALIAS_SAFETY_HOLDOUT),
        blueprint_id=str(blueprint["blueprint_id"]),
        base_version=str(blueprint["version"]),
        candidate_version=str(delta["candidate_version"]),
        delta_digest=content_digest(delta),
        baseline_passed=safety_baseline,
        candidate_passed=safety_candidate,
        total_cases=len(_CAPABILITY_ALIAS_SAFETY_HOLDOUT),
        positive_case_gain=safety_gain,
        negative_case_regression_count=safety_regression,
        decision=safety_decision,
        automatic_promotion_allowed=False,
        manual_review_eligible=safety_decision == BlueprintDeltaHoldoutDecision.ELIGIBLE_FOR_MANUAL_REVIEW,
        blockers=("PUBLIC_SYNTHETIC_SAFETY_FIXTURE_ONLY",),
    )
    eligible = primary.manual_review_eligible and safety.manual_review_eligible
    decision = (
        BlueprintDeltaHoldoutDecision.ELIGIBLE_FOR_MANUAL_REVIEW
        if eligible
        else BlueprintDeltaHoldoutDecision.REGRESSION
        if primary.decision == BlueprintDeltaHoldoutDecision.REGRESSION or safety.decision == BlueprintDeltaHoldoutDecision.REGRESSION
        else BlueprintDeltaHoldoutDecision.NO_IMPROVEMENT
    )
    return BlueprintDeltaHoldoutSuiteReport(
        schema=BLUEPRINT_DELTA_HOLDOUT_SUITE_SCHEMA,
        fixture_count=2,
        fixture_digests=(primary.fixture_digest, safety.fixture_digest),
        reports=(primary, safety),
        decision=decision,
        automatic_promotion_allowed=False,
        manual_review_eligible=eligible,
        blockers=(
            "PUBLIC_SYNTHETIC_MULTI_FIXTURE_ONLY",
            "NO_AUTOMATIC_CATALOG_OR_NETWORK_PROMOTION",
            "REQUIRES_SIGNED_PROVENANCE_AND_OPERATOR_REVIEW",
        ),
    )
