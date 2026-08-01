"""Immutable, provider-free user policy for reusing an already approved route.

This module intentionally cannot select a new route, activate an adapter, read a
credential, or grant egress.  It records only a user's local reuse preference
against a separately supplied immutable approved-route registry.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_CREDENTIAL_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]{1,127}\Z")


class UserRoutingPolicyMode(StrEnum):
    """The four local reuse choices exposed by the first RC candidate."""

    QUALITY_FIRST = "QUALITY_FIRST"
    BALANCED = "BALANCED"
    EFFICIENT = "EFFICIENT"
    PRIVATE_LOCAL_FIRST = "PRIVATE_LOCAL_FIRST"


class RouteReuseDisposition(StrEnum):
    REUSED_QUALITY_FIRST = "REUSED_QUALITY_FIRST"
    REUSED_BALANCED = "REUSED_BALANCED"
    REUSED_EFFICIENT = "REUSED_EFFICIENT"
    REUSED_PRIVATE_LOCAL_FIRST = "REUSED_PRIVATE_LOCAL_FIRST"
    DENIED_FIRST_RUN_NO_APPROVED_ROUTES = "DENIED_FIRST_RUN_NO_APPROVED_ROUTES"
    DENIED_MISSING_APPROVAL = "DENIED_MISSING_APPROVAL"


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{field} must be a bounded opaque identifier")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ApprovedRouteMetadata:
    """Non-secret metadata for a route previously approved by the user."""

    route_id: str
    execution_route_binding_digest: str
    provider_config_digest: str
    credential_reference: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_id", _token(self.route_id, "route_id"))
        object.__setattr__(
            self,
            "execution_route_binding_digest",
            _digest(self.execution_route_binding_digest, "execution_route_binding_digest"),
        )
        object.__setattr__(self, "provider_config_digest", _digest(self.provider_config_digest, "provider_config_digest"))
        if not isinstance(self.credential_reference, str) or not _CREDENTIAL_REFERENCE.fullmatch(self.credential_reference):
            raise ValueError("credential_reference must be a non-secret reference name")

    def canonical_payload(self) -> dict[str, str]:
        return {
            "credential_reference": self.credential_reference,
            "execution_route_binding_digest": self.execution_route_binding_digest,
            "provider_config_digest": self.provider_config_digest,
            "route_id": self.route_id,
        }


@dataclass(frozen=True, slots=True)
class ApprovedRouteRegistry:
    """An immutable registry; it stores metadata but does not construct routes."""

    routes: tuple[ApprovedRouteMetadata, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(route, ApprovedRouteMetadata) for route in self.routes):
            raise ValueError("approved routes must be typed")
        if len({route.route_id for route in self.routes}) != len(self.routes):
            raise ValueError("approved route identifiers must be unique")
        object.__setattr__(self, "routes", tuple(sorted(self.routes, key=lambda route: route.route_id)))

    def canonical_payload(self) -> dict[str, list[dict[str, str]]]:
        return {"routes": [route.canonical_payload() for route in self.routes]}

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def contains(self, route_id: object) -> bool:
        return any(route.route_id == _token(route_id, "route_id") for route in self.routes)

    @classmethod
    def from_canonical_json(cls, raw: object) -> "ApprovedRouteRegistry":
        if not isinstance(raw, str):
            raise ValueError("approved route registry JSON must be a string")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("approved route registry JSON is invalid") from exc
        if not isinstance(value, dict) or set(value) != {"routes"} or not isinstance(value["routes"], list):
            raise ValueError("approved route registry JSON has unknown or missing fields")
        try:
            registry = cls(tuple(ApprovedRouteMetadata(**route) for route in value["routes"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("approved route registry JSON is invalid") from exc
        if raw != registry.canonical_json():
            raise ValueError("approved route registry JSON is not canonical")
        return registry


@dataclass(frozen=True, slots=True)
class UserRoutingPolicy:
    mode: UserRoutingPolicyMode

    def __post_init__(self) -> None:
        if not isinstance(self.mode, UserRoutingPolicyMode):
            object.__setattr__(self, "mode", UserRoutingPolicyMode(self.mode))

    def canonical_payload(self) -> dict[str, str]:
        return {"mode": self.mode.value}

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_canonical_json(cls, raw: object) -> "UserRoutingPolicy":
        if not isinstance(raw, str):
            raise ValueError("user routing policy JSON must be a string")
        try:
            value = json.loads(raw)
            policy = cls(**value) if isinstance(value, dict) and set(value) == {"mode"} else None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("user routing policy JSON is invalid") from exc
        if policy is None or raw != policy.canonical_json():
            raise ValueError("user routing policy JSON is unknown, missing, or not canonical")
        return policy


@dataclass(frozen=True, slots=True)
class ApprovedRouteReuseDecision:
    """Content-free local decision; it never asks to activate a route."""

    requested_route_id: str
    selected_route_id: str | None
    disposition: RouteReuseDisposition
    policy_digest: str
    registry_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_route_id", _token(self.requested_route_id, "requested_route_id"))
        if self.selected_route_id is not None:
            object.__setattr__(self, "selected_route_id", _token(self.selected_route_id, "selected_route_id"))
        if not isinstance(self.disposition, RouteReuseDisposition):
            object.__setattr__(self, "disposition", RouteReuseDisposition(self.disposition))
        object.__setattr__(self, "policy_digest", _digest(self.policy_digest, "policy_digest"))
        object.__setattr__(self, "registry_digest", _digest(self.registry_digest, "registry_digest"))
        reused = self.disposition in {
            RouteReuseDisposition.REUSED_QUALITY_FIRST,
            RouteReuseDisposition.REUSED_BALANCED,
            RouteReuseDisposition.REUSED_EFFICIENT,
            RouteReuseDisposition.REUSED_PRIVATE_LOCAL_FIRST,
        }
        if reused != (self.selected_route_id == self.requested_route_id):
            raise ValueError("only a reused requested route can be selected")
        if not reused and self.selected_route_id is not None:
            raise ValueError("a denied reuse decision cannot select a route")

    def canonical_payload(self) -> dict[str, str | None]:
        return {
            "disposition": self.disposition.value,
            "policy_digest": self.policy_digest,
            "registry_digest": self.registry_digest,
            "requested_route_id": self.requested_route_id,
            "selected_route_id": self.selected_route_id,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_canonical_json(cls, raw: object) -> "ApprovedRouteReuseDecision":
        if not isinstance(raw, str):
            raise ValueError("route reuse decision JSON must be a string")
        try:
            value = json.loads(raw)
            decision = cls(**value) if isinstance(value, dict) and set(value) == set(cls.__dataclass_fields__) else None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("route reuse decision JSON is invalid") from exc
        if decision is None or raw != decision.canonical_json():
            raise ValueError("route reuse decision JSON is unknown, missing, or not canonical")
        return decision


def decide_approved_route_reuse(
    policy: UserRoutingPolicy,
    registry: ApprovedRouteRegistry,
    requested_route_id: object,
) -> ApprovedRouteReuseDecision:
    """Return a deterministic local reuse decision without selecting or activating providers."""
    if not isinstance(policy, UserRoutingPolicy) or not isinstance(registry, ApprovedRouteRegistry):
        raise TypeError("a user routing policy and approved route registry are required")
    route_id = _token(requested_route_id, "requested_route_id")
    if not registry.routes:
        disposition = RouteReuseDisposition.DENIED_FIRST_RUN_NO_APPROVED_ROUTES
    elif not registry.contains(route_id):
        disposition = RouteReuseDisposition.DENIED_MISSING_APPROVAL
    else:
        disposition = {
            UserRoutingPolicyMode.QUALITY_FIRST: RouteReuseDisposition.REUSED_QUALITY_FIRST,
            UserRoutingPolicyMode.BALANCED: RouteReuseDisposition.REUSED_BALANCED,
            UserRoutingPolicyMode.EFFICIENT: RouteReuseDisposition.REUSED_EFFICIENT,
            UserRoutingPolicyMode.PRIVATE_LOCAL_FIRST: RouteReuseDisposition.REUSED_PRIVATE_LOCAL_FIRST,
        }[policy.mode]
    selected_route_id = route_id if disposition in {
        RouteReuseDisposition.REUSED_QUALITY_FIRST,
        RouteReuseDisposition.REUSED_BALANCED,
        RouteReuseDisposition.REUSED_EFFICIENT,
        RouteReuseDisposition.REUSED_PRIVATE_LOCAL_FIRST,
    } else None
    return ApprovedRouteReuseDecision(
        requested_route_id=route_id,
        selected_route_id=selected_route_id,
        disposition=disposition,
        policy_digest=policy.digest,
        registry_digest=registry.digest,
    )
