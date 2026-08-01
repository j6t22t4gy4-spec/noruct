"""Conservative combination of a central task prior and local observations.

This is a recommendation signal only.  It neither selects a route nor treats a
missing observation as a performance claim.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class PosteriorStatus(StrEnum):
    NO_LOCAL_EVIDENCE = "NO_LOCAL_EVIDENCE"
    TASK_CLASS_MISMATCH = "TASK_CLASS_MISMATCH"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    HIGH_UNCERTAINTY = "HIGH_UNCERTAINTY"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    BOUNDED_LOCAL_CORRECTION = "BOUNDED_LOCAL_CORRECTION"


def _probability(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite probability")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field_name} must be a finite probability")
    return number


def _task_class(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(char.isspace() for char in value):
        raise ValueError("task_class must be a non-empty opaque token")
    return value


@dataclass(frozen=True, slots=True)
class CentralPrior:
    task_class: str
    score: float
    uncertainty: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_class", _task_class(self.task_class))
        object.__setattr__(self, "score", _probability(self.score, "score"))
        object.__setattr__(self, "uncertainty", _probability(self.uncertainty, "uncertainty"))


@dataclass(frozen=True, slots=True)
class LocalPosteriorEvidence:
    task_class: str
    sample_count: int
    score: float
    uncertainty: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_class", _task_class(self.task_class))
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 0:
            raise ValueError("sample_count must be a non-negative integer")
        object.__setattr__(self, "score", _probability(self.score, "score"))
        object.__setattr__(self, "uncertainty", _probability(self.uncertainty, "uncertainty"))


@dataclass(frozen=True, slots=True)
class PosteriorRecommendation:
    task_class: str
    central_score: float
    score: float
    status: PosteriorStatus

    @property
    def correction_applied(self) -> bool:
        return self.status is PosteriorStatus.BOUNDED_LOCAL_CORRECTION


@dataclass(frozen=True, slots=True)
class PosteriorPolicy:
    minimum_sample: int = 8
    maximum_uncertainty: float = 0.20
    maximum_disagreement: float = 0.25
    maximum_adjustment: float = 0.10

    def __post_init__(self) -> None:
        if isinstance(self.minimum_sample, bool) or not isinstance(self.minimum_sample, int) or self.minimum_sample < 1:
            raise ValueError("minimum_sample must be positive")
        for name in ("maximum_uncertainty", "maximum_disagreement", "maximum_adjustment"):
            object.__setattr__(self, name, _probability(getattr(self, name), name))


def resolve_local_posterior(
    prior: CentralPrior, evidence: LocalPosteriorEvidence | None, policy: PosteriorPolicy = PosteriorPolicy()
) -> PosteriorRecommendation:
    """Apply only agreement-backed, low-uncertainty local correction."""

    if not isinstance(prior, CentralPrior) or not isinstance(policy, PosteriorPolicy):
        raise TypeError("central prior and posterior policy are required")
    if evidence is None:
        return PosteriorRecommendation(prior.task_class, prior.score, prior.score, PosteriorStatus.NO_LOCAL_EVIDENCE)
    if not isinstance(evidence, LocalPosteriorEvidence):
        raise TypeError("local evidence must be typed")
    if evidence.task_class != prior.task_class:
        return PosteriorRecommendation(prior.task_class, prior.score, prior.score, PosteriorStatus.TASK_CLASS_MISMATCH)
    if evidence.sample_count < policy.minimum_sample:
        return PosteriorRecommendation(prior.task_class, prior.score, prior.score, PosteriorStatus.INSUFFICIENT_SAMPLE)
    if max(prior.uncertainty, evidence.uncertainty) > policy.maximum_uncertainty:
        return PosteriorRecommendation(prior.task_class, prior.score, prior.score, PosteriorStatus.HIGH_UNCERTAINTY)
    delta = evidence.score - prior.score
    if abs(delta) > policy.maximum_disagreement:
        return PosteriorRecommendation(prior.task_class, prior.score, prior.score, PosteriorStatus.CONFLICTING_EVIDENCE)
    bounded_delta = max(-policy.maximum_adjustment, min(policy.maximum_adjustment, delta))
    return PosteriorRecommendation(prior.task_class, prior.score, prior.score + bounded_delta, PosteriorStatus.BOUNDED_LOCAL_CORRECTION)
