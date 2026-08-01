"""Read-only diagnostics projection for the Knowledge lifecycle.

Archive/restore and destructive deletion remain separate lifecycle actions.
This component owns only bounded health inspection and must not repair, scan,
or mutate user-owned Knowledge state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

from . import lifecycle as _lifecycle
from .locking import KnowledgeStateLock


_ALLOWLISTED_TABLES = (
    "knowledge_folders", "knowledge_folder_entries", "knowledge_assets",
    "knowledge_representations", "knowledge_chunks", "knowledge_records",
    "evidence_packs", "knowledge_write_candidates", "knowledge_intents",
    "knowledge_decisions", "knowledge_execution_bindings",
    "knowledge_epistemic_annotations", "knowledge_decision_contexts",
    "knowledge_oracle_contracts", "knowledge_outcome_observations",
)


def _knowledge_diagnostics_unlocked(database_path: str | Path, vault_path: str | Path):
    """Return content-free health facts without repairing or opening a Store."""

    requested_database = Path(database_path).expanduser()
    requested_vault = Path(vault_path).expanduser()
    asset_marker = requested_vault / ".asset-delete.json"
    pending_asset_mutation = asset_marker.exists() or asset_marker.is_symlink()
    database_present = requested_database.exists() and not requested_database.is_symlink() and requested_database.is_file()
    vault_present = requested_vault.exists() and not requested_vault.is_symlink() and requested_vault.is_dir()
    if not database_present:
        return _lifecycle.KnowledgeDiagnostics(
            schema_version="noruct.knowledge-diagnostics.v1", database_present=False,
            vault_present=vault_present, database_integrity="not_present",
            knowledge_schema_version=None, table_counts={}, referenced_object_count=0,
            present_object_count=0, missing_object_count=0, invalid_object_count=0,
            referenced_bytes=0, pending_asset_mutation=pending_asset_mutation,
        )

    database = requested_database.resolve()
    vault = requested_vault.resolve() if vault_present else requested_vault
    counts: dict[str, int] = {}
    integrity = "failed"
    schema_version: int | None = None
    references = ()
    try:
        connection = _lifecycle._connect_read_only(database)
        try:
            integrity = _lifecycle._integrity(connection)
            if integrity == "ok":
                available = {
                    str(row[0])
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                for table in _ALLOWLISTED_TABLES:
                    if table in available:
                        counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            connection.close()
        if integrity == "ok":
            schema_version, references = _lifecycle._schema_and_references(database)
    except (OSError, sqlite3.DatabaseError, ValueError):
        integrity = "failed"
        counts = {}
        references = ()

    present = missing = invalid = 0
    if integrity == "ok":
        for reference in references:
            if not vault_present:
                missing += 1
                continue
            try:
                lexical = vault.joinpath(*PurePosixPath(reference.relative_path).parts)
                if lexical.is_symlink():
                    invalid += 1
                    continue
                target = _lifecycle._safe_vault_file(vault, reference.relative_path)
                digest, size = _lifecycle._sha256_file(target, maximum=reference.byte_size)
                if digest == reference.sha256 and size == reference.byte_size:
                    present += 1
                else:
                    invalid += 1
            except ValueError:
                lexical = vault.joinpath(*PurePosixPath(reference.relative_path).parts)
                if lexical.exists() or lexical.is_symlink():
                    invalid += 1
                else:
                    missing += 1
    return _lifecycle.KnowledgeDiagnostics(
        schema_version="noruct.knowledge-diagnostics.v1", database_present=True,
        vault_present=vault_present, database_integrity=integrity,
        knowledge_schema_version=schema_version, table_counts=counts,
        referenced_object_count=len(references), present_object_count=present,
        missing_object_count=missing, invalid_object_count=invalid,
        referenced_bytes=sum(reference.byte_size for reference in references),
        pending_asset_mutation=pending_asset_mutation,
    )


def knowledge_diagnostics(database_path: str | Path, vault_path: str | Path):
    """Read a stable diagnostic snapshot or refuse while a mutation is active."""

    database = Path(database_path).expanduser()
    if not database.parent.exists():
        return _knowledge_diagnostics_unlocked(database_path, vault_path)
    with KnowledgeStateLock(database, mode="shared"):
        return _knowledge_diagnostics_unlocked(database_path, vault_path)


__all__ = ["knowledge_diagnostics"]
