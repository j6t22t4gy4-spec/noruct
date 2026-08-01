"""Content-free continuation identity pinned to a frozen execution route.

This module records only opaque identifiers and digests.  It deliberately does
not resolve a route, load a session, or retain provider-native conversation
state.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")


class ContinuationRouteState(StrEnum):
    """The only permitted route/session relationship for a continuation."""

    STABLE = "STABLE"
    FRESH_SESSION_STABLE_ROUTE = "FRESH_SESSION_STABLE_ROUTE"
    ROUTE_REBOUND = "ROUTE_REBOUND"


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{field} must be a bounded opaque identifier")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class RouteBoundContinuation:
    """A continuation envelope; validation makes all route drift explicit.

    ``FRESH_SESSION_STABLE_ROUTE`` proves a receipt-bound partial continuation
    started a new execution session without changing an already frozen route.
    ``ROUTE_REBOUND`` is evidence of a new session and an independently
    approved/frozen route change, not permission to select a route.  Neither
    form retains provider-native session state.
    """

    continuation_id: str
    job_id: str
    prior_session_id: str
    session_id: str
    prior_route_binding_digest: str
    route_binding_digest: str
    prior_frozen_route_admission_digest: str
    frozen_route_admission_digest: str
    context_projection_digest: str
    intelligence_snapshot_digest: str
    policy_digest: str
    route_state: ContinuationRouteState

    def __post_init__(self) -> None:
        for field in ("continuation_id", "job_id", "prior_session_id", "session_id"):
            object.__setattr__(self, field, _token(getattr(self, field), field))
        for field in (
            "prior_route_binding_digest",
            "route_binding_digest",
            "prior_frozen_route_admission_digest",
            "frozen_route_admission_digest",
            "context_projection_digest",
            "intelligence_snapshot_digest",
            "policy_digest",
        ):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        if not isinstance(self.route_state, ContinuationRouteState):
            object.__setattr__(self, "route_state", ContinuationRouteState(self.route_state))
        if self.route_state is ContinuationRouteState.STABLE:
            if self.prior_session_id != self.session_id:
                raise ValueError("stable continuation must retain its session identity")
            if self.prior_route_binding_digest != self.route_binding_digest:
                raise ValueError("stable continuation must retain its route binding")
            if self.prior_frozen_route_admission_digest != self.frozen_route_admission_digest:
                raise ValueError("stable continuation must retain its frozen route admission")
        elif self.route_state is ContinuationRouteState.FRESH_SESSION_STABLE_ROUTE:
            if self.prior_session_id == self.session_id:
                raise ValueError("fresh stable-route continuation must use a fresh session identity")
            if self.prior_route_binding_digest != self.route_binding_digest:
                raise ValueError("fresh stable-route continuation must retain its route binding")
            if self.prior_frozen_route_admission_digest != self.frozen_route_admission_digest:
                raise ValueError("fresh stable-route continuation must retain its frozen route admission")
        elif self.prior_session_id == self.session_id:
            raise ValueError("route rebound must use a fresh session identity")
        elif self.prior_route_binding_digest == self.route_binding_digest:
            raise ValueError("route rebound must change its route binding")
        elif self.prior_frozen_route_admission_digest == self.frozen_route_admission_digest:
            raise ValueError("route rebound must change its frozen route admission")

    def canonical_payload(self) -> dict[str, str]:
        return {
            field: (getattr(self, field).value if field == "route_state" else getattr(self, field))
            for field in self.__dataclass_fields__
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_json(cls, raw: object) -> "RouteBoundContinuation":
        try:
            value = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError as exc:
            raise ValueError("continuation JSON is invalid") from exc
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("continuation JSON has unknown or missing fields")
        return cls(**value)
