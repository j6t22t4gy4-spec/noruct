"""Representation and folder retrieval projections for the canonical KnowledgeStore."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from typing import Mapping, Sequence

from .epistemic import ContentTrustClass, EpistemicStatus
from .folder_models import KnowledgeFolderEntryStatus
from .models import AssetStatus, DerivedRepresentation, KnowledgeAsset
from .store_primitives import (
    _bounded_mapping,
    _bounded_text,
    _json,
    _loads,
    _now,
)
from dynamic_firm.korean_lexical import korean_retrieval_variants


class KnowledgeRetrievalMixin:
    @staticmethod
    def _asset(row: sqlite3.Row) -> KnowledgeAsset:
        return KnowledgeAsset(
            asset_id=str(row["asset_id"]),
            content_hash=str(row["content_hash"]),
            original_name=str(row["original_name"]),
            title=str(row["title"]),
            media_type=str(row["media_type"]),
            byte_size=int(row["byte_size"]),
            vault_relative_path=str(row["vault_relative_path"]),
            origin=str(row["origin"]),
            access_scope=str(row["access_scope"]),
            status=AssetStatus(str(row["status"])),
            processor=str(row["processor"]),
            processor_version=str(row["processor_version"]),
            processing_error=str(row["processing_error"]),
            parent_asset_id=(
                str(row["parent_asset_id"]) if row["parent_asset_id"] is not None else None
            ),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            labels=tuple(str(item) for item in _loads(row["labels_json"], [])),
        )

    @staticmethod
    def _representation(row: sqlite3.Row) -> DerivedRepresentation:
        return DerivedRepresentation(
            representation_id=str(row["representation_id"]),
            asset_id=str(row["asset_id"]),
            kind=str(row["kind"]),
            media_type=str(row["media_type"]),
            content_hash=str(row["content_hash"]),
            byte_size=int(row["byte_size"]),
            vault_relative_path=str(row["vault_relative_path"]),
            processor=str(row["processor"]),
            processor_version=str(row["processor_version"]),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
        )


    def create_representation(
        self,
        *,
        asset_id: str,
        kind: str,
        media_type: str,
        content_hash: str,
        byte_size: int,
        vault_relative_path: str,
        processor: str,
        processor_version: str,
        chunks: Sequence[Mapping[str, object]],
    ) -> DerivedRepresentation:
        asset_id = _bounded_text(asset_id, "Knowledge Asset id", 256)
        kind = _bounded_text(kind, "Knowledge representation kind", 128)
        media_type = _bounded_text(media_type, "Knowledge representation media type", 256)
        content_hash = _bounded_text(
            content_hash, "Knowledge representation content hash", 64
        )
        if len(content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in content_hash
        ):
            raise ValueError("Knowledge representation content hash is invalid")
        if (
            not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 1
            or byte_size > 32 * 1024 * 1024
        ):
            raise ValueError("Knowledge representation byte size is invalid")
        vault_relative_path = _bounded_text(
            vault_relative_path, "Knowledge representation Vault path", 2048
        )
        processor = _bounded_text(processor, "Knowledge representation processor", 1024)
        processor_version = _bounded_text(
            processor_version,
            "Knowledge representation processor version",
            256,
            required=False,
        )
        if not chunks or len(chunks) > 100_000:
            raise ValueError("Knowledge representation chunk count is invalid")
        normalized_chunks: list[dict[str, object]] = []
        total_chunk_bytes = 0
        for item in chunks:
            if not isinstance(item, Mapping):
                raise ValueError("Knowledge representation chunk is invalid")
            try:
                content = _bounded_text(
                    item["content"], "Knowledge chunk content", 64_000
                )
                digest = _bounded_text(
                    item["content_hash"], "Knowledge chunk content hash", 64
                )
                raw_start = item["char_start"]
                raw_end = item["char_end"]
                if (
                    not isinstance(raw_start, int)
                    or isinstance(raw_start, bool)
                    or not isinstance(raw_end, int)
                    or isinstance(raw_end, bool)
                ):
                    raise ValueError("Knowledge representation chunk offsets are invalid")
                char_start = raw_start
                char_end = raw_end
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Knowledge representation chunk is invalid") from exc
            if (
                char_start < 0
                or char_end < char_start
                or char_end - char_start != len(content)
                or digest != hashlib.sha256(content.encode("utf-8")).hexdigest()
            ):
                raise ValueError("Knowledge representation chunk provenance is invalid")
            location = _bounded_mapping(
                item.get("location", {}), "Knowledge chunk location", 8192
            )
            total_chunk_bytes += len(content.encode("utf-8"))
            if total_chunk_bytes > byte_size:
                raise ValueError("Knowledge representation chunks exceed the source bytes")
            normalized_chunks.append(
                {
                    "content": content,
                    "content_hash": digest,
                    "char_start": char_start,
                    "char_end": char_end,
                    "location": location,
                }
            )
        representation_id = f"repr-{uuid.uuid4()}"
        now = _now()
        with self._transaction() as conn:
            asset = conn.execute(
                "SELECT asset_id, title, original_name FROM knowledge_assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if asset is None:
                raise ValueError(f"Knowledge Asset was not found: {asset_id}")
            row = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM knowledge_representations WHERE asset_id = ? AND kind = ?",
                (asset_id, kind),
            ).fetchone()
            revision = int(row[0])
            conn.execute(
                """
                INSERT INTO knowledge_representations(
                    representation_id, asset_id, kind, media_type, content_hash,
                    byte_size, vault_relative_path, processor, processor_version,
                    revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    representation_id,
                    asset_id,
                    kind,
                    media_type,
                    content_hash,
                    byte_size,
                    vault_relative_path,
                    processor,
                    processor_version,
                    revision,
                    now,
                ),
            )
            managed_fts5_enabled = self._managed_fts5_available_on(conn)
            managed_cjk_candidates_enabled = self._managed_cjk_candidates_available_on(conn)
            asset_title = str(asset["title"] or asset["original_name"])
            for ordinal, item in enumerate(normalized_chunks):
                chunk_id = f"chunk-{uuid.uuid4()}"
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks(
                        chunk_id, asset_id, representation_id, ordinal, content,
                        content_hash, char_start, char_end, location_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        asset_id,
                        representation_id,
                        ordinal,
                        str(item["content"]),
                        str(item["content_hash"]),
                        int(item["char_start"]),
                        int(item["char_end"]),
                        _json(dict(item.get("location", {}))),
                    ),
                )
                if managed_fts5_enabled:
                    conn.execute(
                        f"""
                        INSERT INTO {self._MANAGED_FTS5_TABLE}(
                            chunk_id, asset_id, representation_id, title, content
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            chunk_id,
                            asset_id,
                            representation_id,
                            asset_title,
                            str(item["content"]),
                        ),
                    )
                if managed_cjk_candidates_enabled:
                    cjk_values = [
                        (chunk_id, token)
                        for token in self._cjk_candidate_tokens(
                            f"{asset_title} {item['content']}"
                        )
                    ]
                    if cjk_values:
                        conn.executemany(
                            f"INSERT OR IGNORE INTO {self._MANAGED_CJK_CANDIDATES_TABLE}(chunk_id, token) "
                            "VALUES (?, ?)",
                            cjk_values,
                        )
            self._event(
                conn,
                "REPRESENTATION_CREATED",
                "representation",
                representation_id,
                {
                    "asset_id": asset_id,
                    "revision": revision,
                    "chunk_count": len(normalized_chunks),
                },
            )
        with self._lock:
            created = self._conn.execute(
                "SELECT * FROM knowledge_representations WHERE representation_id = ?",
                (representation_id,),
            ).fetchone()
        assert created is not None
        return self._representation(created)

    def retrieval_rows(
        self,
        *,
        access_scope: str = "private",
        limit: int = 2000,
        include_representation_chunks: bool = True,
        include_folder_entries: bool = True,
    ) -> tuple[dict[str, object], ...]:
        if limit < 1 or limit > 10_000:
            raise ValueError("Retrieval candidate limit must be between 1 and 10000")
        with self._lock:
            if include_representation_chunks:
                chunks = self._conn.execute(
                    """
                    SELECT c.*, a.title, a.original_name, a.revision AS asset_revision,
                           r.revision AS representation_revision,
                           r.created_at AS representation_created_at
                    FROM knowledge_chunks c
                    JOIN knowledge_assets a ON a.asset_id = c.asset_id
                    JOIN knowledge_representations r ON r.representation_id = c.representation_id
                    WHERE a.status = ? AND a.access_scope = ?
                      AND r.revision = (
                          SELECT MAX(latest.revision)
                          FROM knowledge_representations latest
                          WHERE latest.asset_id = r.asset_id AND latest.kind = r.kind
                      )
                    ORDER BY a.updated_at DESC, c.ordinal
                    LIMIT ?
                    """,
                    (AssetStatus.READY.value, access_scope, limit),
                ).fetchall()
            else:
                chunks = ()
            records = self._conn.execute(
                """
                SELECT record.*,
                       annotation.epistemic_status AS epistemic_status,
                       annotation.trust_class AS trust_class,
                       annotation.freshness_expires_at AS freshness_expires_at,
                       annotation.conflict_refs_json AS epistemic_conflict_refs_json,
                       annotation.unknown_refs_json AS epistemic_unknown_refs_json
                FROM knowledge_records record
                LEFT JOIN knowledge_epistemic_annotations annotation
                  ON annotation.subject_type = 'RECORD'
                 AND annotation.subject_id = record.record_id
                WHERE record.status = 'ACTIVE' AND record.access_scope = ?
                ORDER BY record.updated_at DESC, record.record_id LIMIT ?
                """,
                (access_scope, limit),
            ).fetchall()
            if include_folder_entries:
                folder_entries = self._conn.execute(
                    """
                    SELECT entry.*, folder.display_name, folder.access_scope,
                           folder.root_path
                    FROM knowledge_folder_entries entry
                    JOIN knowledge_folders folder ON folder.folder_id = entry.folder_id
                    WHERE folder.status = 'ACTIVE' AND folder.access_scope = ?
                      AND entry.index_status = ? AND entry.index_text != ''
                    ORDER BY entry.updated_at DESC, entry.relative_path
                    LIMIT ?
                    """,
                    (access_scope, KnowledgeFolderEntryStatus.READY.value, limit),
                ).fetchall()
            else:
                folder_entries = ()
        result: list[dict[str, object]] = []
        # A current raw Folder entry is the authority for its on-demand
        # snapshot.  Keeping both the entry and its derived snapshot chunk in
        # the pre-ranking candidate pool wastes the bounded candidate budget
        # and can hide unrelated raw files after repeated recalls.
        folder_snapshot_asset_ids = {
            str(row["snapshot_asset_id"])
            for row in folder_entries
            if row["snapshot_asset_id"] is not None
        }
        for row in chunks:
            if str(row["asset_id"]) in folder_snapshot_asset_ids:
                continue
            result.append(self._chunk_retrieval_row(row))
        chunk_end = len(result)
        for row in records:
            result.append(
                {
                    "source_type": "knowledge_record",
                    "source_id": str(row["record_id"]),
                    "asset_id": row["source_asset_id"],
                    "representation_id": row["source_representation_id"],
                    "title": f"{str(row['kind']).replace('_', ' ').title()} record",
                    "content": str(row["statement"]),
                    "content_hash": "",
                    "location": _loads(row["source_span_json"], {}),
                    "confidence": float(row["confidence"]),
                    "source_revision": str(row["revision"]),
                    "source_created_at": str(row["created_at"]),
                    "epistemic_status": str(
                        row["epistemic_status"] or EpistemicStatus.UNKNOWN.value
                    ),
                    "trust_class": str(
                        row["trust_class"] or ContentTrustClass.UNSPECIFIED.value
                    ),
                    "freshness_expires_at": (
                        str(row["freshness_expires_at"])
                        if row["freshness_expires_at"]
                        else None
                    ),
                    "conflict_refs": tuple(
                        str(item)
                        for item in _loads(row["epistemic_conflict_refs_json"], [])
                    ),
                    "unknown_refs": tuple(
                        str(item)
                        for item in _loads(row["epistemic_unknown_refs_json"], [])
                    ),
                }
            )
        record_end = len(result)
        result.extend(self._folder_retrieval_row(row) for row in folder_entries)
        groups = (
            result[:chunk_end],
            result[chunk_end:record_end],
            result[record_end:],
        )
        balanced: list[dict[str, object]] = []
        for ordinal in range(max((len(group) for group in groups), default=0)):
            for group in groups:
                if ordinal < len(group):
                    balanced.append(group[ordinal])
                    if len(balanced) == limit:
                        return tuple(balanced)
        return tuple(balanced)

    @staticmethod
    def _chunk_retrieval_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "source_type": "representation_chunk",
            "source_id": str(row["chunk_id"]),
            "asset_id": str(row["asset_id"]),
            "representation_id": str(row["representation_id"]),
            "title": str(row["title"] or row["original_name"]),
            "content": str(row["content"]),
            "content_hash": str(row["content_hash"]),
            "location": _loads(row["location_json"], {}),
            "confidence": 1.0,
            "source_revision": (
                f"asset-r{int(row['asset_revision'])}:"
                f"repr-r{int(row['representation_revision'])}"
            ),
            "source_created_at": str(row["representation_created_at"]),
            "epistemic_status": EpistemicStatus.OBSERVED.value,
            "trust_class": ContentTrustClass.UNTRUSTED_EXTERNAL.value,
            "freshness_expires_at": None,
            "conflict_refs": (),
            "unknown_refs": (),
        }

    @staticmethod
    def _folder_retrieval_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "source_type": "folder_file",
            "source_id": str(row["entry_id"]),
            "asset_id": row["snapshot_asset_id"],
            "representation_id": None,
            "folder_id": str(row["folder_id"]),
            "folder_root": str(row["root_path"]),
            "relative_path": str(row["relative_path"]),
            "title": str(row["relative_path"]),
            "content": str(row["index_text"]),
            "content_hash": str(row["content_hash"]),
            "location": {
                "folder_id": str(row["folder_id"]),
                "relative_path": str(row["relative_path"]),
            },
            "confidence": 1.0,
            "source_revision": (
                f"folder-entry-r{int(row['revision'])}:"
                f"{str(row['content_hash'])}"
            ),
            "source_created_at": str(row["created_at"]),
            "epistemic_status": EpistemicStatus.OBSERVED.value,
            "trust_class": ContentTrustClass.UNTRUSTED_EXTERNAL.value,
            "freshness_expires_at": None,
            "conflict_refs": (),
            "unknown_refs": (),
        }

    @staticmethod
    def _folder_fts5_match(query: str) -> str | None:
        """Return a safe FTS5 AND query, or preserve hybrid fallback semantics.

        Whitespace-delimited CJK terms map safely to unicode61 tokens and can
        use the same candidate projection as Latin terms.  One contiguous CJK
        run (for example ``가격전략``) is intentionally left to the hybrid
        bigram ranker: FTS5 cannot infer whether that run should match a raw
        folder's spaced words, compound noun, or morphological variant.
        """

        normalized = query.strip()
        if not normalized:
            return None
        cjk_runs = re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", normalized)
        if cjk_runs and (len(cjk_runs) == 1 or any(len(run) < 2 for run in cjk_runs)):
            return None
        terms = re.findall(r"[\w]+", normalized, flags=re.UNICODE)
        if not terms:
            return None
        # Each token is generated from the conservative character allowlist,
        # then quoted to keep FTS operators and column selectors inert.
        return " AND ".join(f'"{term}"' for term in terms[:12])

    @staticmethod
    def _compact_cjk_query_variants(query: str) -> tuple[str, ...]:
        """Return bounded contiguous CJK forms for local candidate narrowing.

        The original CJK run preserves existing compound behaviour.  Hangul
        variants such as ``가격전략을 → 가격전략`` and the conservative raw
        connector form ``가격전략의변경 → 가격전략변경`` are limited lexical
        recall aids, not morphological analysis or rewritten user queries.
        The normal hybrid ranker remains evidence-selection authority.
        """
        runs = re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", query)
        if len(runs) != 1 or len(runs[0]) < 3:
            return ()
        run = runs[0]
        variants = korean_retrieval_variants(run)
        return variants or (run,)

    @staticmethod
    def _compact_cjk_text(value: str) -> str:
        return "".join(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", value))

    def indexed_folder_retrieval_rows(
        self,
        *,
        access_scope: str,
        query: str,
        limit: int = 256,
    ) -> tuple[dict[str, object], ...] | None:
        """Use the optional Folder FTS5 projection for qualified queries.

        ``None`` means the caller must use the full hybrid candidate path:
        FTS5 is absent, the query requires CJK-aware ranking, or no eligible
        Folder exists.  An empty tuple is a valid indexed result: Folder
        authority exists but none of its files matched the safe query.
        """

        if limit < 1 or limit > 2_000:
            raise ValueError("Indexed Folder candidate limit must be between 1 and 2000")
        match = self._folder_fts5_match(query)
        compact_cjk_variants = self._compact_cjk_query_variants(query) if match is None else ()
        if match is None and not compact_cjk_variants:
            return None
        with self._lock:
            if match is not None and not self._folder_fts5_available_on(self._conn):
                return None
            eligible = self._conn.execute(
                """
                SELECT 1
                FROM knowledge_folder_entries entry
                JOIN knowledge_folders folder ON folder.folder_id = entry.folder_id
                WHERE folder.status = 'ACTIVE' AND folder.access_scope = ?
                  AND entry.index_status = ? AND entry.index_text != ''
                LIMIT 1
                """,
                (access_scope, KnowledgeFolderEntryStatus.READY.value),
            ).fetchone()
            if eligible is None:
                return None
            if compact_cjk_variants:
                # SQLite's unicode61 tokenizer cannot bridge CJK whitespace.
                # The separate local n-gram projection narrows only the
                # candidate rows; hybrid scoring and evidence selection keep
                # authority. Old/partial stores fail closed to the complete
                # bounded hybrid scan rather than guessing a linguistic match.
                # The shortest variant is the least restrictive safe index
                # probe. Exact compact checks below preserve correctness.
                compact_cjk = min(compact_cjk_variants, key=len)
                tokens = self._cjk_candidate_tokens(compact_cjk)
                if not tokens or not self._folder_cjk_candidates_available_on(self._conn):
                    return None
                placeholders = ", ".join("?" for _ in tokens)
                rows = self._conn.execute(
                    f"""
                    SELECT entry.*, folder.display_name, folder.access_scope, folder.root_path
                    FROM knowledge_folder_entries entry
                    JOIN knowledge_folders folder ON folder.folder_id = entry.folder_id
                    JOIN knowledge_folder_cjk_candidates candidate
                      ON candidate.entry_id = entry.entry_id
                    WHERE folder.status = 'ACTIVE' AND folder.access_scope = ?
                      AND entry.index_status = ? AND entry.index_text != ''
                      AND candidate.token IN ({placeholders})
                    GROUP BY entry.entry_id
                    HAVING COUNT(DISTINCT candidate.token) = ?
                    ORDER BY entry.updated_at DESC, entry.relative_path
                    LIMIT ?
                    """,
                    (
                        access_scope,
                        KnowledgeFolderEntryStatus.READY.value,
                        *tokens,
                        len(tokens),
                        limit,
                    ),
                ).fetchall()
                # Bigrams can collide across arbitrary text; keep the exact
                # compact-string check after the indexed narrowing.
                selected = [
                    row for row in rows
                    if any(variant in self._compact_cjk_text(
                        f"{row['relative_path']} {row['index_text']}"
                    ) for variant in compact_cjk_variants)
                ]
                return tuple(self._folder_retrieval_row(row) for row in selected)
            rows = self._conn.execute(
                """
                SELECT entry.*, folder.display_name, folder.access_scope, folder.root_path
                FROM knowledge_folder_fts
                JOIN knowledge_folder_entries entry
                  ON entry.entry_id = knowledge_folder_fts.entry_id
                JOIN knowledge_folders folder ON folder.folder_id = entry.folder_id
                WHERE knowledge_folder_fts MATCH ?
                  AND folder.status = 'ACTIVE' AND folder.access_scope = ?
                  AND entry.index_status = ? AND entry.index_text != ''
                ORDER BY bm25(knowledge_folder_fts, 0.0, 0.0, 4.0, 1.0), entry.relative_path
                LIMIT ?
                """,
                (match, access_scope, KnowledgeFolderEntryStatus.READY.value, limit),
            ).fetchall()
        return tuple(self._folder_retrieval_row(row) for row in rows)

    def indexed_representation_retrieval_rows(
        self,
        *,
        access_scope: str,
        query: str,
        limit: int = 512,
    ) -> tuple[dict[str, object], ...] | None:
        """Return qualified managed Asset chunks from optional local projections.

        ``None`` retains the complete hybrid candidate path.  An empty tuple
        means managed content exists in scope but none matched the sanitized
        query.  The table contains no state that is not already present in
        Asset/Representation/Chunk authority and is deleted with its Asset.
        """

        if limit < 1 or limit > 2_000:
            raise ValueError("Indexed representation candidate limit must be between 1 and 2000")
        match = self._folder_fts5_match(query)
        compact_cjk_variants = self._compact_cjk_query_variants(query) if match is None else ()
        if match is None and not compact_cjk_variants:
            return None
        with self._lock:
            if match is not None and not self._managed_fts5_available_on(self._conn):
                return None
            eligible = self._conn.execute(
                """
                SELECT 1
                FROM knowledge_chunks chunk
                JOIN knowledge_assets asset ON asset.asset_id = chunk.asset_id
                JOIN knowledge_representations representation
                  ON representation.representation_id = chunk.representation_id
                WHERE asset.status = ? AND asset.access_scope = ?
                  AND representation.revision = (
                      SELECT MAX(latest.revision)
                      FROM knowledge_representations latest
                      WHERE latest.asset_id = representation.asset_id
                        AND latest.kind = representation.kind
                  )
                LIMIT 1
                """,
                (AssetStatus.READY.value, access_scope),
            ).fetchone()
            if eligible is None:
                return None
            if compact_cjk_variants:
                compact_cjk = min(compact_cjk_variants, key=len)
                tokens = self._cjk_candidate_tokens(compact_cjk)
                if not tokens or not self._managed_cjk_candidates_available_on(self._conn):
                    return None
                placeholders = ", ".join("?" for _ in tokens)
                rows = self._conn.execute(
                    f"""
                    SELECT chunk.*, asset.title, asset.original_name,
                           asset.revision AS asset_revision,
                           representation.revision AS representation_revision,
                           representation.created_at AS representation_created_at
                    FROM knowledge_chunks chunk
                    JOIN knowledge_chunk_cjk_candidates candidate
                      ON candidate.chunk_id = chunk.chunk_id
                    JOIN knowledge_assets asset ON asset.asset_id = chunk.asset_id
                    JOIN knowledge_representations representation
                      ON representation.representation_id = chunk.representation_id
                    WHERE asset.status = ? AND asset.access_scope = ?
                      AND candidate.token IN ({placeholders})
                      AND representation.revision = (
                          SELECT MAX(latest.revision)
                          FROM knowledge_representations latest
                          WHERE latest.asset_id = representation.asset_id
                            AND latest.kind = representation.kind
                      )
                    GROUP BY chunk.chunk_id
                    HAVING COUNT(DISTINCT candidate.token) = ?
                    ORDER BY asset.updated_at DESC, chunk.ordinal
                    LIMIT ?
                    """,
                    (
                        AssetStatus.READY.value,
                        access_scope,
                        *tokens,
                        len(tokens),
                        limit,
                    ),
                ).fetchall()
                selected = [
                    row for row in rows
                    if any(
                        variant in self._compact_cjk_text(
                            f"{row['title'] or row['original_name']} {row['content']}"
                        )
                        for variant in compact_cjk_variants
                    )
                ]
                return tuple(self._chunk_retrieval_row(row) for row in selected)
            rows = self._conn.execute(
                """
                SELECT chunk.*, asset.title, asset.original_name,
                       asset.revision AS asset_revision,
                       representation.revision AS representation_revision,
                       representation.created_at AS representation_created_at
                FROM knowledge_chunk_fts
                JOIN knowledge_chunks chunk
                  ON chunk.chunk_id = knowledge_chunk_fts.chunk_id
                JOIN knowledge_assets asset ON asset.asset_id = chunk.asset_id
                JOIN knowledge_representations representation
                  ON representation.representation_id = chunk.representation_id
                WHERE knowledge_chunk_fts MATCH ?
                  AND asset.status = ? AND asset.access_scope = ?
                  AND representation.revision = (
                      SELECT MAX(latest.revision)
                      FROM knowledge_representations latest
                      WHERE latest.asset_id = representation.asset_id
                        AND latest.kind = representation.kind
                  )
                ORDER BY bm25(knowledge_chunk_fts, 0.0, 0.0, 0.0, 3.0, 1.0),
                         asset.updated_at DESC, chunk.ordinal
                LIMIT ?
                """,
                (match, AssetStatus.READY.value, access_scope, limit),
            ).fetchall()
        return tuple(self._chunk_retrieval_row(row) for row in rows)

    def current_folder_snapshot_asset_ids(self, *, access_scope: str) -> frozenset[str]:
        """Return snapshot Asset ids superseded by live raw Folder authority."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT entry.snapshot_asset_id
                FROM knowledge_folder_entries entry
                JOIN knowledge_folders folder ON folder.folder_id = entry.folder_id
                WHERE folder.status = 'ACTIVE' AND folder.access_scope = ?
                  AND entry.index_status = ? AND entry.snapshot_asset_id IS NOT NULL
                """,
                (access_scope, KnowledgeFolderEntryStatus.READY.value),
            ).fetchall()
        return frozenset(str(row["snapshot_asset_id"]) for row in rows)

    def retrieval_candidate_count(self, *, access_scope: str, limit: int = 10_000) -> int:
        """Report the bounded logical corpus size without materializing bodies.

        Indexed candidate projections change the rows inspected by the ranker,
        not the Evidence Pack's disclosure of how many eligible sources existed
        at selection time.
        """

        if limit < 1 or limit > 10_000:
            raise ValueError("Retrieval candidate limit must be between 1 and 10000")
        with self._lock:
            rows = self._conn.execute(
                """
                WITH live_folder_snapshots AS (
                    SELECT entry.snapshot_asset_id AS asset_id
                    FROM knowledge_folder_entries entry
                    JOIN knowledge_folders folder ON folder.folder_id = entry.folder_id
                    WHERE folder.status = 'ACTIVE' AND folder.access_scope = ?
                      AND entry.index_status = ? AND entry.snapshot_asset_id IS NOT NULL
                ), current_chunks AS (
                    SELECT COUNT(*) AS value
                    FROM knowledge_chunks chunk
                    JOIN knowledge_assets asset ON asset.asset_id = chunk.asset_id
                    JOIN knowledge_representations representation
                      ON representation.representation_id = chunk.representation_id
                    WHERE asset.status = ? AND asset.access_scope = ?
                      AND representation.revision = (
                          SELECT MAX(latest.revision)
                          FROM knowledge_representations latest
                          WHERE latest.asset_id = representation.asset_id
                            AND latest.kind = representation.kind
                      )
                      AND chunk.asset_id NOT IN (SELECT asset_id FROM live_folder_snapshots)
                ), active_records AS (
                    SELECT COUNT(*) AS value
                    FROM knowledge_records
                    WHERE status = 'ACTIVE' AND access_scope = ?
                ), live_folder_entries AS (
                    SELECT COUNT(*) AS value
                    FROM knowledge_folder_entries entry
                    JOIN knowledge_folders folder ON folder.folder_id = entry.folder_id
                    WHERE folder.status = 'ACTIVE' AND folder.access_scope = ?
                      AND entry.index_status = ? AND entry.index_text != ''
                )
                SELECT (SELECT value FROM current_chunks)
                     + (SELECT value FROM active_records)
                     + (SELECT value FROM live_folder_entries) AS value
                """,
                (
                    access_scope,
                    KnowledgeFolderEntryStatus.READY.value,
                    AssetStatus.READY.value,
                    access_scope,
                    access_scope,
                    access_scope,
                    KnowledgeFolderEntryStatus.READY.value,
                ),
            ).fetchone()
        return min(limit, int(rows["value"] if rows is not None else 0))

