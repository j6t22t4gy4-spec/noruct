"""SQLite initialization, migration, locking, and transaction lifecycle for KnowledgeStore."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .locking import KnowledgeStateLock


SCHEMA_VERSION = 8


class KnowledgeCoreLifecycleMixin:
    _MANAGED_FTS5_TABLE = "knowledge_chunk_fts"
    _MANAGED_CJK_CANDIDATES_TABLE = "knowledge_chunk_cjk_candidates"
    def __init__(self, path: str | Path) -> None:
        requested = Path(path).expanduser()
        if requested.is_symlink():
            raise ValueError("Knowledge DB path must not be a symbolic link")
        self.path = requested.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self._state_lifecycle_lock = KnowledgeStateLock(
            self.path,
            mode="shared",
            create_parent=True,
        ).acquire()
        try:
            if self.path.exists():
                if self.path.is_symlink() or not self.path.is_file():
                    raise ValueError("Knowledge DB path must be a regular non-symlink file")
                self.path.chmod(0o600)
            else:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.close(descriptor)
        except BaseException:
            self._state_lifecycle_lock.close()
            raise
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None, timeout=5.0
            )
        except BaseException:
            self._state_lifecycle_lock.close()
            raise
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA trusted_schema = OFF")
            self._conn.execute("PRAGMA secure_delete = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._preflight_schema_version()
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._initialize()
            self._apply_private_permissions()
        except BaseException:
            self._conn.close()
            self._state_lifecycle_lock.close()
            raise
        self._apply_private_permissions()

    def _apply_private_permissions(self) -> None:
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
            Path(f"{self.path}-journal"),
        ):
            try:
                if candidate.exists() and not candidate.is_symlink():
                    candidate.chmod(0o600)
            except OSError:
                pass

    def _preflight_schema_version(self) -> None:
        """Reject a future database before running any schema DDL or migrations."""

        table = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_meta'"
        ).fetchone()
        if table is None:
            return
        row = self._conn.execute(
            "SELECT value FROM knowledge_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return
        try:
            observed = int(row[0])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Knowledge DB schema version: {row[0]!r}") from exc
        if observed < 1 or observed > SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported Knowledge DB schema {observed}; expected 1 through {SCHEMA_VERSION}"
            )

    def _initialize(self) -> None:
        with self._transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_assets (
                    asset_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                    vault_relative_path TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    access_scope TEXT NOT NULL,
                    status TEXT NOT NULL,
                    processor TEXT NOT NULL DEFAULT '',
                    processor_version TEXT NOT NULL DEFAULT '',
                    processing_error TEXT NOT NULL DEFAULT '',
                    parent_asset_id TEXT REFERENCES knowledge_assets(asset_id),
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    labels_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(content_hash, access_scope)
                );

                CREATE INDEX IF NOT EXISTS knowledge_assets_created_idx
                    ON knowledge_assets(created_at DESC, asset_id);
                CREATE INDEX IF NOT EXISTS knowledge_assets_status_idx
                    ON knowledge_assets(status, created_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_remote_asset_sources (
                    asset_id TEXT PRIMARY KEY REFERENCES knowledge_assets(asset_id) ON DELETE CASCADE,
                    source_url TEXT NOT NULL,
                    response_etag TEXT,
                    response_last_modified TEXT,
                    fetched_at TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_processing_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL REFERENCES knowledge_assets(asset_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    processor TEXT NOT NULL,
                    processor_version TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    error_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS knowledge_processing_asset_idx
                    ON knowledge_processing_attempts(asset_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_representations (
                    representation_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL REFERENCES knowledge_assets(asset_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                    vault_relative_path TEXT NOT NULL,
                    processor TEXT NOT NULL,
                    processor_version TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    created_at TEXT NOT NULL,
                    UNIQUE(asset_id, kind, revision)
                );

                CREATE INDEX IF NOT EXISTS knowledge_representations_asset_idx
                    ON knowledge_representations(asset_id, revision DESC);

                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL REFERENCES knowledge_assets(asset_id) ON DELETE CASCADE,
                    representation_id TEXT NOT NULL REFERENCES knowledge_representations(representation_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    char_start INTEGER NOT NULL CHECK(char_start >= 0),
                    char_end INTEGER NOT NULL CHECK(char_end >= char_start),
                    location_json TEXT NOT NULL,
                    UNIQUE(representation_id, ordinal)
                );

                CREATE INDEX IF NOT EXISTS knowledge_chunks_asset_idx
                    ON knowledge_chunks(asset_id, representation_id, ordinal);

                CREATE TABLE IF NOT EXISTS knowledge_records (
                    record_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                    source_asset_id TEXT REFERENCES knowledge_assets(asset_id) ON DELETE SET NULL,
                    source_representation_id TEXT REFERENCES knowledge_representations(representation_id) ON DELETE SET NULL,
                    source_span_json TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    supersedes_record_id TEXT REFERENCES knowledge_records(record_id) ON DELETE SET NULL,
                    source_candidate_id TEXT REFERENCES knowledge_write_candidates(candidate_id) ON DELETE SET NULL,
                    source_job_id TEXT,
                    evidence_pack_id TEXT REFERENCES evidence_packs(pack_id) ON DELETE SET NULL,
                    access_scope TEXT NOT NULL DEFAULT 'private',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS knowledge_records_updated_idx
                    ON knowledge_records(updated_at DESC, record_id);

                CREATE TABLE IF NOT EXISTS evidence_packs (
                    pack_id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    selected_bytes INTEGER NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    access_scope TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    conflict_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence_pack_sources (
                    pack_id TEXT NOT NULL REFERENCES evidence_packs(pack_id) ON DELETE CASCADE,
                    evidence_id TEXT NOT NULL,
                    asset_id TEXT REFERENCES knowledge_assets(asset_id) ON DELETE CASCADE,
                    representation_id TEXT REFERENCES knowledge_representations(representation_id) ON DELETE CASCADE,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    PRIMARY KEY(pack_id, evidence_id)
                );

                CREATE INDEX IF NOT EXISTS evidence_pack_sources_asset_idx
                    ON evidence_pack_sources(asset_id, pack_id);

                CREATE TABLE IF NOT EXISTS knowledge_write_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    evidence_pack_id TEXT REFERENCES evidence_packs(pack_id) ON DELETE SET NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    accepted_record_id TEXT REFERENCES knowledge_records(record_id) ON DELETE SET NULL,
                    UNIQUE(job_id, kind)
                );

                CREATE INDEX IF NOT EXISTS knowledge_candidates_status_idx
                    ON knowledge_write_candidates(status, created_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_intents (
                    intent_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority >= 0 AND priority <= 100),
                    status TEXT NOT NULL,
                    constraints_json TEXT NOT NULL,
                    acceptance_criteria_json TEXT NOT NULL,
                    knowledge_query TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS knowledge_intents_status_priority_idx
                    ON knowledge_intents(status, priority DESC, updated_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_intent_revisions (
                    intent_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(intent_id, revision)
                );

                CREATE TABLE IF NOT EXISTS knowledge_decisions (
                    decision_id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    status TEXT NOT NULL,
                    intent_id TEXT REFERENCES knowledge_intents(intent_id) ON DELETE SET NULL,
                    evidence_pack_id TEXT REFERENCES evidence_packs(pack_id) ON DELETE SET NULL,
                    supersedes_decision_id TEXT REFERENCES knowledge_decisions(decision_id) ON DELETE SET NULL,
                    review_at TEXT,
                    actor TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS knowledge_decisions_updated_idx
                    ON knowledge_decisions(updated_at DESC, decision_id);

                CREATE TABLE IF NOT EXISTS knowledge_decision_revisions (
                    decision_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(decision_id, revision)
                );

                CREATE TABLE IF NOT EXISTS knowledge_questions (
                    question_id TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL,
                    intent_id TEXT REFERENCES knowledge_intents(intent_id) ON DELETE SET NULL,
                    decision_id TEXT REFERENCES knowledge_decisions(decision_id) ON DELETE SET NULL,
                    evidence_pack_id TEXT REFERENCES evidence_packs(pack_id) ON DELETE SET NULL,
                    answer_criteria_json TEXT NOT NULL,
                    knowledge_query TEXT NOT NULL,
                    review_at TEXT,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS knowledge_questions_status_updated_idx
                    ON knowledge_questions(status, updated_at DESC, question_id);

                CREATE TABLE IF NOT EXISTS knowledge_question_revisions (
                    question_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(question_id, revision)
                );

                CREATE TABLE IF NOT EXISTS knowledge_research_requests (
                    request_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL,
                    question_id TEXT REFERENCES knowledge_questions(question_id) ON DELETE SET NULL,
                    intent_id TEXT REFERENCES knowledge_intents(intent_id) ON DELETE SET NULL,
                    decision_id TEXT REFERENCES knowledge_decisions(decision_id) ON DELETE SET NULL,
                    decision_revision INTEGER CHECK(decision_revision >= 1),
                    evidence_pack_id TEXT REFERENCES evidence_packs(pack_id) ON DELETE SET NULL,
                    knowledge_query TEXT NOT NULL,
                    required_evidence_json TEXT NOT NULL,
                    freshness_at TEXT,
                    counterargument_required INTEGER NOT NULL CHECK(counterargument_required IN (0, 1)),
                    max_cost_units INTEGER NOT NULL CHECK(max_cost_units >= 0 AND max_cost_units <= 1000000),
                    max_duration_minutes INTEGER NOT NULL CHECK(max_duration_minutes >= 1 AND max_duration_minutes <= 10080),
                    compiled_intent_id TEXT REFERENCES knowledge_intents(intent_id) ON DELETE SET NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(decision_id, decision_revision)
                );

                CREATE INDEX IF NOT EXISTS knowledge_research_requests_status_updated_idx
                    ON knowledge_research_requests(status, updated_at DESC, request_id);

                CREATE TABLE IF NOT EXISTS knowledge_research_request_revisions (
                    request_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(request_id, revision)
                );

                CREATE TABLE IF NOT EXISTS knowledge_execution_bindings (
                    binding_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    job_id TEXT NOT NULL UNIQUE,
                    intent_id TEXT NOT NULL,
                    intent_revision INTEGER NOT NULL CHECK(intent_revision >= 1),
                    intent_hash TEXT NOT NULL,
                    pack_id TEXT NOT NULL,
                    pack_revision INTEGER NOT NULL CHECK(pack_revision >= 1),
                    pack_digest TEXT NOT NULL,
                    delivery_digest TEXT NOT NULL,
                    item_count INTEGER NOT NULL CHECK(item_count >= 0),
                    selected_bytes INTEGER NOT NULL CHECK(selected_bytes >= 0),
                    access_scope TEXT NOT NULL,
                    status TEXT NOT NULL,
                    job_status TEXT NOT NULL DEFAULT '',
                    candidate_id TEXT REFERENCES knowledge_write_candidates(candidate_id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS knowledge_execution_bindings_status_idx
                    ON knowledge_execution_bindings(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_epistemic_annotations (
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    epistemic_status TEXT NOT NULL,
                    trust_class TEXT NOT NULL,
                    freshness_expires_at TEXT,
                    conflict_refs_json TEXT NOT NULL DEFAULT '[]',
                    unknown_refs_json TEXT NOT NULL DEFAULT '[]',
                    source_revision TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(subject_type, subject_id)
                );

                CREATE INDEX IF NOT EXISTS knowledge_epistemic_status_idx
                    ON knowledge_epistemic_annotations(epistemic_status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_decision_contexts (
                    snapshot_id TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL UNIQUE REFERENCES knowledge_execution_bindings(binding_id),
                    request_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    intent_revision INTEGER NOT NULL CHECK(intent_revision >= 1),
                    intent_hash TEXT NOT NULL,
                    decision_id TEXT REFERENCES knowledge_decisions(decision_id) ON DELETE SET NULL,
                    decision_revision INTEGER CHECK(decision_revision >= 1),
                    evidence_pack_id TEXT NOT NULL REFERENCES evidence_packs(pack_id),
                    evidence_pack_revision INTEGER NOT NULL CHECK(evidence_pack_revision >= 1),
                    evidence_pack_digest TEXT NOT NULL,
                    known_refs_json TEXT NOT NULL,
                    unknown_refs_json TEXT NOT NULL,
                    assumptions_json TEXT NOT NULL,
                    constraints_json TEXT NOT NULL,
                    excluded_alternatives_json TEXT NOT NULL,
                    owner_ref TEXT NOT NULL,
                    authority_ref TEXT NOT NULL,
                    supersedes_snapshot_id TEXT REFERENCES knowledge_decision_contexts(snapshot_id),
                    content_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_oracle_contracts (
                    oracle_contract_id TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL UNIQUE REFERENCES knowledge_execution_bindings(binding_id),
                    request_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    acceptance_criteria_json TEXT NOT NULL,
                    failure_criteria_json TEXT NOT NULL,
                    observable_signals_json TEXT NOT NULL,
                    observation_channel TEXT NOT NULL,
                    validator_type TEXT NOT NULL,
                    independence_class TEXT NOT NULL,
                    accountable_owner_ref TEXT NOT NULL,
                    authority_ref TEXT NOT NULL,
                    feedback_due_at TEXT,
                    reversibility_class TEXT NOT NULL,
                    risk_class TEXT NOT NULL,
                    proxy_metric TEXT,
                    proxy_failure_modes_json TEXT NOT NULL,
                    inconclusive_policy TEXT NOT NULL,
                    max_attempts INTEGER NOT NULL CHECK(max_attempts >= 1 AND max_attempts <= 100),
                    max_evidence_items INTEGER NOT NULL CHECK(max_evidence_items >= 0 AND max_evidence_items <= 1000),
                    content_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_outcome_observations (
                    outcome_id TEXT PRIMARY KEY,
                    oracle_contract_id TEXT NOT NULL REFERENCES knowledge_oracle_contracts(oracle_contract_id),
                    binding_id TEXT NOT NULL UNIQUE REFERENCES knowledge_execution_bindings(binding_id),
                    request_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    expected_signal TEXT NOT NULL,
                    observed_signal TEXT NOT NULL,
                    observed_at TEXT,
                    source_ref TEXT,
                    verdict TEXT NOT NULL,
                    confounders_json TEXT NOT NULL,
                    attribution_status TEXT NOT NULL,
                    reviewer_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS knowledge_outcome_verdict_idx
                    ON knowledge_outcome_observations(verdict, updated_at DESC);

                CREATE TABLE IF NOT EXISTS knowledge_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS knowledge_events_subject_idx
                    ON knowledge_events(subject_type, subject_id, created_at);
                """
            )
            self._initialize_folder_schema(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_page_publications (
                    publication_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE
                        REFERENCES knowledge_write_candidates(candidate_id) ON DELETE CASCADE,
                    accepted_record_id TEXT NOT NULL
                        REFERENCES knowledge_records(record_id) ON DELETE CASCADE,
                    folder_id TEXT NOT NULL
                        REFERENCES knowledge_folders(folder_id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                    published_at TEXT NOT NULL,
                    UNIQUE(folder_id, relative_path)
                );

                CREATE INDEX IF NOT EXISTS knowledge_page_publications_folder_idx
                    ON knowledge_page_publications(folder_id, published_at DESC);
                """
            )
            self._initialize_managed_fts5(conn)
            self._initialize_managed_cjk_candidates(conn)
            conn.execute(
                """
                INSERT OR IGNORE INTO knowledge_epistemic_annotations(
                    subject_type, subject_id, epistemic_status, trust_class,
                    conflict_refs_json, unknown_refs_json, source_revision,
                    created_at, updated_at
                )
                SELECT 'RECORD', record_id, 'UNKNOWN', 'UNSPECIFIED', '[]', '[]',
                       CAST(revision AS TEXT), created_at, updated_at
                FROM knowledge_records
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO knowledge_epistemic_annotations(
                    subject_type, subject_id, epistemic_status, trust_class,
                    conflict_refs_json, unknown_refs_json, source_revision,
                    created_at, updated_at
                )
                SELECT 'WRITE_CANDIDATE', candidate_id, 'INFERRED',
                       'MODEL_GENERATED', '[]', '[]', '1', created_at, created_at
                FROM knowledge_write_candidates
                """
            )
            current = conn.execute(
                "SELECT value FROM knowledge_meta WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO knowledge_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(current[0]) < SCHEMA_VERSION:
                conn.execute(
                    "UPDATE knowledge_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            elif int(current[0]) != SCHEMA_VERSION:
                raise ValueError(
                    f"Unsupported Knowledge DB schema {current[0]}; expected {SCHEMA_VERSION}"
                )

    @classmethod
    def _managed_fts5_available_on(cls, conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (cls._MANAGED_FTS5_TABLE,),
        ).fetchone() is not None

    @classmethod
    def _rebuild_managed_fts5(cls, conn: sqlite3.Connection) -> None:
        """Rebuild the optional, non-authoritative managed-chunk projection."""

        if not cls._managed_fts5_available_on(conn):
            return
        conn.execute(f"DELETE FROM {cls._MANAGED_FTS5_TABLE}")
        conn.execute(
            f"""
            INSERT INTO {cls._MANAGED_FTS5_TABLE}(
                chunk_id, asset_id, representation_id, title, content
            )
            SELECT chunk.chunk_id, chunk.asset_id, chunk.representation_id,
                   COALESCE(asset.title, asset.original_name), chunk.content
            FROM knowledge_chunks chunk
            JOIN knowledge_assets asset ON asset.asset_id = chunk.asset_id
            """
        )

    @classmethod
    def _initialize_managed_fts5(cls, conn: sqlite3.Connection) -> None:
        had_fts5 = cls._managed_fts5_available_on(conn)
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunk_fts
                USING fts5(
                    chunk_id UNINDEXED,
                    asset_id UNINDEXED,
                    representation_id UNINDEXED,
                    title,
                    content,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )
        except sqlite3.OperationalError:
            return
        if not had_fts5:
            cls._rebuild_managed_fts5(conn)

    @classmethod
    def _managed_cjk_candidates_available_on(cls, conn: sqlite3.Connection) -> bool:
        """Return whether the disposable managed-chunk CJK index exists."""

        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (cls._MANAGED_CJK_CANDIDATES_TABLE,),
        ).fetchone() is not None

    @classmethod
    def _rebuild_managed_cjk_candidates(cls, conn: sqlite3.Connection) -> None:
        """Rebuild the local CJK narrowing projection from authoritative chunks.

        This projection has no semantic authority: it only avoids a complete
        managed-Asset scan for contiguous Korean/CJK input.  The subsequent
        exact compact-string check and normal hybrid ranker still decide which
        evidence is usable.
        """

        if not cls._managed_cjk_candidates_available_on(conn):
            return
        conn.execute(f"DELETE FROM {cls._MANAGED_CJK_CANDIDATES_TABLE}")
        rows = conn.execute(
            """
            SELECT chunk.chunk_id, COALESCE(asset.title, asset.original_name) AS title,
                   chunk.content
            FROM knowledge_chunks chunk
            JOIN knowledge_assets asset ON asset.asset_id = chunk.asset_id
            """
        ).fetchall()
        values = [
            (str(row["chunk_id"]), token)
            for row in rows
            for token in cls._cjk_candidate_tokens(
                f"{row['title']} {row['content']}"
            )
        ]
        if values:
            conn.executemany(
                f"INSERT OR IGNORE INTO {cls._MANAGED_CJK_CANDIDATES_TABLE}(chunk_id, token) "
                "VALUES (?, ?)",
                values,
            )

    @classmethod
    def _initialize_managed_cjk_candidates(cls, conn: sqlite3.Connection) -> None:
        """Create and backfill the rebuildable managed CJK candidate index."""

        had_candidates = cls._managed_cjk_candidates_available_on(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_chunk_cjk_candidates (
                chunk_id TEXT NOT NULL REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE,
                token TEXT NOT NULL,
                PRIMARY KEY(chunk_id, token)
            );
            CREATE INDEX IF NOT EXISTS knowledge_chunk_cjk_candidates_token_idx
                ON knowledge_chunk_cjk_candidates(token, chunk_id);
            """
        )
        if not had_candidates:
            cls._rebuild_managed_cjk_candidates(conn)

    def managed_fts5_available(self) -> bool:
        with self._lock:
            return self._managed_fts5_available_on(self._conn)

    def managed_cjk_candidate_index_available(self) -> bool:
        """Expose managed CJK candidate acceleration without making it authority."""

        with self._lock:
            return self._managed_cjk_candidates_available_on(self._conn)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
                self._apply_private_permissions()

    def close(self) -> None:
        with self._lock:
            try:
                self._apply_private_permissions()
                self._conn.close()
            finally:
                self._state_lifecycle_lock.close()

    def _sanitize_deleted_content(self) -> None:
        """Rewrite live content and truncate WAL after an explicit selective forget."""

        with self._lock:
            self._conn.execute("VACUUM")
            checkpoint = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise RuntimeError(
                    "Knowledge deletion committed, but WAL sanitization is blocked by another reader"
                )
            self._apply_private_permissions()

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
