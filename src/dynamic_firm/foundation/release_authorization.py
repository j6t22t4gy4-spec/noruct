"""Fail-closed validation for a human-owned Employee Runtime release draft."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm import __version__
from .provider_evidence import ProviderSlotEvidenceError, validate_provider_evidence_matrix_records


SCHEMA = "noruct.employee-runtime-preview-release-authorization.v1"
_GATES = {
    "shipped_runtime_secondary_provenance_review",
    "provider_terms_privacy_and_commercial_use_review",
    "migration_signing_publisher_authorization",
    "hosted_evolution_network_authorization",
}

_SLOTS = ("direct", "read_tool", "approval", "cancel_recovery")


class ReleaseAuthorizationError(ValueError):
    """Stable refusal for an unsafe or non-draft authorization record."""


def _technical_evidence_is_current(value: object, source: Path) -> bool:
    if not isinstance(value, Mapping) or value.get("product_version") != __version__:
        return False
    records = value.get("provider_matrix")
    if not isinstance(records, list) or [item.get("slot") if isinstance(item, Mapping) else None for item in records] != list(_SLOTS):
        return False
    root = source.parents[3]
    paths: dict[str, Path] = {}
    for item in records:
        if not isinstance(item, Mapping) or set(item) != {"slot", "path", "sha256", "evidence_id"}:
            return False
        relative = item["path"]
        if not isinstance(relative, str) or not relative.startswith("docs/50-mvp/evaluations/"):
            return False
        artifact = (root / relative).resolve()
        if root not in artifact.parents or not artifact.is_file() or artifact.is_symlink():
            return False
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != item["sha256"]:
            return False
        try:
            evidence = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if evidence.get("evidence_id") != item["evidence_id"]:
            return False
        paths[str(item["slot"])] = artifact
    try:
        validate_provider_evidence_matrix_records(paths)
    except ProviderSlotEvidenceError:
        return False
    return True


def validate_release_authorization_draft(path: str | Path) -> dict[str, Any]:
    """Accept only the inert, unsigned draft shape.

    This deliberately cannot validate an approval: legal/provenance findings and
    a release-owner signature are external human authority, not product input.
    """

    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.is_symlink() or source.stat().st_size > 64 * 1024:
        raise ReleaseAuthorizationError("release authorization draft is unavailable")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReleaseAuthorizationError("release authorization draft is invalid") from None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        raise ReleaseAuthorizationError("release authorization draft schema is invalid")
    if value.get("status") != "DRAFT_NOT_AUTHORIZED":
        raise ReleaseAuthorizationError("release authorization is human-owned and cannot be machine-authorized")
    activation = value.get("activation")
    expected_activation = {
        "employee_runtime_default": "legacy",
        "commercial_default_eligible": False,
        "release_authorized": False,
        "shared_network_release_authorized": False,
    }
    if activation != expected_activation:
        raise ReleaseAuthorizationError("release authorization draft attempts activation")
    if value.get("authorized_release_owner") is not None or value.get("authorization_signature_ref") is not None:
        raise ReleaseAuthorizationError("release authorization draft contains a human authorization claim")
    if value.get("product_version") != __version__:
        raise ReleaseAuthorizationError("release authorization draft is not bound to the current product version")
    if not _technical_evidence_is_current(value.get("technical_evidence"), source):
        raise ReleaseAuthorizationError("release authorization draft is not bound to the current technical evidence")
    gates = value.get("required_human_gates")
    if not isinstance(gates, list) or {item.get("id") for item in gates if isinstance(item, dict)} != _GATES:
        raise ReleaseAuthorizationError("release authorization draft gates are incomplete")
    for gate in gates:
        if not isinstance(gate, dict) or gate.get("status") != "PENDING":
            raise ReleaseAuthorizationError("release authorization draft contains a human disposition")
        if any(gate.get(key) not in (None, []) for key in ("reviewer", "reviewed_at", "evidence_refs", "disposition")):
            raise ReleaseAuthorizationError("release authorization draft contains review evidence")
    return {
        "schema_version": SCHEMA,
        "draft_valid": True,
        "release_authorized": False,
        "commercial_default_eligible": False,
        "shared_network_release_authorized": False,
        "pending_human_gates": sorted(_GATES),
        "technical_evidence_bound": True,
    }
