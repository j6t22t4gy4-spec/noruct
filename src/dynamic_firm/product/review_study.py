"""Content-free, provider-free fixtures for a future review-summary study.

This module deliberately has no runtime, provider, persistence, or transport
dependencies.  Its observations are synthetic-only and contain labels and
bounded numeric measures, never a summary or other user content.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


PROTOCOL_VERSION = "noruct.review-study.v1"
SYNTHETIC_STUDY_MARKER = "SYNTHETIC_STUDY"
NO_HUMAN_STUDY_OR_OUTCOME_CLAIM = "NO_HUMAN_STUDY_OR_OUTCOME_CLAIM"

_PHASES = frozenset({"before", "after"})
_RAW_CONTENT_NAMES = frozenset(
    {
        "content",
        "prompt",
        "transcript",
        "message",
        "messages",
        "raw_content",
        "raw_summary",
        "tool_output",
        "repository",
        "path",
        "memory",
        "credential",
        "secret",
        "token",
    }
)


def _content_free_label(name: str, value: str, *, max_length: int = 64) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{name} must be a non-empty bounded label")
    if name.lower() in _RAW_CONTENT_NAMES or any(
        token in value.lower() for token in _RAW_CONTENT_NAMES
    ):
        raise ValueError(f"{name} must be a content-free label")
    if any(character in value for character in "\r\n"):
        raise ValueError(f"{name} must be a single-line label")
    return value


@dataclass(frozen=True)
class ReviewStudyProtocol:
    """The immutable protocol boundary for synthetic local observations."""

    version: str = PROTOCOL_VERSION
    marker: str = SYNTHETIC_STUDY_MARKER
    claim_boundary: str = NO_HUMAN_STUDY_OR_OUTCOME_CLAIM

    def __post_init__(self) -> None:
        if self.version != PROTOCOL_VERSION:
            raise ValueError("unsupported review-study protocol")
        if self.marker != SYNTHETIC_STUDY_MARKER:
            raise ValueError("the synthetic-study marker is required")
        if self.claim_boundary != NO_HUMAN_STUDY_OR_OUTCOME_CLAIM:
            raise ValueError("the no-claim boundary is required")


@dataclass(frozen=True)
class ReviewStudyObservation:
    """One synthetic, content-free before/after observation."""

    phase: str
    purpose: str
    ai_scope: str
    review_focus: str
    unverified_item_correct: bool
    review_time_seconds: float
    reopened_evidence_count: int
    rework_count: int
    approval_friction_count: int
    synthetic_study_marker: str = SYNTHETIC_STUDY_MARKER
    protocol_version: str = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("unsupported review-study protocol")
        if self.synthetic_study_marker != SYNTHETIC_STUDY_MARKER:
            raise ValueError("observations require the synthetic-study marker")
        if self.phase not in _PHASES:
            raise ValueError("phase must be 'before' or 'after'")
        for name in ("purpose", "ai_scope", "review_focus"):
            _content_free_label(name, getattr(self, name))
        if not isinstance(self.unverified_item_correct, bool):
            raise TypeError("unverified_item_correct must be boolean")
        if isinstance(self.review_time_seconds, bool) or not isinstance(
            self.review_time_seconds, (int, float)
        ):
            raise TypeError("review_time_seconds must be numeric")
        if self.review_time_seconds < 0:
            raise ValueError("review_time_seconds must not be negative")
        for name in (
            "reopened_evidence_count",
            "rework_count",
            "approval_friction_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "ReviewStudyObservation":
        """Build from an allowlisted mapping and reject raw-content fields."""

        forbidden = _RAW_CONTENT_NAMES.intersection(values)
        if forbidden:
            raise ValueError(f"raw content fields are not accepted: {sorted(forbidden)}")
        allowed = {
            "phase",
            "purpose",
            "ai_scope",
            "review_focus",
            "unverified_item_correct",
            "review_time_seconds",
            "reopened_evidence_count",
            "rework_count",
            "approval_friction_count",
            "synthetic_study_marker",
            "protocol_version",
        }
        unknown = set(values).difference(allowed)
        if unknown:
            raise ValueError(f"unsupported observation fields: {sorted(unknown)}")
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PhaseSummary:
    """Immutable aggregate for one explicit protocol phase."""

    phase: str
    observation_count: int
    correct_unverified_item_count: int
    correct_unverified_item_rate: float
    mean_review_time_seconds: float
    mean_reopened_evidence_count: float
    mean_rework_count: float
    mean_approval_friction_count: float


@dataclass(frozen=True)
class ReviewStudyAggregation:
    """Provider-free comparison with an explicit no-claims boundary."""

    protocol_version: str
    phases: Mapping[str, PhaseSummary]
    deltas_after_minus_before: Mapping[str, float]
    claim_boundary: str = NO_HUMAN_STUDY_OR_OUTCOME_CLAIM

    def __post_init__(self) -> None:
        object.__setattr__(self, "phases", MappingProxyType(dict(self.phases)))
        object.__setattr__(
            self,
            "deltas_after_minus_before",
            MappingProxyType(dict(self.deltas_after_minus_before)),
        )
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("unsupported review-study protocol")
        if self.claim_boundary != NO_HUMAN_STUDY_OR_OUTCOME_CLAIM:
            raise ValueError("aggregation cannot make a human-study or outcome claim")

    @property
    def human_study_claim(self) -> bool:
        return False

    @property
    def outcome_claim(self) -> bool:
        return False


def aggregate_observations(
    observations: Iterable[ReviewStudyObservation],
) -> ReviewStudyAggregation:
    """Aggregate explicit synthetic observations, grouped only by before/after."""

    grouped: dict[str, list[ReviewStudyObservation]] = {"before": [], "after": []}
    for observation in observations:
        if not isinstance(observation, ReviewStudyObservation):
            raise TypeError("aggregation accepts ReviewStudyObservation values only")
        grouped[observation.phase].append(observation)

    phases: dict[str, PhaseSummary] = {}
    for phase, items in grouped.items():
        if not items:
            continue
        count = len(items)
        correct = sum(item.unverified_item_correct for item in items)
        phases[phase] = PhaseSummary(
            phase=phase,
            observation_count=count,
            correct_unverified_item_count=correct,
            correct_unverified_item_rate=correct / count,
            mean_review_time_seconds=sum(item.review_time_seconds for item in items) / count,
            mean_reopened_evidence_count=sum(
                item.reopened_evidence_count for item in items
            )
            / count,
            mean_rework_count=sum(item.rework_count for item in items) / count,
            mean_approval_friction_count=sum(
                item.approval_friction_count for item in items
            )
            / count,
        )

    deltas: dict[str, float] = {}
    if "before" in phases and "after" in phases:
        before = phases["before"]
        after = phases["after"]
        deltas = {
            "correct_unverified_item_rate": after.correct_unverified_item_rate
            - before.correct_unverified_item_rate,
            "mean_review_time_seconds": after.mean_review_time_seconds
            - before.mean_review_time_seconds,
            "mean_reopened_evidence_count": after.mean_reopened_evidence_count
            - before.mean_reopened_evidence_count,
            "mean_rework_count": after.mean_rework_count - before.mean_rework_count,
            "mean_approval_friction_count": after.mean_approval_friction_count
            - before.mean_approval_friction_count,
        }
    return ReviewStudyAggregation(PROTOCOL_VERSION, phases, deltas)


def synthetic_review_study_fixture() -> tuple[ReviewStudyObservation, ...]:
    """Return deterministic local fixtures; no provider or user content is used."""

    return (
        ReviewStudyObservation(
            phase="before",
            purpose="identify-purpose",
            ai_scope="identify-ai-scope",
            review_focus="identify-review-focus",
            unverified_item_correct=True,
            review_time_seconds=90,
            reopened_evidence_count=2,
            rework_count=2,
            approval_friction_count=1,
        ),
        ReviewStudyObservation(
            phase="before",
            purpose="identify-purpose",
            ai_scope="identify-ai-scope",
            review_focus="identify-review-focus",
            unverified_item_correct=False,
            review_time_seconds=110,
            reopened_evidence_count=1,
            rework_count=1,
            approval_friction_count=2,
        ),
        ReviewStudyObservation(
            phase="after",
            purpose="identify-purpose",
            ai_scope="identify-ai-scope",
            review_focus="identify-review-focus",
            unverified_item_correct=True,
            review_time_seconds=70,
            reopened_evidence_count=1,
            rework_count=1,
            approval_friction_count=1,
        ),
        ReviewStudyObservation(
            phase="after",
            purpose="identify-purpose",
            ai_scope="identify-ai-scope",
            review_focus="identify-review-focus",
            unverified_item_correct=True,
            review_time_seconds=80,
            reopened_evidence_count=0,
            rework_count=0,
            approval_friction_count=0,
        ),
    )


def simulate_review_study() -> ReviewStudyAggregation:
    """Aggregate the deterministic fixtures locally."""

    return aggregate_observations(synthetic_review_study_fixture())


# Short aliases keep the schema convenient for local fixture authors while the
# descriptive names remain the canonical API.
Observation = ReviewStudyObservation
Aggregation = ReviewStudyAggregation
Protocol = ReviewStudyProtocol
