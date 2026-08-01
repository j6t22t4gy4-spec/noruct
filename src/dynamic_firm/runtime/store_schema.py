"""Runtime schema bootstrap and migration components.

RunStore remains the canonical connection and transaction owner. Bootstrap
and version-specific table rewrites live here so ordinary runtime and audit
mutation code does not carry schema details.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Collection, Mapping

from .store_run_primitives import safe_request_json


class RunStoreSchemaMigrationMixin:
    @staticmethod
    def _initialize_run_event_session_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS employee_runs (
                run_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                employee_id TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                prompt_hash TEXT,
                context_hash TEXT,
                usage_json TEXT NOT NULL,
                result_json TEXT,
                failure_json TEXT,
                cancel_reason TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS run_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                usage_delta_json TEXT,
                occurred_at TEXT NOT NULL,
                UNIQUE(run_id, seq)
            );

            CREATE INDEX IF NOT EXISTS run_events_run_seq_idx
                ON run_events(run_id, seq);

            CREATE INDEX IF NOT EXISTS employee_runs_job_created_idx
                ON employee_runs(job_id, created_at, run_id);

            CREATE INDEX IF NOT EXISTS run_events_occurred_idx
                ON run_events(occurred_at, run_id, seq);

            CREATE TABLE IF NOT EXISTS run_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                role TEXT NOT NULL,
                content_json TEXT NOT NULL,
                tool_call_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, position)
            );

            CREATE TABLE IF NOT EXISTS employee_session_state (
                namespace_hash TEXT PRIMARY KEY,
                employee_id TEXT NOT NULL,
                format_version INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                history_json TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                byte_length INTEGER NOT NULL,
                last_run_id TEXT NOT NULL REFERENCES employee_runs(run_id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS employee_session_state_employee_idx
                ON employee_session_state(employee_id, updated_at);

            CREATE TABLE IF NOT EXISTS employee_session_leases (
                namespace_hash TEXT PRIMARY KEY,
                employee_id TEXT NOT NULL,
                owner_run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                acquired_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS employee_session_leases_owner_idx
                ON employee_session_leases(owner_run_id);

            CREATE TABLE IF NOT EXISTS employee_run_frozen_routes (
                run_id TEXT PRIMARY KEY REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                binding_json TEXT NOT NULL,
                binding_digest TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS employee_run_frozen_route_admissions (
                run_id TEXT PRIMARY KEY REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                admission_json TEXT NOT NULL,
                admission_digest TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- A content-free, immutable result record for one physical model
            -- invocation.  The durable frozen route binding remains the
            -- authority that makes this receipt attributable to the run.
            CREATE TABLE IF NOT EXISTS employee_run_model_invocation_receipts (
                run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                invocation_id TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                receipt_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id, invocation_id)
            );

            CREATE INDEX IF NOT EXISTS employee_run_model_invocation_receipts_run_idx
                ON employee_run_model_invocation_receipts(run_id, invocation_id);

            -- Reservation is recorded before a frozen provider dispatch.  It
            -- contains only attribution material, never provider input or
            -- output.  A reservation is consumed atomically with its terminal
            -- receipt; a later dispatch reconciles an orphan as indeterminate.
            CREATE TABLE IF NOT EXISTS employee_run_model_invocation_dispatch_reservations (
                run_id TEXT NOT NULL REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                invocation_id TEXT NOT NULL,
                dispatch_epoch TEXT NOT NULL,
                route_binding_digest TEXT NOT NULL,
                context_projection_digest TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id, invocation_id)
            );

            CREATE INDEX IF NOT EXISTS employee_run_model_invocation_dispatch_reservations_run_idx
                ON employee_run_model_invocation_dispatch_reservations(run_id, invocation_id);

            -- One opaque, process-local dispatcher epoch owns the physical
            -- invocation lane for a frozen run.  This is deliberately not a
            -- recovery lease: it has no expiry or takeover path, because a
            -- different process cannot infer that an earlier provider call
            -- was abandoned.
            CREATE TABLE IF NOT EXISTS employee_run_model_invocation_dispatch_leases (
                run_id TEXT PRIMARY KEY REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                dispatch_epoch TEXT NOT NULL,
                acquired_at TEXT NOT NULL
            );

            -- Explicit operator recovery, never an automatic timeout or
            -- cross-epoch takeover.  A claim makes a stopped frozen physical
            -- invocation non-replayable before its run is terminalized.
            CREATE TABLE IF NOT EXISTS employee_run_frozen_route_recovery_claims (
                run_id TEXT PRIMARY KEY REFERENCES employee_runs(run_id) ON DELETE CASCADE,
                recovery_id TEXT NOT NULL UNIQUE,
                binding_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('CLAIMED', 'TERMINALIZED')),
                created_at TEXT NOT NULL,
                terminalized_at TEXT
            );
            """
        )

    @staticmethod
    def _initialize_schema_version_and_sanitize(
        conn: sqlite3.Connection,
        *,
        schema_version: int,
        supported_versions: Collection[int],
    ) -> None:
        row = conn.execute(
            "SELECT value FROM runtime_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row and int(row["value"]) not in supported_versions:
            raise RuntimeError(f"Unsupported runtime schema version: {row['value']}")
        if row:
            conn.execute(
                "UPDATE runtime_meta SET value = ? WHERE key = 'schema_version'",
                (str(schema_version),),
            )
        else:
            conn.execute(
                "INSERT INTO runtime_meta(key, value) VALUES('schema_version', ?)",
                (str(schema_version),),
            )
        # Sanitize snapshots created by an earlier runtime before this
        # persistence boundary existed. Idempotency compares the same safe
        # projection below, so no raw request needs to remain in SQLite.
        for run in conn.execute(
            "SELECT run_id, employee_id, request_json FROM employee_runs"
        ).fetchall():
            safe_request = safe_request_json(
                json.loads(run["request_json"]) if run["request_json"] else {},
                str(run["employee_id"]),
            )
            if safe_request != run["request_json"]:
                conn.execute(
                    "UPDATE employee_runs SET request_json = ? WHERE run_id = ?",
                    (safe_request, run["run_id"]),
                )

    @staticmethod
    def _migrate_graph_proposal_schema(conn: sqlite3.Connection) -> None:
        """Upgrade proposal receipts without mutating their append-only payload.

        SQLite cannot widen a ``CHECK`` constraint in place.  V20 adds the
        durable ``PENDING`` state and an indexed stable candidate identity, so
        older tables are copied verbatim into a replacement table.  Historical
        receipts did not have a proposal id; their immutable event id remains
        their legacy candidate identity and therefore cannot be resolved as a
        new pending proposal.
        """

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_graph_proposals'"
        ).fetchone()
        if row is None:
            return
        definition = str(row["sql"] or "")
        columns = {
            str(item["name"])
            for item in conn.execute("PRAGMA table_info(job_graph_proposals)").fetchall()
        }
        if "proposal_id" in columns and "'PENDING'" in definition:
            return

        rows = conn.execute(
            "SELECT * FROM job_graph_proposals ORDER BY job_id, ledger_seq"
        ).fetchall()
        conn.execute("DROP INDEX IF EXISTS job_graph_proposals_job_seq_idx")
        conn.execute("DROP TRIGGER IF EXISTS job_graph_proposals_no_update")
        conn.execute("DROP TRIGGER IF EXISTS job_graph_proposals_no_delete")
        conn.execute("ALTER TABLE job_graph_proposals RENAME TO job_graph_proposals_v19")
        conn.executescript(
            """
            CREATE TABLE job_graph_proposals (
                event_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                ledger_seq INTEGER NOT NULL,
                decision_sequence INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','UNAVAILABLE')),
                semantic_operation TEXT NOT NULL,
                base_graph_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                previous_chain_hash TEXT NOT NULL,
                chain_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, ledger_seq),
                UNIQUE(job_id, decision_sequence),
                UNIQUE(job_id, proposal_id, status)
            );

            CREATE INDEX job_graph_proposals_job_seq_idx
                ON job_graph_proposals(job_id, ledger_seq);

            CREATE TRIGGER job_graph_proposals_no_update
            BEFORE UPDATE ON job_graph_proposals BEGIN
                SELECT RAISE(ABORT, 'job_graph_proposals are append-only');
            END;
            CREATE TRIGGER job_graph_proposals_no_delete
            BEFORE DELETE ON job_graph_proposals BEGIN
                SELECT RAISE(ABORT, 'job_graph_proposals are append-only');
            END;
            """
        )
        for item in rows:
            payload = json.loads(str(item["payload_json"])) if item["payload_json"] else {}
            candidate_id = (
                str(payload.get("proposal_id", ""))
                if isinstance(payload, Mapping)
                else ""
            )
            proposal_id = candidate_id or f"legacy-{str(item['event_id'])}"
            conn.execute(
                """
                INSERT INTO job_graph_proposals(
                    event_id, proposal_id, job_id, ledger_seq, decision_sequence,
                    status, semantic_operation, base_graph_version, payload_json,
                    payload_hash, previous_chain_hash, chain_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["event_id"], proposal_id, item["job_id"], item["ledger_seq"],
                    item["decision_sequence"], item["status"], item["semantic_operation"],
                    item["base_graph_version"], item["payload_json"], item["payload_hash"],
                    item["previous_chain_hash"], item["chain_hash"], item["created_at"],
                ),
            )
        conn.execute("DROP TABLE job_graph_proposals_v19")
