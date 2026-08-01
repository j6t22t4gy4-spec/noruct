"""Immutable, content-free receipt for exactly one physical model call."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")


class InvocationTerminalStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INDETERMINATE = "INDETERMINATE"


class ReceiptAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value): raise ValueError(f"{field} must be a bounded opaque token")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value): raise ValueError(f"{field} must be sha256")
    return value


def _measurement(availability: ReceiptAvailability, value: object, field: str) -> float | None:
    if availability is ReceiptAvailability.UNAVAILABLE:
        if value is not None: raise ValueError(f"unavailable {field} cannot have a value")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"available {field} must be finite and non-negative")
    return float(value)


@dataclass(frozen=True, slots=True)
class ModelInvocationReceipt:
    invocation_id: str
    route_binding_digest: str
    context_projection_digest: str
    attempt_id: str
    fanout_parent_id: str | None
    terminal_status: InvocationTerminalStatus
    output_digest: str | None
    usage_availability: ReceiptAvailability
    usage_units: float | None
    cost_availability: ReceiptAvailability
    cost_usd: float | None
    latency_ms: float
    safe_error_code: str | None = None

    def __post_init__(self) -> None:
        for field in ("invocation_id", "attempt_id"):
            object.__setattr__(self, field, _token(getattr(self, field), field))
        for field in ("route_binding_digest", "context_projection_digest"):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        if self.fanout_parent_id is not None: object.__setattr__(self, "fanout_parent_id", _token(self.fanout_parent_id, "fanout_parent_id"))
        if not isinstance(self.terminal_status, InvocationTerminalStatus): object.__setattr__(self, "terminal_status", InvocationTerminalStatus(self.terminal_status))
        for field in ("usage_availability", "cost_availability"):
            if not isinstance(getattr(self, field), ReceiptAvailability): object.__setattr__(self, field, ReceiptAvailability(getattr(self, field)))
        object.__setattr__(self, "usage_units", _measurement(self.usage_availability, self.usage_units, "usage_units"))
        object.__setattr__(self, "cost_usd", _measurement(self.cost_availability, self.cost_usd, "cost_usd"))
        if self.terminal_status is InvocationTerminalStatus.SUCCEEDED:
            object.__setattr__(self, "output_digest", _digest(self.output_digest, "output_digest"))
            if self.safe_error_code is not None: raise ValueError("successful calls cannot carry an error")
        elif self.output_digest is not None:
            raise ValueError("non-successful calls cannot claim output")
        if self.safe_error_code is not None: object.__setattr__(self, "safe_error_code", _token(self.safe_error_code, "safe_error_code"))
        object.__setattr__(self, "latency_ms", _measurement(ReceiptAvailability.AVAILABLE, self.latency_ms, "latency_ms"))

    def canonical_payload(self) -> dict[str, object]:
        return {name: (getattr(self, name).value if isinstance(getattr(self, name), StrEnum) else getattr(self, name)) for name in self.__dataclass_fields__}
    def canonical_json(self) -> str: return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",",":"))
    @property
    def digest(self) -> str: return hashlib.sha256(self.canonical_json().encode()).hexdigest()
    @classmethod
    def from_canonical_json(cls, raw: object) -> "ModelInvocationReceipt":
        try: value=json.loads(raw) if isinstance(raw,str) else None
        except json.JSONDecodeError as exc: raise ValueError("receipt JSON is invalid") from exc
        if not isinstance(value,dict) or set(value)!=set(cls.__dataclass_fields__): raise ValueError("receipt JSON fields are invalid")
        return cls(**value)
