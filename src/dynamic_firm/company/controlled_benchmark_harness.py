"""Provider-free controlled comparison harness for local synthetic benchmarks.

The harness deliberately compares strategies only after every supplied result
is bound to one immutable, content-free task/tool/context/resource envelope.
It reports separate dimensions and never derives an organizational score,
winner, or ranking.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Mapping


CONTROLLED_BENCHMARK_SCHEMA = "noruct.controlled-benchmark-comparison.v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")


class BenchmarkStrategy(StrEnum):
    """The four comparands permitted by this bounded harness."""

    STRONG_SOLO = "STRONG_SOLO"
    SAME_MODEL_BEST_OF_N = "SAME_MODEL_BEST_OF_N"
    HETEROGENEOUS_MULTI_PROVIDER = "HETEROGENEOUS_MULTI_PROVIDER"
    MANAGER_LED = "MANAGER_LED"


class ObservationAvailability(StrEnum):
    """An unavailable observation is distinct from an observed numeric zero."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class DataEgressClass(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    RESTRICTED = "RESTRICTED"


def _digest(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase sha256 digest")
    return value


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded opaque identifier")
    return value


def _finite(value: object, *, field_name: str, minimum: float = 0.0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite numeric value")
    number = float(value)
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise ValueError(f"{field_name} is outside its finite permitted range")
    return number


def _observation(
    availability: ObservationAvailability,
    value: object,
    *,
    field_name: str,
) -> float | None:
    if availability is ObservationAvailability.UNAVAILABLE:
        if value is not None:
            raise ValueError(f"{field_name} must be null when unavailable")
        return None
    if value is None:
        raise ValueError(f"{field_name} requires a numeric value when available")
    return _finite(value, field_name=field_name)


def _strict_mapping(value: object, *, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("canonical payload has missing or unknown fields")
    return value


@dataclass(frozen=True, slots=True)
class ControlledScenarioEnvelope:
    """Digests of the exact conditions that every strategy must share."""

    scenario_id: str
    task_digest: str
    tool_envelope_digest: str
    context_envelope_digest: str
    resource_envelope_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _identifier(self.scenario_id, field_name="scenario_id"))
        for name in (
            "task_digest",
            "tool_envelope_digest",
            "context_envelope_digest",
            "resource_envelope_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), field_name=name))

    def canonical_payload(self) -> dict[str, str]:
        return {
            "scenario_id": self.scenario_id,
            "task_digest": self.task_digest,
            "tool_envelope_digest": self.tool_envelope_digest,
            "context_envelope_digest": self.context_envelope_digest,
            "resource_envelope_digest": self.resource_envelope_digest,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SyntheticStrategyResult:
    """A synthetic result with separately observable comparison dimensions.

    ``error_correlation`` is a signed Pearson-style error coefficient, not a
    simultaneous-error rate.  It may therefore be negative.
    """

    strategy: BenchmarkStrategy
    envelope: ControlledScenarioEnvelope
    quality: float
    complete_failure: bool
    cost_availability: ObservationAvailability
    cost_usd: float | None
    latency_availability: ObservationAvailability
    latency_ms: float | None
    error_correlation: float
    data_egress_class: DataEgressClass
    human_review_minutes: float

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, BenchmarkStrategy):
            object.__setattr__(self, "strategy", BenchmarkStrategy(self.strategy))
        if not isinstance(self.envelope, ControlledScenarioEnvelope):
            raise TypeError("envelope must be a ControlledScenarioEnvelope")
        if not isinstance(self.complete_failure, bool):
            raise ValueError("complete_failure must be boolean")
        object.__setattr__(self, "quality", _finite(self.quality, field_name="quality", maximum=1.0))
        if self.complete_failure and self.quality != 0.0:
            raise ValueError("complete failures must report zero quality")
        for name in ("cost_availability", "latency_availability"):
            value = getattr(self, name)
            if not isinstance(value, ObservationAvailability):
                object.__setattr__(self, name, ObservationAvailability(value))
        object.__setattr__(self, "cost_usd", _observation(self.cost_availability, self.cost_usd, field_name="cost_usd"))
        object.__setattr__(self, "latency_ms", _observation(self.latency_availability, self.latency_ms, field_name="latency_ms"))
        object.__setattr__(self, "error_correlation", _finite(self.error_correlation, field_name="error_correlation", minimum=-1.0, maximum=1.0))
        if not isinstance(self.data_egress_class, DataEgressClass):
            object.__setattr__(self, "data_egress_class", DataEgressClass(self.data_egress_class))
        object.__setattr__(self, "human_review_minutes", _finite(self.human_review_minutes, field_name="human_review_minutes"))

    def canonical_row(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.value,
            "quality": self.quality,
            "complete_failure": self.complete_failure,
            "cost_availability": self.cost_availability.value,
            "cost_usd": self.cost_usd,
            "latency_availability": self.latency_availability.value,
            "latency_ms": self.latency_ms,
            "error_correlation": self.error_correlation,
            "data_egress_class": self.data_egress_class.value,
            "human_review_minutes": self.human_review_minutes,
        }

    @classmethod
    def from_canonical_row(cls, value: object, *, envelope: ControlledScenarioEnvelope) -> "SyntheticStrategyResult":
        values = _strict_mapping(
            value,
            fields=frozenset(
                {
                    "strategy",
                    "quality",
                    "complete_failure",
                    "cost_availability",
                    "cost_usd",
                    "latency_availability",
                    "latency_ms",
                    "error_correlation",
                    "data_egress_class",
                    "human_review_minutes",
                }
            ),
        )
        return cls(envelope=envelope, **values)


@dataclass(frozen=True, slots=True)
class ComparisonMatrix:
    """Canonical rows only; deliberately contains no aggregate winner or rank."""

    envelope: ControlledScenarioEnvelope
    rows: tuple[SyntheticStrategyResult, ...]
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, ControlledScenarioEnvelope):
            raise TypeError("envelope must be a ControlledScenarioEnvelope")
        rows = tuple(self.rows)
        if len(rows) != len(BenchmarkStrategy):
            raise ValueError("one result is required for every benchmark strategy")
        if any(not isinstance(row, SyntheticStrategyResult) for row in rows):
            raise TypeError("rows must be SyntheticStrategyResult values")
        if any(row.envelope != self.envelope for row in rows):
            raise ValueError("every strategy result must use the identical scenario envelope")
        strategies = tuple(row.strategy for row in rows)
        if len(set(strategies)) != len(strategies):
            raise ValueError("duplicate benchmark strategy results are forbidden")
        if set(strategies) != set(BenchmarkStrategy):
            raise ValueError("comparison must contain the four required strategies")
        ordered = tuple(sorted(rows, key=lambda row: row.strategy.value))
        object.__setattr__(self, "rows", ordered)
        object.__setattr__(self, "content_digest", hashlib.sha256(self.canonical_bytes()).hexdigest())

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": CONTROLLED_BENCHMARK_SCHEMA,
            "envelope": self.envelope.canonical_payload(),
            "rows": [row.canonical_row() for row in self.rows],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    @classmethod
    def from_canonical_json(cls, raw: object) -> "ComparisonMatrix":
        if not isinstance(raw, str):
            raise TypeError("canonical JSON must be text")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid canonical JSON") from error
        values = _strict_mapping(value, fields=frozenset({"schema", "envelope", "rows"}))
        if values["schema"] != CONTROLLED_BENCHMARK_SCHEMA:
            raise ValueError("unexpected controlled benchmark schema")
        envelope_values = _strict_mapping(
            values["envelope"],
            fields=frozenset(
                {
                    "scenario_id",
                    "task_digest",
                    "tool_envelope_digest",
                    "context_envelope_digest",
                    "resource_envelope_digest",
                }
            ),
        )
        envelope = ControlledScenarioEnvelope(**envelope_values)
        if not isinstance(values["rows"], list):
            raise ValueError("rows must be a canonical list")
        matrix = cls(
            envelope=envelope,
            rows=tuple(SyntheticStrategyResult.from_canonical_row(row, envelope=envelope) for row in values["rows"]),
        )
        if raw != matrix.canonical_json():
            raise ValueError("noncanonical controlled benchmark serialization")
        return matrix


@dataclass(frozen=True, slots=True)
class ControlledBenchmarkHarness:
    """Evaluates supplied synthetic rows without providers, tools, or ranking."""

    envelope: ControlledScenarioEnvelope

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, ControlledScenarioEnvelope):
            raise TypeError("envelope must be a ControlledScenarioEnvelope")

    def compare(self, results: Iterable[SyntheticStrategyResult]) -> ComparisonMatrix:
        return ComparisonMatrix(envelope=self.envelope, rows=tuple(results))

    def evaluate(self, results: Iterable[SyntheticStrategyResult]) -> ComparisonMatrix:
        """Alias with the same deterministic, provider-free comparison semantics."""

        return self.compare(results)
