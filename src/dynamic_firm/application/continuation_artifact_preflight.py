"""Fail-closed Evolution Artifact checks before same-Job continuation.

The ACTIVE JOB audit deliberately retains only immutable Artifact references.
Before a continuation re-enters the Firm Kernel, this module proves that those
references still name the exact local runtime snapshot and that projecting the
catalog again produces the exact network-derived Skill inputs already frozen
inside the original request.  It performs no activation, update, or network
operation.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from dynamic_firm.evolution.runtime_adapter import (
    SUPPORTED_RUNTIME_CONTRACTS,
    EvolutionRuntimeArtifactAdapter,
    RuntimeArtifactResolution,
    runtime_artifact_scopes,
)
from dynamic_firm.evolution.service import validate_evolution_artifact
from dynamic_firm.evolution.store import EvolutionStore
from dynamic_firm.kernel.models import CompanyRunRequest
from dynamic_firm.runtime.models import VersionedContent


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PIN_FIELDS = ("kind", "artifact_id", "version", "manifest_digest", "scope_key")


class ContinuationArtifactPreflightCode(StrEnum):
    """Stable, content-free reasons why a continuation was refused."""

    STORE_REQUIRED = "ARTIFACT_STORE_REQUIRED"
    PIN_INVALID = "ARTIFACT_PIN_INVALID"
    RUNTIME_PIN_MISMATCH = "ARTIFACT_RUNTIME_PIN_MISMATCH"
    CATALOG_INVALID = "ARTIFACT_CATALOG_INVALID"
    RUNTIME_CONTRACT_UNSUPPORTED = "ARTIFACT_RUNTIME_CONTRACT_UNSUPPORTED"
    SKILL_SNAPSHOT_MISMATCH = "ARTIFACT_SKILL_SNAPSHOT_MISMATCH"


class ContinuationArtifactPreflightError(RuntimeError):
    """Safe refusal raised before a continued Job may dispatch work."""

    def __init__(self, code: ContinuationArtifactPreflightCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ContinuationArtifactPreflightResult:
    """Provider-free proof that the original Artifact projection is intact."""

    job_id: str
    pin_count: int
    projected_skill_count: int
    resolution: RuntimeArtifactResolution | None


def preflight_continuation_artifacts(
    *,
    request: CompanyRunRequest,
    audit_pins: Sequence[Mapping[str, Any]],
    store: EvolutionStore | None,
) -> ContinuationArtifactPreflightResult:
    """Revalidate exact local Artifact inputs for one frozen request.

    Historical Jobs with no Artifact pins need no optional Evolution store.
    Once any pin exists, absence, catalog drift, unsupported runtime contracts,
    or a changed frozen Skill projection is a hard stop.  The function never
    creates a store and never falls back to the currently active release.
    """

    expected = _normalize_pins(audit_pins)
    if not expected:
        if not _projected_skills_match_frozen_request(
            {}, request.employee_skill_snapshots
        ):
            raise ContinuationArtifactPreflightError(
                ContinuationArtifactPreflightCode.SKILL_SNAPSHOT_MISMATCH
            )
        return ContinuationArtifactPreflightResult(
            job_id=request.job_id,
            pin_count=0,
            projected_skill_count=0,
            resolution=None,
        )
    if store is None:
        raise ContinuationArtifactPreflightError(
            ContinuationArtifactPreflightCode.STORE_REQUIRED
        )

    try:
        current = _normalize_pins(store.list_runtime_job_artifact_pins(request.job_id))
    except ContinuationArtifactPreflightError as exc:
        raise ContinuationArtifactPreflightError(
            ContinuationArtifactPreflightCode.CATALOG_INVALID
        ) from exc
    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
        raise ContinuationArtifactPreflightError(
            ContinuationArtifactPreflightCode.CATALOG_INVALID
        ) from exc
    if current != expected:
        raise ContinuationArtifactPreflightError(
            ContinuationArtifactPreflightCode.RUNTIME_PIN_MISMATCH
        )

    permitted_scopes = frozenset(runtime_artifact_scopes(request.roster))
    if any(pin["scope_key"] not in permitted_scopes for pin in expected):
        raise ContinuationArtifactPreflightError(
            ContinuationArtifactPreflightCode.PIN_INVALID
        )

    try:
        _validate_catalog_contracts(store, expected)
        resolution = EvolutionRuntimeArtifactAdapter(store).resolve(
            job_id=request.job_id,
            roster=request.roster,
            pins=expected,
        )
    except ContinuationArtifactPreflightError:
        raise
    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
        raise ContinuationArtifactPreflightError(
            ContinuationArtifactPreflightCode.CATALOG_INVALID
        ) from exc

    if not _projected_skills_match_frozen_request(
        resolution.employee_skills,
        request.employee_skill_snapshots,
    ):
        raise ContinuationArtifactPreflightError(
            ContinuationArtifactPreflightCode.SKILL_SNAPSHOT_MISMATCH
        )
    return ContinuationArtifactPreflightResult(
        job_id=request.job_id,
        pin_count=len(expected),
        projected_skill_count=sum(len(items) for items in resolution.employee_skills.values()),
        resolution=resolution,
    )


def preflight_continuation_artifacts_from_state(
    *,
    request: CompanyRunRequest,
    audit_pins: Sequence[Mapping[str, Any]],
    runtime_state_path: Path,
) -> ContinuationArtifactPreflightResult:
    """Open only an existing sibling catalog and close it after validation."""

    evolution_path = runtime_state_path.with_name(
        f"{runtime_state_path.stem}.evolution.db"
    )
    if not audit_pins or not evolution_path.is_file():
        return preflight_continuation_artifacts(
            request=request,
            audit_pins=audit_pins,
            store=None,
        )
    with EvolutionStore(evolution_path, timeout_seconds=0.05) as evolution_store:
        return preflight_continuation_artifacts(
            request=request,
            audit_pins=audit_pins,
            store=evolution_store,
        )


def _normalize_pins(
    pins: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, str], ...]:
    if isinstance(pins, (str, bytes)) or len(pins) > 64:
        raise ContinuationArtifactPreflightError(
            ContinuationArtifactPreflightCode.PIN_INVALID
        )
    normalized: list[Mapping[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for pin in pins:
        if not isinstance(pin, Mapping):
            raise ContinuationArtifactPreflightError(
                ContinuationArtifactPreflightCode.PIN_INVALID
            )
        item = {field: str(pin.get(field, "")) for field in _PIN_FIELDS}
        if (
            any(
                not item[field] or len(item[field]) > 160
                for field in ("kind", "artifact_id", "version", "scope_key")
            )
            or _SHA256.fullmatch(item["manifest_digest"]) is None
        ):
            raise ContinuationArtifactPreflightError(
                ContinuationArtifactPreflightCode.PIN_INVALID
            )
        identity = (item["scope_key"], item["artifact_id"])
        if identity in identities:
            raise ContinuationArtifactPreflightError(
                ContinuationArtifactPreflightCode.PIN_INVALID
            )
        identities.add(identity)
        normalized.append(item)
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                item["scope_key"],
                item["kind"],
                item["artifact_id"],
                item["version"],
            ),
        )
    )


def _validate_catalog_contracts(
    store: EvolutionStore,
    pins: Sequence[Mapping[str, str]],
) -> None:
    for pin in pins:
        artifact = store.get_artifact_version(pin["artifact_id"], pin["version"])
        manifest = validate_evolution_artifact(artifact["manifest"])
        if (
            manifest["artifact_id"] != pin["artifact_id"]
            or manifest["version"] != pin["version"]
            or manifest["kind"] != pin["kind"]
            or str(artifact["manifest_digest"]) != pin["manifest_digest"]
        ):
            raise ContinuationArtifactPreflightError(
                ContinuationArtifactPreflightCode.CATALOG_INVALID
            )
        if manifest["compatibility"]["runtime_contract"] not in SUPPORTED_RUNTIME_CONTRACTS:
            raise ContinuationArtifactPreflightError(
                ContinuationArtifactPreflightCode.RUNTIME_CONTRACT_UNSUPPORTED
            )


def _projected_skills_match_frozen_request(
    projected: Mapping[str, tuple[VersionedContent, ...]],
    frozen: Mapping[str, tuple[VersionedContent, ...]],
) -> bool:
    projected_entries = [
        ((employee_id, item.content_id, item.revision), item)
        for employee_id, items in projected.items()
        for item in items
    ]
    frozen_network_entries = [
        ((employee_id, item.content_id, item.revision), item)
        for employee_id, items in frozen.items()
        for item in items
        if item.content_id.startswith("employee-skill:") and ":network:" in item.content_id
    ]
    projected_items = dict(projected_entries)
    frozen_network_items = dict(frozen_network_entries)
    return (
        len(projected_entries) == len(projected_items)
        and len(frozen_network_entries) == len(frozen_network_items)
        and projected_items == frozen_network_items
    )
