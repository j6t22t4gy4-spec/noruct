"""Claim and activation receipts for a paused ACTIVE JOB graph proposal.

This mixin consumes the owning RunStore's single SQLite connection and
transaction boundary. It does not dispatch work or create another Job state
authority.
"""

from __future__ import annotations

import json
from typing import Any

from .models import utc_now


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


class RunStoreGraphProposalContinuationMixin:
    """Exact pending/claimed/resumed graph-proposal continuation lifecycle."""

    def authorize_graph_proposal_continuation(
        self,
        *,
        job_id: str,
        proposal_id: str,
        request_snapshot_hash: str,
        before_graph_digest: str,
        after_graph_digest: str,
    ) -> dict[str, Any]:
        """Create one non-dispatchable exact continuation receipt.

        This may be written only while the Job is paused on the matching
        pending proposal.  A later claimant must still prove an APPROVED
        terminal receipt before it can transition lifecycle state or execute.
        """

        if (
            not proposal_id
            or any(len(value) != 64 for value in (
                request_snapshot_hash, before_graph_digest, after_graph_digest
            ))
        ):
            raise ValueError("Graph proposal continuation identity is invalid")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            snapshot = self._job_snapshot_row(conn, job_id)
            if str(snapshot["frozen_snapshot_hash"]) != request_snapshot_hash:
                raise ValueError("Graph proposal continuation request mismatch")
            lifecycle = conn.execute(
                "SELECT state FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lifecycle is None or str(lifecycle["state"]) != "PAUSED":
                raise ValueError("Graph proposal continuation requires a paused Job")
            pending = conn.execute(
                "SELECT payload_json FROM job_graph_proposals "
                "WHERE job_id = ? AND proposal_id = ? AND status = 'PENDING'",
                (job_id, proposal_id),
            ).fetchone()
            if pending is None:
                raise ValueError("Graph proposal continuation has no pending candidate")
            payload = _loads(str(pending["payload_json"]), {})
            if (
                payload.get("before_graph_digest") != before_graph_digest
                or payload.get("after_graph_digest") != after_graph_digest
            ):
                raise ValueError("Graph proposal continuation graph identity mismatch")
            existing = conn.execute(
                "SELECT * FROM graph_proposal_continuations WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchone()
            if existing is not None:
                identity = (
                    str(existing["request_snapshot_hash"]), str(existing["before_graph_digest"]),
                    str(existing["after_graph_digest"]),
                )
                if identity != (request_snapshot_hash, before_graph_digest, after_graph_digest):
                    raise ValueError("Graph proposal continuation identity conflicts")
                return dict(existing)
            conn.execute(
                """
                INSERT INTO graph_proposal_continuations(
                    job_id, proposal_id, request_snapshot_hash, before_graph_digest,
                    after_graph_digest, status, created_at, claimed_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, NULL)
                """,
                (job_id, proposal_id, request_snapshot_hash, before_graph_digest, after_graph_digest, now),
            )
            row = conn.execute(
                "SELECT * FROM graph_proposal_continuations WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchone()
        assert row is not None
        return dict(row)

    def job_frozen_snapshot_hash(self, job_id: str) -> str:
        """Return the immutable request binding needed by continuation services."""

        with self._lock:
            row = self._conn.execute(
                "SELECT frozen_snapshot_hash FROM job_snapshots WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"ACTIVE JOB snapshot does not exist: {job_id}")
        return str(row["frozen_snapshot_hash"])

    def claim_approved_graph_proposal_continuation(
        self,
        *,
        job_id: str,
        proposal_id: str,
        request_snapshot_hash: str,
        before_graph_digest: str,
        after_graph_digest: str,
    ) -> dict[str, Any]:
        """Claim only an exactly approved paused proposal, without dispatch.

        A claim deliberately leaves the lifecycle ``PAUSED``.  The mutation
        lease and append-only Graph patch must be durable before a separate
        idempotent activation can expose ``ADMITTED`` to a worker.  This gives
        a crash/retry a stable claimed receipt rather than an admitted Job
        without its graph rewrite.
        """

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM graph_proposal_continuations WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchone()
            if row is None:
                raise ValueError("Graph proposal continuation was not authorized")
            if (
                str(row["request_snapshot_hash"]) != request_snapshot_hash
                or str(row["before_graph_digest"]) != before_graph_digest
                or str(row["after_graph_digest"]) != after_graph_digest
            ):
                raise ValueError("Graph proposal continuation claim identity mismatch")
            claimed = str(row["status"]) == "CLAIMED"
            if str(row["status"]) not in {"PENDING", "CLAIMED"}:
                raise ValueError("Graph proposal continuation state is invalid")
            decisions = conn.execute(
                "SELECT status FROM job_graph_proposals WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchall()
            states = {str(item["status"]) for item in decisions}
            if states != {"PENDING", "APPROVED"}:
                raise ValueError("Graph proposal continuation requires one exact approval")
            lifecycle = conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lifecycle is None or str(lifecycle["state"]) not in {"PAUSED", "ADMITTED"}:
                raise ValueError("Graph proposal continuation requires a paused or admitted Job")
            if claimed:
                if str(lifecycle["state"]) not in {"PAUSED", "ADMITTED"}:
                    raise ValueError("Claimed Graph proposal lifecycle is invalid")
                return dict(row)
            if str(lifecycle["state"]) != "PAUSED":
                raise ValueError("New Graph proposal continuation requires a paused Job")
            now = utc_now().isoformat()
            conn.execute(
                "UPDATE graph_proposal_continuations SET status = 'CLAIMED', claimed_at = ? "
                "WHERE job_id = ? AND proposal_id = ? AND status = 'PENDING'",
                (now, job_id, proposal_id),
            )
            claimed = conn.execute(
                "SELECT * FROM graph_proposal_continuations WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchone()
        assert claimed is not None
        return dict(claimed)

    def activate_claimed_graph_proposal_continuation(
        self,
        *,
        job_id: str,
        proposal_id: str,
        graph_patch_event_id: str,
    ) -> dict[str, Any]:
        """Expose a claimed continuation only after its exact Graph patch exists."""

        with self._transaction() as conn:
            continuation = conn.execute(
                "SELECT * FROM graph_proposal_continuations WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchone()
            if continuation is None or str(continuation["status"]) != "CLAIMED":
                raise ValueError("Graph proposal continuation is not claimed")
            patch = conn.execute(
                "SELECT payload_json FROM job_graph_patches WHERE job_id = ? AND event_id = ?",
                (job_id, graph_patch_event_id),
            ).fetchone()
            if patch is None:
                raise ValueError("Graph proposal continuation patch is not durable")
            patch_payload = _loads(str(patch["payload_json"]), {})
            if (
                patch_payload.get("before_graph_digest") != continuation["before_graph_digest"]
                or patch_payload.get("after_graph_digest") != continuation["after_graph_digest"]
            ):
                raise ValueError("Graph proposal continuation patch identity mismatch")
            lifecycle = conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lifecycle is None:
                raise ValueError("Graph proposal continuation lifecycle is missing")
            if str(lifecycle["state"]) == "ADMITTED":
                return dict(lifecycle)
            if str(lifecycle["state"]) != "PAUSED":
                raise ValueError("Graph proposal continuation lifecycle is not paused")
            now = utc_now().isoformat()
            revision = int(lifecycle["revision"]) + 1
            reason = f"GRAPH_PROPOSAL_ACTIVATED:{proposal_id[:48]}"
            conn.execute(
                "UPDATE job_lifecycle_state SET state = 'ADMITTED', revision = ?, reason = ?, updated_at = ? WHERE job_id = ?",
                (revision, reason, now, job_id),
            )
            conn.execute(
                "INSERT INTO job_lifecycle_events(event_id, job_id, sequence, operation, from_state, to_state, reason, created_at) "
                "VALUES (?, ?, ?, 'RESUME', 'PAUSED', 'ADMITTED', ?, ?)",
                (f"lifecycle:{job_id}:{revision}", job_id, revision, reason, now),
            )
            row = conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def claim_rejected_graph_proposal_continuation(
        self,
        *,
        job_id: str,
        proposal_id: str,
        request_snapshot_hash: str,
        before_graph_digest: str,
        after_graph_digest: str,
    ) -> dict[str, Any]:
        """Claim one rejected proposal before the unchanged Graph can resume.

        This shares the same opaque continuation row as approval.  ``CLAIMED``
        means only that exactly one device owns the decision continuation; it
        never implies the proposed patch was accepted.
        """

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM graph_proposal_continuations WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchone()
            if row is None:
                raise ValueError("Graph proposal continuation was not authorized")
            if (
                str(row["request_snapshot_hash"]) != request_snapshot_hash
                or str(row["before_graph_digest"]) != before_graph_digest
                or str(row["after_graph_digest"]) != after_graph_digest
            ):
                raise ValueError("Graph proposal rejection claim identity mismatch")
            if str(row["status"]) not in {"PENDING", "CLAIMED"}:
                raise ValueError("Graph proposal continuation state is invalid")
            decisions = conn.execute(
                "SELECT status FROM job_graph_proposals WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchall()
            if {str(item["status"]) for item in decisions} != {"PENDING", "REJECTED"}:
                raise ValueError("Graph proposal continuation requires one exact rejection")
            lifecycle = conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lifecycle is None or str(lifecycle["state"]) not in {"PAUSED", "ADMITTED"}:
                raise ValueError("Graph proposal continuation requires a paused or admitted Job")
            if str(row["status"]) == "CLAIMED":
                return dict(row)
            if str(lifecycle["state"]) != "PAUSED":
                raise ValueError("New Graph rejection continuation requires a paused Job")
            now = utc_now().isoformat()
            conn.execute(
                "UPDATE graph_proposal_continuations SET status = 'CLAIMED', claimed_at = ? "
                "WHERE job_id = ? AND proposal_id = ? AND status = 'PENDING'",
                (now, job_id, proposal_id),
            )
            claimed = conn.execute(
                "SELECT * FROM graph_proposal_continuations WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchone()
        assert claimed is not None
        return dict(claimed)

    def activate_claimed_graph_proposal_rejection(
        self,
        *,
        job_id: str,
        proposal_id: str,
        before_graph_digest: str,
        after_graph_digest: str,
    ) -> dict[str, Any]:
        """Resume the original topology after a claimed rejected proposal."""

        with self._transaction() as conn:
            continuation = conn.execute(
                "SELECT * FROM graph_proposal_continuations WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchone()
            if continuation is None or str(continuation["status"]) != "CLAIMED":
                raise ValueError("Graph proposal rejection is not claimed")
            if (
                str(continuation["before_graph_digest"]) != before_graph_digest
                or str(continuation["after_graph_digest"]) != after_graph_digest
            ):
                raise ValueError("Graph proposal rejection identity mismatch")
            decisions = conn.execute(
                "SELECT status FROM job_graph_proposals WHERE job_id = ? AND proposal_id = ?",
                (job_id, proposal_id),
            ).fetchall()
            if {str(item["status"]) for item in decisions} != {"PENDING", "REJECTED"}:
                raise ValueError("Graph proposal rejection requires one exact rejection")
            lifecycle = conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
            if lifecycle is None:
                raise ValueError("Graph proposal rejection lifecycle is missing")
            if str(lifecycle["state"]) == "ADMITTED":
                return dict(lifecycle)
            if str(lifecycle["state"]) != "PAUSED":
                raise ValueError("Graph proposal rejection lifecycle is not paused")
            now = utc_now().isoformat()
            revision = int(lifecycle["revision"]) + 1
            reason = f"GRAPH_PROPOSAL_REJECTED:{proposal_id[:48]}"
            conn.execute(
                "UPDATE job_lifecycle_state SET state = 'ADMITTED', revision = ?, reason = ?, updated_at = ? WHERE job_id = ?",
                (revision, reason, now, job_id),
            )
            conn.execute(
                "INSERT INTO job_lifecycle_events(event_id, job_id, sequence, operation, from_state, to_state, reason, created_at) "
                "VALUES (?, ?, ?, 'RESUME', 'PAUSED', 'ADMITTED', ?, ?)",
                (f"lifecycle:{job_id}:{revision}", job_id, revision, reason, now),
            )
            activated = conn.execute(
                "SELECT * FROM job_lifecycle_state WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert activated is not None
        return dict(activated)


