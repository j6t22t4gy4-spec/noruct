"""Asset deletion planning and cascade mutation for the canonical Knowledge Store.

The component shares the owning store's one SQLite transaction, event ledger,
FTS projections, provenance closure, and Vault sanitation path.  It is a
module boundary, not a second deletion authority or a separate archive.
"""

from __future__ import annotations

import sqlite3

from .models import KnowledgeAsset


class KnowledgeAssetDeletionMixin:
    """Revision-tree deletion with optimistic closure verification."""

    @staticmethod
    def _asset_deletion_ids(conn: sqlite3.Connection, asset_id: str) -> set[str]:
        rows = conn.execute(
            """
            WITH RECURSIVE asset_tree(asset_id) AS (
                SELECT asset_id FROM knowledge_assets WHERE asset_id = ?
                UNION
                SELECT child.asset_id
                FROM knowledge_assets child
                JOIN asset_tree parent ON child.parent_asset_id = parent.asset_id
            )
            SELECT asset_id FROM asset_tree
            """,
            (asset_id,),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def asset_deletion_closure(self, asset_id: str) -> tuple[KnowledgeAsset, ...]:
        with self._lock:
            identities = self._asset_deletion_ids(self._conn, asset_id)
            if not identities:
                raise ValueError(f"Knowledge Asset was not found: {asset_id}")
            placeholders = ",".join("?" for _ in identities)
            rows = self._conn.execute(
                f"SELECT * FROM knowledge_assets WHERE asset_id IN ({placeholders}) "
                "ORDER BY revision DESC, asset_id",
                tuple(sorted(identities)),
            ).fetchall()
        return tuple(self._asset(row) for row in rows)

    def delete_asset(
        self,
        asset_id: str,
        *,
        expected_asset_ids: set[str] | None = None,
        expected_representation_ids: set[str] | None = None,
    ) -> KnowledgeAsset:
        asset = self.asset(asset_id)
        if asset is None:
            raise ValueError(f"Knowledge Asset was not found: {asset_id}")
        with self._transaction() as conn:
            asset_ids = self._asset_deletion_ids(conn, asset_id)
            if expected_asset_ids is not None and asset_ids != expected_asset_ids:
                raise ValueError(
                    "Knowledge Asset revision tree changed during deletion; retry the operation"
                )
            placeholders = ",".join("?" for _ in asset_ids)
            ordered_ids = tuple(sorted(asset_ids))
            pack_rows = conn.execute(
                f"SELECT DISTINCT pack_id FROM evidence_pack_sources "
                f"WHERE asset_id IN ({placeholders})",
                ordered_ids,
            ).fetchall()
            representation_rows = conn.execute(
                f"SELECT representation_id FROM knowledge_representations "
                f"WHERE asset_id IN ({placeholders})",
                ordered_ids,
            ).fetchall()
            representation_ids = {str(row[0]) for row in representation_rows}
            if (
                expected_representation_ids is not None
                and representation_ids != expected_representation_ids
            ):
                raise ValueError(
                    "Knowledge Asset representations changed during deletion; retry the operation"
                )
            record_predicate = f"source_asset_id IN ({placeholders})"
            parameters: tuple[object, ...] = ordered_ids
            if representation_ids:
                representation_placeholders = ",".join("?" for _ in representation_ids)
                record_predicate += (
                    f" OR source_representation_id IN ({representation_placeholders})"
                )
                parameters += tuple(sorted(representation_ids))
            record_rows = conn.execute(
                f"SELECT record_id FROM knowledge_records WHERE {record_predicate}",
                parameters,
            ).fetchall()
            closure = self._provenance_closure(
                conn,
                pack_ids={str(row["pack_id"]) for row in pack_rows},
                record_ids={str(row["record_id"]) for row in record_rows},
            )
            self._delete_provenance_closure(conn, closure)
            if self._managed_fts5_available_on(conn):
                conn.execute(
                    f"DELETE FROM {self._MANAGED_FTS5_TABLE} WHERE asset_id IN ({placeholders})",
                    ordered_ids,
                )
            if self._managed_cjk_candidates_available_on(conn):
                conn.execute(
                    f"DELETE FROM {self._MANAGED_CJK_CANDIDATES_TABLE} WHERE chunk_id IN "
                    "(SELECT chunk_id FROM knowledge_chunks WHERE asset_id IN "
                    f"({placeholders}))",
                    ordered_ids,
                )
            conn.execute(
                f"DELETE FROM knowledge_assets WHERE asset_id IN ({placeholders})",
                ordered_ids,
            )
            self._event(
                conn,
                "ASSET_DELETED",
                "asset",
                asset_id,
                {"asset_count": len(asset_ids)},
            )
        self._sanitize_deleted_content()
        return asset
