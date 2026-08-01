"""Provider-free, matched-baseline improvement comparison.

This projection reports fixed metric deltas only.  It does not read Job state,
run a provider, score a cohort, or turn mechanism evidence into an outcome
claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math


IMPROVEMENT_CONCLUSION_SCHEMA = "noruct.improvement-conclusion.v1"
OUTCOME_NOT_ESTABLISHED = "OUTCOME_NOT_ESTABLISHED"
COMPARISON_RECORDED = "COMPARISON_RECORDED"

FIXED_METRICS = (
    "quality",
    "complete_failure",
    "safety_failure",
    "cost",
    "time",
    "review",
    "rework",
)

CONFOUNDER_NOT_ASSESSED = "NOT_ASSESSED"
CONFOUNDER_RECORDED = "RECORDED"

COVERAGE_MATCHED = "MATCHED"
COVERAGE_BASELINE_MISSING = "BASELINE_MISSING"
COVERAGE_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
COVERAGE_METRIC_SET_MISMATCH = "METRIC_SET_MISMATCH"
COVERAGE_METRIC_VALUE_MISSING = "METRIC_VALUE_MISSING"
COVERAGE_INVALID_IDENTITY = "INVALID_IDENTITY"


def _identity_text(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ComparisonIdentity:
    """The exact cohort identity required for a comparison."""

    task_revision: str
    source: str
    authority: str
    budget: str
    metric_set: tuple[str, ...] = FIXED_METRICS

    def __post_init__(self) -> None:
        for name, value in (
            ("task_revision", self.task_revision),
            ("source", self.source),
            ("authority", self.authority),
            ("budget", self.budget),
        ):
            _identity_text(value, name)
        if self.metric_set != FIXED_METRICS:
            raise ValueError("metric_set must be the fixed improvement metric set")

    @property
    def budget_baseline(self) -> str:
        """Compatibility spelling for the budget baseline identity."""

        return self.budget

    def to_dict(self) -> dict[str, object]:
        return {
            "task_revision": self.task_revision,
            "source": self.source,
            "authority": self.authority,
            "budget": self.budget,
            "metric_set": self.metric_set,
        }


@dataclass(frozen=True, slots=True)
class MetricDelta:
    """One fixed numeric delta, always calculated as candidate minus baseline."""

    metric: str
    baseline: int | float
    candidate: int | float
    delta: int | float

    def __post_init__(self) -> None:
        if self.metric not in FIXED_METRICS:
            raise ValueError("metric is not in the fixed improvement metric set")
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
        """Compatibility spelling for the candidate observation."""

        return self.candidate

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "delta": self.delta,
        }


@dataclass(frozen=True, slots=True)
class ImprovementConclusion:
    """Bounded comparison result with no implicit user-outcome claim."""

    conclusion: str
    comparison_identity: ComparisonIdentity | None
    metric_deltas: tuple[MetricDelta, ...]
    confounder_status: str
    coverage_status: str
    outcome_claim: str = "NO_USER_OUTCOME_CLAIM"

    def __post_init__(self) -> None:
        if self.conclusion not in {COMPARISON_RECORDED, OUTCOME_NOT_ESTABLISHED}:
            raise ValueError("unsupported improvement conclusion")
        if not isinstance(self.metric_deltas, tuple):
            raise ValueError("metric_deltas must be immutable")
        if any(not isinstance(item, MetricDelta) for item in self.metric_deltas):
            raise ValueError("metric_deltas must contain MetricDelta records")
        if self.conclusion == OUTCOME_NOT_ESTABLISHED and self.metric_deltas:
            raise ValueError("an unestablished outcome cannot contain metric deltas")
        if self.confounder_status not in {CONFOUNDER_NOT_ASSESSED, CONFOUNDER_RECORDED}:
            raise ValueError("unsupported confounder status")
        if not isinstance(self.coverage_status, str) or not self.coverage_status:
            raise ValueError("coverage_status must be explicit")
        if self.outcome_claim != "NO_USER_OUTCOME_CLAIM":
            raise ValueError("comparison cannot claim a user outcome")

    @property
    def status(self) -> str:
        """Expose the conclusion under the common projection spelling."""

        return self.conclusion

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": IMPROVEMENT_CONCLUSION_SCHEMA,
            "conclusion": self.conclusion,
            "comparison_identity": (
                None if self.comparison_identity is None else self.comparison_identity.to_dict()
            ),
            "metric_deltas": tuple(item.to_dict() for item in self.metric_deltas),
            "confounder_status": self.confounder_status,
            "coverage_status": self.coverage_status,
            "outcome_claim": self.outcome_claim,
        }

    payload = to_dict


def _as_identity(value: object) -> ComparisonIdentity | None:
    if isinstance(value, ComparisonIdentity):
        return value
    if not isinstance(value, Mapping):
        return None
    budget = value.get("budget", value.get("budget_baseline"))
    try:
        return ComparisonIdentity(
            task_revision=value.get("task_revision"),
            source=value.get("source"),
            authority=value.get("authority"),
            budget=budget,
        )
    except (TypeError, ValueError):
        return None


def _valid_metrics(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != set(FIXED_METRICS):
        return False
    return all(
        isinstance(item, (int, float))
        and not isinstance(item, bool)
        and math.isfinite(float(item))
        for item in value.values()
    )


def conclude_improvement(
    candidate_identity: ComparisonIdentity | Mapping[str, object],
    candidate_metrics: Mapping[str, object],
    baseline_identity: ComparisonIdentity | Mapping[str, object] | None = None,
    baseline_metrics: Mapping[str, object] | None = None,
    *,
    confounders: tuple[str, ...] = (),
) -> ImprovementConclusion:
    """Compare exact matched records using only the seven fixed metric deltas.

    ``delta`` is always ``candidate - baseline``.  A missing or mismatched
    identity or metric set returns no deltas and ``OUTCOME_NOT_ESTABLISHED``.
    The optional confounder input is recorded as presence only; it is never
    interpreted or used to adjust a metric.
    """

    candidate = _as_identity(candidate_identity)
    confounder_status = CONFOUNDER_RECORDED if confounders else CONFOUNDER_NOT_ASSESSED
    if baseline_identity is None or baseline_metrics is None:
        return ImprovementConclusion(
            OUTCOME_NOT_ESTABLISHED,
            candidate,
            (),
            confounder_status,
            COVERAGE_BASELINE_MISSING,
        )
    baseline = _as_identity(baseline_identity)
    if candidate is None or baseline is None:
        return ImprovementConclusion(
            OUTCOME_NOT_ESTABLISHED,
            candidate,
            (),
            confounder_status,
            COVERAGE_INVALID_IDENTITY,
        )
    if candidate != baseline:
        return ImprovementConclusion(
            OUTCOME_NOT_ESTABLISHED,
            candidate,
            (),
            confounder_status,
            COVERAGE_IDENTITY_MISMATCH,
        )
    if not _valid_metrics(candidate_metrics) or not _valid_metrics(baseline_metrics):
        coverage = (
            COVERAGE_METRIC_SET_MISMATCH
            if (
                not isinstance(candidate_metrics, Mapping)
                or not isinstance(baseline_metrics, Mapping)
                or set(candidate_metrics) != set(FIXED_METRICS)
                or set(baseline_metrics) != set(FIXED_METRICS)
            )
            else COVERAGE_METRIC_VALUE_MISSING
        )
        return ImprovementConclusion(
            OUTCOME_NOT_ESTABLISHED,
            candidate,
            (),
            confounder_status,
            coverage,
        )
    deltas = tuple(
        MetricDelta(
            metric,
            baseline_metrics[metric],
            candidate_metrics[metric],
            candidate_metrics[metric] - baseline_metrics[metric],
        )
        for metric in FIXED_METRICS
    )
    return ImprovementConclusion(
        COMPARISON_RECORDED,
        candidate,
        deltas,
        confounder_status,
        COVERAGE_MATCHED,
    )


compare_improvement = conclude_improvement
improvement_conclusion = conclude_improvement
