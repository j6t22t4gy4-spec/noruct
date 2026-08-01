"""SQLite persistence for deterministic portfolio scheduling and replay."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Mapping

from .portfolio_scheduling import PortfolioScheduleRecord, plan_portfolio_admission
from .work_order_portfolio_models import (
    PortfolioLifecycleState,
    PortfolioPolicy,
    PortfolioSchedulingEnvelope,
    PortfolioStatus,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tuple_json(value: str) -> tuple[str, ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ValueError("Portfolio scheduling list is corrupt")
    return tuple(parsed)


def initialize_portfolio_scheduling(conn: sqlite3.Connection, *, now: str) -> None:
    """Install additive scheduling tables and migrate prior local entries."""

    policy_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(portfolio_policy)")
    }
    if "capability_slots_json" not in policy_columns:
        conn.execute(
            "ALTER TABLE portfolio_policy ADD COLUMN capability_slots_json "
            "TEXT NOT NULL DEFAULT '[]'"
        )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS portfolio_scheduling_envelopes (
            work_order_id TEXT PRIMARY KEY REFERENCES canonical_work_orders(work_order_id),
            dependency_work_order_ids_json TEXT NOT NULL,
            deadline_at TEXT,
            required_capabilities_json TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN (
                'QUEUED','RUNNING','PAUSED','BLOCKED','CANCELLED','TERMINAL'
            )),
            lifecycle_reason TEXT NOT NULL,
            defer_count INTEGER NOT NULL CHECK(defer_count >= 0),
            inherited_priority INTEGER NOT NULL CHECK(inherited_priority >= 0),
            effective_deadline_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS portfolio_lifecycle_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            work_order_id TEXT NOT NULL REFERENCES canonical_work_orders(work_order_id),
            from_state TEXT,
            to_state TEXT NOT NULL CHECK(to_state IN (
                'QUEUED','RUNNING','PAUSED','BLOCKED','CANCELLED','TERMINAL'
            )),
            reason TEXT NOT NULL,
            job_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS portfolio_lifecycle_event_order_idx
            ON portfolio_lifecycle_events(work_order_id, sequence);
        """
    )
    conn.execute(
        """INSERT OR IGNORE INTO portfolio_scheduling_envelopes(
               work_order_id, dependency_work_order_ids_json, deadline_at,
               required_capabilities_json, lifecycle_state, lifecycle_reason,
               defer_count, inherited_priority, effective_deadline_at,
               created_at, updated_at
           )
           SELECT work_order_id, '[]', NULL, '[]',
               CASE
                   WHEN status = 'CLOSED' THEN 'TERMINAL'
                   WHEN status = 'REJECTED' OR status = 'DEFERRED' THEN 'BLOCKED'
                   WHEN status = 'ADMITTED' AND job_id IS NOT NULL THEN 'RUNNING'
                   ELSE 'QUEUED'
               END,
               'MIGRATED_PORTFOLIO_STATE', 0, priority, NULL, created_at, ?
           FROM portfolio_entries""",
        (now,),
    )
    conn.execute(
        """INSERT INTO portfolio_lifecycle_events(
               work_order_id, from_state, to_state, reason, job_id, created_at
           )
           SELECT schedule.work_order_id, NULL, schedule.lifecycle_state,
                  schedule.lifecycle_reason, entry.job_id, ?
           FROM portfolio_scheduling_envelopes schedule
           JOIN portfolio_entries entry USING(work_order_id)
           WHERE NOT EXISTS (
               SELECT 1 FROM portfolio_lifecycle_events event
               WHERE event.work_order_id = schedule.work_order_id
           )""",
        (now,),
    )


