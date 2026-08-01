"""Append-only local estimate-change notices; never a runtime stop mechanism."""

from __future__ import annotations

import math
import re
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Protocol
from uuid import uuid4

from dynamic_firm.runtime.models import utc_now

from .work_order_portfolio_models import (
    PortfolioReestimate,
    PortfolioReestimateChoice,
    PortfolioStatus,
)


_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class PortfolioReestimateHost(Protocol):
    _conn: sqlite3.Connection
    _lock: Any

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...


def initialize_portfolio_reestimates(conn: sqlite3.Connection) -> None:
    """Install only local, append-only planning receipt tables."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS portfolio_reestimates (
            reestimate_id TEXT PRIMARY KEY,
            work_order_id TEXT NOT NULL REFERENCES portfolio_entries(work_order_id),
            job_id TEXT,
            prior_reserved_cost_usd REAL NOT NULL CHECK(prior_reserved_cost_usd >= 0),
            proposed_reserved_cost_usd REAL NOT NULL CHECK(proposed_reserved_cost_usd >= 0),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS portfolio_reestimate_order_idx
            ON portfolio_reestimates(work_order_id, created_at, reestimate_id);
        CREATE TABLE IF NOT EXISTS portfolio_reestimate_decisions (
            reestimate_id TEXT PRIMARY KEY REFERENCES portfolio_reestimates(reestimate_id),
            choice TEXT NOT NULL CHECK(choice IN ('CONTINUE','REDUCE','CANCEL')),
            reason TEXT NOT NULL,
            decided_at TEXT NOT NULL
        );
        """
    )


class PortfolioReestimateOperations:
    """A mixin for durable re-estimate notices and explicit user choices.

    An estimate is a local scheduling fact, not a second cost authority.  In
    particular, recording or deciding a notice cannot cancel, pause, dispatch,
    modify a canonical Work Order, or alter a Company budget lease.
    """

    @staticmethod
    def _reason(value: str) -> str:
        normalized = value.strip()
        if not _REASON.fullmatch(normalized):
            raise ValueError("Portfolio re-estimate reason must be an uppercase bounded code")
        return normalized

    @staticmethod
    def _amount(value: float, *, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Portfolio {label} must be a finite non-negative amount")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError(f"Portfolio {label} must be a finite non-negative amount")
        return normalized

    @staticmethod
    def _row(row: sqlite3.Row) -> PortfolioReestimate:
        choice = row["choice"]
        return PortfolioReestimate(
            reestimate_id=str(row["reestimate_id"]),
            work_order_id=str(row["work_order_id"]),
            job_id=None if row["job_id"] is None else str(row["job_id"]),
            prior_reserved_cost_usd=float(row["prior_reserved_cost_usd"]),
            proposed_reserved_cost_usd=float(row["proposed_reserved_cost_usd"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
            choice=None if choice is None else PortfolioReestimateChoice(str(choice)),
            choice_reason=None if row["choice_reason"] is None else str(row["choice_reason"]),
            decided_at=None if row["decided_at"] is None else str(row["decided_at"]),
        )

    def report_reestimate(
        self: PortfolioReestimateHost,
        work_order_id: str,
        *,
        proposed_reserved_cost_usd: float,
        reason: str,
    ) -> PortfolioReestimate:
        """Record a changed estimate while leaving the existing Job untouched."""

        proposed = self._amount(proposed_reserved_cost_usd, label="re-estimate")
        normalized_reason = self._reason(reason)
        with self._transaction() as conn:
            entry = conn.execute(
                "SELECT job_id, reserved_cost_usd, status FROM portfolio_entries WHERE work_order_id = ?",
                (work_order_id,),
            ).fetchone()
            if entry is None or str(entry["status"]) == PortfolioStatus.CLOSED.value:
                raise ValueError("Only a non-terminal portfolio Work Order may receive a re-estimate")
            now = utc_now().isoformat()
            reestimate_id = f"estimate-{uuid4()}"
            conn.execute(
                """INSERT INTO portfolio_reestimates(
                       reestimate_id, work_order_id, job_id, prior_reserved_cost_usd,
                       proposed_reserved_cost_usd, reason, created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    reestimate_id,
                    work_order_id,
                    entry["job_id"],
                    float(entry["reserved_cost_usd"]),
                    proposed,
                    normalized_reason,
                    now,
                ),
            )
            row = conn.execute(
                """SELECT request.*, decision.choice, decision.reason AS choice_reason,
                          decision.decided_at
                   FROM portfolio_reestimates AS request
                   LEFT JOIN portfolio_reestimate_decisions AS decision
                     ON decision.reestimate_id = request.reestimate_id
                   WHERE request.reestimate_id = ?""",
                (reestimate_id,),
            ).fetchone()
        assert row is not None
        return self._row(row)

    def decide_reestimate(
        self: PortfolioReestimateHost,
        reestimate_id: str,
        *,
        choice: PortfolioReestimateChoice,
        reason: str,
        confirmed: bool,
    ) -> PortfolioReestimate:
        """Append one user decision without transforming it into a runtime action."""

        if confirmed is not True:
            raise ValueError("Portfolio re-estimate decision requires --confirm")
        if not isinstance(choice, PortfolioReestimateChoice):
            raise ValueError("Portfolio re-estimate choice is invalid")
        normalized_reason = self._reason(reason)
        with self._transaction() as conn:
            request = conn.execute(
                "SELECT 1 FROM portfolio_reestimates WHERE reestimate_id = ?", (reestimate_id,)
            ).fetchone()
            if request is None:
                raise KeyError(f"Unknown portfolio re-estimate: {reestimate_id}")
            existing = conn.execute(
                "SELECT choice, reason FROM portfolio_reestimate_decisions WHERE reestimate_id = ?",
                (reestimate_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["choice"]) == choice.value and str(existing["reason"]) == normalized_reason:
                    return self.get_reestimate(reestimate_id)
                raise ValueError("Portfolio re-estimate already has an immutable user decision")
            conn.execute(
                """INSERT INTO portfolio_reestimate_decisions(reestimate_id, choice, reason, decided_at)
                   VALUES(?,?,?,?)""",
                (reestimate_id, choice.value, normalized_reason, utc_now().isoformat()),
            )
        return self.get_reestimate(reestimate_id)

    def get_reestimate(self: PortfolioReestimateHost, reestimate_id: str) -> PortfolioReestimate:
        with self._lock:
            row = self._conn.execute(
                """SELECT request.*, decision.choice, decision.reason AS choice_reason,
                          decision.decided_at
                   FROM portfolio_reestimates AS request
                   LEFT JOIN portfolio_reestimate_decisions AS decision
                     ON decision.reestimate_id = request.reestimate_id
                   WHERE request.reestimate_id = ?""",
                (reestimate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown portfolio re-estimate: {reestimate_id}")
        return self._row(row)

    def reestimate_projection(self: PortfolioReestimateHost) -> tuple[PortfolioReestimate, ...]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT request.*, decision.choice, decision.reason AS choice_reason,
                          decision.decided_at
                   FROM portfolio_reestimates AS request
                   LEFT JOIN portfolio_reestimate_decisions AS decision
                     ON decision.reestimate_id = request.reestimate_id
                   ORDER BY request.created_at, request.reestimate_id"""
            ).fetchall()
        return tuple(self._row(row) for row in rows)


__all__ = ["PortfolioReestimateOperations", "initialize_portfolio_reestimates"]
