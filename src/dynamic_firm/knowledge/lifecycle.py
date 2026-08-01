from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Mapping

from .locking import KnowledgeStateLock, knowledge_mutation_marker_path
from .page_schema_surface import (
    PAGE_PUBLICATION_FOREIGN_KEYS,
    PAGE_PUBLICATION_INDEX_COLUMNS,
    PAGE_PUBLICATION_TABLE_COLUMNS,
)
from .store import SCHEMA_VERSION, KnowledgeStore
from .vault import MAX_ASSET_BYTES


ARCHIVE_SCHEMA = "noruct.knowledge-archive.v1"
MANIFEST_NAME = "manifest.json"
DATABASE_ARCHIVE_NAME = "knowledge.db"
VAULT_ARCHIVE_PREFIX = "vault/"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MUTATION_MARKER_BYTES = 64 * 1024
MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000
_DATABASE_SIDECARS = ("", "-wal", "-shm", "-journal")
_BUFFER_BYTES = 1024 * 1024
_FOLDER_FTS5_TABLES = frozenset(
    {
        "knowledge_folder_fts",
        "knowledge_folder_fts_config",
        "knowledge_folder_fts_content",
        "knowledge_folder_fts_data",
        "knowledge_folder_fts_docsize",
        "knowledge_folder_fts_idx",
    }
)
_MANAGED_FTS5_TABLES = frozenset(
    {
        "knowledge_chunk_fts",
        "knowledge_chunk_fts_config",
        "knowledge_chunk_fts_content",
        "knowledge_chunk_fts_data",
        "knowledge_chunk_fts_docsize",
        "knowledge_chunk_fts_idx",
    }
)
_OPTIONAL_FTS5_TABLES = _FOLDER_FTS5_TABLES | _MANAGED_FTS5_TABLES
_EXPECTED_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "knowledge_meta": ("key", "value"),
    "knowledge_folders": (
        "folder_id", "root_path", "display_name", "access_scope", "ignore_globs_json", "status",
        "scan_generation", "last_scan_status", "last_scan_at", "created_at", "updated_at",
    ),
    "knowledge_folder_entries": (
        "entry_id", "folder_id", "relative_path", "content_hash", "byte_size",
        "modified_ns", "media_type", "index_status", "index_text", "index_error",
        "indexer_revision", "snapshot_asset_id", "revision", "last_seen_generation",
        "created_at", "updated_at",
    ),
    "knowledge_folder_cjk_candidates": ("entry_id", "token"),
    "knowledge_chunk_cjk_candidates": ("chunk_id", "token"),
    "knowledge_assets": (
        "asset_id", "content_hash", "original_name", "title", "media_type", "byte_size",
        "vault_relative_path", "origin", "access_scope", "status", "processor",
        "processor_version", "processing_error", "parent_asset_id", "revision",
        "labels_json", "created_at", "updated_at",
    ),
    "knowledge_remote_asset_sources": (
        "asset_id", "source_url", "response_etag", "response_last_modified", "fetched_at", "checked_at",
    ),
    "knowledge_processing_attempts": (
        "attempt_id", "asset_id", "status", "processor", "processor_version",
        "error_code", "error_summary", "created_at",
    ),
    "knowledge_representations": (
        "representation_id", "asset_id", "kind", "media_type", "content_hash", "byte_size",
        "vault_relative_path", "processor", "processor_version", "revision", "created_at",
    ),
    "knowledge_chunks": (
        "chunk_id", "asset_id", "representation_id", "ordinal", "content", "content_hash",
        "char_start", "char_end", "location_json",
    ),
    "knowledge_records": (
        "record_id", "kind", "statement", "status", "confidence", "source_asset_id",
        "source_representation_id", "source_span_json", "revision", "supersedes_record_id",
        "source_candidate_id", "source_job_id", "evidence_pack_id", "access_scope",
        "created_at", "updated_at",
    ),
    "evidence_packs": (
        "pack_id", "query", "item_count", "selected_bytes", "candidate_count",
        "payload_json", "digest", "access_scope", "revision", "conflict_refs_json", "created_at",
    ),
    "evidence_pack_sources": (
        "pack_id", "evidence_id", "asset_id", "representation_id", "source_type",
        "source_id", "source_revision",
    ),
    "knowledge_write_candidates": (
        "candidate_id", "job_id", "kind", "statement", "evidence_pack_id", "status",
        "created_at", "resolved_at", "accepted_record_id",
    ),
    **PAGE_PUBLICATION_TABLE_COLUMNS,
    "knowledge_intents": (
        "intent_id", "goal", "priority", "status", "constraints_json",
        "acceptance_criteria_json", "knowledge_query", "revision", "created_at", "updated_at",
    ),
    "knowledge_intent_revisions": (
        "intent_id", "revision", "payload_json", "content_hash", "created_at",
    ),
    "knowledge_decisions": (
        "decision_id", "statement", "rationale", "status", "intent_id", "evidence_pack_id",
        "supersedes_decision_id", "review_at", "actor", "revision", "created_at", "updated_at",
    ),
    "knowledge_decision_revisions": (
        "decision_id", "revision", "payload_json", "content_hash", "created_at",
    ),
    "knowledge_questions": (
        "question_id", "prompt", "owner", "status", "intent_id", "decision_id",
        "evidence_pack_id", "answer_criteria_json", "knowledge_query", "review_at",
        "revision", "created_at", "updated_at",
    ),
    "knowledge_question_revisions": (
        "question_id", "revision", "payload_json", "content_hash", "created_at",
    ),
    "knowledge_research_requests": (
        "request_id", "title", "objective", "owner", "status", "question_id", "intent_id",
        "decision_id", "decision_revision", "evidence_pack_id", "knowledge_query",
        "required_evidence_json", "freshness_at", "counterargument_required", "max_cost_units",
        "max_duration_minutes", "compiled_intent_id", "revision", "created_at", "updated_at",
    ),
    "knowledge_research_request_revisions": (
        "request_id", "revision", "payload_json", "content_hash", "created_at",
    ),
    "knowledge_execution_bindings": (
        "binding_id", "request_id", "job_id", "intent_id", "intent_revision", "intent_hash",
        "pack_id", "pack_revision", "pack_digest", "delivery_digest", "item_count",
        "selected_bytes", "access_scope", "status", "job_status", "candidate_id", "created_at",
        "updated_at",
    ),
    "knowledge_epistemic_annotations": (
        "subject_type", "subject_id", "epistemic_status", "trust_class",
        "freshness_expires_at", "conflict_refs_json", "unknown_refs_json",
        "source_revision", "created_at", "updated_at",
    ),
    "knowledge_decision_contexts": (
        "snapshot_id", "binding_id", "request_id", "job_id", "intent_id",
        "intent_revision", "intent_hash", "decision_id", "decision_revision",
        "evidence_pack_id", "evidence_pack_revision", "evidence_pack_digest",
        "known_refs_json", "unknown_refs_json", "assumptions_json",
        "constraints_json", "excluded_alternatives_json", "owner_ref",
        "authority_ref", "supersedes_snapshot_id", "content_digest", "created_at",
    ),
    "knowledge_oracle_contracts": (
        "oracle_contract_id", "binding_id", "request_id", "job_id", "revision",
        "acceptance_criteria_json", "failure_criteria_json", "observable_signals_json",
        "observation_channel", "validator_type", "independence_class",
        "accountable_owner_ref", "authority_ref", "feedback_due_at",
        "reversibility_class", "risk_class", "proxy_metric",
        "proxy_failure_modes_json", "inconclusive_policy", "max_attempts",
        "max_evidence_items", "content_digest", "created_at",
    ),
    "knowledge_outcome_observations": (
        "outcome_id", "oracle_contract_id", "binding_id", "request_id", "job_id",
        "result_digest", "expected_signal", "observed_signal", "observed_at",
        "source_ref", "verdict", "confounders_json", "attribution_status",
        "reviewer_ref", "created_at", "updated_at",
    ),
    "knowledge_events": (
        "event_id", "event_type", "subject_type", "subject_id", "metadata_json", "created_at",
    ),
}
_EXPECTED_INDEX_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "knowledge_folders_status_idx": ("status", "updated_at"),
    "knowledge_folder_entries_current_idx": ("folder_id", "index_status", "relative_path"),
    "knowledge_folder_entries_hash_idx": ("folder_id", "content_hash", "index_status"),
    "knowledge_folder_cjk_candidates_token_idx": ("token", "entry_id"),
    "knowledge_chunk_cjk_candidates_token_idx": ("token", "chunk_id"),
    "knowledge_assets_created_idx": ("created_at", "asset_id"),
    "knowledge_assets_status_idx": ("status", "created_at"),
    "knowledge_processing_asset_idx": ("asset_id", "created_at"),
    "knowledge_representations_asset_idx": ("asset_id", "revision"),
    "knowledge_chunks_asset_idx": ("asset_id", "representation_id", "ordinal"),
    "knowledge_records_updated_idx": ("updated_at", "record_id"),
    "evidence_pack_sources_asset_idx": ("asset_id", "pack_id"),
    "knowledge_candidates_status_idx": ("status", "created_at"),
    **PAGE_PUBLICATION_INDEX_COLUMNS,
    "knowledge_intents_status_priority_idx": ("status", "priority", "updated_at"),
    "knowledge_decisions_updated_idx": ("updated_at", "decision_id"),
    "knowledge_questions_status_updated_idx": ("status", "updated_at", "question_id"),
    "knowledge_research_requests_status_updated_idx": ("status", "updated_at", "request_id"),
    "knowledge_execution_bindings_status_idx": ("status", "updated_at"),
    "knowledge_epistemic_status_idx": ("epistemic_status", "updated_at"),
    "knowledge_outcome_verdict_idx": ("verdict", "updated_at"),
    "knowledge_events_subject_idx": ("subject_type", "subject_id", "created_at"),
}
_EXPECTED_FOREIGN_KEYS: Mapping[str, frozenset[tuple[str, str, str, str, str]]] = {
    "knowledge_meta": frozenset(),
    "knowledge_folders": frozenset(),
    "knowledge_folder_entries": frozenset(
        {
            ("folder_id", "knowledge_folders", "folder_id", "NO ACTION", "CASCADE"),
            ("snapshot_asset_id", "knowledge_assets", "asset_id", "NO ACTION", "SET NULL"),
        }
    ),
    "knowledge_folder_cjk_candidates": frozenset(
        {("entry_id", "knowledge_folder_entries", "entry_id", "NO ACTION", "CASCADE")}
    ),
    "knowledge_chunk_cjk_candidates": frozenset(
        {("chunk_id", "knowledge_chunks", "chunk_id", "NO ACTION", "CASCADE")}
    ),
    "knowledge_assets": frozenset(
        {("parent_asset_id", "knowledge_assets", "asset_id", "NO ACTION", "NO ACTION")}
    ),
    "knowledge_remote_asset_sources": frozenset(
        {("asset_id", "knowledge_assets", "asset_id", "NO ACTION", "CASCADE")}
    ),
    "knowledge_processing_attempts": frozenset(
        {("asset_id", "knowledge_assets", "asset_id", "NO ACTION", "CASCADE")}
    ),
    "knowledge_representations": frozenset(
        {("asset_id", "knowledge_assets", "asset_id", "NO ACTION", "CASCADE")}
    ),
    "knowledge_chunks": frozenset(
        {
            ("asset_id", "knowledge_assets", "asset_id", "NO ACTION", "CASCADE"),
            (
                "representation_id",
                "knowledge_representations",
                "representation_id",
                "NO ACTION",
                "CASCADE",
            ),
        }
    ),
    "knowledge_records": frozenset(
        {
            ("source_asset_id", "knowledge_assets", "asset_id", "NO ACTION", "SET NULL"),
            (
                "source_representation_id",
                "knowledge_representations",
                "representation_id",
                "NO ACTION",
                "SET NULL",
            ),
            (
                "supersedes_record_id",
                "knowledge_records",
                "record_id",
                "NO ACTION",
                "SET NULL",
            ),
            (
                "source_candidate_id",
                "knowledge_write_candidates",
                "candidate_id",
                "NO ACTION",
                "SET NULL",
            ),
            ("evidence_pack_id", "evidence_packs", "pack_id", "NO ACTION", "SET NULL"),
        }
    ),
    "evidence_packs": frozenset(),
    "evidence_pack_sources": frozenset(
        {
            ("pack_id", "evidence_packs", "pack_id", "NO ACTION", "CASCADE"),
            ("asset_id", "knowledge_assets", "asset_id", "NO ACTION", "CASCADE"),
            (
                "representation_id",
                "knowledge_representations",
                "representation_id",
                "NO ACTION",
                "CASCADE",
            ),
        }
    ),
    "knowledge_write_candidates": frozenset(
        {
            ("evidence_pack_id", "evidence_packs", "pack_id", "NO ACTION", "SET NULL"),
            (
                "accepted_record_id",
                "knowledge_records",
                "record_id",
                "NO ACTION",
                "SET NULL",
            ),
        }
    ),
    **PAGE_PUBLICATION_FOREIGN_KEYS,
    "knowledge_intents": frozenset(),
    "knowledge_intent_revisions": frozenset(),
    "knowledge_decisions": frozenset(
        {
            ("intent_id", "knowledge_intents", "intent_id", "NO ACTION", "SET NULL"),
            ("evidence_pack_id", "evidence_packs", "pack_id", "NO ACTION", "SET NULL"),
            (
                "supersedes_decision_id",
                "knowledge_decisions",
                "decision_id",
                "NO ACTION",
                "SET NULL",
            ),
        }
    ),
    "knowledge_decision_revisions": frozenset(),
    "knowledge_questions": frozenset(
        {
            ("intent_id", "knowledge_intents", "intent_id", "NO ACTION", "SET NULL"),
            ("decision_id", "knowledge_decisions", "decision_id", "NO ACTION", "SET NULL"),
            ("evidence_pack_id", "evidence_packs", "pack_id", "NO ACTION", "SET NULL"),
        }
    ),
    "knowledge_question_revisions": frozenset(),
    "knowledge_research_requests": frozenset(
        {
            ("question_id", "knowledge_questions", "question_id", "NO ACTION", "SET NULL"),
            ("intent_id", "knowledge_intents", "intent_id", "NO ACTION", "SET NULL"),
            ("decision_id", "knowledge_decisions", "decision_id", "NO ACTION", "SET NULL"),
            ("evidence_pack_id", "evidence_packs", "pack_id", "NO ACTION", "SET NULL"),
            ("compiled_intent_id", "knowledge_intents", "intent_id", "NO ACTION", "SET NULL"),
        }
    ),
    "knowledge_research_request_revisions": frozenset(),
    "knowledge_execution_bindings": frozenset(
        {
            (
                "candidate_id",
                "knowledge_write_candidates",
                "candidate_id",
                "NO ACTION",
                "SET NULL",
            )
        }
    ),
    "knowledge_epistemic_annotations": frozenset(),
    "knowledge_decision_contexts": frozenset(
        {
            ("binding_id", "knowledge_execution_bindings", "binding_id", "NO ACTION", "NO ACTION"),
            ("decision_id", "knowledge_decisions", "decision_id", "NO ACTION", "SET NULL"),
            ("evidence_pack_id", "evidence_packs", "pack_id", "NO ACTION", "NO ACTION"),
            (
                "supersedes_snapshot_id",
                "knowledge_decision_contexts",
                "snapshot_id",
                "NO ACTION",
                "NO ACTION",
            ),
        }
    ),
    "knowledge_oracle_contracts": frozenset(
        {
            ("binding_id", "knowledge_execution_bindings", "binding_id", "NO ACTION", "NO ACTION"),
        }
    ),
    "knowledge_outcome_observations": frozenset(
        {
            (
                "oracle_contract_id",
                "knowledge_oracle_contracts",
                "oracle_contract_id",
                "NO ACTION",
                "NO ACTION",
            ),
            ("binding_id", "knowledge_execution_bindings", "binding_id", "NO ACTION", "NO ACTION"),
        }
    ),
    "knowledge_events": frozenset(),
}
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class KnowledgeDeletionRecord:
    schema_version: str
    deleted: bool
    deleted_components: tuple[str, ...]
    residual_backup_warning: str


