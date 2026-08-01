"""Asset lifecycle mutation component for the canonical Knowledge Store.

This mixin deliberately shares the owning ``KnowledgeStore`` connection,
locking, transaction, event log, and read projections.  It introduces neither
a second Knowledge database nor an independent Vault authority.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Mapping, Sequence

from .models import AssetStatus, KnowledgeAsset
from .store_primitives import _bounded_text, _json, _normalized_scope, _now, _truncated_text


class KnowledgeAssetMutationMixin:
    """Bounded asset registration, processing, and Vault-reference operations."""

    def record_remote_asset_source(
        self,
        asset_id: str,
        *,
        source_url: str,
        etag: str | None,
        last_modified: str | None,
        content_fetched: bool,
    ) -> Mapping[str, object]:
        """Persist only public cache validators for explicit one-shot refresh."""

        source_url = _bounded_text(source_url, "Knowledge remote source URL", 2_048)
        for label, value in (("Knowledge remote ETag", etag), ("Knowledge remote Last-Modified", last_modified)):
            if value is not None:
                _bounded_text(value, label, 512)
                if "\r" in value or "\n" in value:
                    raise ValueError(f"{label} is invalid")
        now = _now()
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM knowledge_assets WHERE asset_id = ?", (asset_id,)
            ).fetchone() is None:
                raise ValueError(f"Knowledge Asset was not found: {asset_id}")
            existing = conn.execute(
                "SELECT fetched_at FROM knowledge_remote_asset_sources WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            fetched_at = now if content_fetched or existing is None else str(existing["fetched_at"])
            conn.execute(
                """
                INSERT INTO knowledge_remote_asset_sources(
                    asset_id, source_url, response_etag, response_last_modified, fetched_at, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    source_url = excluded.source_url,
                    response_etag = COALESCE(excluded.response_etag, knowledge_remote_asset_sources.response_etag),
                    response_last_modified = COALESCE(excluded.response_last_modified, knowledge_remote_asset_sources.response_last_modified),
                    fetched_at = excluded.fetched_at,
                    checked_at = excluded.checked_at
                """,
                (asset_id, source_url, etag, last_modified, fetched_at, now),
            )
            self._event(
                conn,
                "REMOTE_ASSET_REFRESH_CHECKED",
                "asset",
                asset_id,
                {"content_fetched": content_fetched},
            )
        value = self.remote_asset_source(asset_id)
        assert value is not None
        return value

    def create_asset(
        self,
        *,
        content_hash: str,
        original_name: str,
        title: str,
        media_type: str,
        byte_size: int,
        vault_relative_path: str,
        origin: str,
        access_scope: str,
        labels: Sequence[str] = (),
        parent_asset_id: str | None = None,
    ) -> tuple[KnowledgeAsset, bool]:
        content_hash = _bounded_text(content_hash, "Knowledge Asset content hash", 64)
        if len(content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in content_hash
        ):
            raise ValueError("Knowledge Asset content hash is invalid")
        original_name = _bounded_text(original_name, "Knowledge Asset original name", 1024)
        title = _bounded_text(title, "Knowledge Asset title", 4096)
        media_type = _bounded_text(media_type, "Knowledge Asset media type", 256)
        vault_relative_path = _bounded_text(
            vault_relative_path, "Knowledge Asset Vault path", 2048
        )
        origin = _bounded_text(origin, "Knowledge Asset origin", 1024)
        access_scope = _normalized_scope(access_scope)
        if (
            not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 0
            or byte_size > 256 * 1024 * 1024
        ):
            raise ValueError("Knowledge Asset byte size is invalid")
        if len(labels) > 64 or any(
            not isinstance(label, str)
            or not label.strip()
            or len(label.strip().encode("utf-8")) > 256
            for label in labels
        ):
            raise ValueError("Knowledge Asset labels exceed their bounded contract")
        labels = tuple(label.strip() for label in labels)
        existing = self.asset_by_hash(content_hash, access_scope)
        if existing is not None:
            return existing, True
        asset_id = f"asset-{uuid.uuid4()}"
        now = _now()
        with self._transaction() as conn:
            try:
                revision = 1
                if parent_asset_id is not None:
                    parent = conn.execute(
                        "SELECT revision, access_scope FROM knowledge_assets WHERE asset_id = ?",
                        (parent_asset_id,),
                    ).fetchone()
                    if parent is None:
                        raise ValueError(f"Parent Knowledge Asset was not found: {parent_asset_id}")
                    if str(parent["access_scope"]) != access_scope:
                        raise ValueError("Parent and child Knowledge Assets must use the same scope")
                    revision = int(parent["revision"]) + 1
                conn.execute(
                    """
                    INSERT INTO knowledge_assets(
                        asset_id, content_hash, original_name, title, media_type, byte_size,
                        vault_relative_path, origin, access_scope, status, parent_asset_id,
                        revision, labels_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        content_hash,
                        original_name,
                        title,
                        media_type,
                        byte_size,
                        vault_relative_path,
                        origin,
                        access_scope,
                        AssetStatus.STORED.value,
                        parent_asset_id,
                        revision,
                        _json(list(labels)),
                        now,
                        now,
                    ),
                )
                self._event(conn, "ASSET_STORED", "asset", asset_id, {"byte_size": byte_size})
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT * FROM knowledge_assets WHERE content_hash = ? AND access_scope = ?",
                    (content_hash, access_scope),
                ).fetchone()
                if row is None:
                    raise
                return self._asset(row), True
        created = self.asset(asset_id)
        assert created is not None
        return created, False

    def set_asset_processing(
        self,
        asset_id: str,
        *,
        status: AssetStatus,
        processor: str = "",
        processor_version: str = "",
        error: str = "",
    ) -> KnowledgeAsset:
        processor = _bounded_text(
            processor, "Knowledge processor", 1024, required=False
        )
        processor_version = _bounded_text(
            processor_version,
            "Knowledge processor version",
            256,
            required=False,
        )
        error = _truncated_text(error, "Knowledge processing error", 2_000)
        now = _now()
        with self._transaction() as conn:
            changed = conn.execute(
                """
                UPDATE knowledge_assets
                SET status = ?, processor = ?, processor_version = ?, processing_error = ?, updated_at = ?
                WHERE asset_id = ?
                """,
                (status.value, processor, processor_version, error, now, asset_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"Knowledge Asset was not found: {asset_id}")
            conn.execute(
                """
                INSERT INTO knowledge_processing_attempts(
                    attempt_id, asset_id, status, processor, processor_version,
                    error_code, error_summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"attempt-{uuid.uuid4()}",
                    asset_id,
                    status.value,
                    processor,
                    processor_version,
                    "PROCESSING_ERROR" if error else "",
                    error,
                    now,
                ),
            )
            self._event(
                conn,
                "ASSET_PROCESSING_CHANGED",
                "asset",
                asset_id,
                {"status": status.value, "processor": processor},
            )
        value = self.asset(asset_id)
        assert value is not None
        return value

    def referenced_vault_paths(self, relative_paths: Sequence[str]) -> set[str]:
        """Return only paths still authorized by a live Asset or representation row."""

        normalized = tuple(dict.fromkeys(relative_paths))
        if len(normalized) > 100_000 or any(
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 2048
            for value in normalized
        ):
            raise ValueError("Knowledge Vault reference lookup exceeds its bounded contract")
        referenced: set[str] = set()
        with self._lock:
            for offset in range(0, len(normalized), 400):
                batch = normalized[offset : offset + 400]
                if not batch:
                    continue
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(
                    f"""
                    SELECT vault_relative_path FROM knowledge_assets
                    WHERE vault_relative_path IN ({placeholders})
                    UNION
                    SELECT vault_relative_path FROM knowledge_representations
                    WHERE vault_relative_path IN ({placeholders})
                    """,
                    (*batch, *batch),
                ).fetchall()
                referenced.update(str(row[0]) for row in rows)
        return referenced
