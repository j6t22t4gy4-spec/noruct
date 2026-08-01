"""Provider-free observed model identity and immutable B01 snapshot binding."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .model_intelligence import ModelIdentityAssurance, ModelIntelligenceSnapshot


_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")


@dataclass(frozen=True, slots=True)
class ObservedModelIdentity:
    requested_model_id: str
    provider_route_class: str
    assurance: ModelIdentityAssurance
    local_content_digest: str | None = None
    provider_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.requested_model_id, str) or not self.requested_model_id or not isinstance(self.provider_route_class, str) or not self.provider_route_class:
            raise ValueError("requested_model_id and provider_route_class are required")
        if not isinstance(self.assurance, ModelIdentityAssurance):
            object.__setattr__(self, "assurance", ModelIdentityAssurance(self.assurance))
        digest = self.local_content_digest
        if self.assurance is ModelIdentityAssurance.LOCAL_CONTENT_DIGEST:
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("LOCAL_CONTENT_DIGEST requires an explicit lowercase local bytes digest")
        elif digest is not None:
            raise ValueError("remote provider identity must not be represented as a content digest")
        if self.assurance is ModelIdentityAssurance.IMMUTABLE_PROVIDER_REVISION:
            if not isinstance(self.provider_revision, str) or not self.provider_revision:
                raise ValueError("IMMUTABLE_PROVIDER_REVISION requires an explicit provider revision")
        elif self.provider_revision is not None:
            raise ValueError("provider revision is only valid for IMMUTABLE_PROVIDER_REVISION")

    def canonical_payload(self) -> dict[str, str | None]:
        return {"requested_model_id": self.requested_model_id, "provider_route_class": self.provider_route_class, "assurance": self.assurance.value, "local_content_digest": self.local_content_digest, "provider_revision": self.provider_revision}

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def classify_observed_metadata(
    requested_model_id: str, provider_route_class: str, metadata: Mapping[str, object] | None
) -> ObservedModelIdentity:
    """Classify one closed local observation; ambiguity always becomes unknown.

    ``local_content_digest`` is reserved for explicit local artifact bytes.
    Provider revisions and versioned IDs are identity labels, never byte digests.
    """

    if not isinstance(requested_model_id, str) or not requested_model_id:
        raise ValueError("requested_model_id is required")
    if not isinstance(metadata, Mapping):
        return ObservedModelIdentity(requested_model_id, provider_route_class, ModelIdentityAssurance.IDENTITY_UNKNOWN)
    known = {
        "local_artifact_bytes",
        "immutable_provider_revision",
        "versioned_model_id",
        "floating_alias",
    }
    present = [(name, metadata[name]) for name in known if name in metadata]
    if set(metadata) - known or len(present) != 1:
        return ObservedModelIdentity(requested_model_id, provider_route_class, ModelIdentityAssurance.IDENTITY_UNKNOWN)
    name, value = present[0]
    if name == "local_artifact_bytes":
        try:
            if not isinstance(value, bytes): raise ValueError("local bytes required")
            return ObservedModelIdentity(
                requested_model_id,
                provider_route_class,
                ModelIdentityAssurance.LOCAL_CONTENT_DIGEST,
                local_content_digest=hashlib.sha256(value).hexdigest(),
            )
        except ValueError:
            return ObservedModelIdentity(
                requested_model_id,
                provider_route_class,
                ModelIdentityAssurance.IDENTITY_UNKNOWN,
            )
    if name == "immutable_provider_revision" and isinstance(value, str) and _REVISION.fullmatch(value):
        return ObservedModelIdentity(
            requested_model_id, provider_route_class,
            ModelIdentityAssurance.IMMUTABLE_PROVIDER_REVISION,
            provider_revision=value,
        )
    if name == "versioned_model_id" and value == requested_model_id:
        return ObservedModelIdentity(requested_model_id, provider_route_class, ModelIdentityAssurance.VERSIONED_MODEL_ID)
    if name == "floating_alias" and value == requested_model_id:
        return ObservedModelIdentity(requested_model_id, provider_route_class, ModelIdentityAssurance.FLOATING_ALIAS)
    return ObservedModelIdentity(requested_model_id, provider_route_class, ModelIdentityAssurance.IDENTITY_UNKNOWN)


@dataclass(frozen=True, slots=True)
class SnapshotIdentityBinding:
    snapshot_digest: str
    provider_route_class: str
    requested_model_id: str
    assurance: ModelIdentityAssurance
    observation_digest: str

    @classmethod
    def bind(cls, snapshot: ModelIntelligenceSnapshot, observation: ObservedModelIdentity) -> "SnapshotIdentityBinding":
        if snapshot.requested_model_id != observation.requested_model_id:
            raise ValueError("observed model identity does not match snapshot requested_model_id")
        if snapshot.provider_route_class != observation.provider_route_class:
            raise ValueError("observed provider_route_class does not match snapshot")
        if snapshot.identity_assurance is not observation.assurance:
            raise ValueError("observed model assurance does not match snapshot assurance")
        return cls(snapshot.content_digest, snapshot.provider_route_class, snapshot.requested_model_id, snapshot.identity_assurance, observation.digest)

    def canonical_json(self) -> str:
        return json.dumps({"snapshot_digest": self.snapshot_digest, "provider_route_class": self.provider_route_class, "requested_model_id": self.requested_model_id, "assurance": self.assurance.value, "observation_digest": self.observation_digest}, sort_keys=True, separators=(",", ":"))
