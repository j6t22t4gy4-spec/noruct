from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


NETWORK_GATE_SCHEMA = "noruct.evolution-network-gate.v1"


@dataclass(frozen=True, slots=True)
class NetworkGateStatus:
    schema: str
    hosted_transport: str
    network_worker: str
    capability_gateway: str
    customer_workspace_upload: str
    required_gates: Mapping[str, str]
    release_authorized: bool

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


def network_gate_status() -> NetworkGateStatus:
    """Describe the implemented intake boundary and the closed remote-worker gate."""
    gates = {
        "privacy_processing_disclosure": "PENDING_EXTERNAL_REVIEW",
        "consent_and_withdrawal_contract": "IMPLEMENTED_EXPLICIT_HTTPS_RECEIPTS",
        "operator_release_authorization": "PENDING_AUTHORIZED_RECORD",
        "hosted_security_isolation": "PENDING_SECURITY_REVIEW",
        "remote_capability_gateway": "NOT_IMPLEMENTED",
        "incident_response_and_residency": "PENDING_OPERATIONS_REVIEW",
        "provider_terms_and_data_flow": "PENDING_LEGAL_REVIEW",
    }
    return NetworkGateStatus(
        schema=NETWORK_GATE_SCHEMA,
        hosted_transport="IMPLEMENTED_EXPLICIT_OPT_IN_NOT_AUTO_ACTIVATED",
        network_worker="DISABLED",
        capability_gateway="NOT_IMPLEMENTED",
        customer_workspace_upload="PROHIBITED",
        required_gates=gates,
        release_authorized=False,
    )


def preview_network_worker(tenant_id: str, release_id: str) -> Mapping[str, Any]:
    status = network_gate_status()
    return {
        "tenant_id": tenant_id,
        "release_id": release_id,
        "decision": "DENIED_NETWORK_WORKER_DISABLED",
        "gate": status.to_dict(),
        "workspace_effect": "NONE",
        "credential_effect": "NONE",
        "network_request_performed": False,
    }
