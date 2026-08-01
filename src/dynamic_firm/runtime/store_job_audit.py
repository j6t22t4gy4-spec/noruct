"""Immutable ACTIVE JOB audit writes composed into the canonical RunStore.

The owning RunStore retains the single SQLite connection, transaction boundary,
and public API. This mixin owns snapshot, attempt, mutation, graph-patch and
graph-proposal receipt chains only.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any, Mapping

from .models import to_primitive, utc_now
from .store_ledger_primitives import job_chain_digest


def _json(value: Any) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class RunStoreJobAuditMixin:
    """Append-only ACTIVE JOB snapshot and graph decision receipts."""

    @staticmethod
    def _validate_record_content_hash(payload: Mapping[str, Any], record_type: str) -> None:
        content_hash = str(payload.get("content_hash", ""))
        unhashed = dict(payload)
        unhashed["content_hash"] = ""
        if not content_hash or content_hash != _digest_json(unhashed):
            raise ValueError(f"{record_type} content hash mismatch")

    @staticmethod
    def _job_snapshot_row(
        conn: sqlite3.Connection,
        job_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM job_snapshots WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"ACTIVE JOB snapshot does not exist: {job_id}")
        return row

    @staticmethod
    def _job_tail(
        conn: sqlite3.Connection,
        job_id: str,
    ) -> tuple[int, str]:
        snapshot = RunStoreJobAuditMixin._job_snapshot_row(conn, job_id)
        terminal = conn.execute(
            "SELECT 1 FROM job_terminal_events WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if terminal is not None:
            raise ValueError(f"ACTIVE JOB is already terminal: {job_id}")
        row = conn.execute(
            """
            SELECT ledger_seq, chain_hash FROM (
                SELECT ledger_seq, chain_hash FROM job_attempts WHERE job_id = ?
                UNION ALL
                SELECT ledger_seq, chain_hash FROM job_mutations WHERE job_id = ?
                UNION ALL
                SELECT ledger_seq, chain_hash FROM job_graph_patches WHERE job_id = ?
                UNION ALL
                SELECT ledger_seq, chain_hash FROM job_graph_proposals WHERE job_id = ?
            )
            ORDER BY ledger_seq DESC
            LIMIT 1
            """,
            (job_id, job_id, job_id, job_id),
        ).fetchone()
        if row is None:
            return 1, str(snapshot["chain_hash"])
        return int(row["ledger_seq"]) + 1, str(row["chain_hash"])

    def create_job_snapshot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Insert exactly one privacy-bounded frozen snapshot for a company job."""

        required = (
            "job_id",
            "request_id",
            "proposal_id",
            "graph_version",
            "final_task_id",
            "company_revision",
            "roster_revision",
            "playbook_revision",
            "frozen_snapshot_hash",
        )
        missing = [key for key in required if payload.get(key) in {None, ""}]
        if missing:
            raise ValueError(f"ACTIVE JOB snapshot is missing: {', '.join(missing)}")
        payload_json = _json(payload)
        payload_hash = _digest_json(payload)
        chain_hash = job_chain_digest("GENESIS", "SNAPSHOT", payload_hash)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM job_snapshots WHERE job_id = ?",
                (str(payload["job_id"]),),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) == payload_hash:
                    return dict(existing)
                raise ValueError(f"ACTIVE JOB snapshot already exists: {payload['job_id']}")
            conn.execute(
                """
                INSERT INTO job_snapshots(
                    job_id, request_id, proposal_id, graph_version, final_task_id,
                    company_revision, roster_revision, playbook_revision,
                    frozen_snapshot_hash, payload_json, payload_hash, chain_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload["job_id"]),
                    str(payload["request_id"]),
                    str(payload["proposal_id"]),
                    int(payload["graph_version"]),
                    str(payload["final_task_id"]),
                    int(payload["company_revision"]),
                    int(payload["roster_revision"]),
                    int(payload["playbook_revision"]),
                    str(payload["frozen_snapshot_hash"]),
                    payload_json,
                    payload_hash,
                    chain_hash,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM job_snapshots WHERE job_id = ?",
                (str(payload["job_id"]),),
            ).fetchone()
        return dict(row)

    def append_job_attempt(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append one exact TaskAttemptRecord after validating its source relation."""

        self._validate_record_content_hash(payload, "Task attempt")
        attempt_id = str(payload.get("attempt_id", ""))
        task_id = str(payload.get("task_id", ""))
        employee_id = str(payload.get("employee_id", ""))
        sequence = int(payload.get("sequence", 0))
        source_attempt_id = payload.get("source_attempt_id")
        if not attempt_id or not task_id or not employee_id or sequence < 1:
            raise ValueError("Task attempt identity is incomplete")
        now = utc_now().isoformat()
        payload_json = _json(payload)
        payload_hash = _digest_json(payload)
        with self._transaction() as conn:
            snapshot = self._job_snapshot_row(conn, job_id)
            existing = conn.execute(
                "SELECT * FROM job_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) == payload_hash:
                    return dict(existing)
                raise ValueError(f"Duplicate task attempt: {attempt_id}")
            if str(payload.get("frozen_snapshot_hash", "")) != str(
                snapshot["frozen_snapshot_hash"]
            ):
                raise ValueError("Task attempt frozen snapshot mismatch")
            for key in ("company_revision", "roster_revision", "playbook_revision"):
                if int(payload.get(key, -1)) != int(snapshot[key]):
                    raise ValueError(f"Task attempt {key} mismatch")
            if source_attempt_id is None:
                if sequence != 1:
                    raise ValueError("Only attempt sequence 1 may omit source_attempt_id")
            else:
                source = conn.execute(
                    "SELECT * FROM job_attempts WHERE attempt_id = ? AND job_id = ?",
                    (str(source_attempt_id), job_id),
                ).fetchone()
                if source is None:
                    raise ValueError("Task attempt source does not exist in this job")
                if str(source["task_id"]) != task_id or int(source["attempt_sequence"]) + 1 != sequence:
                    raise ValueError("Task attempt source task or sequence mismatch")
                mutation = conn.execute(
                    """
                    SELECT * FROM job_mutations
                    WHERE job_id = ? AND target_attempt_id = ? AND source_attempt_id = ?
                    """,
                    (job_id, attempt_id, str(source_attempt_id)),
                ).fetchone()
                if mutation is None:
                    raise ValueError("Task attempt has no matching prior mutation")
                if (
                    str(mutation["task_id"]) != task_id
                    or str(mutation["to_employee_id"]) != employee_id
                ):
                    raise ValueError("Task attempt target identity mismatches its mutation")
            ledger_seq, previous_hash = self._job_tail(conn, job_id)
            chain_hash = job_chain_digest(previous_hash, "ATTEMPT", payload_hash)
            conn.execute(
                """
                INSERT INTO job_attempts(
                    attempt_id, job_id, ledger_seq, task_id, attempt_sequence,
                    employee_id, source_attempt_id, status, payload_json, payload_hash,
                    previous_chain_hash, chain_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    ledger_seq,
                    task_id,
                    sequence,
                    employee_id,
                    None if source_attempt_id is None else str(source_attempt_id),
                    str(payload.get("status", "")),
                    payload_json,
                    payload_hash,
                    previous_hash,
                    chain_hash,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM job_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return dict(row)

    def append_job_mutation(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append one RETRY/REROUTE decision after exact source validation."""

        self._validate_record_content_hash(payload, "Job mutation")
        event_id = str(payload.get("event_id", ""))
        task_id = str(payload.get("task_id", ""))
        source_attempt_id = str(payload.get("source_attempt_id", ""))
        target_attempt_id = str(payload.get("target_attempt_id", ""))
        mutation_type = str(payload.get("mutation_type", ""))
        event_sequence = int(payload.get("sequence", 0))
        if not all((event_id, task_id, source_attempt_id, target_attempt_id)):
            raise ValueError("Job mutation identity is incomplete")
        if mutation_type not in {"RETRY", "REROUTE"} or event_sequence < 1:
            raise ValueError("Job mutation type or sequence is invalid")
        payload_json = _json(payload)
        payload_hash = _digest_json(payload)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            snapshot = self._job_snapshot_row(conn, job_id)
            existing = conn.execute(
                "SELECT * FROM job_mutations WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) == payload_hash:
                    return dict(existing)
                raise ValueError(f"Duplicate job mutation: {event_id}")
            source = conn.execute(
                "SELECT * FROM job_attempts WHERE attempt_id = ? AND job_id = ?",
                (source_attempt_id, job_id),
            ).fetchone()
            if source is None:
                raise ValueError("Job mutation source attempt does not exist in this job")
            source_payload = _loads(str(source["payload_json"]), {})
            expected_event_sequence = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM job_mutations WHERE job_id = ?",
                    (job_id,),
                ).fetchone()["count"]
            ) + 1
            if event_sequence != expected_event_sequence:
                raise ValueError("Job mutation sequence is not contiguous")
            if (
                str(source["task_id"]) != task_id
                or int(source["attempt_sequence"]) != int(payload.get("source_attempt_sequence", 0))
                or int(payload.get("target_attempt_sequence", 0)) != int(source["attempt_sequence"]) + 1
                or str(source["employee_id"]) != str(payload.get("from_employee_id", ""))
                or str(source_payload.get("content_hash", ""))
                != str(payload.get("source_attempt_content_hash", ""))
            ):
                raise ValueError("Job mutation source identity mismatch")
            if str(source_payload.get("failure_kind", "")) != str(
                payload.get("failure_kind", "")
            ):
                raise ValueError("Job mutation failure kind mismatches source attempt")
            if str(source_payload.get("status", "")) != "FAILED":
                raise ValueError("Job mutation source attempt is not failed")
            if str(payload.get("frozen_snapshot_hash", "")) != str(
                snapshot["frozen_snapshot_hash"]
            ):
                raise ValueError("Job mutation frozen snapshot mismatch")
            if conn.execute(
                "SELECT 1 FROM job_attempts WHERE attempt_id = ?",
                (target_attempt_id,),
            ).fetchone():
                raise ValueError("Job mutation target attempt already exists")
            from_employee_id = str(payload.get("from_employee_id", ""))
            to_employee_id = str(payload.get("to_employee_id", ""))
            if mutation_type == "RETRY" and from_employee_id != to_employee_id:
                raise ValueError("RETRY must keep the same employee")
            if mutation_type == "REROUTE":
                if from_employee_id == to_employee_id:
                    raise ValueError("REROUTE must change employee")
                snapshot_payload = _loads(str(snapshot["payload_json"]), {})
                roster = {
                    str(item.get("employee_id", "")): item
                    for item in snapshot_payload.get("roster", ())
                    if isinstance(item, Mapping)
                }
                target = roster.get(to_employee_id)
                required = set(payload.get("matched_capabilities", ()))
                if (
                    target is None
                    or not bool(target.get("active"))
                    or bool(target.get("temporary"))
                    or not required.issubset(set(target.get("capabilities", ())))
                ):
                    raise ValueError("REROUTE target is not a frozen exact-capable employee")
            if int(payload.get("mutation_budget_after", -1)) != int(
                payload.get("mutation_budget_before", -1)
            ) - 1:
                raise ValueError("Job mutation budget evidence is inconsistent")
            ledger_seq, previous_hash = self._job_tail(conn, job_id)
            chain_hash = job_chain_digest(previous_hash, "MUTATION", payload_hash)
            conn.execute(
                """
                INSERT INTO job_mutations(
                    event_id, job_id, ledger_seq, event_sequence, mutation_type,
                    task_id, source_attempt_id, target_attempt_id,
                    from_employee_id, to_employee_id, payload_json, payload_hash,
                    previous_chain_hash, chain_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    job_id,
                    ledger_seq,
                    event_sequence,
                    mutation_type,
                    task_id,
                    source_attempt_id,
                    target_attempt_id,
                    from_employee_id,
                    to_employee_id,
                    payload_json,
                    payload_hash,
                    previous_hash,
                    chain_hash,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM job_mutations WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return dict(row)

    def append_job_graph_patch(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append one validated Company graph rewrite to the ACTIVE JOB chain."""

        self._validate_record_content_hash(payload, "Graph patch")
        event_id = str(payload.get("event_id", ""))
        event_sequence = int(payload.get("sequence", 0) or 0)
        patch = payload.get("patch")
        if not isinstance(patch, Mapping):
            raise ValueError("Graph patch payload is missing its patch")
        patch_id = str(patch.get("patch_id", ""))
        semantic_operation = str(patch.get("semantic_operation", ""))
        trigger_task_id = str(patch.get("trigger_task_id", ""))
        base_graph_version = int(patch.get("base_graph_version", 0) or 0)
        target_graph_version = int(payload.get("target_graph_version", 0) or 0)
        digests = (
            str(payload.get("before_graph_digest", "")),
            str(payload.get("after_graph_digest", "")),
        )
        mutation_lease = payload.get("mutation_lease", {})
        if mutation_lease is None:
            mutation_lease = {}
        if not isinstance(mutation_lease, Mapping):
            raise ValueError("Graph patch mutation lease is invalid")
        lease_model_calls = mutation_lease.get("model_calls", 0)
        lease_tool_calls = mutation_lease.get("tool_calls", 0)
        lease_cost_usd = mutation_lease.get("cost_usd", 0.0)
        if (
            not event_id
            or event_sequence < 1
            or not patch_id
            or not trigger_task_id
            or semantic_operation not in {"SPLIT", "JOIN", "MERGE", "INSERT", "CANCEL"}
            or base_graph_version < 1
            or target_graph_version != base_graph_version + 1
            or any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in digests)
            or type(lease_model_calls) is not int
            or lease_model_calls < 0
            or type(lease_tool_calls) is not int
            or lease_tool_calls < 0
            or isinstance(lease_cost_usd, bool)
            or not isinstance(lease_cost_usd, (int, float))
            or not math.isfinite(float(lease_cost_usd))
            or float(lease_cost_usd) < 0
        ):
            raise ValueError("Graph patch identity or version evidence is invalid")
        payload_json = _json(payload)
        payload_hash = _digest_json(payload)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            snapshot = self._job_snapshot_row(conn, job_id)
            existing = conn.execute(
                "SELECT * FROM job_graph_patches WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) == payload_hash:
                    return dict(existing)
                raise ValueError(f"Duplicate job graph patch: {event_id}")
            expected_sequence = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM job_graph_patches WHERE job_id = ?",
                    (job_id,),
                ).fetchone()["count"]
            ) + 1
            if event_sequence != expected_sequence:
                raise ValueError("Graph patch sequence is not contiguous")
            previous_version_row = conn.execute(
                """
                SELECT target_graph_version FROM job_graph_patches
                WHERE job_id = ? ORDER BY event_sequence DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            expected_base_version = (
                int(snapshot["graph_version"])
                if previous_version_row is None
                else int(previous_version_row["target_graph_version"])
            )
            if base_graph_version != expected_base_version:
                raise ValueError("Graph patch base version is not contiguous")
            ledger_seq, previous_hash = self._job_tail(conn, job_id)
            chain_hash = job_chain_digest(previous_hash, "GRAPH_PATCH", payload_hash)
            conn.execute(
                """
                INSERT INTO job_graph_patches(
                    event_id, job_id, ledger_seq, event_sequence, patch_id,
                    semantic_operation, base_graph_version, target_graph_version,
                    trigger_task_id, payload_json, payload_hash, previous_chain_hash,
                    chain_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    job_id,
                    ledger_seq,
                    event_sequence,
                    patch_id,
                    semantic_operation,
                    base_graph_version,
                    target_graph_version,
                    trigger_task_id,
                    payload_json,
                    payload_hash,
                    previous_hash,
                    chain_hash,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM job_graph_patches WHERE event_id = ?", (event_id,)
            ).fetchone()
        return dict(row)

    def append_job_graph_proposal(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append one immutable Graph proposal receipt to the Job chain."""

        self._validate_record_content_hash(payload, "Graph proposal")
        event_id = str(payload.get("event_id", ""))
        proposal_id = str(payload.get("proposal_id", ""))
        status = str(payload.get("status", ""))
        patch = payload.get("patch")
        lease = payload.get("proposed_lease")
        if not isinstance(patch, Mapping) or not isinstance(lease, Mapping):
            raise ValueError("Graph proposal payload is malformed")
        operation = str(patch.get("semantic_operation", ""))
        base_version = int(patch.get("base_graph_version", 0) or 0)
        lease_values = (lease.get("model_calls"), lease.get("tool_calls"), lease.get("cost_usd"))
        digests = (str(payload.get("before_graph_digest", "")), str(payload.get("after_graph_digest", "")))
        if (
            not event_id
            or len(proposal_id) > 128
            or status not in {"PENDING", "APPROVED", "REJECTED", "UNAVAILABLE"}
            or operation not in {"SPLIT", "JOIN", "MERGE", "INSERT", "CANCEL"}
            or base_version < 1
            or any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in digests)
            or type(lease_values[0]) is not int
            or int(lease_values[0]) < 0
            or type(lease_values[1]) is not int
            or int(lease_values[1]) < 0
            or isinstance(lease_values[2], bool)
            or not isinstance(lease_values[2], (int, float))
            or not math.isfinite(float(lease_values[2]))
            or float(lease_values[2]) < 0
        ):
            raise ValueError("Graph proposal identity or lease evidence is invalid")
        payload_json = _json(payload)
        payload_hash = _digest_json(payload)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            self._job_snapshot_row(conn, job_id)
            existing = conn.execute(
                "SELECT * FROM job_graph_proposals WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) == payload_hash:
                    return dict(existing)
                raise ValueError(f"Duplicate job graph proposal: {event_id}")
            sequence = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM job_graph_proposals WHERE job_id = ?",
                    (job_id,),
                ).fetchone()["count"]
            ) + 1
            ledger_seq, previous_hash = self._job_tail(conn, job_id)
            chain_hash = job_chain_digest(previous_hash, "GRAPH_PROPOSAL", payload_hash)
            conn.execute(
                """
                INSERT INTO job_graph_proposals(
                    event_id, proposal_id, job_id, ledger_seq, decision_sequence, status,
                    semantic_operation, base_graph_version, payload_json, payload_hash,
                    previous_chain_hash, chain_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, proposal_id, job_id, ledger_seq, sequence, status, operation,
                    base_version, payload_json, payload_hash, previous_hash,
                    chain_hash, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM job_graph_proposals WHERE event_id = ?", (event_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def resolve_pending_job_graph_proposal(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append one terminal decision for an exact pending graph candidate.

        Proposal rows are immutable.  A resolution is therefore a second
        chained receipt, never an UPDATE of the pending row.  The caller must
        present the full candidate again; this transaction verifies that its
        stable ``proposal_id`` and structural/lease identity match the sole
        pending receipt and rejects stale, duplicate, or substituted choices.
        """

        status = str(payload.get("status", ""))
        proposal_id = str(payload.get("proposal_id", ""))
        if status not in {"APPROVED", "REJECTED"} or not proposal_id:
            raise ValueError("Graph proposal resolution is invalid")
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM job_graph_proposals "
                "WHERE job_id = ? AND proposal_id = ? ORDER BY ledger_seq",
                (job_id, proposal_id),
            ).fetchall()
        related = [_loads(str(row["payload_json"]), {}) for row in rows]
        if not related:
            raise ValueError("Graph proposal is not pending for this Job")
        pending = [item for item in related if item.get("status") == "PENDING"]
        resolved = [item for item in related if item.get("status") != "PENDING"]
        if len(pending) != 1 or resolved:
            raise ValueError("Graph proposal has already been resolved")
        candidate = pending[0]
        for key in (
            "patch",
            "before_graph_digest",
            "after_graph_digest",
            "proposed_lease",
        ):
            if payload.get(key) != candidate.get(key):
                raise ValueError("Graph proposal resolution does not match pending candidate")
        return self.append_job_graph_proposal(job_id, payload)


