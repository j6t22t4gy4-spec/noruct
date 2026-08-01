"""Immutable Organization Fit Profile projection.

First-party gap decision: the registered reference sources cover runtime,
coordination, and operator concerns, not this Noruct-specific eight-dimension
projection.  No external source, dependency, score, inference, admission
path, or mutable authority is needed; the schema is intentionally local and
serialization-only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


ORGANIZATION_FIT_PROFILE_SCHEMA = "noruct.organization-fit-profile.v1"


class OrganizationFitLevel(StrEnum):
    """The only values permitted for an OrganizationFitProfile dimension."""

    LOW = "LOW"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


_DIMENSION_NAMES = (
    "decomposability",
    "dependency_coupling",
    "context_coupling",
    "information_dispersion",
    "verifiability",
    "risk_irreversibility",
    "error_correlation",
    "latency_sensitivity",
)
_KNOWN_INPUT_FIELDS = frozenset((*_DIMENSION_NAMES, "schema"))


def _level(value: object) -> OrganizationFitLevel:
    if isinstance(value, OrganizationFitLevel):
        return value
    if isinstance(value, str):
        try:
            return OrganizationFitLevel(value)
        except ValueError:
            pass
    raise ValueError("Organization Fit Profile dimensions must be LOW, HIGH, or UNKNOWN")


@dataclass(frozen=True, slots=True)
class OrganizationFitProfile:
    """Read-only fit evidence; it owns no Company or execution authority."""

    decomposability: OrganizationFitLevel = OrganizationFitLevel.UNKNOWN
    dependency_coupling: OrganizationFitLevel = OrganizationFitLevel.UNKNOWN
    context_coupling: OrganizationFitLevel = OrganizationFitLevel.UNKNOWN
    information_dispersion: OrganizationFitLevel = OrganizationFitLevel.UNKNOWN
    verifiability: OrganizationFitLevel = OrganizationFitLevel.UNKNOWN
    risk_irreversibility: OrganizationFitLevel = OrganizationFitLevel.UNKNOWN
    error_correlation: OrganizationFitLevel = OrganizationFitLevel.UNKNOWN
    latency_sensitivity: OrganizationFitLevel = OrganizationFitLevel.UNKNOWN
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        for name in _DIMENSION_NAMES:
            object.__setattr__(self, name, _level(getattr(self, name)))
        object.__setattr__(self, "digest", hashlib.sha256(self.canonical_bytes()).hexdigest())

    def canonical_payload(self) -> dict[str, str]:
        """Return the versioned, fixed-field payload used for hashing."""

        return {
            "schema": ORGANIZATION_FIT_PROFILE_SCHEMA,
            **{name: getattr(self, name).value for name in _DIMENSION_NAMES},
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    @property
    def content_digest(self) -> str:
        """Digest alias used by other immutable Company projections."""

        return self.digest

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "OrganizationFitProfile":
        """Build a profile, defaulting omitted dimensions to ``UNKNOWN``."""

        if not isinstance(values, Mapping):
            raise TypeError("Organization Fit Profile input must be a mapping")
        unknown_fields = set(values) - _KNOWN_INPUT_FIELDS
        if unknown_fields:
            names = ", ".join(sorted(str(name) for name in unknown_fields))
            raise ValueError(f"Unknown Organization Fit Profile field(s): {names}")
        schema = values.get("schema", ORGANIZATION_FIT_PROFILE_SCHEMA)
        if schema != ORGANIZATION_FIT_PROFILE_SCHEMA:
            raise ValueError("Unsupported Organization Fit Profile schema")
        return cls(**{name: _level(values[name]) for name in _DIMENSION_NAMES if name in values})

    @classmethod
    def from_dict(cls, values: Mapping[str, object]) -> "OrganizationFitProfile":
        return cls.from_mapping(values)

    def to_dict(self) -> dict[str, str]:
        return self.canonical_payload()