@dataclass(frozen=True, slots=True)
class KnowledgeDiagnostics:
    """Content-free local health data suitable for a support bundle."""

    schema_version: str
    database_present: bool
    vault_present: bool
    database_integrity: str
    knowledge_schema_version: int | None
    table_counts: Mapping[str, int]
    referenced_object_count: int
    present_object_count: int
    missing_object_count: int
    invalid_object_count: int
    referenced_bytes: int
    pending_asset_mutation: bool


class KnowledgeDeletionAuthorization:
    """Opaque, target-bound proof of an explicit destructive caller choice."""

    __slots__ = ("_database", "_vault", "_marker")

    def __init__(self, database: Path, vault: Path, marker: object) -> None:
        if marker is not _DELETION_MARKER:
            raise TypeError("Use authorize_knowledge_deletion()")
        self._database = database
        self._vault = vault
        self._marker = marker


_DELETION_MARKER = object()


@dataclass(frozen=True, slots=True)
class _VaultReference:
    relative_path: str
    sha256: str
    byte_size: int
    kinds: tuple[str, ...]

    def manifest_payload(self) -> dict[str, object]:
        return {
            "archive_path": f"{VAULT_ARCHIVE_PREFIX}{self.relative_path}",
            "byte_size": self.byte_size,
            "reference_kinds": list(self.kinds),
            "sha256": self.sha256,
            "vault_relative_path": self.relative_path,
        }


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path, *, maximum: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    observed = 0
    with _open_regular(path) as handle:
        while chunk := handle.read(_BUFFER_BYTES):
            observed += len(chunk)
            if maximum is not None and observed > maximum:
                raise ValueError("Knowledge lifecycle file exceeds its bounded size limit")
            digest.update(chunk)
    return digest.hexdigest(), observed


