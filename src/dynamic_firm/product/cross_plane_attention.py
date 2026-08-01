"""Content-free, read-only supplemental attention across local state planes.

The Company runtime remains the authority for Job/approval/effect incidents.
Knowledge and Evolution keep their own SQLite authorities, so this module opens
their sibling databases only through SQLite's read-only URI mode.  It never
initializes a schema, purges retention state, contacts a network, or resolves
any candidate.  The resulting items are operator navigation hints, not action
tokens or an implicit cross-plane workflow.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dynamic_firm.knowledge.store import knowledge_state_path


DEFAULT_SUPPLEMENTAL_ATTENTION_LIMIT = 20
MAX_SUPPLEMENTAL_ATTENTION_LIMIT = 100


class SupplementalAttentionKind(StrEnum):
    KNOWLEDGE_CANDIDATE = "KNOWLEDGE_CANDIDATE"
    ARTIFACT_INSTALLATION = "ARTIFACT_INSTALLATION"
    ARTIFACT_REGISTRY_REVIEW = "ARTIFACT_REGISTRY_REVIEW"


@dataclass(frozen=True, slots=True)
class SupplementalAttentionItem:
    """One content-free item owned by an existing non-Company plane."""

    kind: SupplementalAttentionKind
    subject_id: str
    state: str
    created_at: str
    recommended_action: str


@dataclass(frozen=True, slots=True)
class SupplementalOperatorAttention:
    """Bounded supplemental queue with explicit partial/unavailable state."""

    knowledge_state: str
    evolution_state: str
    knowledge_pending_candidate_count: int
    artifact_review_count: int
    truncated: bool
    items: tuple[SupplementalAttentionItem, ...]


def inspect_supplemental_operator_attention(
    runtime_state_path: Path,
    *,
    limit: int = DEFAULT_SUPPLEMENTAL_ATTENTION_LIMIT,
) -> SupplementalOperatorAttention:
    """Read pending Knowledge and Artifact review facts without opening owners.

    A missing sibling database means the optional plane was not configured; an
    unreadable or incompatible one is shown as unavailable rather than silently
    created, migrated, or repaired from an operator surface.
    """

    if not 1 <= limit <= MAX_SUPPLEMENTAL_ATTENTION_LIMIT:
        raise ValueError(
            "Supplemental operator attention limit must be between 1 and 100"
        )
    state_path = runtime_state_path.expanduser().resolve()
    knowledge_items, knowledge_count, knowledge_state = _knowledge_items(
        knowledge_state_path(state_path), limit=limit
    )
    remaining = max(0, limit - len(knowledge_items))
    evolution_items, artifact_count, evolution_state = _evolution_items(
        state_path.with_name(f"{state_path.stem}.evolution.db"), limit=remaining
    )
    items = (*knowledge_items, *evolution_items)
    return SupplementalOperatorAttention(
        knowledge_state=knowledge_state,
        evolution_state=evolution_state,
        knowledge_pending_candidate_count=knowledge_count,
        artifact_review_count=artifact_count,
        truncated=(knowledge_count + artifact_count) > len(items),
        items=items,
    )


def _readonly_connection(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise OSError("state path is not a regular file")
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def _knowledge_items(
    path: Path, *, limit: int
) -> tuple[tuple[SupplementalAttentionItem, ...], int, str]:
    try:
        connection = _readonly_connection(path)
        if connection is None:
            return (), 0, "NOT_CONFIGURED"
        try:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_write_candidates WHERE status = 'PENDING'"
                ).fetchone()[0]
            )
            rows = connection.execute(
                """SELECT candidate_id, created_at
                     FROM knowledge_write_candidates
                    WHERE status = 'PENDING'
                    ORDER BY created_at DESC, candidate_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return (), 0, "UNAVAILABLE"
    return (
        tuple(
            SupplementalAttentionItem(
                kind=SupplementalAttentionKind.KNOWLEDGE_CANDIDATE,
                subject_id=str(row[0]),
                state="PENDING_EXPLICIT_REVIEW",
                created_at=str(row[1]),
                recommended_action=(
                    "Open Knowledge candidates or /workbench review, inspect the "
                    "candidate, then explicitly accept or reject it."
                ),
            )
            for row in rows
        ),
        count,
        "READY",
    )


def _evolution_items(
    path: Path, *, limit: int
) -> tuple[tuple[SupplementalAttentionItem, ...], int, str]:
    try:
        connection = _readonly_connection(path)
        if connection is None:
            return (), 0, "NOT_CONFIGURED"
        try:
            count = int(
                connection.execute(
                    """SELECT
                          (SELECT COUNT(*) FROM evolution_artifact_installations
                            WHERE status = 'STAGED') +
                          (SELECT COUNT(*) FROM trusted_artifact_registry_snapshots
                            WHERE status = 'STAGED_TRUSTED_NOT_IMPORTABLE')"""
                ).fetchone()[0]
            )
            rows = connection.execute(
                """SELECT installation_id AS subject_id, staged_at AS created_at,
                          'ARTIFACT_INSTALLATION' AS kind
                     FROM evolution_artifact_installations WHERE status = 'STAGED'
                    UNION ALL
                   SELECT snapshot_id AS subject_id, verified_at AS created_at,
                          'ARTIFACT_REGISTRY_REVIEW' AS kind
                     FROM trusted_artifact_registry_snapshots
                    WHERE status = 'STAGED_TRUSTED_NOT_IMPORTABLE'
                    ORDER BY created_at DESC, subject_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return (), 0, "UNAVAILABLE"
    items = tuple(
        SupplementalAttentionItem(
            kind=SupplementalAttentionKind(str(row[2])),
            subject_id=str(row[0]),
            state=(
                "STAGED_EXACT_ARTIFACT"
                if str(row[2]) == SupplementalAttentionKind.ARTIFACT_INSTALLATION
                else "TRUSTED_REGISTRY_REVIEW_REQUIRED"
            ),
            created_at=str(row[1]),
            recommended_action=(
                "Inspect the exact staged Artifact and explicitly install/activate it "
                "for a future Job; running Jobs remain pinned."
                if str(row[2]) == SupplementalAttentionKind.ARTIFACT_INSTALLATION
                else "Inspect the trusted staged Artifact registry and explicitly "
                "approve or reject its import review."
            ),
        )
        for row in rows
    )
    return items, count, "READY"


__all__ = [
    "DEFAULT_SUPPLEMENTAL_ATTENTION_LIMIT",
    "MAX_SUPPLEMENTAL_ATTENTION_LIMIT",
    "SupplementalAttentionItem",
    "SupplementalAttentionKind",
    "SupplementalOperatorAttention",
    "inspect_supplemental_operator_attention",
]
