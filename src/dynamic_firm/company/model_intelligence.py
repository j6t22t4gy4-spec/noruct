"""Immutable, data-only Model Intelligence snapshot schema.

This module deliberately owns only the portable benchmark prior described by
ADR-0211.  It does not fetch, store, verify, activate, or route anything: a
caller can serialize an already supplied snapshot and bind its digest to a
later authority-owned decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping, Sequence


MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA = "noruct.model-intelligence-snapshot.v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_TASK_CLASSES = frozenset(
    {
        "coding",
        "repository_navigation",
        "research",
        "planning",
        "long_horizon_execution",
        "tool_reliability",
        "structured_output",
        "verification",
        "multilingual_cjk",
        "low_latency_interaction",
    }
)


class ModelIdentityAssurance(StrEnum):
    LOCAL_CONTENT_DIGEST = "LOCAL_CONTENT_DIGEST"
    IMMUTABLE_PROVIDER_REVISION = "IMMUTABLE_PROVIDER_REVISION"
    VERSIONED_MODEL_ID = "VERSIONED_MODEL_ID"
    FLOATING_ALIAS = "FLOATING_ALIAS"
    IDENTITY_UNKNOWN = "IDENTITY_UNKNOWN"


class ObservationAvailability(StrEnum):
    """Whether an observation exists; unavailable is never encoded as zero."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded opaque identifier")
    return value


