"""Content-free, provider-free route compatibility evidence.

The contract accepts only synthetic adapter observations.  It does not invoke
providers, inspect credentials, or decide route selection; callers may cache
one bounded smoke result and must replace it when the adapter or model identity
materially changes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


ROUTE_COMPATIBILITY_SCHEMA = "noruct.route-compatibility-evidence.v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CAPABILITIES = (
    "auth",
    "endpoint",
    "model",
    "structured_output",
    "tool_round_trip",
    "stream_cancel",
)


class CapabilityState(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


class SafeErrorClassification(StrEnum):
    NONE = "NONE"
    AUTHENTICATION = "AUTHENTICATION"
    ENDPOINT = "ENDPOINT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"
    TOOL_ROUND_TRIP = "TOOL_ROUND_TRIP"
    STREAM_CANCEL = "STREAM_CANCEL"
    POLICY = "POLICY"
    UNKNOWN = "UNKNOWN"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded opaque identifier")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("material_identity_digest must be a lowercase sha256 digest")
    return value


def _state(value: object) -> CapabilityState:
    if not isinstance(value, str):
        raise ValueError("capability state must be a known string")
    try:
        return CapabilityState(value)
    except ValueError as exc:
        raise ValueError("capability state is unknown") from exc


def _safe_error(value: object) -> SafeErrorClassification:
    if not isinstance(value, str):
        raise ValueError("safe_error must be a known string")
    try:
        return SafeErrorClassification(value)
    except ValueError as exc:
        raise ValueError("safe_error is unknown") from exc


@dataclass(frozen=True, slots=True)
class CompatibilityEvidence:
    """An immutable smoke result, never a quality or cost benchmark."""

    route_id: str
    adapter_revision: str
    material_identity_digest: str
    states: tuple[tuple[str, CapabilityState], ...]
    safe_error: SafeErrorClassification = SafeErrorClassification.NONE

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_id", _identifier(self.route_id, "route_id"))
        object.__setattr__(self, "adapter_revision", _identifier(self.adapter_revision, "adapter_revision"))
        object.__setattr__(self, "material_identity_digest", _digest(self.material_identity_digest))
        if not isinstance(self.safe_error, SafeErrorClassification):
            object.__setattr__(self, "safe_error", _safe_error(self.safe_error))
        if not isinstance(self.states, tuple) or tuple(name for name, _ in self.states) != _CAPABILITIES:
            raise ValueError("states must contain exactly the ordered compatibility capabilities")
        if any(not isinstance(state, CapabilityState) for _, state in self.states):
            raise ValueError("states must contain known capability states")

    @classmethod
    def from_result(
        cls,
        route_id: object,
        adapter_revision: object,
        material_identity_digest: object,
        result: object,
        safe_error: object = SafeErrorClassification.NONE,
    ) -> "CompatibilityEvidence":
        if not isinstance(result, Mapping) or set(result) != set(_CAPABILITIES):
            raise ValueError("synthetic adapter result must contain exactly six capabilities")
        return cls(
            route_id=_identifier(route_id, "route_id"),
            adapter_revision=_identifier(adapter_revision, "adapter_revision"),
            material_identity_digest=_digest(material_identity_digest),
            states=tuple((name, _state(result[name])) for name in _CAPABILITIES),
            safe_error=_safe_error(safe_error),
        )

    @property
    def is_compatible(self) -> bool:
        return self.safe_error is SafeErrorClassification.NONE and all(
            state is CapabilityState.SUPPORTED for _, state in self.states
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": ROUTE_COMPATIBILITY_SCHEMA,
            "route_id": self.route_id,
            "adapter_revision": self.adapter_revision,
            "material_identity_digest": self.material_identity_digest,
            "states": {name: state.value for name, state in self.states},
            "safe_error": self.safe_error.value,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_canonical_json(cls, raw: object) -> "CompatibilityEvidence":
        if not isinstance(raw, str):
            raise ValueError("canonical compatibility evidence must be JSON text")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("canonical compatibility evidence is invalid JSON") from exc
        fields = {
            "schema",
            "route_id",
            "adapter_revision",
            "material_identity_digest",
            "states",
            "safe_error",
        }
        if not isinstance(payload, dict) or set(payload) != fields or payload["schema"] != ROUTE_COMPATIBILITY_SCHEMA:
            raise ValueError("canonical compatibility evidence has an invalid schema")
        return cls.from_result(
            payload["route_id"],
            payload["adapter_revision"],
            payload["material_identity_digest"],
            payload["states"],
            payload["safe_error"],
        )


class CompatibilityCache:
    """In-memory cache keyed by the two material drift dimensions."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], CompatibilityEvidence] = {}

    def get(
        self, route_id: object, adapter_revision: object, material_identity_digest: object
    ) -> CompatibilityEvidence | None:
        return self._items.get(
            (
                _identifier(route_id, "route_id"),
                _identifier(adapter_revision, "adapter_revision"),
                _digest(material_identity_digest),
            )
        )

    def put(self, evidence: CompatibilityEvidence) -> CompatibilityEvidence:
        if not isinstance(evidence, CompatibilityEvidence):
            raise TypeError("compatibility cache accepts CompatibilityEvidence only")
        self._items[(evidence.route_id, evidence.adapter_revision, evidence.material_identity_digest)] = evidence
        return evidence
