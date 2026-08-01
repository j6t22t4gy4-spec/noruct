"""Pure identities and decisions for local Artifact shadow evaluation.

The module is intentionally provider-free and transport-free.  It only turns
already validated Artifact manifests and bounded scalar evidence into stable
first-party identities.  The Evolution store owns persistence and the
Artifact lifecycle owns activation authority.
"""

from __future__ import annotations

import hashlib
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from dynamic_firm.company.models import canonical_json, content_digest


ARTIFACT_SHADOW_RECEIPT_SCHEMA = "noruct.artifact-shadow-receipt.v1"
ARTIFACT_SHADOW_SLOT_SCHEMA = "noruct.artifact-shadow-slot.v1"
ARTIFACT_SHADOW_PROJECTION_SCHEMA = "noruct.artifact-shadow-projection.v1"

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

SHADOW_FIXTURE_KINDS = frozenset({"PUBLIC", "SYNTHETIC"})
SHADOW_TERMINAL_STATES = frozenset({"COMPLETE", "FAILED", "CANCELLED", "INCOMPLETE"})
SHADOW_RESULTS = frozenset(
    {
        "PASS",
        "REGRESSION",
        "FAILED",
        "INCOMPLETE",
        "COST_CEILING_EXCEEDED",
        "CONTRACT_MISMATCH",
        "PERMISSION_EXPANSION",
    }
)


class ShadowEvaluationIntegrityError(ValueError):
    """An immutable shadow receipt no longer matches its recorded digest."""


def require_safe_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lower-case identifier (2-80 characters)")
    return value


def require_semver(value: object, name: str) -> str:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a semantic version like 1.2.3")
    return value


def require_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def canonical_decimal(
    value: object,
    name: str,
    *,
    minimum: Decimal = Decimal("0"),
    maximum: Decimal | None = None,
) -> str:
    """Return an exact, finite decimal string suitable for SQLite replay."""

    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not parsed.is_finite() or parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    if parsed == 0:
        return "0"
    return format(parsed.normalize(), "f")


def artifact_required_capabilities(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    compatibility = manifest.get("compatibility")
    content = manifest.get("content")
    if not isinstance(compatibility, Mapping) or not isinstance(content, Mapping):
        raise ValueError("Shadow evaluation requires a validated Artifact manifest")
    values = tuple(compatibility.get("required_capabilities", ())) + tuple(
        content.get("required_capabilities", ())
    )
    capabilities = tuple(sorted({require_safe_id(value, "required_capability") for value in values}))
    return capabilities


def artifact_contract_digest(manifest: Mapping[str, Any]) -> str:
    """Hash the full frozen runtime compatibility contract."""

    compatibility = manifest.get("compatibility")
    if not isinstance(compatibility, Mapping):
        raise ValueError("Shadow evaluation requires Artifact compatibility")
    return content_digest({"compatibility": compatibility})


def artifact_required_capabilities_digest(manifest: Mapping[str, Any]) -> str:
    return content_digest(
        {"required_capabilities": artifact_required_capabilities(manifest)}
    )


def build_shadow_slot(
    *,
    scope_key: str,
    kind: str,
    artifact_id: str,
    base_version: str,
    base_manifest_digest: str,
    base_contract_digest: str,
    base_required_capabilities_digest: str,
    candidate_version: str,
    candidate_manifest_digest: str,
    candidate_contract_digest: str,
    candidate_required_capabilities_digest: str,
    fixture_kind: str,
    fixture_id: str,
    fixture_version: str,
    fixture_digest: str,
) -> Mapping[str, Any]:
    """Build the stable slot identity shared by evaluation and activation."""

    if fixture_kind not in SHADOW_FIXTURE_KINDS:
        raise ValueError("Shadow fixture kind must be PUBLIC or SYNTHETIC")
    return {
        "schema": ARTIFACT_SHADOW_SLOT_SCHEMA,
        "scope_key": require_safe_id(scope_key, "scope_key"),
        "kind": require_safe_id(kind.lower(), "kind").upper(),
        "artifact_id": require_safe_id(artifact_id, "artifact_id"),
        "base": {
            "version": require_semver(base_version, "base_version"),
            "manifest_digest": require_sha256(
                base_manifest_digest, "base_manifest_digest"
            ),
            "contract_digest": require_sha256(
                base_contract_digest, "base_contract_digest"
            ),
            "required_capabilities_digest": require_sha256(
                base_required_capabilities_digest,
                "base_required_capabilities_digest",
            ),
        },
        "candidate": {
            "version": require_semver(candidate_version, "candidate_version"),
            "manifest_digest": require_sha256(
                candidate_manifest_digest, "candidate_manifest_digest"
            ),
            "contract_digest": require_sha256(
                candidate_contract_digest, "candidate_contract_digest"
            ),
            "required_capabilities_digest": require_sha256(
                candidate_required_capabilities_digest,
                "candidate_required_capabilities_digest",
            ),
        },
        "fixture": {
            "kind": fixture_kind,
            "fixture_id": require_safe_id(fixture_id, "fixture_id"),
            "version": require_semver(fixture_version, "fixture_version"),
            "digest": require_sha256(fixture_digest, "fixture_digest"),
        },
    }


def shadow_slot_digest(slot: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(slot).encode("utf-8")).hexdigest()


def decide_shadow_result(
    *,
    base_contract_digest: str,
    candidate_contract_digest: str,
    base_required_capabilities: tuple[str, ...],
    candidate_required_capabilities: tuple[str, ...],
    terminal_state: str,
    complete: bool,
    baseline_quality: str,
    candidate_quality: str,
    baseline_safety: str,
    candidate_safety: str,
    candidate_cost: str,
    cost_ceiling: str,
) -> str:
    """Derive the result; callers cannot label a failing receipt as PASS."""

    expanded = set(candidate_required_capabilities) - set(base_required_capabilities)
    if expanded:
        return "PERMISSION_EXPANSION"
    if (
        base_contract_digest != candidate_contract_digest
        or base_required_capabilities != candidate_required_capabilities
    ):
        return "CONTRACT_MISMATCH"
    if terminal_state in {"FAILED", "CANCELLED"}:
        return "FAILED"
    if terminal_state != "COMPLETE" or not complete:
        return "INCOMPLETE"
    if (
        Decimal(candidate_quality) < Decimal(baseline_quality)
        or Decimal(candidate_safety) < Decimal(baseline_safety)
    ):
        return "REGRESSION"
    if Decimal(candidate_cost) > Decimal(cost_ceiling):
        return "COST_CEILING_EXCEEDED"
    return "PASS"


def receipt_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
