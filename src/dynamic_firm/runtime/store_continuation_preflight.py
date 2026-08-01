"""Append-only, content-free refusal receipts for same-Job continuation."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .models import utc_now


_CONTINUATION_KINDS = frozenset({"READ_ONLY_PARTIAL", "GRAPH_PROPOSAL"})
_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,95}\Z")


def _receipt_id(*, job_id: str, continuation_kind: str, code: str) -> str:
    payload = json.dumps(
        {"job_id": job_id, "continuation_kind": continuation_kind, "code": code},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"continuation-preflight:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class RunStoreContinuationPreflightMixin:
    """Record only a stable refusal class; never error text or capability data."""

    def _initialize_continuation_preflight_schema(self, conn: Any) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS job_continuation_preflight_receipts (
                receipt_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES job_snapshots(job_id),
                continuation_kind TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                CHECK(continuation_kind IN ('READ_ONLY_PARTIAL','GRAPH_PROPOSAL')),
                CHECK(length(code) BETWEEN 3 AND 96)
            );
            CREATE INDEX IF NOT EXISTS job_continuation_preflight_receipts_job_idx
                ON job_continuation_preflight_receipts(job_id, created_at);
            CREATE TRIGGER IF NOT EXISTS job_continuation_preflight_receipts_no_update
            BEFORE UPDATE ON job_continuation_preflight_receipts BEGIN
                SELECT RAISE(ABORT, 'job_continuation_preflight_receipts are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS job_continuation_preflight_receipts_no_delete
            BEFORE DELETE ON job_continuation_preflight_receipts BEGIN
                SELECT RAISE(ABORT, 'job_continuation_preflight_receipts are append-only');
            END;
            """
        )

    def append_job_continuation_preflight_refusal(
        self,
        *,
        job_id: str,
        continuation_kind: str,
        code: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(job_id, str)
            or not job_id.strip()
            or len(job_id) > 192
            or continuation_kind not in _CONTINUATION_KINDS
            or not isinstance(code, str)
            or not _CODE.fullmatch(code)
        ):
            raise ValueError("Continuation preflight refusal receipt is invalid")
        receipt_id = _receipt_id(
            job_id=job_id,
            continuation_kind=continuation_kind,
            code=code,
        )
        now = utc_now().isoformat()
        with self._transaction() as conn:
            self._job_snapshot_row(conn, job_id)
            existing = conn.execute(
                "SELECT * FROM job_continuation_preflight_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            conn.execute(
                """
                INSERT INTO job_continuation_preflight_receipts(
                    receipt_id, job_id, continuation_kind, code, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (receipt_id, job_id, continuation_kind, code, now),
            )
            row = conn.execute(
                "SELECT * FROM job_continuation_preflight_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_job_continuation_preflight_receipts(
        self,
        job_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Read opaque continuation refusals for audit projections only."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT receipt_id, job_id, continuation_kind, code, created_at
                FROM job_continuation_preflight_receipts
                WHERE job_id = ?
                ORDER BY created_at, receipt_id
                """,
                (job_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)
