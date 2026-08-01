"""Durable ACTIVE JOB lifecycle, graph-mutation lease and operator signals."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from .models import to_primitive, utc_now


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


class RunStoreJobLifecycleMixin:
    """CAS lifecycle control, bounded lease and user-correction receipts."""

    def admit_job_lifecycle(self, *, job_id: str, request_id: str) -> dict[str, Any]:
        """Create the durable control row, or replay the exact admission."""

        now = utc_now().isoformat()
        with self._transaction() as conn:
            self._job_snapshot_row(conn, job_id)
            existing = conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["request_id"]) != request_id:
                    raise ValueError("Job lifecycle request identity conflicts")
                return dict(existing)
            conn.execute(
                """
                INSERT INTO job_lifecycle_state(job_id, request_id, state, revision, reason, created_at, updated_at)
                VALUES (?, ?, 'ADMITTED', 1, 'INITIAL_ADMISSION', ?, ?)
                """,
                (job_id, request_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO job_lifecycle_events(event_id, job_id, sequence, operation, from_state, to_state, reason, created_at)
                VALUES (?, ?, 1, 'ADMIT', NULL, 'ADMITTED', 'INITIAL_ADMISSION', ?)
                """,
                (f"lifecycle:{job_id}:1", job_id, now),
            )
            row = conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def transition_job_lifecycle(
        self,
        *,
        job_id: str,
        operation: str,
        reason: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Apply one CAS-protected deterministic operator lifecycle transition."""

        allowed = {
            "DEFER": {"ADMITTED"},
            "PAUSE": {"ADMITTED", "DEFERRED"},
            "RESUME": {"DEFERRED", "PAUSED"},
            "CANCEL": {"ADMITTED", "DEFERRED", "PAUSED"},
            "TERMINALIZE": {"ADMITTED", "DEFERRED", "PAUSED", "CANCELLED"},
        }
        if operation not in allowed or not reason.strip() or len(reason) > 256:
            raise ValueError("Job lifecycle transition is invalid")
        target = {
            "DEFER": "DEFERRED", "PAUSE": "PAUSED", "RESUME": "ADMITTED",
            "CANCEL": "CANCELLED", "TERMINALIZE": "TERMINAL",
        }[operation]
        now = utc_now().isoformat()
        with self._transaction() as conn:
            current = conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if current is None:
                raise ValueError("Job lifecycle is not admitted")
            if expected_revision is not None and int(current["revision"]) != expected_revision:
                raise ValueError("Job lifecycle revision conflicts")
            if str(current["state"]) == target:
                return dict(current)
            if str(current["state"]) not in allowed[operation]:
                raise ValueError("Job lifecycle transition is not allowed from current state")
            revision = int(current["revision"]) + 1
            conn.execute(
                "UPDATE job_lifecycle_state SET state = ?, revision = ?, reason = ?, updated_at = ? WHERE job_id = ?",
                (target, revision, reason, now, job_id),
            )
            conn.execute(
                """
                INSERT INTO job_lifecycle_events(event_id, job_id, sequence, operation, from_state, to_state, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"lifecycle:{job_id}:{revision}", job_id, revision, operation, current["state"], target, reason, now),
            )
            row = conn.execute("SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)).fetchone()
        assert row is not None
        return dict(row)

    def get_job_lifecycle(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            events = self._conn.execute(
                "SELECT operation, from_state, to_state, reason, created_at FROM job_lifecycle_events WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
            leases = self._conn.execute(
                """
                SELECT lease_id, kind, model_calls, tool_calls, cost_usd, status,
                       reason, created_at, settled_at
                FROM job_lifecycle_leases
                WHERE job_id = ? ORDER BY created_at, lease_id
                """,
                (job_id,),
            ).fetchall()
        return {
            **dict(row),
            "events": tuple(dict(event) for event in events),
            "leases": tuple(dict(lease) for lease in leases),
        }

    @staticmethod
    def _validated_graph_mutation_lease(
        lease: Mapping[str, Any],
    ) -> tuple[int, int, float, str]:
        model_calls = lease.get("model_calls", 0)
        tool_calls = lease.get("tool_calls", 0)
        cost_usd = lease.get("cost_usd", 0.0)
        if (
            type(model_calls) is not int
            or model_calls < 0
            or type(tool_calls) is not int
            or tool_calls < 0
            or isinstance(cost_usd, bool)
            or not isinstance(cost_usd, (int, float))
            or not math.isfinite(float(cost_usd))
            or float(cost_usd) < 0
        ):
            raise ValueError("Job lifecycle lease is invalid")
        payload = {
            "kind": "GRAPH_MUTATION",
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "cost_usd": round(float(cost_usd), 12),
        }
        return model_calls, tool_calls, payload["cost_usd"], _digest_json(payload)

    def reserve_job_lifecycle_lease(
        self,
        *,
        job_id: str,
        lease_id: str,
        lease: Mapping[str, Any],
        reason: str,
        claimed_graph_proposal_id: str = "",
        before_graph_digest: str = "",
        after_graph_digest: str = "",
    ) -> dict[str, Any]:
        """Durably reserve a bounded graph-mutation delta before dispatch.

        The Kernel remains the execution authority and does the primary
        reservation calculation.  This transaction adds the missing durable
        re-entry guard: an interrupted process cannot silently accept the same
        graph rewrite twice, nor can later rewrites consume capacity already
        committed by an earlier accepted patch.
        """

        if not lease_id.strip() or not reason.strip() or len(reason) > 256:
            raise ValueError("Job lifecycle lease identity is invalid")
        model_calls, tool_calls, cost_usd, payload_hash = (
            self._validated_graph_mutation_lease(lease)
        )
        now = utc_now().isoformat()
        with self._transaction() as conn:
            lifecycle = conn.execute(
                "SELECT state FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lifecycle is None:
                raise ValueError("Job lifecycle is not admitted")
            existing = conn.execute(
                "SELECT * FROM job_lifecycle_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["job_id"]) == job_id
                    and str(existing["payload_hash"]) == payload_hash
                ):
                    return dict(existing)
                raise ValueError("Job lifecycle lease identity conflicts")
            if str(lifecycle["state"]) != "ADMITTED":
                if (
                    str(lifecycle["state"]) != "PAUSED"
                    or not claimed_graph_proposal_id
                    or len(before_graph_digest) != 64
                    or len(after_graph_digest) != 64
                ):
                    raise ValueError("Job lifecycle does not allow a new lease")
                continuation = conn.execute(
                    "SELECT * FROM graph_proposal_continuations WHERE job_id = ? AND proposal_id = ?",
                    (job_id, claimed_graph_proposal_id),
                ).fetchone()
                if (
                    continuation is None
                    or str(continuation["status"]) != "CLAIMED"
                    or str(continuation["before_graph_digest"]) != before_graph_digest
                    or str(continuation["after_graph_digest"]) != after_graph_digest
                ):
                    raise ValueError("Paused Graph proposal continuation cannot reserve this lease")
            snapshot = self._job_snapshot_row(conn, job_id)
            snapshot_payload = _loads(str(snapshot["payload_json"]), {})
            limits = snapshot_payload.get("job_limits", {})
            if not isinstance(limits, Mapping):
                raise ValueError("Job lifecycle limits are unavailable")
            active = conn.execute(
                """
                SELECT COALESCE(SUM(model_calls), 0) AS model_calls,
                       COALESCE(SUM(tool_calls), 0) AS tool_calls,
                       COALESCE(SUM(cost_usd), 0.0) AS cost_usd
                FROM job_lifecycle_leases
                WHERE job_id = ?
                  AND (
                    status = 'ACTIVE'
                    OR (status = 'SETTLED' AND reason LIKE 'FORFEITED_UNKNOWN_USAGE:%')
                  )
                """,
                (job_id,),
            ).fetchone()
            # Compiler and completed attempt usage are already irreversible;
            # active mutation leases are commitments.  This is the durable
            # form of hard cap - spent - committed.  Pending running work is
            # conservatively handled by the Kernel's in-memory reservation
            # until it becomes an append-only attempt or terminal settlement.
            planning = snapshot_payload.get("planning", {})
            compiler_usage = (
                planning.get("compiler_usage", {})
                if isinstance(planning, Mapping)
                else {}
            )
            spent_rows = conn.execute(
                "SELECT payload_json FROM job_attempts WHERE job_id = ?", (job_id,)
            ).fetchall()
            spent_model = int(compiler_usage.get("model_calls", 0) or 0)
            spent_tool = int(compiler_usage.get("tool_calls", 0) or 0)
            spent_cost = float(compiler_usage.get("cost_usd", 0.0) or 0.0)
            for spent_row in spent_rows:
                attempt = _loads(str(spent_row["payload_json"]), {})
                attempt_usage = attempt.get("usage", {}) if isinstance(attempt, Mapping) else {}
                if isinstance(attempt_usage, Mapping):
                    spent_model += int(attempt_usage.get("model_calls", 0) or 0)
                    spent_tool += int(attempt_usage.get("tool_calls", 0) or 0)
                    spent_cost += float(attempt_usage.get("cost_usd", 0.0) or 0.0)
            if (
                spent_model + int(active["model_calls"]) + model_calls
                > int(limits.get("max_total_model_calls", 0) or 0)
                or spent_tool + int(active["tool_calls"]) + tool_calls
                > int(limits.get("max_total_tool_calls", 0) or 0)
                or round(spent_cost + float(active["cost_usd"]) + cost_usd, 12)
                > float(limits.get("max_total_cost_usd", 0.0) or 0.0)
            ):
                raise ValueError("Job lifecycle lease exceeds the admitted hard cap")
            conn.execute(
                """
                INSERT INTO job_lifecycle_leases(
                    lease_id, job_id, kind, model_calls, tool_calls, cost_usd,
                    payload_hash, status, reason, created_at, settled_at
                ) VALUES (?, ?, 'GRAPH_MUTATION', ?, ?, ?, ?, 'ACTIVE', ?, ?, NULL)
                """,
                (lease_id, job_id, model_calls, tool_calls, cost_usd, payload_hash, reason, now),
            )
            row = conn.execute(
                "SELECT * FROM job_lifecycle_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def settle_job_lifecycle_leases(self, *, job_id: str, reason: str) -> None:
        """Close active Job-local commitments only when the Job terminals."""

        if not reason.strip() or len(reason) > 256:
            raise ValueError("Job lifecycle settlement reason is invalid")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            lifecycle = conn.execute(
                "SELECT state FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lifecycle is None:
                raise ValueError("Job lifecycle is not admitted")
            conn.execute(
                """
                UPDATE job_lifecycle_leases
                SET status = 'SETTLED', reason = ?, settled_at = ?
                WHERE job_id = ? AND status = 'ACTIVE'
                """,
                (reason, now, job_id),
            )

    def release_job_lifecycle_lease(
        self,
        *,
        job_id: str,
        lease_id: str,
        reason: str,
    ) -> None:
        """Release only an unconsumed pre-append lease after a rejected patch.

        This is intentionally unavailable to ordinary scheduling or operator
        hold paths.  Once an append-only graph patch exists, its capacity is a
        commitment until terminal settlement; only the reserve→append failure
        window can prove that none of the lease was made executable.
        """

        if not lease_id.strip() or not reason.strip() or len(reason) > 256:
            raise ValueError("Job lifecycle lease release identity is invalid")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            lease = conn.execute(
                "SELECT * FROM job_lifecycle_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if lease is None or str(lease["job_id"]) != job_id:
                raise ValueError("Job lifecycle lease was not found")
            if str(lease["status"]) == "RELEASED":
                return
            if str(lease["status"]) != "ACTIVE":
                raise ValueError("Job lifecycle lease is already settled")
            patch = conn.execute(
                "SELECT 1 FROM job_graph_patches WHERE event_id = ?", (lease_id,)
            ).fetchone()
            if patch is not None:
                raise ValueError("Accepted graph patch lease cannot be released")
            conn.execute(
                """
                UPDATE job_lifecycle_leases
                SET status = 'RELEASED', reason = ?, settled_at = ?
                WHERE lease_id = ?
                """,
                (reason, now, lease_id),
            )

    def forfeit_interrupted_job_lifecycle_leases(
        self,
        *,
        job_id: str,
        reason: str,
    ) -> int:
        """Record unknown interrupted mutation capacity as non-reusable.

        A process crash can leave a graph patch accepted while its added work
        has no terminal attempt record. The capacity cannot be returned just
        because the process disappeared. An operator must first pause or
        cancel the Job, then this explicit settlement marks it as spent for
        all future lease admissions of the same Job.
        """

        if not reason.strip() or len(reason) > 192:
            raise ValueError("Unknown-usage forfeit reason is invalid")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            lifecycle = conn.execute(
                "SELECT state FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lifecycle is None:
                raise ValueError("Job lifecycle is not admitted")
            if str(lifecycle["state"]) not in {"PAUSED", "CANCELLED"}:
                raise ValueError("Unknown-usage forfeit requires a paused or cancelled Job")
            cursor = conn.execute(
                """
                UPDATE job_lifecycle_leases
                SET status = 'SETTLED', reason = ?, settled_at = ?
                WHERE job_id = ? AND status = 'ACTIVE'
                """,
                (f"FORFEITED_UNKNOWN_USAGE:{reason}", now, job_id),
            )
        return int(cursor.rowcount)

    def submit_job_user_correction(
        self,
        *,
        job_id: str,
        target_task_id: str,
        reference: str,
    ) -> dict[str, Any]:
        """Queue one opaque user correction for a still-admitted Job task."""

        if (
            not target_task_id.strip()
            or not reference.strip()
            or len(target_task_id) > 160
            or len(reference) > 160
        ):
            raise ValueError("User correction signal identity is invalid")
        signal_id = _digest_json(
            {
                "job_id": job_id,
                "target_task_id": target_task_id,
                "code": "USER_CORRECTION",
                "reference": reference,
            }
        )
        now = utc_now().isoformat()
        with self._transaction() as conn:
            lifecycle = conn.execute(
                "SELECT state FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lifecycle is None or str(lifecycle["state"]) != "ADMITTED":
                raise ValueError("User correction requires an admitted Job")
            snapshot = self._job_snapshot_row(conn, job_id)
            snapshot_payload = _loads(str(snapshot["payload_json"]), {})
            task_ids = {
                str(task.get("task_id", ""))
                for task in snapshot_payload.get("tasks", ())
                if isinstance(task, Mapping)
            }
            if target_task_id not in task_ids:
                raise ValueError("User correction target is not in the Job snapshot")
            existing = conn.execute(
                "SELECT * FROM job_operator_signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            conn.execute(
                """
                INSERT INTO job_operator_signals(
                    signal_id, job_id, target_task_id, code, reference,
                    status, created_at, consumed_at
                ) VALUES (?, ?, ?, 'USER_CORRECTION', ?, 'PENDING', ?, NULL)
                """,
                (signal_id, job_id, target_task_id, reference, now),
            )
            row = conn.execute(
                "SELECT * FROM job_operator_signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def consume_job_operator_signals(
        self,
        *,
        job_id: str,
        target_task_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Claim queued corrections exactly once at the task-result boundary."""

        now = utc_now().isoformat()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM job_operator_signals
                WHERE job_id = ? AND target_task_id = ? AND status = 'PENDING'
                ORDER BY created_at, signal_id
                """,
                (job_id, target_task_id),
            ).fetchall()
            if not rows:
                return ()
            conn.executemany(
                """
                UPDATE job_operator_signals
                SET status = 'CONSUMED', consumed_at = ?
                WHERE signal_id = ? AND status = 'PENDING'
                """,
                ((now, str(row["signal_id"])) for row in rows),
            )
        return tuple(dict(row) for row in rows)

    def list_job_operator_signals(self, job_id: str) -> tuple[dict[str, Any], ...]:
        """Return typed operator-signal receipts without exposing references upstream.

        The caller must deliberately discard the ``reference`` field before a
        report/episode leaves the runtime store.  This method exists so an
        outcome projection can distinguish pending from consumed corrections;
        it grants no consume, retry, or graph-mutation authority.
        """

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT signal_id, job_id, target_task_id, code, status, created_at, consumed_at
                FROM job_operator_signals
                WHERE job_id = ?
                ORDER BY created_at, signal_id
                """,
                (job_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)


