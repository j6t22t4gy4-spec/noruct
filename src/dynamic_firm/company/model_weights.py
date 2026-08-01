"""Versioned, data-only route preference and eligibility projection."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum


MODEL_WEIGHT_PROFILE_SCHEMA = "noruct.model-weight-profile.v1"
MISSING_SIGNAL_PENALTY = 0.05


class ModelWeightProfile(StrEnum):
    QUALITY_FIRST = "QUALITY_FIRST"
    BALANCED = "BALANCED"
    EFFICIENT = "EFFICIENT"
    PRIVATE_LOCAL_FIRST = "PRIVATE_LOCAL_FIRST"


_WEIGHTS = {
    ModelWeightProfile.QUALITY_FIRST: (0.55, 0.25, 0.15, 0.05),
    ModelWeightProfile.BALANCED: (0.40, 0.30, 0.20, 0.10),
    ModelWeightProfile.EFFICIENT: (0.25, 0.25, 0.20, 0.30),
    ModelWeightProfile.PRIVATE_LOCAL_FIRST: (0.35, 0.25, 0.30, 0.10),
}


@dataclass(frozen=True, slots=True)
class VersionedWeightProfile:
    profile: ModelWeightProfile
    version: str = "v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile", ModelWeightProfile(self.profile))
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("weight profile version is required")

    def canonical_payload(self) -> dict[str, object]:
        weights = _WEIGHTS[self.profile]
        if not math.isclose(sum(weights), 1.0):
            raise ValueError("profile weights must sum to one")
        return {
            "schema": MODEL_WEIGHT_PROFILE_SCHEMA,
            "version": self.version,
            "profile": self.profile.value,
            "quality_weight": weights[0],
            "reliability_weight": weights[1],
            "latency_weight": weights[2],
            "cost_weight": weights[3],
            "missing_signal_penalty": MISSING_SIGNAL_PENALTY,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":")).encode()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _signal(value: float | None, *, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a finite normalized number in [0, 1]")
    return float(value)


@dataclass(frozen=True, slots=True)
class RouteSignals:
    route_id: str
    simpler_rank: int
    eligible_authority: bool
    eligible_capability: bool
    eligible_egress: bool
    quality: float | None
    reliability: float | None
    latency: float | None
    cost: float | None
    uncertainty: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.route_id, str) or not self.route_id:
            raise ValueError("route_id is required")
        if isinstance(self.simpler_rank, bool) or not isinstance(self.simpler_rank, int) or self.simpler_rank < 0:
            raise ValueError("simpler_rank must be a non-negative integer")
        object.__setattr__(self, "quality", _signal(self.quality, name="quality"))
        object.__setattr__(self, "reliability", _signal(self.reliability, name="reliability"))
        object.__setattr__(self, "latency", _signal(self.latency, name="latency"))
        object.__setattr__(self, "cost", _signal(self.cost, name="cost"))
        object.__setattr__(self, "uncertainty", _signal(self.uncertainty, name="uncertainty"))

    @property
    def eligible(self) -> bool:
        return self.eligible_authority and self.eligible_capability and self.eligible_egress


def _score(route: RouteSignals, profile: VersionedWeightProfile) -> float:
    weights = profile.canonical_payload()
    quality = route.quality
    reliability = route.reliability
    assert quality is not None and reliability is not None
    score = float(weights["quality_weight"]) * quality + float(weights["reliability_weight"]) * reliability
    if route.latency is None:
        score -= MISSING_SIGNAL_PENALTY
    else:
        score += float(weights["latency_weight"]) * route.latency
    if route.cost is None:
        score -= MISSING_SIGNAL_PENALTY
    else:
        score -= float(weights["cost_weight"]) * route.cost
    return score


def select_route(candidates: tuple[RouteSignals, ...], profile: VersionedWeightProfile) -> RouteSignals | None:
    """Apply hard eligibility first; neither cost nor missing data selects alone."""

    eligible = tuple(route for route in candidates if route.eligible and route.quality is not None and route.reliability is not None)
    if not eligible:
        return None
    leader = max(eligible, key=lambda route: _score(route, profile))
    leader_score = _score(leader, profile)
    tied = tuple(
        route for route in eligible
        if leader_score - _score(route, profile) <= max(leader.uncertainty, route.uncertainty)
    )
    return min(tied, key=lambda route: route.simpler_rank)
