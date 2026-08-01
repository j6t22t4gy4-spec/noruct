"""Knowledge Record read and explicit-deletion operations.

This is a component of the canonical KnowledgeStore: it shares the owner's
SQLite lock, transaction, provenance closure and vault sanitation boundary.
It deliberately creates neither a second knowledge database nor an employee
memory authority.
"""

from __future__ import annotations

from dynamic_firm.knowledge.models import KnowledgeRecord

from .store_primitives import _loads


class KnowledgeRecordProjectionMixin:
    """Read existing Records and delete a provenance closure on explicit request."""

    def record(self, record_id: str) -> KnowledgeRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_records WHERE record_id = ?", (record_id,)
            ).fetchone()
        if row is None:
            return None
        return KnowledgeRecord(
            record_id=str(row["record_id"]),
            kind=str(row["kind"]),
            statement=str(row["statement"]),
            status=str(row["status"]),
            confidence=float(row["confidence"]),
            source_asset_id=(str(row["source_asset_id"]) if row["source_asset_id"] else None),
            source_representation_id=(
                str(row["source_representation_id"])
                if row["source_representation_id"]
                else None
            ),
            source_span=_loads(row["source_span_json"], {}),
            revision=int(row["revision"]),
            supersedes_record_id=(
                str(row["supersedes_record_id"])
                if row["supersedes_record_id"]
                else None
            ),
            source_candidate_id=(
                str(row["source_candidate_id"])
                if row["source_candidate_id"]
                else None
            ),
            source_job_id=(str(row["source_job_id"]) if row["source_job_id"] else None),
            evidence_pack_id=(str(row["evidence_pack_id"]) if row["evidence_pack_id"] else None),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            access_scope=str(row["access_scope"]),
        )

    def list_records(
        self, *, limit: int = 100, include_superseded: bool = False
    ) -> tuple[KnowledgeRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Knowledge record list limit must be between 1 and 1000")
        query = "SELECT record_id FROM knowledge_records"
        if not include_superseded:
            query += " WHERE status = 'ACTIVE'"
        query += " ORDER BY updated_at DESC, record_id LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (limit,)).fetchall()
        return tuple(
            value for row in rows if (value := self.record(str(row[0]))) is not None
        )

    def forget_record(self, record_id: str) -> bool:
        with self._transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM knowledge_records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if exists is None:
                return False
            closure = self._provenance_closure(conn, record_ids={record_id})
            self._delete_provenance_closure(conn, closure)
        self._sanitize_deleted_content()
        return True
