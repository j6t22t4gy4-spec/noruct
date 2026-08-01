"""Read-only ACTIVE JOB projections for the canonical runtime SQLite store.

The methods intentionally operate on the owning store's private connection and
lock.  They are a read projection, not a second database or a cache: mutation,
migration and transaction authority remain in :mod:`dynamic_firm.runtime.store`.
"""

from __future__ import annotations

from typing import Any


class RunStoreReadProjectionMixin:
    """Read-only audit projections composed into :class:`RunStore`."""

    def get_job_ledger_rows(self, job_id: str) -> dict[str, Any] | None:
        """Return raw immutable audit rows for read-only validation and replay."""

        with self._lock:
            snapshot = self._conn.execute(
                "SELECT * FROM job_snapshots WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if snapshot is None:
                return None
            attempts = self._conn.execute(
                "SELECT * FROM job_attempts WHERE job_id = ? ORDER BY ledger_seq",
                (job_id,),
            ).fetchall()
            mutations = self._conn.execute(
                "SELECT * FROM job_mutations WHERE job_id = ? ORDER BY ledger_seq",
                (job_id,),
            ).fetchall()
            graph_patches = self._conn.execute(
                "SELECT * FROM job_graph_patches WHERE job_id = ? ORDER BY ledger_seq",
                (job_id,),
            ).fetchall()
            graph_proposals = self._conn.execute(
                "SELECT * FROM job_graph_proposals WHERE job_id = ? ORDER BY ledger_seq",
                (job_id,),
            ).fetchall()
            terminal = self._conn.execute(
                "SELECT * FROM job_terminal_events WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return {
            "snapshot": dict(snapshot),
            "attempts": tuple(dict(row) for row in attempts),
            "mutations": tuple(dict(row) for row in mutations),
            "graph_patches": tuple(dict(row) for row in graph_patches),
            "graph_proposals": tuple(dict(row) for row in graph_proposals),
            "terminal": None if terminal is None else dict(terminal),
        }

    def list_job_snapshot_rows(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("Job list limit must be between 1 and 1000")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM job_snapshots ORDER BY created_at DESC, job_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_job_table_payloads(self, job_id: str) -> tuple[str, ...]:
        """Return persisted JSON payloads for privacy regression checks only."""

        with self._lock:
            rows: list[str] = []
            snapshot = self._conn.execute(
                "SELECT payload_json FROM job_snapshots WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if snapshot is not None:
                rows.append(str(snapshot["payload_json"]))
            for table in (
                "job_attempts",
                "job_mutations",
                "job_graph_patches",
                "job_graph_proposals",
                "job_terminal_events",
            ):
                rows.extend(
                    str(row["payload_json"])
                    for row in self._conn.execute(
                        f"SELECT payload_json FROM {table} WHERE job_id = ?",
                        (job_id,),
                    ).fetchall()
                )
        return tuple(rows)
