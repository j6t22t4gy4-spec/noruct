"""Strict content-free local outcome observations for future analysis only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


_FORBIDDEN_FIELDS = frozenset(
    {"prompt", "artifact", "workspace", "customer", "user", "content", "model", "provider", "credential", "error"}
)
_FIELDS = frozenset(
    {
        "observation_id", "task_class", "terminal_status", "validation_failed", "rework_required",
        "tool_failure", "structured_failure", "latency_availability", "latency_ms",
        "usage_availability", "usage_units",
    }
)


class ObservationAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class TerminalStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(character.isspace() for character in value):
        raise ValueError(f"{name} must be a non-empty content-free token")
    return value


def _nonnegative(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class LocalOutcomeObservation:
    observation_id: str
    task_class: str
    terminal_status: TerminalStatus
    validation_failed: bool
    rework_required: bool
    tool_failure: bool
    structured_failure: bool
    latency_availability: ObservationAvailability
    latency_ms: int | None
    usage_availability: ObservationAvailability
    usage_units: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _token(self.observation_id, "observation_id"))
        object.__setattr__(self, "task_class", _token(self.task_class, "task_class"))
        for field in ("terminal_status", "latency_availability", "usage_availability"):
            enum = {"terminal_status": TerminalStatus, "latency_availability": ObservationAvailability,
                    "usage_availability": ObservationAvailability}[field]
            value = getattr(self, field)
            if not isinstance(value, enum):
                object.__setattr__(self, field, enum(value))
        for field in ("validation_failed", "rework_required", "tool_failure", "structured_failure"):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"{field} must be boolean")
        self._validate_measurement("latency", self.latency_availability, self.latency_ms)
        self._validate_measurement("usage", self.usage_availability, self.usage_units)

    @staticmethod
    def _validate_measurement(name: str, availability: ObservationAvailability, value: int | None) -> None:
        if availability is ObservationAvailability.UNAVAILABLE:
            if value is not None:
                raise ValueError(f"unavailable {name} must not have a numeric value")
        elif value is None:
            raise ValueError(f"available {name} requires a numeric value")
        else:
            _nonnegative(value, f"{name}_value")

    @classmethod
    def from_mapping(cls, value: object) -> "LocalOutcomeObservation":
        if not isinstance(value, Mapping):
            raise ValueError("local outcome observation must be a mapping")
        lowered = {str(key).lower() for key in value}
        if lowered & _FORBIDDEN_FIELDS or set(value) != _FIELDS:
            raise ValueError("local outcome observation has forbidden or unknown fields")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class LocalOutcomeSummary:
    task_class: str
    sample_count: int
    minimum_sample_met: bool
    terminal_counts: tuple[tuple[TerminalStatus, int], ...]
    validation_failures: int
    rework_count: int
    tool_failures: int
    structured_failures: int
    observed_latency_count: int
    observed_usage_count: int


class LocalOutcomeAggregate:
    """Idempotent in-memory collector; it takes no routing action."""

    def __init__(self) -> None:
        self._observations: dict[str, LocalOutcomeObservation] = {}

    def record(self, observation: LocalOutcomeObservation) -> bool:
        if not isinstance(observation, LocalOutcomeObservation):
            raise TypeError("local outcome observation is required")
        existing = self._observations.get(observation.observation_id)
        if existing is None:
            self._observations[observation.observation_id] = observation
            return True
        if existing != observation:
            raise ValueError("observation_id replay conflicts with existing content-free observation")
        return False

    def summary(self, task_class: object, *, minimum_sample: object = 3) -> LocalOutcomeSummary:
        task_class = _token(task_class, "task_class")
        minimum_sample = _nonnegative(minimum_sample, "minimum_sample")
        if minimum_sample < 1:
            raise ValueError("minimum_sample must be positive")
        values = tuple(item for item in self._observations.values() if item.task_class == task_class)
        counts = tuple((status, sum(item.terminal_status is status for item in values)) for status in TerminalStatus)
        return LocalOutcomeSummary(
            task_class=task_class, sample_count=len(values), minimum_sample_met=len(values) >= minimum_sample,
            terminal_counts=counts, validation_failures=sum(item.validation_failed for item in values),
            rework_count=sum(item.rework_required for item in values), tool_failures=sum(item.tool_failure for item in values),
            structured_failures=sum(item.structured_failure for item in values),
            observed_latency_count=sum(item.latency_availability is ObservationAvailability.AVAILABLE for item in values),
            observed_usage_count=sum(item.usage_availability is ObservationAvailability.AVAILABLE for item in values),
        )