def _timestamp(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError(f"{field_name} must be a UTC second-precision timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid UTC timestamp") from exc
    return value


def _probability(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number between zero and one")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field_name} must be a finite number between zero and one")
    return number


def _correlation_coefficient(value: object) -> float:
    """Validate a Pearson-style signed coefficient in the closed [-1, 1] range."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("correlation must be a finite coefficient between minus one and one")
    number = float(value)
    if not math.isfinite(number) or not -1.0 <= number <= 1.0:
        raise ValueError("correlation must be a finite coefficient between minus one and one")
    return number


def _nonnegative_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return number


@dataclass(frozen=True, slots=True)
class TaskClassDistribution:
    sample_count: int
    success_rate: float
    lower_bound: float
    upper_bound: float

    def __post_init__(self) -> None:
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 1:
            raise ValueError("task-class sample_count must be a positive integer")
        object.__setattr__(self, "success_rate", _probability(self.success_rate, field_name="success_rate"))
        object.__setattr__(self, "lower_bound", _probability(self.lower_bound, field_name="lower_bound"))
        object.__setattr__(self, "upper_bound", _probability(self.upper_bound, field_name="upper_bound"))
        if not self.lower_bound <= self.success_rate <= self.upper_bound:
            raise ValueError("task-class bounds must contain success_rate")

    def canonical_payload(self) -> dict[str, int | float]:
        return {
            "sample_count": self.sample_count,
            "success_rate": self.success_rate,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
        }


@dataclass(frozen=True, slots=True)
class ErrorCorrelationEvidence:
    """Pearson correlation coefficient between this and the compared model's errors.

    ``correlation`` is a signed statistical coefficient, not a simultaneous
    error probability or an unsigned overlap score: ``-1.0`` and ``1.0`` are
    both valid boundary values.
    """

    compared_model_id: str
    correlation: float
    sample_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "compared_model_id", _identifier(self.compared_model_id, field_name="compared_model_id"))
        object.__setattr__(self, "correlation", _correlation_coefficient(self.correlation))
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 1:
            raise ValueError("error-correlation sample_count must be a positive integer")

    def canonical_payload(self) -> dict[str, str | int | float]:
        return {
            "compared_model_id": self.compared_model_id,
            "correlation": self.correlation,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class CostLatencySource:
    region: str
    observed_at: str
    source_revision: str
    latency_availability: ObservationAvailability
    latency_ms_p50: float | None
    cost_availability: ObservationAvailability
    input_cost_per_million: float | None
    output_cost_per_million: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "region", _identifier(self.region, field_name="region"))
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, field_name="observed_at"))
        object.__setattr__(self, "source_revision", _identifier(self.source_revision, field_name="source_revision"))
        for name in ("latency_availability", "cost_availability"):
            value = getattr(self, name)
            if not isinstance(value, ObservationAvailability):
                object.__setattr__(self, name, ObservationAvailability(value))
        self._validate_observation(
            availability=self.latency_availability,
            values=(("latency_ms_p50", self.latency_ms_p50),),
        )
        self._validate_observation(
            availability=self.cost_availability,
            values=(
                ("input_cost_per_million", self.input_cost_per_million),
                ("output_cost_per_million", self.output_cost_per_million),
            ),
        )

    def _validate_observation(
        self,
        *,
        availability: ObservationAvailability,
        values: tuple[tuple[str, float | None], ...],
    ) -> None:
        if availability is ObservationAvailability.UNAVAILABLE:
            if any(value is not None for _, value in values):
                raise ValueError("unavailable observations must retain no numeric value")
            return
        for name, value in values:
            if value is None:
                raise ValueError("available observations require a numeric value")
            object.__setattr__(self, name, _nonnegative_number(value, field_name=name))

    def canonical_payload(self) -> dict[str, str | float | None]:
        return {
            "region": self.region,
            "observed_at": self.observed_at,
            "source_revision": self.source_revision,
            "latency_availability": self.latency_availability.value,
            "latency_ms_p50": self.latency_ms_p50,
            "cost_availability": self.cost_availability.value,
            "input_cost_per_million": self.input_cost_per_million,
            "output_cost_per_million": self.output_cost_per_million,
        }


def _strict_mapping(value: object, *, field_name: str, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise ValueError(f"{field_name} has unknown or missing fields")
    return value


def _task_distribution(value: object, *, task_class: str) -> TaskClassDistribution:
    values = _strict_mapping(
        value,
        field_name=f"task_class_distributions.{task_class}",
        fields=frozenset({"sample_count", "success_rate", "lower_bound", "upper_bound"}),
    )
    return TaskClassDistribution(**values)


def _error_correlation(value: object) -> ErrorCorrelationEvidence:
    values = _strict_mapping(
        value,
        field_name="error_correlation",
        fields=frozenset({"compared_model_id", "correlation", "sample_count"}),
    )
    return ErrorCorrelationEvidence(**values)


def _cost_latency_source(value: object) -> CostLatencySource:
    values = _strict_mapping(
        value,
        field_name="cost_latency_source",
        fields=frozenset(
            {
                "region",
                "observed_at",
                "source_revision",
                "latency_availability",
                "latency_ms_p50",
                "cost_availability",
                "input_cost_per_million",
                "output_cost_per_million",
            }
        ),
    )
    return CostLatencySource(**values)


@dataclass(frozen=True, slots=True)
class ModelIntelligenceSnapshot:
    """A pure, closed-schema benchmark prior with stable canonical bytes."""

    snapshot_id: str
    generated_at: str
    expires_at: str
    publisher_identity: str
    signature_reference: str
    benchmark_harness_revision: str
    dataset_revision: str
    evaluator_revision: str
    provider_route_class: str
    requested_model_id: str
    identity_assurance: ModelIdentityAssurance
    task_class_distributions: tuple[tuple[str, TaskClassDistribution], ...]
    error_correlation: tuple[ErrorCorrelationEvidence, ...]
    cost_latency_source: CostLatencySource
    limitations: tuple[str, ...]
    contamination_disclosure: str
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id", "publisher_identity", "signature_reference", "benchmark_harness_revision",
            "dataset_revision", "evaluator_revision", "provider_route_class", "requested_model_id",
            "contamination_disclosure",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), field_name=name))
        object.__setattr__(self, "generated_at", _timestamp(self.generated_at, field_name="generated_at"))
        object.__setattr__(self, "expires_at", _timestamp(self.expires_at, field_name="expires_at"))
        if self.expires_at <= self.generated_at:
            raise ValueError("expires_at must be later than generated_at")
        if not isinstance(self.identity_assurance, ModelIdentityAssurance):
            object.__setattr__(self, "identity_assurance", ModelIdentityAssurance(self.identity_assurance))
        distributions = tuple(self.task_class_distributions)
        task_names = tuple(name for name, _ in distributions)
        if not distributions or len(set(task_names)) != len(task_names) or not set(task_names) <= _TASK_CLASSES:
            raise ValueError("task_class_distributions must be a non-empty unique known task-class vector")
        if not all(isinstance(item, TaskClassDistribution) for _, item in distributions):
            raise TypeError("task_class_distributions must contain TaskClassDistribution values")
        object.__setattr__(self, "task_class_distributions", tuple(sorted(distributions)))
        correlations = tuple(self.error_correlation)
        if len({item.compared_model_id for item in correlations}) != len(correlations):
            raise ValueError("error_correlation compared_model_id values must be unique")
        if not all(isinstance(item, ErrorCorrelationEvidence) for item in correlations):
            raise TypeError("error_correlation must contain ErrorCorrelationEvidence values")
        object.__setattr__(self, "error_correlation", tuple(sorted(correlations, key=lambda item: item.compared_model_id)))
        if not isinstance(self.cost_latency_source, CostLatencySource):
            raise TypeError("cost_latency_source must be CostLatencySource")
        limitations = tuple(self.limitations)
        if not limitations or any(not isinstance(item, str) or not item or len(item) > 512 for item in limitations):
            raise ValueError("limitations must be non-empty bounded text statements")
        object.__setattr__(self, "limitations", limitations)
        object.__setattr__(self, "digest", hashlib.sha256(self.canonical_bytes()).hexdigest())

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA,
            "snapshot_id": self.snapshot_id,
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "publisher_identity": self.publisher_identity,
            "signature_reference": self.signature_reference,
            "benchmark_harness_revision": self.benchmark_harness_revision,
            "dataset_revision": self.dataset_revision,
            "evaluator_revision": self.evaluator_revision,
            "provider_route_class": self.provider_route_class,
            "requested_model_id": self.requested_model_id,
            "identity_assurance": self.identity_assurance.value,
            "task_class_distributions": {
                name: value.canonical_payload() for name, value in self.task_class_distributions
            },
            "error_correlation": [item.canonical_payload() for item in self.error_correlation],
            "cost_latency_source": self.cost_latency_source.canonical_payload(),
            "limitations": list(self.limitations),
            "contamination_disclosure": self.contamination_disclosure,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    @property
    def content_digest(self) -> str:
        return self.digest

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "ModelIntelligenceSnapshot":
        fields = frozenset(
            {
                "schema", "snapshot_id", "generated_at", "expires_at", "publisher_identity",
                "signature_reference", "benchmark_harness_revision", "dataset_revision", "evaluator_revision",
                "provider_route_class", "requested_model_id", "identity_assurance", "task_class_distributions",
                "error_correlation", "cost_latency_source", "limitations", "contamination_disclosure",
            }
        )
        source = _strict_mapping(values, field_name="ModelIntelligenceSnapshot", fields=fields)
        if source["schema"] != MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA:
            raise ValueError("Unsupported ModelIntelligenceSnapshot schema")
        task_values = source["task_class_distributions"]
        if not isinstance(task_values, Mapping):
            raise TypeError("task_class_distributions must be a mapping")
        if not set(task_values) <= _TASK_CLASSES:
            raise ValueError("task_class_distributions contains an unknown task class")
        correlation_values = source["error_correlation"]
        if isinstance(correlation_values, (str, bytes)) or not isinstance(correlation_values, Sequence):
            raise TypeError("error_correlation must be a sequence")
        limitations = source["limitations"]
        if isinstance(limitations, (str, bytes)) or not isinstance(limitations, Sequence):
            raise TypeError("limitations must be a sequence")
        return cls(
            snapshot_id=source["snapshot_id"], generated_at=source["generated_at"], expires_at=source["expires_at"],
            publisher_identity=source["publisher_identity"], signature_reference=source["signature_reference"],
            benchmark_harness_revision=source["benchmark_harness_revision"], dataset_revision=source["dataset_revision"],
            evaluator_revision=source["evaluator_revision"], provider_route_class=source["provider_route_class"],
            requested_model_id=source["requested_model_id"], identity_assurance=source["identity_assurance"],
            task_class_distributions=tuple((name, _task_distribution(value, task_class=name)) for name, value in task_values.items()),
            error_correlation=tuple(_error_correlation(value) for value in correlation_values),
            cost_latency_source=_cost_latency_source(source["cost_latency_source"]),
            limitations=tuple(limitations), contamination_disclosure=source["contamination_disclosure"],
        )

    @classmethod
    def from_dict(cls, values: Mapping[str, object]) -> "ModelIntelligenceSnapshot":
        return cls.from_mapping(values)

    def to_dict(self) -> dict[str, object]:
        return self.canonical_payload()
