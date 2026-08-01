"""Append-only receipts for explicit Knowledge candidate page publication."""

from __future__ import annotations

import hashlib
import sqlite3

from .page_contracts import (
    normalize_page_title,
    normalize_relative_page_path,
    page_payload_identity,
    render_candidate_markdown,
    verify_published_file,
)
from .page_models import KnowledgePagePublication
from .store_primitives import _now


class KnowledgePagePublicationMixin:
    @staticmethod
    def _page_publication(row: sqlite3.Row) -> KnowledgePagePublication:
        return KnowledgePagePublication(
            publication_id=str(row["publication_id"]),
            candidate_id=str(row["candidate_id"]),
            accepted_record_id=str(row["accepted_record_id"]),
            folder_id=str(row["folder_id"]),
            relative_path=str(row["relative_path"]),
            title=str(row["title"]),
            content_sha256=str(row["content_sha256"]),
            byte_size=int(row["byte_size"]),
            published_at=str(row["published_at"]),
        )

    def page_publication(
        self, candidate_id: str
    ) -> KnowledgePagePublication | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_page_publications WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return None if row is None else self._page_publication(row)

    def list_page_publications(
        self, *, folder_id: str | None = None, limit: int = 100
    ) -> tuple[KnowledgePagePublication, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Knowledge page list limit must be between 1 and 1000")
        query = "SELECT * FROM knowledge_page_publications"
        parameters: list[object] = []
        if folder_id is not None:
            query += " WHERE folder_id = ?"
            parameters.append(folder_id)
        query += " ORDER BY published_at DESC, publication_id LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(parameters)).fetchall()
        return tuple(self._page_publication(row) for row in rows)

    def record_page_publication(
        self,
        *,
        candidate_id: str,
        accepted_record_id: str,
        folder_id: str,
        relative_path: str,
        title: str,
        content_sha256: str,
        byte_size: int,
    ) -> KnowledgePagePublication:
        identity = "\0".join(
            (
                candidate_id,
                accepted_record_id,
                folder_id,
                relative_path,
                title,
                content_sha256,
                str(byte_size),
            )
        )
        publication_id = (
            "knowledge-page-"
            + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        )
        now = _now()
        with self._transaction() as conn:
            candidate = conn.execute(
                """
                SELECT candidate.status, candidate.accepted_record_id,
                       candidate.kind, candidate.statement, candidate.job_id,
                       candidate.evidence_pack_id, candidate.created_at,
                       candidate.resolved_at, record.source_candidate_id,
                       record.access_scope AS record_access_scope,
                       folder.status AS folder_status,
                       folder.access_scope AS folder_access_scope,
                       folder.root_path AS folder_root_path
                FROM knowledge_write_candidates AS candidate
                JOIN knowledge_records AS record ON record.record_id = ?
                JOIN knowledge_folders AS folder ON folder.folder_id = ?
                WHERE candidate.candidate_id = ?
                """,
                (accepted_record_id, folder_id, candidate_id),
            ).fetchone()
            if (
                candidate is None
                or str(candidate["status"]) != "ACCEPTED"
                or str(candidate["accepted_record_id"] or "")
                != accepted_record_id
                or str(candidate["source_candidate_id"] or "") != candidate_id
            ):
                raise ValueError(
                    "Knowledge page publication requires the exact accepted candidate"
                )
            if str(candidate["folder_status"]) != "ACTIVE":
                raise ValueError(
                    "Knowledge page publication requires an active Knowledge Folder"
                )
            if str(candidate["record_access_scope"]) != str(
                candidate["folder_access_scope"]
            ):
                raise ValueError(
                    "Knowledge page publication Folder must match the record scope"
                )
            normalized_path = normalize_relative_page_path(relative_path).as_posix()
            normalized_title = normalize_page_title(title)
            if normalized_path != relative_path or normalized_title != title:
                raise ValueError("Knowledge page publication identity is not normalized")
            markdown = render_candidate_markdown(
                title=normalized_title,
                accepted_date=str(
                    candidate["resolved_at"] or candidate["created_at"]
                ),
                knowledge_kind=str(candidate["kind"]),
                candidate_id=candidate_id,
                record_id=accepted_record_id,
                job_id=str(candidate["job_id"]),
                evidence_pack_id=(
                    None
                    if candidate["evidence_pack_id"] is None
                    else str(candidate["evidence_pack_id"])
                ),
                access_scope=str(candidate["record_access_scope"]),
                statement=str(candidate["statement"]),
            )
            expected_payload, expected_sha256, expected_size = page_payload_identity(
                markdown
            )
            if content_sha256 != expected_sha256 or byte_size != expected_size:
                raise ValueError(
                    "Knowledge page publication receipt does not match accepted content"
                )
            verify_published_file(
                root_path=str(candidate["folder_root_path"]),
                relative_path=relative_path,
                expected_payload=expected_payload,
            )
            existing = conn.execute(
                "SELECT * FROM knowledge_page_publications WHERE candidate_id = ? "
                "OR (folder_id = ? AND relative_path = ?)",
                (candidate_id, folder_id, relative_path),
            ).fetchone()
            expected = (
                publication_id,
                candidate_id,
                accepted_record_id,
                folder_id,
                relative_path,
                title,
                content_sha256,
                byte_size,
            )
            if existing is not None:
                observed = tuple(existing[key] for key in (
                    "publication_id",
                    "candidate_id",
                    "accepted_record_id",
                    "folder_id",
                    "relative_path",
                    "title",
                    "content_sha256",
                    "byte_size",
                ))
                if observed == expected:
                    return self._page_publication(existing)
                raise ValueError(
                    "Knowledge candidate or page path already has another publication"
                )
            conn.execute(
                """
                INSERT INTO knowledge_page_publications(
                    publication_id, candidate_id, accepted_record_id, folder_id,
                    relative_path, title, content_sha256, byte_size, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*expected, now),
            )
            self._event(
                conn,
                "KNOWLEDGE_PAGE_PUBLISHED",
                "candidate",
                candidate_id,
                {
                    "folder_id": folder_id,
                    "content_sha256": content_sha256,
                    "byte_size": byte_size,
                },
            )
            row = conn.execute(
                "SELECT * FROM knowledge_page_publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        assert row is not None
        return self._page_publication(row)