def _sha256_handle(handle: BinaryIO, *, maximum: int | None = None) -> tuple[str, int]:
    handle.seek(0)
    digest = hashlib.sha256()
    observed = 0
    while chunk := handle.read(_BUFFER_BYTES):
        observed += len(chunk)
        if maximum is not None and observed > maximum:
            raise ValueError("Knowledge lifecycle file exceeds its bounded size limit")
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest(), observed


def _open_regular(path: Path) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("Knowledge lifecycle source is missing or unsafe") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("Knowledge lifecycle source must be a regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _require_regular(path: str | Path, label: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    resolved = requested.resolve()
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"{label} must be an existing regular file")
    with _open_regular(resolved):
        pass
    return resolved


def _require_vault(path: str | Path) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ValueError("Knowledge Vault root must not be a symbolic link")
    resolved = requested.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError("Knowledge Vault root must be an existing directory")
    return resolved


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Knowledge Vault reference is not a safe relative path")
    pure = PurePosixPath(value)
    unsafe_component = any(
        part in ("", ".", "..")
        or ":" in part
        or part.endswith((" ", "."))
        or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        for part in pure.parts
    )
    if pure.is_absolute() or value != pure.as_posix() or unsafe_component:
        raise ValueError("Knowledge Vault reference is not a safe relative path")
    return value


