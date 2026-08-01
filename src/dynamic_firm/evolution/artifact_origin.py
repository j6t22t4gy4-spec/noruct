"""Immutable origin contract for versioned Evolution Artifacts."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Mapping


ARTIFACT_ORIGIN_SCHEMA = "noruct.artifact-origin.v1"
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactOriginKind(str, Enum):
    """How an immutable Artifact version entered the local catalog."""

    LOCAL_DERIVED = "LOCAL_DERIVED"
    USER_IMPORTED = "USER_IMPORTED"
    NETWORK_IMPORTED = "NETWORK_IMPORTED"
    VENDORED_BUILTIN = "VENDORED_BUILTIN"
    UNKNOWN_LEGACY = "UNKNOWN_LEGACY"


def user_imported_origin(*, ingress: str = "DIRECT_LOCAL_REGISTRATION") -> Mapping[str, str]:
    if ingress not in {
        "DIRECT_LOCAL_REGISTRATION",
        "MANAGED_SKILL_REGISTRATION",
        "MCP_POLICY_REGISTRATION",
    }:
        raise ValueError("Unsupported user-imported Artifact ingress")
    return {"schema": ARTIFACT_ORIGIN_SCHEMA, "ingress": ingress}


def local_derived_origin(
    *,
    base_artifact_id: str,
    base_version: str,
    base_manifest_digest: str,
    producer: str,
    evidence_digest: str,
) -> Mapping[str, str]:
    return {
        "schema": ARTIFACT_ORIGIN_SCHEMA,
        "base_artifact_id": base_artifact_id,
        "base_version": base_version,
        "base_manifest_digest": base_manifest_digest,
        "producer": producer,
        "evidence_digest": evidence_digest,
    }


def network_imported_origin(
    *, snapshot_id: str, source_label: str, registry_id: str, bundle_digest: str
) -> Mapping[str, str]:
    return {
        "schema": ARTIFACT_ORIGIN_SCHEMA,
        "snapshot_id": snapshot_id,
        "source_label": source_label,
        "registry_id": registry_id,
        "bundle_digest": bundle_digest,
    }


def unknown_legacy_origin(*, prior_schema_version: int) -> Mapping[str, Any]:
    return {
        "schema": ARTIFACT_ORIGIN_SCHEMA,
        "migration": "LEGACY_ORIGIN_UNAVAILABLE",
        "prior_schema_version": prior_schema_version,
    }


def validate_artifact_origin(
    origin_kind: ArtifactOriginKind | str,
    metadata: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]]:
    """Validate one immutable origin and return its stable primitive form."""

    try:
        kind = ArtifactOriginKind(origin_kind)
    except ValueError as exc:
        raise ValueError("Unsupported Artifact origin kind") from exc
    if not isinstance(metadata, Mapping) or metadata.get("schema") != ARTIFACT_ORIGIN_SCHEMA:
        raise ValueError("Artifact origin metadata has an unsupported schema")

    fields = set(metadata)
    expected: set[str]
    if kind is ArtifactOriginKind.USER_IMPORTED:
        expected = {"schema", "ingress"}
        user_imported_origin(ingress=str(metadata.get("ingress", "")))
    elif kind is ArtifactOriginKind.LOCAL_DERIVED:
        expected = {
            "schema",
            "base_artifact_id",
            "base_version",
            "base_manifest_digest",
            "producer",
            "evidence_digest",
        }
        if not _SAFE_ID.fullmatch(str(metadata.get("base_artifact_id", ""))):
            raise ValueError("Local-derived Artifact origin has an invalid base artifact id")
        if not _SEMVER.fullmatch(str(metadata.get("base_version", ""))):
            raise ValueError("Local-derived Artifact origin has an invalid base version")
        if not _SHA256.fullmatch(str(metadata.get("base_manifest_digest", ""))):
            raise ValueError("Local-derived Artifact origin has an invalid base digest")
        if not _SAFE_ID.fullmatch(str(metadata.get("producer", ""))):
            raise ValueError("Local-derived Artifact origin has an invalid producer")
        if not _SHA256.fullmatch(str(metadata.get("evidence_digest", ""))):
            raise ValueError("Local-derived Artifact origin has an invalid evidence digest")
    elif kind is ArtifactOriginKind.NETWORK_IMPORTED:
        expected = {"schema", "snapshot_id", "source_label", "registry_id", "bundle_digest"}
        for field in ("snapshot_id", "source_label", "registry_id"):
            value = metadata.get(field)
            if not isinstance(value, str) or not value or len(value) > 200:
                raise ValueError(f"Network-imported Artifact origin has an invalid {field}")
        if not _SHA256.fullmatch(str(metadata.get("bundle_digest", ""))):
            raise ValueError("Network-imported Artifact origin has an invalid bundle digest")
    elif kind is ArtifactOriginKind.VENDORED_BUILTIN:
        expected = {"schema", "source_register_id", "source_commit"}
        if not _SAFE_ID.fullmatch(str(metadata.get("source_register_id", ""))):
            raise ValueError("Vendored Artifact origin has an invalid source register id")
        commit = metadata.get("source_commit")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ValueError("Vendored Artifact origin has an invalid source commit")
    else:
        expected = {"schema", "migration", "prior_schema_version"}
        if metadata.get("migration") != "LEGACY_ORIGIN_UNAVAILABLE":
            raise ValueError("Legacy Artifact origin requires the migration marker")
        prior = metadata.get("prior_schema_version")
        if not isinstance(prior, int) or prior < 1:
            raise ValueError("Legacy Artifact origin requires a prior schema version")

    if fields != expected:
        raise ValueError("Artifact origin metadata has unsupported or missing fields")
    return kind.value, dict(metadata)
