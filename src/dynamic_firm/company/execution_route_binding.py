"""Immutable, content-free route binding for one EmployeeRun attempt."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .model_intelligence import ModelIdentityAssurance


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_CREDENTIAL_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]{1,127}\Z")
_DIGEST_FIELDS = (
    "provider_config_digest", "required_capability_digest", "inference_contract_digest", "egress_policy_digest",
    "intelligence_snapshot_digest", "orchestration_policy_digest", "compatibility_evidence_digest",
    "fallback_policy_digest", "fanout_policy_digest", "continuation_policy_digest",
)


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{name} must be a bounded opaque identifier")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionRouteBinding:
    attempt_id: str
    route_id: str
    execution_profile_id: str
    provider_config_digest: str
    credential_reference: str
    requested_model_id: str
    identity_assurance: ModelIdentityAssurance
    required_capability_digest: str
    inference_contract_digest: str
    egress_policy_digest: str
    intelligence_snapshot_digest: str
    orchestration_policy_digest: str
    compatibility_evidence_digest: str
    fallback_policy_digest: str
    fanout_policy_digest: str
    continuation_policy_digest: str

    def __post_init__(self) -> None:
        for name in ("attempt_id", "route_id", "execution_profile_id", "requested_model_id"):
            object.__setattr__(self, name, _token(getattr(self, name), name))
        if not isinstance(self.credential_reference, str) or not _CREDENTIAL_REFERENCE.fullmatch(self.credential_reference):
            raise ValueError("credential_reference must be an environment-variable name, never a value")
        if not isinstance(self.identity_assurance, ModelIdentityAssurance):
            object.__setattr__(self, "identity_assurance", ModelIdentityAssurance(self.identity_assurance))
        for name in _DIGEST_FIELDS:
            object.__setattr__(self, name, _digest(getattr(self, name), name))

    def canonical_payload(self) -> dict[str, str]:
        return {name: (getattr(self, name).value if name == "identity_assurance" else getattr(self, name)) for name in self.__dataclass_fields__}

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    @classmethod
    def from_canonical_json(cls, raw: object) -> "ExecutionRouteBinding":
        try: value = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError as exc: raise ValueError("binding JSON is invalid") from exc
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("binding JSON has unknown or missing fields")
        return cls(**value)
