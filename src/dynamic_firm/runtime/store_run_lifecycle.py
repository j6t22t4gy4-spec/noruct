"""Core Employee-run lifecycle composed into the canonical RunStore.

The owner supplies one SQLite connection, transaction boundary, subscriber
notification and Employee-session persistence. This mixin owns run creation,
event replay, messages and terminal result transitions.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from dynamic_firm._vendor.paperclip_runtime.run_summary import summarize_terminal_result
from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.frozen_route_admission import FrozenRouteAdmission

from .models import (
    EmployeeRunRequest,
    EmployeeRunResult,
    EventType,
    ModelMessage,
    RunEvent,
    RunHandle,
    RunStatus,
    Usage,
    result_from_dict,
    to_primitive,
    usage_from_dict,
    utc_now,
)
from .redaction import redact_prompt_text, redact_runtime_value
from .store_employee_session import EmployeeSessionUpdate
from .store_run_primitives import safe_request_json as serialize_request_json


def _json(value: Any) -> str:
    return json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_json(value: Any) -> str:
    return _json(redact_runtime_value(to_primitive(value)))


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


class RunStoreRunLifecycleMixin:
    """Durable Employee-run and event lifecycle methods."""

    def _next_seq(self, conn: sqlite3.Connection, run_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["next_seq"])

    def _event_from_values(
        self,
        *,
        event_id: str,
        run: Mapping[str, Any],
        seq: int,
        event_type: EventType,
        payload: Mapping[str, Any],
        usage_delta: Usage | None,
        occurred_at,
    ) -> RunEvent:
        return RunEvent(
            event_id=event_id,
            run_id=str(run["run_id"]),
            seq=seq,
            job_id=str(run["job_id"]),
            task_id=str(run["task_id"]),
            employee_id=str(run["employee_id"]),
            type=event_type,
            payload=dict(payload),
            usage_delta=usage_delta,
            occurred_at=occurred_at,
        )

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        run: Mapping[str, Any],
        event_type: EventType,
        payload: Mapping[str, Any],
        usage_delta: Usage | None = None,
    ) -> RunEvent:
        safe_payload = redact_runtime_value(to_primitive(payload))
        seq = self._next_seq(conn, str(run["run_id"]))
        event_id = str(uuid.uuid4())
        now = utc_now()
        conn.execute(
            """
            INSERT INTO run_events(
                event_id, run_id, seq, event_type, payload_json, usage_delta_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run["run_id"],
                seq,
                event_type.value,
                _json(safe_payload),
                _json(usage_delta) if usage_delta else None,
                now.isoformat(),
            ),
        )
        return self._event_from_values(
            event_id=event_id,
            run=run,
            seq=seq,
            event_type=event_type,
            payload=safe_payload,
            usage_delta=usage_delta,
            occurred_at=now,
        )

    def create_run(
        self,
        request: EmployeeRunRequest,
        *,
        frozen_route_binding: ExecutionRouteBinding | None = None,
        frozen_route_admission: FrozenRouteAdmission | None = None,
    ) -> tuple[RunHandle, bool]:
        if frozen_route_admission is not None:
            if not isinstance(frozen_route_admission, FrozenRouteAdmission):
                raise TypeError("frozen_route_admission must be a FrozenRouteAdmission")
            if (
                frozen_route_binding is not None
                and frozen_route_binding != frozen_route_admission.binding
            ):
                raise ValueError(
                    "Frozen route admission binding must match frozen route binding"
                )
            frozen_route_binding = frozen_route_admission.binding
        now = utc_now()
        event: RunEvent | None = None
        safe_request_json = serialize_request_json(
            request,
            request.employee.employee_id,
        )
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT run_id, request_id, request_json FROM employee_runs WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if existing:
                if existing["request_json"] != safe_request_json:
                    raise ValueError(
                        f"Idempotency key {request.request_id!r} was reused with a different request snapshot"
                    )
                existing_binding = self._get_frozen_route_binding_in_transaction(
                    conn, str(existing["run_id"])
                )
                if existing_binding != frozen_route_binding:
                    raise ValueError(
                        f"Idempotency key {request.request_id!r} was reused with a different frozen route binding"
                    )
                existing_admission = self._get_frozen_route_admission_in_transaction(
                    conn, str(existing["run_id"])
                )
                if existing_admission != frozen_route_admission:
                    raise ValueError(
                        f"Idempotency key {request.request_id!r} was reused with a different frozen route admission"
                    )
                return RunHandle(existing["run_id"], existing["request_id"]), False
            run_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO employee_runs(
                    run_id, request_id, job_id, task_id, employee_id, status,
                    request_json, usage_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    request.request_id,
                    request.task.job_id,
                    request.task.task_id,
                    request.employee.employee_id,
                    RunStatus.CREATED.value,
                    safe_request_json,
                    _json(Usage()),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            if frozen_route_binding is not None:
                self._insert_frozen_route_binding_in_transaction(
                    conn, run_id, frozen_route_binding, now.isoformat()
                )
            if frozen_route_admission is not None:
                self._insert_frozen_route_admission_in_transaction(
                    conn, run_id, frozen_route_admission, now.isoformat()
                )
            event = self._insert_event(
                conn,
                run,
                EventType.RUN_CREATED,
                {
                    "request_id": request.request_id,
                    "job_graph_version": request.task.job_graph_version,
                    "attempt": request.task.attempt,
                    "prompt_revision": request.employee.prompt_revision,
                    "authority_revision": request.employee.authority_revision,
                },
            )
        assert event is not None
        self._notify(event)
        return RunHandle(run_id, request.request_id), True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_job_runs(self, job_id: str) -> list[dict[str, Any]]:
        """Return the immutable employee-run ledger rows for one company job."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM employee_runs
                WHERE job_id = ?
                ORDER BY created_at, task_id, run_id
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_job_events(self, job_id: str) -> list[RunEvent]:
        """Replay all per-run append-only events for a job in occurrence order."""

        events = [
            event
            for run in self.list_job_runs(job_id)
            for event in self.list_events(str(run["run_id"]))
        ]
        return sorted(
            events,
            key=lambda event: (
                event.occurred_at,
                event.run_id,
                event.seq,
            ),
        )

    def list_job_events_window(
        self,
        job_id: str,
        *,
        from_at: datetime,
        to_at: datetime,
        limit: int,
    ) -> tuple[list[RunEvent], bool]:
        """Return the newest bounded job events in chronological display order.

        This is a read-only operator projection.  It intentionally fetches one
        extra row so callers can disclose truncation instead of silently
        treating a bounded view as a complete audit replay.
        """

        if limit < 1:
            raise ValueError("timeline limit must be positive")
        if from_at.tzinfo is None or to_at.tzinfo is None:
            raise ValueError("timeline timestamps must include a UTC offset")
        window_start = from_at.astimezone(timezone.utc)
        window_end = to_at.astimezone(timezone.utc)
        if window_start > window_end:
            raise ValueError("timeline start must not be after its end")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    events.event_id,
                    events.run_id,
                    events.seq,
                    events.event_type,
                    events.payload_json,
                    events.usage_delta_json,
                    events.occurred_at,
                    runs.job_id,
                    runs.task_id,
                    runs.employee_id
                FROM run_events AS events
                JOIN employee_runs AS runs ON runs.run_id = events.run_id
                WHERE runs.job_id = ?
                  AND events.occurred_at >= ?
                  AND events.occurred_at <= ?
                ORDER BY events.occurred_at DESC, events.run_id DESC, events.seq DESC
                LIMIT ?
                """,
                (job_id, window_start.isoformat(), window_end.isoformat(), limit + 1),
            ).fetchall()
        truncated = len(rows) > limit
        selected = rows[:limit]
        events = [
            self._event_from_values(
                event_id=str(row["event_id"]),
                run=row,
                seq=int(row["seq"]),
                event_type=EventType(row["event_type"]),
                payload=_loads(row["payload_json"], {}),
                usage_delta=(
                    usage_from_dict(_loads(row["usage_delta_json"], {}))
                    if row["usage_delta_json"]
                    else None
                ),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            )
            for row in selected
        ]
        events.sort(key=lambda event: (event.occurred_at, event.run_id, event.seq))
        return events, truncated

    def get_status(self, run_id: str) -> RunStatus:
        row = self.get_run(run_id)
        if not row:
            raise KeyError(f"Unknown run: {run_id}")
        return RunStatus(row["status"])

    def get_usage(self, run_id: str) -> Usage:
        row = self.get_run(run_id)
        if not row:
            raise KeyError(f"Unknown run: {run_id}")
        return usage_from_dict(_loads(row["usage_json"], {}))

    def append_event(
        self,
        run_id: str,
        event_type: EventType,
        payload: Mapping[str, Any],
        *,
        usage_delta: Usage | None = None,
        new_usage: Usage | None = None,
        new_status: RunStatus | None = None,
    ) -> RunEvent:
        with self._transaction() as conn:
            run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                raise KeyError(f"Unknown run: {run_id}")
            current = RunStatus(run["status"])
            if current.terminal:
                raise RuntimeError(f"Cannot append {event_type} to terminal run {run_id}")
            now = utc_now()
            updates = ["updated_at = ?"]
            values: list[Any] = [now.isoformat()]
            if new_usage is not None:
                updates.append("usage_json = ?")
                values.append(_json(new_usage))
            if new_status is not None:
                if new_status.terminal:
                    raise ValueError("Use terminalize() for terminal state")
                updates.append("status = ?")
                values.append(new_status.value)
                if new_status == RunStatus.RUNNING and not run["started_at"]:
                    updates.append("started_at = ?")
                    values.append(now.isoformat())
            values.append(run_id)
            conn.execute(f"UPDATE employee_runs SET {', '.join(updates)} WHERE run_id = ?", values)
            updated = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            event = self._insert_event(conn, updated, event_type, payload, usage_delta)
        self._notify(event)
        return event

    def begin_run(self, run_id: str) -> RunStatus:
        current = self.get_status(run_id)
        if current == RunStatus.CREATED:
            self.append_event(run_id, EventType.RUN_STARTED, {}, new_status=RunStatus.RUNNING)
            return RunStatus.RUNNING
        return current

    def record_prompt(
        self,
        run_id: str,
        prompt_hash: str,
        context_hash: str,
        *,
        knowledge_projection: Mapping[str, Any] | None = None,
        capability_projection: Mapping[str, Any] | None = None,
    ) -> RunEvent:
        with self._transaction() as conn:
            run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                raise KeyError(f"Unknown run: {run_id}")
            if RunStatus(run["status"]).terminal:
                raise RuntimeError("Cannot snapshot a prompt for a terminal run")
            conn.execute(
                "UPDATE employee_runs SET prompt_hash = ?, context_hash = ?, updated_at = ? WHERE run_id = ?",
                (prompt_hash, context_hash, utc_now().isoformat(), run_id),
            )
            updated = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            event = self._insert_event(
                conn,
                updated,
                EventType.PROMPT_SNAPSHOTTED,
                {
                    "prompt_hash": prompt_hash,
                    "context_hash": context_hash,
                    **(
                        {"knowledge_projection": dict(knowledge_projection)}
                        if knowledge_projection
                        else {}
                    ),
                    **(
                        {"capability_projection": dict(capability_projection)}
                        if capability_projection
                        else {}
                    ),
                },
            )
        self._notify(event)
        return event

    def append_message(self, run_id: str, message: ModelMessage) -> int:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS position FROM run_messages WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            position = int(row["position"])
            conn.execute(
                """
                INSERT INTO run_messages(run_id, position, role, content_json, tool_call_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    position,
                    message.role,
                    _safe_json(message.content),
                    redact_prompt_text(message.tool_call_id)
                    if message.tool_call_id
                    else None,
                    utc_now().isoformat(),
                ),
            )
        return position

    def append_tool_message_once(self, run_id: str, message: ModelMessage) -> bool:
        if message.role != "tool" or not message.tool_call_id:
            raise ValueError("Idempotent tool messages require role=tool and tool_call_id")
        if not isinstance(message.content, Mapping) or not message.content.get("action_id"):
            raise ValueError("Idempotent tool messages require an action_id")
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT content_json FROM run_messages
                WHERE run_id = ? AND role = 'tool'
                """,
                (run_id,),
            ).fetchall()
            safe_content = _safe_json(message.content)
            existing = next(
                (
                    row
                    for row in rows
                    if _loads(str(row["content_json"]), {}).get("action_id")
                    == message.content.get("action_id")
                ),
                None,
            )
            if existing is not None:
                if str(existing["content_json"]) != safe_content:
                    raise RuntimeError("Tool result message identity conflict")
                return False
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS position FROM run_messages WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO run_messages(run_id, position, role, content_json, tool_call_id, created_at)
                VALUES (?, ?, 'tool', ?, ?, ?)
                """,
                (
                    run_id,
                    int(row["position"]),
                    safe_content,
                    redact_prompt_text(message.tool_call_id),
                    utc_now().isoformat(),
                ),
            )
        return True

    def list_messages(self, run_id: str) -> list[ModelMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content_json, tool_call_id FROM run_messages WHERE run_id = ? ORDER BY position",
                (run_id,),
            ).fetchall()
        return [
            ModelMessage(row["role"], _loads(row["content_json"], None), row["tool_call_id"])
            for row in rows
        ]

    def list_tool_actions(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tool_actions
                WHERE run_id = ?
                ORDER BY model_call_index, created_at, action_id
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def tool_call_count_before(
        self,
        run_id: str,
        model_call_index: int,
        tool_name: str,
    ) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count FROM tool_actions
                WHERE run_id = ? AND model_call_index < ? AND tool_name = ?
                """,
                (run_id, model_call_index, tool_name),
            ).fetchone()
        return int(row["count"])

    def list_events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(f"Unknown run: {run_id}")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [
            self._event_from_values(
                event_id=row["event_id"],
                run=run,
                seq=int(row["seq"]),
                event_type=EventType(row["event_type"]),
                payload=_loads(row["payload_json"], {}),
                usage_delta=usage_from_dict(_loads(row["usage_delta_json"], {}))
                if row["usage_delta_json"]
                else None,
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
            )
            for row in rows
        ]

    def get_last_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["seq"])

    def request_cancel(self, run_id: str, reason: str) -> tuple[bool, RunStatus]:
        event: RunEvent | None = None
        safe_reason = redact_prompt_text(reason)
        with self._transaction() as conn:
            run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                raise KeyError(f"Unknown run: {run_id}")
            status = RunStatus(run["status"])
            if status.terminal:
                return False, status
            if status == RunStatus.CANCELLING:
                return True, status
            conn.execute(
                "UPDATE employee_runs SET status = ?, cancel_reason = ?, updated_at = ? WHERE run_id = ?",
                (RunStatus.CANCELLING.value, safe_reason, utc_now().isoformat(), run_id),
            )
            updated = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            event = self._insert_event(
                conn,
                updated,
                EventType.CANCEL_REQUESTED,
                {"reason": safe_reason},
            )
        assert event is not None
        self._notify(event)
        return True, RunStatus.CANCELLING

    def terminalize(
        self,
        result: EmployeeRunResult,
        event_type: EventType,
        payload: Mapping[str, Any],
        *,
        employee_session: EmployeeSessionUpdate | None = None,
    ) -> EmployeeRunResult:
        if not result.status.terminal:
            raise ValueError("terminalize requires a terminal result status")
        # Keep the terminal event useful to an operator surface without making the
        # whole result, transcript, or tool output part of its event payload.
        terminal_payload = dict(payload)
        summary = summarize_terminal_result(to_primitive(result))
        if summary is not None:
            terminal_payload["terminal_summary"] = summary
        with self._transaction() as conn:
            run = conn.execute("SELECT * FROM employee_runs WHERE run_id = ?", (result.run_id,)).fetchone()
            if not run:
                raise KeyError(f"Unknown run: {result.run_id}")
            current = RunStatus(run["status"])
            if current.terminal:
                existing = _loads(run["result_json"], None)
                if not existing:
                    raise RuntimeError(f"Terminal run {result.run_id} has no result")
                return result_from_dict(existing)
            event = self._insert_event(conn, run, event_type, terminal_payload)
            if employee_session is not None:
                if result.status != RunStatus.SUCCEEDED:
                    raise ValueError("employee session history may only commit with success")
                self._write_employee_session(
                    conn,
                    update=employee_session,
                    run=run,
                    updated_at=event.occurred_at,
                )
            finished_at = event.occurred_at
            finalized = replace(result, last_event_seq=event.seq, finished_at=finished_at)
            finalized = result_from_dict(
                redact_runtime_value(to_primitive(finalized))
            )
            conn.execute(
                """
                UPDATE employee_runs
                SET status = ?, usage_json = ?, result_json = ?, failure_json = ?,
                    finished_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    finalized.status.value,
                    _json(finalized.usage),
                    _json(finalized),
                    _json(finalized.failure) if finalized.failure else None,
                    finished_at.isoformat(),
                    finished_at.isoformat(),
                    finalized.run_id,
                ),
            )
        self._notify(event)
        return finalized

    def get_result(self, run_id: str) -> EmployeeRunResult | None:
        row = self.get_run(run_id)
        if not row or not row["result_json"]:
            return None
        return result_from_dict(_loads(row["result_json"], {}))
