"""Explicit portfolio lifecycle operations over the shared local transaction."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Protocol

from dynamic_firm.runtime.models import utc_now

from .portfolio_scheduling_store import (
    replay_lifecycle,
    scheduling_envelope,
    transition_lifecycle,
)
from .work_order_portfolio_models import (
    PortfolioLifecycleState,
    PortfolioSchedulingEnvelope,
    PortfolioStatus,
)


class PortfolioLifecycleHost(Protocol):
    _conn: sqlite3.Connection
    _lock: Any

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...


class PortfolioLifecycleOperations:
    """CAS-style pause/resume/cancel transitions; never reconstructs a Job."""

    def __init__(self, host: PortfolioLifecycleHost) -> None:
        self._host = host

    @staticmethod
    def _reason(value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError(f"Portfolio {label} reason is invalid")
        return normalized

    def pause_job(self, job_id: str, *, reason: str) -> PortfolioLifecycleState:
        normalized = self._reason(reason, "pause")
        with self._host._transaction() as conn:
            row = conn.execute(
                "SELECT work_order_id, status FROM portfolio_entries WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None or str(row["status"]) != PortfolioStatus.ADMITTED.value:
                raise ValueError("Only an active portfolio Job may pause")
            transition_lifecycle(
                conn,
                work_order_id=str(row["work_order_id"]),
                target=PortfolioLifecycleState.PAUSED,
                reason=normalized,
                job_id=job_id,
                now=utc_now().isoformat(),
                allowed_from=frozenset({PortfolioLifecycleState.RUNNING}),
            )
        return PortfolioLifecycleState.PAUSED

    def resume_job(self, job_id: str, *, reason: str) -> PortfolioLifecycleState:
        normalized = self._reason(reason, "resume")
        with self._host._transaction() as conn:
            row = conn.execute(
                "SELECT work_order_id, status FROM portfolio_entries WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None or str(row["status"]) != PortfolioStatus.ADMITTED.value:
                raise ValueError("Only a paused active portfolio Job may resume")
            transition_lifecycle(
                conn,
                work_order_id=str(row["work_order_id"]),
                target=PortfolioLifecycleState.RUNNING,
                reason=normalized,
                job_id=job_id,
                now=utc_now().isoformat(),
                allowed_from=frozenset({PortfolioLifecycleState.PAUSED}),
            )
        return PortfolioLifecycleState.RUNNING

    def cancel_job(
        self,
        job_id: str,
        *,
        reason: str,
        terminal_confirmed: bool,
    ) -> PortfolioLifecycleState:
        if terminal_confirmed is not True:
            raise ValueError("Portfolio cancellation requires terminal runtime confirmation")
        normalized = self._reason(reason, "cancellation")
        now = utc_now().isoformat()
        with self._host._transaction() as conn:
            row = conn.execute(
                "SELECT work_order_id, status FROM portfolio_entries WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None or str(row["status"]) != PortfolioStatus.ADMITTED.value:
                raise ValueError("Only an active portfolio Job may cancel")
            conn.execute(
                """UPDATE portfolio_entries SET status = 'CLOSED', reason = ?, updated_at = ?
                   WHERE job_id = ?""",
                (normalized, now, job_id),
            )
            transition_lifecycle(
                conn,
                work_order_id=str(row["work_order_id"]),
                target=PortfolioLifecycleState.CANCELLED,
                reason=normalized,
                job_id=job_id,
                now=now,
                allowed_from=frozenset(
                    {PortfolioLifecycleState.RUNNING, PortfolioLifecycleState.PAUSED}
                ),
            )
        return PortfolioLifecycleState.CANCELLED

    def scheduling_envelope(self, work_order_id: str) -> PortfolioSchedulingEnvelope:
        with self._host._lock:
            return scheduling_envelope(self._host._conn, work_order_id)

    def replay_lifecycle(
        self, work_order_id: str
    ) -> tuple[PortfolioLifecycleState, str]:
        with self._host._lock:
            return replay_lifecycle(self._host._conn, work_order_id)


__all__ = ["PortfolioLifecycleOperations"]
