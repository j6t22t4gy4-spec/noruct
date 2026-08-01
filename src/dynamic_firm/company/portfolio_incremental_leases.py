"""Durable incremental Graph-mutation lease operations for a portfolio.

The canonical Work Order store remains the SQLite authority.  This mixin owns
only the bounded reservation, terminalization, and read-only projection of an
incremental lease; it neither creates a Graph patch nor replaces Kernel lease
validation.
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Mapping, Protocol

from dynamic_firm.kernel.models import GraphMutationLease
from dynamic_firm.runtime.models import utc_now

from .work_order_portfolio_models import (
    PortfolioIncrementalLease,
    PortfolioLeaseStatus,
    PortfolioPolicy,
    PortfolioStatus,
)


class PortfolioIncrementalLeaseHost(Protocol):
    """Minimal canonical-store contract required by the lease component."""

    _conn: sqlite3.Connection
    _lock: Any

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...

    @staticmethod
    def _incremental_lease(row: sqlite3.Row) -> PortfolioIncrementalLease: ...


class PortfolioIncrementalLeaseOperations:
    """Mixin for bounded incremental reservations under the existing store."""

    @staticmethod
    def _validate_lease_id(lease_id: str) -> str:
        normalized = lease_id.strip()
        if not normalized or len(normalized) > 160:
            raise ValueError("Portfolio incremental lease identity is invalid")
        return normalized

    @staticmethod
    def _bounded_incremental_totals(
        conn: sqlite3.Connection,
    ) -> GraphMutationLease:
        row = conn.execute(
            """SELECT COALESCE(SUM(model_calls), 0) AS model_calls,
                      COALESCE(SUM(tool_calls), 0) AS tool_calls,
                      COALESCE(SUM(cost_usd), 0) AS cost_usd
               FROM portfolio_incremental_leases WHERE status = 'RESERVED'"""
        ).fetchone()
        assert row is not None
        return GraphMutationLease(
            model_calls=int(row["model_calls"]),
            tool_calls=int(row["tool_calls"]),
            cost_usd=float(row["cost_usd"]),
        )

    @staticmethod
    def _within_incremental_policy(
        total: GraphMutationLease,
        candidate: GraphMutationLease,
        policy: PortfolioPolicy,
    ) -> bool:
        return (
            (
                policy.max_incremental_model_calls == 0
                or total.model_calls + candidate.model_calls
                <= policy.max_incremental_model_calls
            )
            and (
                policy.max_incremental_tool_calls == 0
                or total.tool_calls + candidate.tool_calls
                <= policy.max_incremental_tool_calls
            )
            and (
                policy.max_incremental_cost_usd == 0
                or total.cost_usd + candidate.cost_usd
                <= policy.max_incremental_cost_usd + 1e-12
            )
        )

    def reserve_incremental_lease(
        self: PortfolioIncrementalLeaseHost,
        work_order_id: str,
        *,
        job_id: str,
        lease_id: str,
        mutation_lease: GraphMutationLease,
        policy: PortfolioPolicy,
    ) -> PortfolioIncrementalLease:
        """Reserve one bounded future mutation delta across admitted local Jobs."""

        normalized_lease_id = self._validate_lease_id(lease_id)
        if not job_id.strip() or len(job_id) > 160:
            raise ValueError("Portfolio Job identity is invalid")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            entry = conn.execute(
                "SELECT job_id, status FROM portfolio_entries WHERE work_order_id = ?",
                (work_order_id,),
            ).fetchone()
            if (
                entry is None
                or str(entry["status"]) != PortfolioStatus.ADMITTED.value
                or str(entry["job_id"] or "") != job_id
            ):
                raise ValueError(
                    "Only the exact admitted and bound Job may reserve a portfolio lease"
                )
            existing = conn.execute(
                "SELECT * FROM portfolio_incremental_leases WHERE lease_id = ?",
                (normalized_lease_id,),
            ).fetchone()
            if existing is not None:
                result = self._incremental_lease(existing)
                if (
                    result.work_order_id != work_order_id
                    or result.job_id != job_id
                    or result.mutation_lease != mutation_lease
                ):
                    raise ValueError("Portfolio incremental lease identity conflicts")
                return result
            total = self._bounded_incremental_totals(conn)
            if not self._within_incremental_policy(total, mutation_lease, policy):
                raise ValueError("Portfolio incremental lease exceeds configured capacity")
            conn.execute(
                """INSERT INTO portfolio_incremental_leases(
                       lease_id, work_order_id, job_id, model_calls, tool_calls,
                       cost_usd, status, reason, created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,'RESERVED','PORTFOLIO_INCREMENTAL_LEASE_RESERVED',?,?)""",
                (
                    normalized_lease_id,
                    work_order_id,
                    job_id,
                    mutation_lease.model_calls,
                    mutation_lease.tool_calls,
                    mutation_lease.cost_usd,
                    now,
                    now,
                ),
            )
            result = conn.execute(
                "SELECT * FROM portfolio_incremental_leases WHERE lease_id = ?",
                (normalized_lease_id,),
            ).fetchone()
        assert result is not None
        return self._incremental_lease(result)

    def resolve_incremental_lease(
        self: PortfolioIncrementalLeaseHost,
        lease_id: str,
        *,
        status: PortfolioLeaseStatus,
        reason: str,
    ) -> PortfolioIncrementalLease:
        """Terminalize an explicit portfolio lease without inventing credits."""

        if status is PortfolioLeaseStatus.RESERVED:
            raise ValueError("Portfolio incremental lease resolution must be terminal")
        if not reason.strip() or len(reason) > 128:
            raise ValueError("Portfolio incremental lease resolution reason is invalid")
        normalized_lease_id = self._validate_lease_id(lease_id)
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM portfolio_incremental_leases WHERE lease_id = ?",
                (normalized_lease_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"Unknown portfolio incremental lease: {normalized_lease_id}")
            current = self._incremental_lease(existing)
            if current.status is not PortfolioLeaseStatus.RESERVED:
                if current.status is status and current.reason == reason:
                    return current
                raise ValueError("Portfolio incremental lease is already terminal")
            conn.execute(
                "UPDATE portfolio_incremental_leases SET status = ?, reason = ?, updated_at = ? WHERE lease_id = ?",
                (status.value, reason, utc_now().isoformat(), normalized_lease_id),
            )
            result = conn.execute(
                "SELECT * FROM portfolio_incremental_leases WHERE lease_id = ?",
                (normalized_lease_id,),
            ).fetchone()
        assert result is not None
        return self._incremental_lease(result)

    def incremental_lease_projection(
        self: PortfolioIncrementalLeaseHost,
    ) -> Mapping[str, object]:
        """Return content-free portfolio lease state for every operator surface."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM portfolio_incremental_leases ORDER BY created_at, lease_id"
            ).fetchall()
        leases = tuple(self._incremental_lease(row) for row in rows)
        reserved = GraphMutationLease(
            model_calls=sum(
                item.mutation_lease.model_calls
                for item in leases
                if item.status is PortfolioLeaseStatus.RESERVED
            ),
            tool_calls=sum(
                item.mutation_lease.tool_calls
                for item in leases
                if item.status is PortfolioLeaseStatus.RESERVED
            ),
            cost_usd=round(
                sum(
                    item.mutation_lease.cost_usd
                    for item in leases
                    if item.status is PortfolioLeaseStatus.RESERVED
                ),
                12,
            ),
        )
        return {
            "reserved": {
                "model_calls": reserved.model_calls,
                "tool_calls": reserved.tool_calls,
                "cost_usd": reserved.cost_usd,
            },
            "leases": tuple(
                {
                    "lease_id": item.lease_id,
                    "work_order_id": item.work_order_id,
                    "job_id": item.job_id,
                    "model_calls": item.mutation_lease.model_calls,
                    "tool_calls": item.mutation_lease.tool_calls,
                    "cost_usd": item.mutation_lease.cost_usd,
                    "status": item.status.value,
                    "reason": item.reason,
                    "updated_at": item.updated_at,
                }
                for item in leases
            ),
        }
