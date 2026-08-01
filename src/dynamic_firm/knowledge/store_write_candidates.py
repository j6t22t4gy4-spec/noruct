"""Explicit Knowledge write-candidate review lifecycle.

Candidates are not employee memory and are never auto-applied.  This mixin
uses KnowledgeStore's existing transaction/event/provenance authority for the
same PENDING → ACCEPTED|REJECTED lifecycle.
"""

from __future__ import annotations

import sqlite3
import uuid

from .epistemic import ContentTrustClass, EpistemicStatus
from .models import KnowledgeWriteCandidate
from .store_primitives import _now


class KnowledgeWriteCandidateMixin:
    @staticmethod
    def _write_candidate(row: sqlite3.Row) -> KnowledgeWriteCandidate:
        return KnowledgeWriteCandidate(
            candidate_id=str(row["candidate_id"]), job_id=str(row["job_id"]),
            kind=str(row["kind"]), statement=str(row["statement"]),
            evidence_pack_id=(str(row["evidence_pack_id"]) if row["evidence_pack_id"] else None),
            status=str(row["status"]), created_at=str(row["created_at"]),
            resolved_at=(str(row["resolved_at"]) if row["resolved_at"] else None),
            accepted_record_id=(str(row["accepted_record_id"]) if row["accepted_record_id"] else None),
        )

    def write_candidate(self, candidate_id: str) -> KnowledgeWriteCandidate | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_write_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        return None if row is None else self._write_candidate(row)

    def list_write_candidates(self, *, status: str | None = None, limit: int = 100) -> tuple[KnowledgeWriteCandidate, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Knowledge candidate list limit must be between 1 and 1000")
        query = "SELECT candidate_id FROM knowledge_write_candidates"
        parameters: list[object] = []
        if status:
            query += " WHERE status = ?"
            parameters.append(status.upper())
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(parameters)).fetchall()
        return tuple(
            value for row in rows if (value := self.write_candidate(str(row[0]))) is not None
        )

    def resolve_write_candidate(self, candidate_id: str, *, accept: bool) -> KnowledgeWriteCandidate:
        now = _now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_write_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Knowledge write candidate was not found: {candidate_id}")
            if row["status"] != "PENDING":
                expected = "ACCEPTED" if accept else "REJECTED"
                if str(row["status"]) == expected:
                    return self._write_candidate(row)
                raise ValueError("Knowledge write candidate is already resolved")
            status = "ACCEPTED" if accept else "REJECTED"
            accepted_record_id: str | None = None
            if accept:
                access_scope = "private"
                if row["evidence_pack_id"]:
                    pack = conn.execute(
                        "SELECT access_scope FROM evidence_packs WHERE pack_id = ?", (row["evidence_pack_id"],)
                    ).fetchone()
                    if pack is None:
                        raise ValueError("Knowledge write candidate Evidence Pack was not found")
                    access_scope = str(pack["access_scope"])
                accepted_record_id = f"record-{uuid.uuid4()}"
                conn.execute(
                    "INSERT INTO knowledge_records(record_id, kind, statement, status, confidence, source_span_json, revision, source_candidate_id, source_job_id, evidence_pack_id, access_scope, created_at, updated_at) VALUES (?, ?, ?, 'ACTIVE', 1.0, '{}', 1, ?, ?, ?, ?, ?, ?)",
                    (accepted_record_id, str(row["kind"]), str(row["statement"]), candidate_id, str(row["job_id"]), row["evidence_pack_id"], access_scope, now, now),
                )
                candidate_epistemic = conn.execute(
                    "SELECT * FROM knowledge_epistemic_annotations WHERE subject_type = 'WRITE_CANDIDATE' AND subject_id = ?", (candidate_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO knowledge_epistemic_annotations(subject_type, subject_id, epistemic_status, trust_class, freshness_expires_at, conflict_refs_json, unknown_refs_json, source_revision, created_at, updated_at) VALUES ('RECORD', ?, ?, ?, ?, ?, ?, '1', ?, ?)",
                    (
                        accepted_record_id,
                        str(candidate_epistemic["epistemic_status"]) if candidate_epistemic is not None else EpistemicStatus.INFERRED.value,
                        str(candidate_epistemic["trust_class"]) if candidate_epistemic is not None else ContentTrustClass.MODEL_GENERATED.value,
                        candidate_epistemic["freshness_expires_at"] if candidate_epistemic is not None else None,
                        str(candidate_epistemic["conflict_refs_json"]) if candidate_epistemic is not None else "[]",
                        str(candidate_epistemic["unknown_refs_json"]) if candidate_epistemic is not None else "[]",
                        now,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE knowledge_write_candidates SET status = ?, resolved_at = ?, accepted_record_id = ? WHERE candidate_id = ?",
                (status, now, accepted_record_id, candidate_id),
            )
            self._event(conn, f"WRITE_CANDIDATE_{status}", "candidate", candidate_id, {})
        value = self.write_candidate(candidate_id)
        assert value is not None
        return value
