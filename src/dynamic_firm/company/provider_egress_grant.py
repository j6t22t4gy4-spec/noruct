"""Frozen provider-specific authorization for one exact outbound projection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class InformationClassification(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    RESTRICTED = "RESTRICTED"


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{field} is invalid")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field} must be sha256")
    return value


@dataclass(frozen=True, slots=True)
class ContextProjectionItem:
    source_id: str
    classification: InformationClassification
    redacted_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _token(self.source_id, "source_id"))
        object.__setattr__(self, "classification", InformationClassification(self.classification))
        object.__setattr__(self, "redacted_digest", _digest(self.redacted_digest, "redacted_digest"))


@dataclass(frozen=True, slots=True)
class ContextProjection:
    """Transient redacted bytes and their content-free source projection."""

    items: tuple[ContextProjectionItem, ...]
    outbound_payload: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or not self.items or any(not isinstance(item, ContextProjectionItem) for item in self.items):
            raise ValueError("context projection items must be a nonempty typed tuple")
        if len({item.source_id for item in self.items}) != len(self.items):
            raise ValueError("context projection source ids must be unique")
        if not isinstance(self.outbound_payload, bytes) or not self.outbound_payload:
            raise ValueError("context projection requires nonempty redacted bytes")

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(self.outbound_payload).hexdigest()

    @property
    def digest(self) -> str:
        payload = {
            "items": [
                {"source_id": item.source_id, "classification": item.classification.value, "redacted_digest": item.redacted_digest}
                for item in self.items
            ],
            "payload_digest": self.payload_digest,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderEgressGrant:
    route_id: str
    route_binding_digest: str
    egress_policy_digest: str
    allowed_source_ids: tuple[str, ...]
    allowed_classifications: tuple[InformationClassification, ...]
    context_projection_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_id", _token(self.route_id, "route_id"))
        for field in ("route_binding_digest", "egress_policy_digest", "context_projection_digest"):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        source_ids = tuple(_token(value, "allowed_source_id") for value in self.allowed_source_ids)
        if not source_ids or len(source_ids) != len(set(source_ids)):
            raise ValueError("allowed_source_ids must be nonempty and unique")
        object.__setattr__(self, "allowed_source_ids", source_ids)
        classifications = tuple(InformationClassification(value) for value in self.allowed_classifications)
        if not classifications or len(classifications) != len(set(classifications)):
            raise ValueError("allowed_classifications must be nonempty and unique")
        object.__setattr__(self, "allowed_classifications", classifications)

    def validate_outbound(
        self,
        route_id: object,
        route_binding_digest: object,
        egress_policy_digest: object,
        projection: ContextProjection,
        outbound_payload: object,
    ) -> None:
        if (
            _token(route_id, "route_id") != self.route_id
            or _digest(route_binding_digest, "route_binding_digest") != self.route_binding_digest
            or _digest(egress_policy_digest, "egress_policy_digest") != self.egress_policy_digest
        ):
            raise ValueError("provider route does not match egress grant")
        if not isinstance(projection, ContextProjection) or not isinstance(outbound_payload, bytes):
            raise ValueError("a typed projection and outbound bytes are required")
        if projection.digest != self.context_projection_digest or hashlib.sha256(outbound_payload).hexdigest() != projection.payload_digest:
            raise ValueError("outbound bytes do not match the granted context projection")
        for item in projection.items:
            if item.source_id not in self.allowed_source_ids or item.classification not in self.allowed_classifications:
                raise ValueError("context projection exceeds provider egress grant")


def authorize_provider_egress(
    grant: ProviderEgressGrant | None,
    route_id: object,
    route_binding_digest: object,
    egress_policy_digest: object,
    projection: ContextProjection,
    outbound_payload: object,
) -> None:
    """The only provider boundary helper; absence of a grant is a refusal."""

    if grant is None:
        raise ValueError("provider egress requires an explicit grant")
    grant.validate_outbound(route_id, route_binding_digest, egress_policy_digest, projection, outbound_payload)


def send_authorized_provider_context(
    grant: ProviderEgressGrant | None,
    route_id: object,
    route_binding_digest: object,
    egress_policy_digest: object,
    projection: ContextProjection,
    sender: Callable[[bytes], object],
) -> object:
    """Validate and send the same redacted bytes in one provider-bound step."""

    if not callable(sender):
        raise ValueError("provider sender is required")
    authorize_provider_egress(
        grant,
        route_id,
        route_binding_digest,
        egress_policy_digest,
        projection,
        projection.outbound_payload,
    )
    return sender(projection.outbound_payload)
