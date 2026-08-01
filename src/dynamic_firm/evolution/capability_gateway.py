from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


CAPABILITY_GATEWAY_SCHEMA = "noruct.network-capability-gateway.v1"
_ALLOWED_PREVIEW_CAPABILITIES = frozenset({"read_public_fixture", "read_redacted_context"})
_DENIED_CAPABILITIES = frozenset({"workspace_write", "credential_access", "external_communication", "shell_execute"})


@dataclass(frozen=True, slots=True)
class CapabilityPreview:
    schema: str
    tenant_id: str
    release_id: str
    job_id: str
    requested_capabilities: tuple[str, ...]
    granted_capabilities: tuple[str, ...]
    denied_capabilities: tuple[str, ...]
    decision: str
    network_request_performed: bool
    credential_exposed: bool

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


def preview_capability_grant(
    tenant_id: str, release_id: str, job_id: str, capabilities: tuple[str, ...]
) -> CapabilityPreview:
    """Preview only: no remote process receives any grant in this phase."""
    requested = tuple(dict.fromkeys(capabilities))
    denied = tuple(capability for capability in requested if capability not in _ALLOWED_PREVIEW_CAPABILITIES)
    # Even allowed read classes remain ungranted until hosted gate authorization.
    return CapabilityPreview(
        schema=CAPABILITY_GATEWAY_SCHEMA,
        tenant_id=tenant_id,
        release_id=release_id,
        job_id=job_id,
        requested_capabilities=requested,
        granted_capabilities=(),
        denied_capabilities=denied if denied else requested,
        decision="DENIED_HOSTED_GATE_CLOSED",
        network_request_performed=False,
        credential_exposed=False,
    )