def scheduling_envelope(
    conn: sqlite3.Connection, work_order_id: str
) -> PortfolioSchedulingEnvelope:
    row = conn.execute(
        "SELECT * FROM portfolio_scheduling_envelopes WHERE work_order_id = ?",
        (work_order_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown portfolio scheduling envelope: {work_order_id}")
    return PortfolioSchedulingEnvelope(
        work_order_id=work_order_id,
        dependency_work_order_ids=_tuple_json(
            str(row["dependency_work_order_ids_json"])
        ),
        deadline_at=None if row["deadline_at"] is None else str(row["deadline_at"]),
        required_capabilities=_tuple_json(str(row["required_capabilities_json"])),
    )


def _assert_acyclic(
    conn: sqlite3.Connection,
    envelope: PortfolioSchedulingEnvelope,
) -> None:
    rows = conn.execute(
        "SELECT work_order_id, dependency_work_order_ids_json "
        "FROM portfolio_scheduling_envelopes"
    ).fetchall()
    graph = {
        str(row["work_order_id"]): _tuple_json(
            str(row["dependency_work_order_ids_json"])
        )
        for row in rows
    }
    graph[envelope.work_order_id] = envelope.dependency_work_order_ids
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visited:
            return
        if identifier in visiting:
            raise ValueError("Portfolio dependencies cannot contain cycles")
        visiting.add(identifier)
        for dependency in graph.get(identifier, ()):
            if dependency in graph:
                visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in sorted(graph):
        visit(identifier)


def retain_scheduling_envelope(
    conn: sqlite3.Connection,
    envelope: PortfolioSchedulingEnvelope,
    *,
    priority: int,
    now: str,
) -> None:
    existing = conn.execute(
        "SELECT * FROM portfolio_scheduling_envelopes WHERE work_order_id = ?",
        (envelope.work_order_id,),
    ).fetchone()
    expected = (
        _json(list(envelope.dependency_work_order_ids)),
        envelope.deadline_at,
        _json(list(envelope.required_capabilities)),
    )
    if existing is not None:
        actual = (
            str(existing["dependency_work_order_ids_json"]),
            None if existing["deadline_at"] is None else str(existing["deadline_at"]),
            str(existing["required_capabilities_json"]),
        )
        if actual != expected:
            raise ValueError("Portfolio scheduling envelope conflicts")
        return
    if envelope.dependency_work_order_ids:
        placeholders = ",".join("?" for _ in envelope.dependency_work_order_ids)
        found = conn.execute(
            f"SELECT work_order_id FROM canonical_work_orders "
            f"WHERE work_order_id IN ({placeholders})",
            envelope.dependency_work_order_ids,
        ).fetchall()
        if {str(row["work_order_id"]) for row in found} != set(
            envelope.dependency_work_order_ids
        ):
            raise ValueError("Portfolio dependencies must already be retained Work Orders")
    _assert_acyclic(conn, envelope)
    conn.execute(
        """INSERT INTO portfolio_scheduling_envelopes(
               work_order_id, dependency_work_order_ids_json, deadline_at,
               required_capabilities_json, lifecycle_state, lifecycle_reason,
               defer_count, inherited_priority, effective_deadline_at,
               created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            envelope.work_order_id,
            expected[0],
            expected[1],
            expected[2],
            PortfolioLifecycleState.QUEUED.value,
            "SUBMITTED",
            0,
            priority,
            envelope.deadline_at,
            now,
            now,
        ),
    )
    conn.execute(
        """INSERT INTO portfolio_lifecycle_events(
               work_order_id, from_state, to_state, reason, job_id, created_at
           ) VALUES(?,NULL,'QUEUED','SUBMITTED',NULL,?)""",
        (envelope.work_order_id, now),
    )


def transition_lifecycle(
    conn: sqlite3.Connection,
    *,
    work_order_id: str,
    target: PortfolioLifecycleState,
    reason: str,
    job_id: str | None,
    now: str,
    allowed_from: frozenset[PortfolioLifecycleState] | None = None,
) -> None:
    row = conn.execute(
        "SELECT lifecycle_state, lifecycle_reason FROM portfolio_scheduling_envelopes "
        "WHERE work_order_id = ?",
        (work_order_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown portfolio scheduling envelope: {work_order_id}")
    current = PortfolioLifecycleState(str(row["lifecycle_state"]))
    if allowed_from is not None and current not in allowed_from and current is not target:
        raise ValueError(f"Portfolio lifecycle cannot transition {current.value} to {target.value}")
    if current is target and str(row["lifecycle_reason"]) == reason:
        return
    conn.execute(
        """UPDATE portfolio_scheduling_envelopes
           SET lifecycle_state = ?, lifecycle_reason = ?, updated_at = ?
           WHERE work_order_id = ?""",
        (target.value, reason, now, work_order_id),
    )
    conn.execute(
        """INSERT INTO portfolio_lifecycle_events(
               work_order_id, from_state, to_state, reason, job_id, created_at
           ) VALUES(?,?,?,?,?,?)""",
        (work_order_id, current.value, target.value, reason, job_id, now),
    )


def _records(conn: sqlite3.Connection) -> tuple[PortfolioScheduleRecord, ...]:
    rows = conn.execute(
        """SELECT entry.*, schedule.*, settlement.terminal_status
           FROM portfolio_entries entry
           JOIN portfolio_scheduling_envelopes schedule USING(work_order_id)
           LEFT JOIN portfolio_job_settlements settlement ON settlement.job_id = entry.job_id
           ORDER BY entry.created_at, entry.work_order_id"""
    ).fetchall()
    return tuple(
        PortfolioScheduleRecord(
            work_order_id=str(row["work_order_id"]),
            priority=int(row["priority"]),
            reserved_cost_usd=float(row["reserved_cost_usd"]),
            admission_status=PortfolioStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            dependency_work_order_ids=_tuple_json(
                str(row["dependency_work_order_ids_json"])
            ),
            deadline_at=None if row["deadline_at"] is None else str(row["deadline_at"]),
            required_capabilities=_tuple_json(str(row["required_capabilities_json"])),
            lifecycle_state=PortfolioLifecycleState(str(row["lifecycle_state"])),
            defer_count=int(row["defer_count"]),
            terminal_status=(
                None if row["terminal_status"] is None else str(row["terminal_status"])
            ),
        )
        for row in rows
    )


def reconcile_scheduling(
    conn: sqlite3.Connection,
    policy: PortfolioPolicy,
    *,
    now: str,
) -> None:
    parsed_now = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(UTC)
    for decision in plan_portfolio_admission(_records(conn), policy, now=parsed_now):
        conn.execute(
            """UPDATE portfolio_entries
               SET status = ?, reason = ?, updated_at = ?
               WHERE work_order_id = ?""",
            (
                decision.admission_status.value,
                decision.admission_reason,
                now,
                decision.work_order_id,
            ),
        )
        conn.execute(
            """UPDATE portfolio_scheduling_envelopes
               SET defer_count = ?, inherited_priority = ?,
                   effective_deadline_at = ?, updated_at = ?
               WHERE work_order_id = ?""",
            (
                decision.defer_count,
                decision.inherited_priority,
                decision.effective_deadline_at,
                now,
                decision.work_order_id,
            ),
        )
        transition_lifecycle(
            conn,
            work_order_id=decision.work_order_id,
            target=decision.lifecycle_state,
            reason=decision.lifecycle_reason,
            job_id=None,
            now=now,
        )


def scheduling_projection(
    conn: sqlite3.Connection,
) -> dict[str, Mapping[str, object]]:
    rows = conn.execute(
        "SELECT * FROM portfolio_scheduling_envelopes"
    ).fetchall()
    return {
        str(row["work_order_id"]): {
            "dependency_work_order_ids": _tuple_json(
                str(row["dependency_work_order_ids_json"])
            ),
            "deadline_at": None if row["deadline_at"] is None else str(row["deadline_at"]),
            "required_capabilities": _tuple_json(
                str(row["required_capabilities_json"])
            ),
            "lifecycle_state": str(row["lifecycle_state"]),
            "lifecycle_reason": str(row["lifecycle_reason"]),
            "defer_count": int(row["defer_count"]),
            "inherited_priority": int(row["inherited_priority"]),
            "effective_deadline_at": (
                None
                if row["effective_deadline_at"] is None
                else str(row["effective_deadline_at"])
            ),
        }
        for row in rows
    }


def replay_lifecycle(
    conn: sqlite3.Connection, work_order_id: str
) -> tuple[PortfolioLifecycleState, str]:
    events = conn.execute(
        """SELECT from_state, to_state, reason
           FROM portfolio_lifecycle_events
           WHERE work_order_id = ? ORDER BY sequence""",
        (work_order_id,),
    ).fetchall()
    if not events or events[0]["from_state"] is not None:
        raise ValueError("Portfolio lifecycle history has no genesis event")
    state: PortfolioLifecycleState | None = None
    reason = ""
    for event in events:
        expected = None if state is None else state.value
        if event["from_state"] != expected:
            raise ValueError("Portfolio lifecycle history is not contiguous")
        state = PortfolioLifecycleState(str(event["to_state"]))
        reason = str(event["reason"])
    assert state is not None
    current = conn.execute(
        """SELECT lifecycle_state, lifecycle_reason
           FROM portfolio_scheduling_envelopes WHERE work_order_id = ?""",
        (work_order_id,),
    ).fetchone()
    if current is None or (state.value, reason) != (
        str(current["lifecycle_state"]),
        str(current["lifecycle_reason"]),
    ):
        raise ValueError("Portfolio lifecycle replay does not match current state")
    return state, reason


__all__ = [
    "initialize_portfolio_scheduling",
    "reconcile_scheduling",
    "replay_lifecycle",
    "retain_scheduling_envelope",
    "scheduling_envelope",
    "scheduling_projection",
    "transition_lifecycle",
]