def _safe_vault_file(root: Path, relative: str) -> Path:
    safe = _safe_relative(relative)
    target = root.joinpath(*PurePosixPath(safe).parts)
    current = root
    for part in PurePosixPath(safe).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Knowledge Vault contains a symbolic link")
    try:
        resolved = target.resolve(strict=True)
    except OSError as error:
        raise ValueError("Knowledge Vault referenced object is missing") from error
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Knowledge Vault referenced object is unsafe")
    return resolved


def _connect_read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    return connection


def _integrity(connection: sqlite3.Connection) -> str:
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as error:
        raise ValueError("Knowledge database failed its integrity check") from error
    return "ok" if rows and all(str(row[0]) == "ok" for row in rows) else "failed"


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    """Bind an archive to the exact semantic DDL emitted by the current store."""

    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'index') "
        "ORDER BY type, name, tbl_name"
    ).fetchall()
    objects: list[dict[str, str]] = []
    for row in rows:
        # FTS5 is an optional, fully rebuildable Folder candidate projection.
        # It must not make archive compatibility depend on a host SQLite build.
        if str(row["name"]) in _OPTIONAL_FTS5_TABLES:
            continue
        sql = row["sql"]
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("Knowledge database schema SQL is incomplete")
        objects.append(
            {
                "name": str(row["name"]),
                "sql": sql.strip(),
                "table": str(row["tbl_name"]),
                "type": str(row["type"]),
            }
        )
    payload: Mapping[str, object] = {
        "objects": objects,
        "schema_version": SCHEMA_VERSION,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


@lru_cache(maxsize=1)
def _current_schema_fingerprint() -> str:
    """Create the current schema once instead of duplicating its DDL in lifecycle code."""

    with tempfile.TemporaryDirectory(prefix="noruct-knowledge-schema-") as temporary:
        database = Path(temporary) / "current.db"
        store = KnowledgeStore(database)
        store.close()
        connection = _connect_read_only(database)
        try:
            return _schema_fingerprint(connection)
        finally:
            connection.close()


def _validate_schema_surface(connection: sqlite3.Connection) -> None:
    trusted = connection.execute("PRAGMA trusted_schema").fetchone()
    if trusted is None or int(trusted[0]) != 0:
        raise ValueError("Knowledge database trusted schema execution is not disabled")

    objects = connection.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'index', 'trigger', 'view')"
    ).fetchall()
    observed_tables = {str(row["name"]) for row in objects if row["type"] == "table"}
    observed_indexes = {str(row["name"]) for row in objects if row["type"] == "index"}
    executable_schema = [
        str(row["name"]) for row in objects if row["type"] in ("trigger", "view")
    ]
    if executable_schema:
        raise ValueError("Knowledge database contains unexpected triggers or views")
    for projection in (_FOLDER_FTS5_TABLES, _MANAGED_FTS5_TABLES):
        observed_fts5_tables = observed_tables & projection
        if observed_fts5_tables and observed_fts5_tables != projection:
            raise ValueError("Knowledge database FTS5 projection is incomplete")
    if observed_tables - _OPTIONAL_FTS5_TABLES != set(_EXPECTED_TABLE_COLUMNS):
        raise ValueError("Knowledge database schema surface is not recognized")
    if observed_indexes != set(_EXPECTED_INDEX_COLUMNS):
        raise ValueError("Knowledge database index surface is not recognized")

    for table, expected_columns in _EXPECTED_TABLE_COLUMNS.items():
        columns = tuple(
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        )
        if columns != expected_columns:
            raise ValueError("Knowledge database table surface is not recognized")
        foreign_keys = frozenset(
            (
                str(row["from"]),
                str(row["table"]),
                str(row["to"]),
                str(row["on_update"]),
                str(row["on_delete"]),
            )
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        )
        if foreign_keys != _EXPECTED_FOREIGN_KEYS[table]:
            raise ValueError("Knowledge database foreign-key surface is not recognized")
    for index, expected_columns in _EXPECTED_INDEX_COLUMNS.items():
        columns = tuple(
            str(row["name"])
            for row in connection.execute(f'PRAGMA index_info("{index}")').fetchall()
        )
        if columns != expected_columns:
            raise ValueError("Knowledge database index surface is not recognized")

    if _schema_fingerprint(connection) != _current_schema_fingerprint():
        raise ValueError("Knowledge database schema semantics are not recognized")

    foreign_key_failures = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_failures:
        raise ValueError("Knowledge database contains invalid foreign-key references")


