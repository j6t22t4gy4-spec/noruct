"""Immutable, provider-free observations for exact Blueprint revision reuse.

This module records reuse facts and compares only explicitly matched reuse and
no-reuse observations.  It never treats structural facts as an outcome, and
the eligibility adapter is deliberately experimental-only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from typing import Final

from ..product.improvement_conclusion import OUTCOME_NOT_ESTABLISHED

from .organization_eligibility import OrganizationReuseEligibility


BLUEPRINT_REUSE_VALUE_SCHEMA: Final = "noruct.blueprint-reuse-value.v1"
COMPARISON_RECORDED: Final = "COMPARISON_RECORDED"
NO_USER_OUTCOME_CLAIM: Final = "NO_USER_OUTCOME_CLAIM"
EXPERIMENTAL_ONLY: Final = "EXPERIMENTAL_ONLY"

REUSE_METRICS: Final = ("cost", "quality", "review")

COVERAGE_MATCHED: Final = "MATCHED"
COVERAGE_ZERO_SAMPLE: Final = "ZERO_SAMPLE"
COVERAGE_INSUFFICIENT_EVIDENCE: Final = "INSUFFICIENT_EVIDENCE"
COVERAGE_NO_REUSE: Final = "NO_REUSE_OBSERVED"
COVERAGE_IDENTITY_MISMATCH: Final = "IDENTITY_MISMATCH"
COVERAGE_INVALID_OBSERVATION: Final = "INVALID_OBSERVATION"


def _identity_text(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _finite_metric(value: object, name: str) -> int | float | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{name} must be a finite number or None")
    return value


@dataclass(frozen=True, slots=True)
class BlueprintRevisionIdentity:
    """The exact identity required for a reuse comparison."""

    blueprint_revision: str
    context: str
    authority: str
    budget: str

    def __post_init__(self) -> None:
        for name, value in (
            ("blueprint_revision", self.blueprint_revision),
            ("context", self.context),
            ("authority", self.authority),
            ("budget", self.budget),
        ):
            _identity_text(value, name)

    def to_dict(self) -> dict[str, str]:
        return {
            "blueprint_revision": self.blueprint_revision,
            "context": self.context,
            "authority": self.authority,
            "budget": self.budget,
        }


@dataclass(frozen=True, slots=True)
class BlueprintReuseObservation:
    """One immutable, content-free observation for a Blueprint revision.

    ``reused`` and the operational facts are recorded as supplied.  Missing
    outcome metrics remain ``None``; they are never replaced by zero or
    inferred from reuse success, adaptation, or structural failure.
    """

    blueprint_revision: str
    context: str
    authority: str
    budget: str
    reused: bool
    reuse_success: bool | None = None
    adaptation_count: int = 0
    structural_failure: bool = False
    planning_call_saving: int = 0
    cost: int | float | None = None
    quality: int | float | None = None
    review: int | float | None = None

    def __post_init__(self) -> None:
        identity = BlueprintRevisionIdentity(
            self.blueprint_revision,
            self.context,
            self.authority,
            self.budget,
        )
        object.__setattr__(self, "blueprint_revision", identity.blueprint_revision)
        object.__setattr__(self, "context", identity.context)
        object.__setattr__(self, "authority", identity.authority)
        object.__setattr__(self, "budget", identity.budget)
        if type(self.reused) is not bool:
            raise TypeError("reused must be a bool")
        if self.reuse_success is not None and type(self.reuse_success) is not bool:
            raise TypeError("reuse_success must be a bool or None")
        if self.reused and self.reuse_success is None:
            raise ValueError("reuse_success is required for a reused observation")
        if not self.reused and self.reuse_success is True:
            raise ValueError("a no-reuse observation cannot record reuse success")
        if (
            not isinstance(self.adaptation_count, int)
            or isinstance(self.adaptation_count, bool)
            or self.adaptation_count < 0
        ):
            raise ValueError("adaptation_count must be a non-negative integer")
        if type(self.structural_failure) is not bool:
            raise TypeError("structural_failure must be a bool")
        if (
            not isinstance(self.planning_call_saving, int)
            or isinstance(self.planning_call_saving, bool)
            or self.planning_call_saving < 0
        ):
            raise ValueError("planning_call_saving must be a non-negative integer")
        for name in REUSE_METRICS:
            _finite_metric(getattr(self, name), name)

    @property
    def identity(self) -> BlueprintRevisionIdentity:
        return BlueprintRevisionIdentity(
            self.blueprint_revision,
            self.context,
            self.authority,
            self.budget,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": BLUEPRINT_REUSE_VALUE_SCHEMA,
            "blueprint_revision": self.blueprint_revision,
            "context": self.context,
            "authority": self.authority,
            "budget": self.budget,
            "reused": self.reused,
            "reuse_success": self.reuse_success,
            "adaptation_count": self.adaptation_count,
            "structural_failure": self.structural_failure,
            "planning_call_saving": self.planning_call_saving,
            "cost": self.cost,
            "quality": self.quality,
            "review": self.review,
        }

    payload = to_dict


@dataclass(frozen=True, slots=True)
class BlueprintReuseMetricDelta:
    """A candidate-minus-baseline delta with no quality interpretation."""

    metric: str
    baseline: int | float
    candidate: int | float
    delta: int | float

    def __post_init__(self) -> None:
        if self.metric not in REUSE_METRICS:
            raise ValueError("metric is not a Blueprint reuse metric")
        for name, value in (
            ("baseline", self.baseline),
            ("candidate", self.candidate),
            ("delta", self.delta),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be a finite number")

    @property
    def current(self) -> int | float:
        return self.candidate

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "delta": self.delta,
        }


@dataclass(frozen=True, slots=True)
class BlueprintReuseComparison:
    """A matched comparison or an explicit fail-closed evidence result."""

    conclusion: str
    comparison_identity: BlueprintRevisionIdentity | None
    metric_deltas: tuple[BlueprintReuseMetricDelta, ...]
    coverage_status: str
    sample_count: int
    eligibility: OrganizationReuseEligibility = (
        OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE
    )
    automatic_reuse: bool = False
    outcome_claim: str = NO_USER_OUTCOME_CLAIM

    def __post_init__(self) -> None:
        if self.conclusion not in {COMPARISON_RECORDED, OUTCOME_NOT_ESTABLISHED}:
            raise ValueError("unsupported Blueprint reuse conclusion")
        if not isinstance(self.metric_deltas, tuple):
            raise ValueError("metric_deltas must be immutable")
        if any(not isinstance(item, BlueprintReuseMetricDelta) for item in self.metric_deltas):
            raise ValueError("metric_deltas must contain Blueprint reuse deltas")
        if self.conclusion == OUTCOME_NOT_ESTABLISHED and self.metric_deltas:
            raise ValueError("an unestablished outcome cannot contain metric deltas")
        if not isinstance(self.coverage_status, str) or not self.coverage_status:
            raise ValueError("coverage_status must be explicit")
        if (
            not isinstance(self.sample_count, int)
            or isinstance(self.sample_count, bool)
            or self.sample_count < 0
        ):
            raise ValueError("sample_count must be a non-negative integer")
        if self.eligibility is not OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE:
            raise ValueError("Blueprint reuse remains experimental-only")
        if self.automatic_reuse is not False:
            raise ValueError("Blueprint reuse cannot activate automatically")
        if self.outcome_claim != NO_USER_OUTCOME_CLAIM:
            raise ValueError("comparison cannot claim a user outcome")

    @property
    def status(self) -> str:
        return self.conclusion

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": BLUEPRINT_REUSE_VALUE_SCHEMA,
            "conclusion": self.conclusion,
            "comparison_identity": (
                None
                if self.comparison_identity is None
                else self.comparison_identity.to_dict()
            ),
            "metric_deltas": tuple(item.to_dict() for item in self.metric_deltas),
            "coverage_status": self.coverage_status,
            "sample_count": self.sample_count,
            "eligibility": self.eligibility.value,
            "automatic_reuse": self.automatic_reuse,
            "outcome_claim": self.outcome_claim,
        }

    payload = to_dict


def _observations(
    value: BlueprintReuseObservation | Iterable[BlueprintReuseObservation] | None,
) -> tuple[BlueprintReuseObservation, ...]:
    if value is None:
        return ()
    if isinstance(value, BlueprintReuseObservation):
        return (value,)
    if isinstance(value, (str, bytes, Mapping)):
        raise TypeError("observations must be BlueprintReuseObservation values")
    try:
        items = tuple(value)
    except TypeError as exc:
        raise TypeError("observations must be BlueprintReuseObservation values") from exc
    if any(not isinstance(item, BlueprintReuseObservation) for item in items):
        raise TypeError("observations must be BlueprintReuseObservation values")
    return items


def _failed_comparison(
    status: str,
    candidate: tuple[BlueprintReuseObservation, ...],
    baseline: tuple[BlueprintReuseObservation, ...],
) -> BlueprintReuseComparison:
    identity = candidate[0].identity if candidate else None
    return BlueprintReuseComparison(
        OUTCOME_NOT_ESTABLISHED,
        identity,
        (),
        status,
        len(candidate) + len(baseline),
    )


def _same_identity(items: tuple[BlueprintReuseObservation, ...]) -> bool:
    return bool(items) and all(item.identity == items[0].identity for item in items)


def _mean(items: tuple[BlueprintReuseObservation, ...], metric: str) -> int | float | None:
    values = [getattr(item, metric) for item in items]
    if any(value is None for value in values):
        return None
    return sum(values) / len(values)  # type: ignore[arg-type]


def compare_blueprint_reuse(
    reused_observations: BlueprintReuseObservation
    | Iterable[BlueprintReuseObservation]
    | None,
    no_reuse_observations: BlueprintReuseObservation
    | Iterable[BlueprintReuseObservation]
    | None = None,
) -> BlueprintReuseComparison:
    """Compare exact matched reuse and no-reuse observations.

    All observations in both arms must share one exact revision/context/
    authority/budget identity.  Only then are cost, quality, and review
    arithmetic deltas emitted.  Aggregation is arithmetic only; it does not
    infer an outcome from reuse success, adaptation, structural failure, or
    planning-call saving.
    """

    reused = _observations(reused_observations)
    baseline = _observations(no_reuse_observations)
    if not reused and not baseline:
        return _failed_comparison(COVERAGE_ZERO_SAMPLE, reused, baseline)
    if not reused or not baseline:
        return _failed_comparison(COVERAGE_INSUFFICIENT_EVIDENCE, reused, baseline)
    if any(not item.reused for item in reused):
        return _failed_comparison(COVERAGE_NO_REUSE, reused, baseline)
    if any(item.reused for item in baseline):
        return _failed_comparison(COVERAGE_NO_REUSE, reused, baseline)
    if not _same_identity(reused) or not _same_identity(baseline):
        return _failed_comparison(COVERAGE_IDENTITY_MISMATCH, reused, baseline)
    if reused[0].identity != baseline[0].identity:
        return _failed_comparison(COVERAGE_IDENTITY_MISMATCH, reused, baseline)

    candidate_values = {metric: _mean(reused, metric) for metric in REUSE_METRICS}
    baseline_values = {metric: _mean(baseline, metric) for metric in REUSE_METRICS}
    if any(value is None for value in candidate_values.values()) or any(
        value is None for value in baseline_values.values()
    ):
        return _failed_comparison(COVERAGE_INSUFFICIENT_EVIDENCE, reused, baseline)

    deltas = tuple(
        BlueprintReuseMetricDelta(
            metric,
            baseline_values[metric],  # type: ignore[arg-type]
            candidate_values[metric],  # type: ignore[arg-type]
            candidate_values[metric] - baseline_values[metric],  # type: ignore[operator]
        )
        for metric in REUSE_METRICS
    )
    return BlueprintReuseComparison(
        COMPARISON_RECORDED,
        reused[0].identity,
        deltas,
        COVERAGE_MATCHED,
        len(reused) + len(baseline),
    )


def adapt_blueprint_reuse_eligibility(
    comparison: BlueprintReuseComparison,
) -> OrganizationReuseEligibility:
    """Project any Blueprint reuse comparison to the experimental-only state."""

    if not isinstance(comparison, BlueprintReuseComparison):
        raise TypeError("BLUEPRINT_REUSE_COMPARISON_REQUIRED")
    return OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE


project_blueprint_reuse_eligibility = adapt_blueprint_reuse_eligibility
compare_reuse_value = compare_blueprint_reuse
