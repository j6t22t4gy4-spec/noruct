"""Terminal aggregates and bounded Manager supervision receipts for RunStore."""

from __future__ import annotations

import hashlib
import json
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


class RunStoreJobOutcomeMixin:
    """Append-only Job terminal and supervision lifecycle."""

    def append_job_terminal(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append the only terminal aggregate after count and identity validation."""

        payload_json = _json(payload)
        payload_hash = _digest_json(payload)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            snapshot = self._job_snapshot_row(conn, job_id)
            existing = conn.execute(
                "SELECT * FROM job_terminal_events WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) == payload_hash:
                    return dict(existing)
                raise ValueError(f"Job terminal already exists: {job_id}")
            attempt_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM job_attempts WHERE job_id = ?",
                    (job_id,),
                ).fetchone()["count"]
            )
            mutation_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM job_mutations WHERE job_id = ?",
                    (job_id,),
                ).fetchone()["count"]
            )
            graph_patch_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM job_graph_patches WHERE job_id = ?",
                    (job_id,),
                ).fetchone()["count"]
            )
            graph_proposal_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM job_graph_proposals WHERE job_id = ?",
                    (job_id,),
                ).fetchone()["count"]
            )
            if str(payload.get("job_id", "")) != job_id or str(
                payload.get("request_id", "")
            ) != str(snapshot["request_id"]):
                raise ValueError("Job terminal identity mismatch")
            if int(payload.get("task_attempt_count", -1)) != attempt_count:
                raise ValueError("Job terminal attempt aggregate mismatch")
            if int(payload.get("task_mutation_count", -1)) != mutation_count:
                raise ValueError("Job terminal mutation aggregate mismatch")
            # Schema v6 introduced separately chained graph-patch records.  A
            # completed v1-v5 job has no such records and did not carry the
            # direct aggregate, so accept the only compatible implicit value
            # (zero) while still rejecting a contradictory new terminal.
            if int(payload.get("graph_patch_count", graph_patch_count)) != graph_patch_count:
                raise ValueError("Job terminal graph patch aggregate mismatch")
            if int(
                payload.get("graph_proposal_decision_count", graph_proposal_count)
            ) != graph_proposal_count:
                raise ValueError("Job terminal graph proposal aggregate mismatch")
            final_graph_version = int(payload.get("final_graph_version", 0))
            if final_graph_version < int(snapshot["graph_version"]):
                raise ValueError("Job terminal graph version regressed")
            ledger_seq, previous_hash = self._job_tail(conn, job_id)
            chain_hash = job_chain_digest(previous_hash, "TERMINAL", payload_hash)
            conn.execute(
                """
                INSERT INTO job_terminal_events(
                    job_id, ledger_seq, status, final_graph_version,
                    task_attempt_count, task_mutation_count, payload_json, payload_hash,
                    previous_chain_hash, chain_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    ledger_seq,
                    str(payload.get("status", "")),
                    final_graph_version,
                    attempt_count,
                    mutation_count,
                    payload_json,
                    payload_hash,
                    previous_hash,
                    chain_hash,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM job_terminal_events WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return dict(row)

    def append_job_supervision(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append one privacy-bounded Manager supervision receipt.

        This deliberately records decision class and typed operational facts,
        never the Manager prompt, hidden reasoning, output text, or a free
        form rationale. The receipt is advisory evidence only; Kernel policy
        and graph validation remain the execution authority.
        """

        event_id = str(payload.get("event_id", ""))
        attempt_id = str(payload.get("attempt_id", ""))
        task_id = str(payload.get("task_id", ""))
        manager_employee_id = str(payload.get("manager_employee_id", ""))
        action = str(payload.get("action", ""))
        signal_code = payload.get("signal_code")
        priority = str(payload.get("priority", ""))
        deadline_bucket = str(payload.get("deadline_bucket", ""))
        shortage = payload.get("capability_shortage_count", 0)
        conflict = payload.get("conflicting_outcome", False)
        if (
            not event_id
            or not attempt_id
            or not task_id
            or not manager_employee_id
            or action not in {"CONTINUE", "SIGNAL"}
            or (action == "SIGNAL" and not isinstance(signal_code, str))
            or (action == "CONTINUE" and signal_code is not None)
            or priority not in {"FINAL_INTEGRATION", "SPECIALIST"}
            or deadline_bucket not in {"READY", "NEAR", "EXPIRED"}
            or type(shortage) is not int
            or shortage < 0
            or type(conflict) is not bool
        ):
            raise ValueError("Job supervision receipt is invalid")
        payload_json = _json(payload)
        payload_hash = _digest_json(payload)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            snapshot = self._job_snapshot_row(conn, job_id)
            snapshot_payload = _loads(str(snapshot["payload_json"]), {})
            manager = snapshot_payload.get("manager")
            if (
                not isinstance(manager, Mapping)
                or str(manager.get("employee_id", "")) != manager_employee_id
            ):
                raise ValueError("Job supervision Manager does not match snapshot")
            attempt = conn.execute(
                "SELECT task_id FROM job_attempts WHERE attempt_id = ? AND job_id = ?",
                (attempt_id, job_id),
            ).fetchone()
            if attempt is None or str(attempt["task_id"]) != task_id:
                raise ValueError("Job supervision attempt does not match snapshot")
            existing = conn.execute(
                "SELECT * FROM job_supervision_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) == payload_hash:
                    return dict(existing)
                raise ValueError("Duplicate job supervision event")
            sequence = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM job_supervision_events WHERE job_id = ?",
                    (job_id,),
                ).fetchone()["count"]
            ) + 1
            conn.execute(
                """
                INSERT INTO job_supervision_events(
                    event_id, job_id, attempt_id, sequence, task_id,
                    manager_employee_id, action, signal_code, priority,
                    deadline_bucket, capability_shortage_count,
                    conflicting_outcome, payload_json, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, job_id, attempt_id, sequence, task_id,
                    manager_employee_id, action, signal_code, priority,
                    deadline_bucket, shortage, int(conflict), payload_json,
                    payload_hash, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM job_supervision_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def get_job_supervision_events(self, job_id: str) -> tuple[dict[str, Any], ...]:
        """Return bounded, content-free Manager supervision receipts."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT event_id, attempt_id, sequence, task_id, manager_employee_id,
                       action, signal_code, priority, deadline_bucket,
                       capability_shortage_count, conflicting_outcome, created_at
                FROM job_supervision_events
                WHERE job_id = ? ORDER BY sequence
                """,
                (job_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)