def _schema_and_references(database: Path) -> tuple[int, tuple[_VaultReference, ...]]:
    connection = _connect_read_only(database)
    try:
        if _integrity(connection) != "ok":
            raise ValueError("Knowledge database failed its integrity check")
        _validate_schema_surface(connection)
        schema = connection.execute(
            "SELECT value FROM knowledge_meta WHERE key = 'schema_version'"
        ).fetchone()
        if schema is None:
            raise ValueError("Knowledge database schema version is missing")
        try:
            schema_version = int(schema[0])
        except (TypeError, ValueError) as error:
            raise ValueError("Knowledge database schema version is invalid") from error
        reference_count = int(
            connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM knowledge_assets) + "
                "(SELECT COUNT(*) FROM knowledge_representations)"
            ).fetchone()[0]
        )
        if reference_count > MAX_ARCHIVE_MEMBERS - 2:
            raise ValueError("Knowledge database exceeds the bounded Vault reference limit")
        rows = connection.execute(
            """
            SELECT vault_relative_path, content_hash, byte_size, 'asset' AS kind
            FROM knowledge_assets
            UNION ALL
            SELECT vault_relative_path, content_hash, byte_size, 'representation' AS kind
            FROM knowledge_representations
            """
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise ValueError("Knowledge database schema is unreadable") from error
    finally:
        connection.close()

    grouped: dict[str, tuple[str, int, set[str]]] = {}
    for row in rows:
        relative = _safe_relative(row["vault_relative_path"])
        digest = str(row["content_hash"])
        size = int(row["byte_size"])
        kind = str(row["kind"])
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Knowledge database contains an invalid content hash")
        if size < 0 or size > MAX_ASSET_BYTES:
            raise ValueError("Knowledge database contains an invalid Vault object size")
        previous = grouped.get(relative)
        if previous is None:
            grouped[relative] = (digest, size, {kind})
        else:
            if previous[0] != digest or previous[1] != size:
                raise ValueError("Knowledge database has conflicting Vault references")
            previous[2].add(kind)
    return schema_version, tuple(
        _VaultReference(path, digest, size, tuple(sorted(kinds)))
        for path, (digest, size, kinds) in sorted(grouped.items())
    )




def authorize_knowledge_deletion(
    database_path: str | Path,
    vault_path: str | Path,
    *,
    confirmed: bool,
) -> KnowledgeDeletionAuthorization:
    """Create target-bound authorization only from an explicit true confirmation."""

    if confirmed is not True:
        raise ValueError("Knowledge deletion requires explicit confirmation")
    database = _archive._restore_target(
        database_path, "Knowledge database deletion target", create_parent=False
    )
    vault = _archive._restore_target(vault_path, "Knowledge Vault deletion target", create_parent=False)
    if (
        vault == Path(vault.anchor)
        or vault == Path.home().resolve()
        or database.is_relative_to(vault)
        or vault.is_relative_to(database)
    ):
        raise ValueError("Knowledge deletion targets are dangerously broad or overlapping")
    return KnowledgeDeletionAuthorization(database, vault, _DELETION_MARKER)


def _delete_knowledge_state_unlocked(
    database_path: str | Path,
    vault_path: str | Path,
    *,
    authorization: KnowledgeDeletionAuthorization | None,
) -> KnowledgeDeletionRecord:
    """Remove the sibling DB, sidecars, and Vault after target-bound authorization."""

    database = _archive._restore_target(
        database_path, "Knowledge database deletion target", create_parent=False
    )
    vault = _archive._restore_target(vault_path, "Knowledge Vault deletion target", create_parent=False)
    if (
        not isinstance(authorization, KnowledgeDeletionAuthorization)
        or authorization._marker is not _DELETION_MARKER
        or authorization._database != database
        or authorization._vault != vault
    ):
        raise ValueError("Knowledge deletion lacks explicit authorization for these targets")
    recovered = list(_archive._recover_delete_marker(database, vault))
    database_targets: list[tuple[str, Path]] = []
    for suffix, label in zip(_DATABASE_SIDECARS, ("database", "database_wal", "database_shm", "database_journal")):
        target = Path(f"{database}{suffix}")
        if not target.exists():
            continue
        if target.is_symlink() or not target.is_file():
            raise ValueError("Knowledge deletion target contains an unsafe database file")
        database_targets.append((label, target))
    vault_exists = vault.exists()
    if vault_exists:
        _archive._validate_existing_tree(vault)

    transaction = uuid.uuid4().hex
    marker_payload: dict[str, object] | None = None
    if database_targets or vault_exists:
        marker_payload = {
            "database": str(database),
            "database_present": any(label == "database" for label, _ in database_targets),
            "operation": "delete",
            "phase": "prepared",
            "schema_version": "noruct.knowledge-mutation.v1",
            "sidecars": sorted(
                suffix
                for suffix in _DATABASE_SIDECARS[1:]
                if any(target == Path(f"{database}{suffix}") for _, target in database_targets)
            ),
            "transaction": transaction,
            "vault": str(vault),
            "vault_present": vault_exists,
        }
        _archive._write_mutation_marker(database, marker_payload)
    try:
        for _, target in database_targets:
            tombstone = _archive._delete_tombstone_path(target, transaction)
            os.replace(target, tombstone)
        if vault_exists:
            tombstone = _archive._delete_tombstone_path(vault, transaction)
            os.replace(vault, tombstone)
        if marker_payload is not None:
            _archive._fsync_directory(database.parent)
            if vault.parent != database.parent:
                _archive._fsync_directory(vault.parent)
            marker_payload["phase"] = "published"
            _archive._write_mutation_marker(database, marker_payload)
    except BaseException:
        if database_targets or vault_exists:
            _archive._recover_delete_marker(database, vault)
        raise

    deleted: list[str] = recovered
    if marker_payload is not None:
        deleted.extend(_archive._recover_delete_marker(database, vault))
    return KnowledgeDeletionRecord(
        schema_version="noruct.knowledge-deletion.v1",
        deleted=bool(deleted),
        deleted_components=tuple(dict.fromkeys(deleted)),
        residual_backup_warning=(
            "Deletion removes current local Knowledge DB and Vault files only. "
            "Copied exports and filesystem snapshots remain outside this operation."
        ),
    )


def delete_knowledge_state(
    database_path: str | Path,
    vault_path: str | Path,
    *,
    authorization: KnowledgeDeletionAuthorization | None,
) -> KnowledgeDeletionRecord:
    """Delete only while no store or other lifecycle operation is open."""

    database = _archive._restore_target(
        database_path, "Knowledge database deletion target", create_parent=False
    )
    vault = _archive._restore_target(vault_path, "Knowledge Vault deletion target", create_parent=False)
    with KnowledgeStateLock(database, mode="exclusive"):
        return _delete_knowledge_state_unlocked(
            database,
            vault,
            authorization=authorization,
        )


from . import lifecycle_archive as _archive  # noqa: E402
from .lifecycle_diagnostics import knowledge_diagnostics  # noqa: E402

export_knowledge_archive, restore_knowledge_archive = _archive.export_knowledge_archive, _archive.restore_knowledge_archive


def __getattr__(name: str) -> object:
    return getattr(_archive, name)
