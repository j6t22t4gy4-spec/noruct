"""Read-only Asset and representation projections for the Knowledge Store.

The mixin uses the owning local store's transaction connection and lock.  It
does not duplicate vault metadata, retrieval state, or user knowledge
authority; write, deletion and integrity operations remain in ``store.py``.
"""

from __future__ import annotations

from typing import Mapping

from .models import AssetStatus, DerivedRepresentation, KnowledgeAsset


class KnowledgeAssetReadProjectionMixin:
    """Read-only asset metadata composed into :class:`KnowledgeStore`."""

    def asset_by_hash(self, content_hash: str, access_scope: str) -> KnowledgeAsset | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_assets WHERE content_hash = ? AND access_scope = ?",
                (content_hash, access_scope),
            ).fetchone()
        return None if row is None else self._asset(row)

    def asset(self, asset_id: str) -> KnowledgeAsset | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
        return None if row is None else self._asset(row)

    def remote_asset_source(self, asset_id: str) -> Mapping[str, object] | None:
        """Return bounded cache validators for one explicit public source."""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT asset_id, source_url, response_etag, response_last_modified,
                       fetched_at, checked_at
                FROM knowledge_remote_asset_sources WHERE asset_id = ?
                """,
                (asset_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "asset_id": str(row["asset_id"]),
            "source_url": str(row["source_url"]),
            "etag": str(row["response_etag"]) if row["response_etag"] is not None else None,
            "last_modified": (
                str(row["response_last_modified"])
                if row["response_last_modified"] is not None
                else None
            ),
            "fetched_at": str(row["fetched_at"]),
            "checked_at": str(row["checked_at"]),
        }

    def list_assets(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
    ) -> tuple[KnowledgeAsset, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("Asset list limit must be between 1 and 500")
        query = "SELECT * FROM knowledge_assets"
        params: list[object] = []
        if status:
            query += " WHERE status = ?"
            params.append(AssetStatus(status).value)
        query += " ORDER BY created_at DESC, asset_id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return tuple(self._asset(row) for row in rows)

    def latest_representation(self, asset_id: str) -> DerivedRepresentation | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM knowledge_representations
                WHERE asset_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (asset_id,),
            ).fetchone()
        return None if row is None else self._representation(row)

    def list_representations(self, asset_id: str) -> tuple[DerivedRepresentation, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM knowledge_representations
                WHERE asset_id = ? ORDER BY revision DESC, representation_id
                """,
                (asset_id,),
            ).fetchall()
        return tuple(self._representation(row) for row in rows)
