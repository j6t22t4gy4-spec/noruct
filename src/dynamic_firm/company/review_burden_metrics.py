"""Content-free review-burden facts and pure episode aggregation.

Review-burden values are observations, not quality judgments.  In
particular, a missing value is never converted to zero and the aggregate
keeps metric coverage separate from the episode quality score.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Any, Iterable, Mapping


NOT_RECORDED = "NOT_RECORDED"
NOT_RUN = "NOT_RUN"

DISCOVERED = "DISCOVERED"
NOT_DISCOVERED = "NOT_DISCOVERED"
COMPREHENDED = "COMPREHENDED"
NOT_COMPREHENDED = "NOT_COMPREHENDED"

REVIEW_BURDEN_NUMERIC_FIELDS = (
    "review_wait_ms",
    "reopened_evidence_count",
    "unused_subartifact_rate",
    "rework_count",
    "approval_friction_count",
)
REVIEW_BURDEN_STATE_FIELDS = (
    "unverified_item_discovery",
    "summary_comprehension_status",
)
REVIEW_BURDEN_FIELDS = REVIEW_BURDEN_NUMERIC_FIELDS + REVIEW_BURDEN_STATE_FIELDS

_MISSING = frozenset({NOT_RECORDED, NOT_RUN})
_DISCOVERY_STATES = frozenset(
    {NOT_RECORDED, NOT_RUN, DISCOVERED, NOT_DISCOVERED}
)
_COMPREHENSION_STATES = frozenset(
    {NOT_RECORDED, NOT_RUN, COMPREHENDED, NOT_COMPREHENDED}
)


def _numeric_value(name: str, value: object) -> int | float | str:
    if value in _MISSING:
        return str(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric or an explicit missing status")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    if name == "unused_subartifact_rate" and value > 1:
        raise ValueError("unused_subartifact_rate must be between zero and one")
    return value


def validate_review_burden_metrics(
    *,
    review_wait_ms: object,
    reopened_evidence_count: object,
    unused_subartifact_rate: object,
    rework_count: object,
    approval_friction_count: object,
    unverified_item_discovery: object,
    summary_comprehension_status: object,
) -> None:
    """Validate the additive scalar/state boundary used by an episode."""

    for name, value in (
        ("review_wait_ms", review_wait_ms),
        ("reopened_evidence_count", reopened_evidence_count),
        ("unused_subartifact_rate", unused_subartifact_rate),
        ("rework_count", rework_count),
        ("approval_friction_count", approval_friction_count),
    ):
        _numeric_value(name, value)
    if unverified_item_discovery not in _DISCOVERY_STATES:
        raise ValueError("unverified_item_discovery has an invalid state")
    if summary_comprehension_status not in _COMPREHENSION_STATES:
        raise ValueError("summary_comprehension_status has an invalid state")


@dataclass(frozen=True, slots=True)
class ReviewBurdenMetrics:
    """Immutable, content-free review observations for one episode.

    Numeric fields intentionally use an explicit string sentinel rather than
    ``0`` for missing data.  A zero is therefore a recorded fact.
    """

    review_wait_ms: int | float | str = NOT_RECORDED
    reopened_evidence_count: int | float | str = NOT_RECORDED
    unused_subartifact_rate: int | float | str = NOT_RECORDED
    rework_count: int | float | str = NOT_RECORDED
    approval_friction_count: int | float | str = NOT_RECORDED
    unverified_item_discovery: str = NOT_RUN
    summary_comprehension_status: str = NOT_RUN

    def __post_init__(self) -> None:
        validate_review_burden_metrics(
            review_wait_ms=self.review_wait_ms,
            reopened_evidence_count=self.reopened_evidence_count,
            unused_subartifact_rate=self.unused_subartifact_rate,
            rework_count=self.rework_count,
            approval_friction_count=self.approval_friction_count,
            unverified_item_discovery=self.unverified_item_discovery,
            summary_comprehension_status=self.summary_comprehension_status,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReviewBurdenMetrics":
        unknown = set(value).difference(REVIEW_BURDEN_FIELDS)
        if unknown:
            raise ValueError(f"unsupported review-burden fields: {sorted(unknown)}")
        return cls(
            review_wait_ms=value.get("review_wait_ms", NOT_RECORDED),
            reopened_evidence_count=value.get(
                "reopened_evidence_count", NOT_RECORDED
            ),
            unused_subartifact_rate=value.get(
                "unused_subartifact_rate", NOT_RECORDED
            ),
            rework_count=value.get("rework_count", NOT_RECORDED),
            approval_friction_count=value.get(
                "approval_friction_count", NOT_RECORDED
            ),
            unverified_item_discovery=value.get(
                "unverified_item_discovery", NOT_RUN
            ),
            summary_comprehension_status=value.get(
                "summary_comprehension_status", NOT_RUN
            ),
        )


@dataclass(frozen=True, slots=True)
class ReviewBurdenMetricSummary:
    """Coverage and arithmetic for one numeric metric."""

    recorded_count: int
    missing_count: int
    total: int | float | None
    mean: float | None


@dataclass(frozen=True, slots=True)
class ReviewBurdenQualitySummary:
    """Quality is reported independently of review-burden coverage."""

    recorded_count: int
    mean_quality_score: float | None


@dataclass(frozen=True, slots=True)
class ReviewBurdenAggregate:
    """Pure aggregate of content-free episode facts."""

    episode_count: int
    recorded_coverage: Mapping[str, int]
    numeric_metrics: Mapping[str, ReviewBurdenMetricSummary]
    state_counts: Mapping[str, Mapping[str, int]]
    quality: ReviewBurdenQualitySummary

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "recorded_coverage", MappingProxyType(dict(self.recorded_coverage))
        )
        object.__setattr__(
            self, "numeric_metrics", MappingProxyType(dict(self.numeric_metrics))
        )
        object.__setattr__(
            self,
            "state_counts",
            MappingProxyType(
                {
                    key: MappingProxyType(dict(counts))
                    for key, counts in self.state_counts.items()
                }
            ),
        )

    @property
    def quality_mean(self) -> float | None:
        """Compatibility-friendly direct view; still separate from coverage."""

        return self.quality.mean_quality_score


def _value(item: object, field: str) -> object:
    if isinstance(item, ReviewBurdenMetrics):
        return getattr(item, field)
    return getattr(item, field, NOT_RUN if field in REVIEW_BURDEN_STATE_FIELDS else NOT_RECORDED)


def aggregate_review_burden(
    episodes: Iterable[object],
) -> ReviewBurdenAggregate:
    """Aggregate episodes without mutating or deriving burden from quality."""

    items = tuple(episodes)
    coverage: dict[str, int] = {}
    numeric: dict[str, ReviewBurdenMetricSummary] = {}
    for field in REVIEW_BURDEN_NUMERIC_FIELDS:
        values = [
            value
            for item in items
            if (value := _value(item, field)) not in _MISSING
        ]
        total: int | float | None = sum(values) if values else None  # type: ignore[arg-type]
        numeric[field] = ReviewBurdenMetricSummary(
            recorded_count=len(values),
            missing_count=len(items) - len(values),
            total=total,
            mean=(float(total) / len(values)) if values else None,
        )
        coverage[field] = len(values)

    state_counts: dict[str, dict[str, int]] = {}
    for field in REVIEW_BURDEN_STATE_FIELDS:
        counts: dict[str, int] = {}
        for item in items:
            state = str(_value(item, field))
            counts[state] = counts.get(state, 0) + 1
        state_counts[field] = dict(sorted(counts.items()))
        coverage[field] = sum(
            count for state, count in counts.items() if state not in _MISSING
        )

    quality_values = [
        getattr(item, "quality_score")
        for item in items
        if isinstance(getattr(item, "quality_score", None), (int, float))
        and not isinstance(getattr(item, "quality_score", None), bool)
    ]
    quality = ReviewBurdenQualitySummary(
        recorded_count=len(quality_values),
        mean_quality_score=(sum(quality_values) / len(quality_values))
        if quality_values
        else None,
    )
    return ReviewBurdenAggregate(
        episode_count=len(items),
        recorded_coverage=coverage,
        numeric_metrics=numeric,
        state_counts=state_counts,
        quality=quality,
    )


aggregate_review_burden_metrics = aggregate_review_burden
OrganizationEpisodeReviewBurdenMetrics = ReviewBurdenMetrics
