"""SQLite projection for user-owned Knowledge Folder indexing."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from .folder_models import (
    KnowledgeFolder,
    KnowledgeFolderEntry,
    KnowledgeFolderEntryStatus,
    KnowledgeFolderStatus,
    ScannedKnowledgeFile,
)


class FolderKnowledgeStoreMixin:
    """Mixin used by KnowledgeStore without creating a second state authority."""

    _FOLDER_FTS5_TABLE = "knowledge_folder_fts"
    _FOLDER_CJK_CANDIDATES_TABLE = "knowledge_folder_cjk_candidates"

    @classmethod
    def _folder_fts5_available_on(cls, conn: sqlite3.Connection) -> bool:
        """Return whether this SQLite build has the optional Folder projection.

        FTS5 is deliberately an acceleration projection, not Knowledge state
        authority.  Some embedded SQLite builds omit it, so all callers must
        retain a correct non-FTS path.
        """

        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (cls._FOLDER_FTS5_TABLE,),
        ).fetchone()
        return row is not None

    @classmethod
    def _rebuild_folder_fts5(
        cls,
        conn: sqlite3.Connection,
        *,
        folder_id: str | None = None,
    ) -> None:
        """Synchronize the derived FTS5 projection from authoritative entries."""

        if not cls._folder_fts5_available_on(conn):
            return
        if folder_id is None:
            conn.execute(f"DELETE FROM {cls._FOLDER_FTS5_TABLE}")
            parameters: tuple[object, ...] = ()
            where = ""
        else:
            conn.execute(
                f"DELETE FROM {cls._FOLDER_FTS5_TABLE} WHERE folder_id = ?",
                (folder_id,),
            )
            parameters = (folder_id,)
            where = "AND entry.folder_id = ?"
        conn.execute(
            f"""
            INSERT INTO {cls._FOLDER_FTS5_TABLE}(
                entry_id, folder_id, relative_path, index_text
            )
            SELECT entry.entry_id, entry.folder_id, entry.relative_path, entry.index_text
            FROM knowledge_folder_entries entry
            WHERE entry.index_status = ? AND entry.index_text != '' {where}
            """,
            (KnowledgeFolderEntryStatus.READY.value, *parameters),
        )

    @classmethod
    def _folder_cjk_candidates_available_on(cls, conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (cls._FOLDER_CJK_CANDIDATES_TABLE,),
        ).fetchone() is not None

    @staticmethod
    def _cjk_candidate_tokens(value: str) -> tuple[str, ...]:
        """Return bounded overlapping CJK bigrams from normalized raw text.

        This is an acceleration index, not Korean linguistic analysis. It
        bridges raw whitespace in terms such as ``가격 전략`` and narrows a
        contiguous user query before the existing hybrid ranker remains the
        only evidence-selection authority.
        """

        import re

        compact = "".join(
            re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", value)
        )
        if len(compact) < 2:
            return ()
        return tuple(sorted({compact[index : index + 2] for index in range(len(compact) - 1)}))

    @classmethod
    def _rebuild_folder_cjk_candidates(
        cls,
        conn: sqlite3.Connection,
        *,
        folder_id: str | None = None,
    ) -> None:
        """Rebuild the disposable local compound-candidate projection."""

        if not cls._folder_cjk_candidates_available_on(conn):
            return
        if folder_id is None:
            conn.execute(f"DELETE FROM {cls._FOLDER_CJK_CANDIDATES_TABLE}")
            rows = conn.execute(
                """
                SELECT entry_id, relative_path, index_text
                FROM knowledge_folder_entries
                WHERE index_status = ? AND index_text != ''
                """,
                (KnowledgeFolderEntryStatus.READY.value,),
            ).fetchall()
        else:
            conn.execute(
                f"DELETE FROM {cls._FOLDER_CJK_CANDIDATES_TABLE} WHERE entry_id IN "
                "(SELECT entry_id FROM knowledge_folder_entries WHERE folder_id = ?)",
                (folder_id,),
            )
            rows = conn.execute(
                """
                SELECT entry_id, relative_path, index_text
                FROM knowledge_folder_entries
                WHERE folder_id = ? AND index_status = ? AND index_text != ''
                """,
                (folder_id, KnowledgeFolderEntryStatus.READY.value),
            ).fetchall()
        values = [
            (str(row["entry_id"]), token)
            for row in rows
            for token in cls._cjk_candidate_tokens(
                f"{row['relative_path']} {row['index_text']}"
            )
        ]
        if values:
            conn.executemany(
                f"INSERT OR IGNORE INTO {cls._FOLDER_CJK_CANDIDATES_TABLE}(entry_id, token) VALUES (?, ?)",
                values,
            )

    @staticmethod
    def _initialize_folder_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_folders (
                folder_id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                access_scope TEXT NOT NULL,
                ignore_globs_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                scan_generation INTEGER NOT NULL DEFAULT 0 CHECK(scan_generation >= 0),
                last_scan_status TEXT NOT NULL DEFAULT 'NEVER',
                last_scan_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS knowledge_folders_status_idx
                ON knowledge_folders(status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS knowledge_folder_entries (
                entry_id TEXT PRIMARY KEY,
                folder_id TEXT NOT NULL REFERENCES knowledge_folders(folder_id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                modified_ns INTEGER NOT NULL CHECK(modified_ns >= 0),
                media_type TEXT NOT NULL,
                index_status TEXT NOT NULL,
                index_text TEXT NOT NULL DEFAULT '',
                index_error TEXT NOT NULL DEFAULT '',
                indexer_revision TEXT NOT NULL DEFAULT '',
                snapshot_asset_id TEXT REFERENCES knowledge_assets(asset_id) ON DELETE SET NULL,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                last_seen_generation INTEGER NOT NULL CHECK(last_seen_generation >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(folder_id, relative_path)
            );

            CREATE INDEX IF NOT EXISTS knowledge_folder_entries_current_idx
                ON knowledge_folder_entries(folder_id, index_status, relative_path);
            CREATE INDEX IF NOT EXISTS knowledge_folder_entries_hash_idx
                ON knowledge_folder_entries(folder_id, content_hash, index_status);

            CREATE TABLE IF NOT EXISTS knowledge_folder_cjk_candidates (
                entry_id TEXT NOT NULL REFERENCES knowledge_folder_entries(entry_id) ON DELETE CASCADE,
                token TEXT NOT NULL,
                PRIMARY KEY(entry_id, token)
            );
            CREATE INDEX IF NOT EXISTS knowledge_folder_cjk_candidates_token_idx
                ON knowledge_folder_cjk_candidates(token, entry_id);
            """
        )
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(knowledge_folder_entries)").fetchall()
        }
        folder_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(knowledge_folders)").fetchall()
        }
        if "ignore_globs_json" not in folder_columns:
            conn.execute(
                "ALTER TABLE knowledge_folders "
                "ADD COLUMN ignore_globs_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "indexer_revision" not in columns:
            conn.execute(
                "ALTER TABLE knowledge_folder_entries "
                "ADD COLUMN indexer_revision TEXT NOT NULL DEFAULT ''"
            )
        had_fts5 = FolderKnowledgeStoreMixin._folder_fts5_available_on(conn)
        had_cjk_candidates = FolderKnowledgeStoreMixin._folder_cjk_candidates_available_on(conn)
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_folder_fts
                USING fts5(
                    entry_id UNINDEXED,
                    folder_id UNINDEXED,
                    relative_path,
                    index_text,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )
        except sqlite3.OperationalError:
            # Correctness never relies on FTS5.  This is expected on minimal
            # or vendor-supplied SQLite builds that omit the extension.
            return
        if not had_fts5:
            FolderKnowledgeStoreMixin._rebuild_folder_fts5(conn)
        if not had_cjk_candidates:
            FolderKnowledgeStoreMixin._rebuild_folder_cjk_candidates(conn)

    def folder_fts5_available(self) -> bool:
        """Expose optional candidate-index availability to local surfaces."""

        with self._lock:
            return self._folder_fts5_available_on(self._conn)

    def folder_cjk_candidate_index_available(self) -> bool:
        """Expose the local rebuildable CJK compound candidate index."""

        with self._lock:
            return self._folder_cjk_candidates_available_on(self._conn)

    @staticmethod
    def _folder(row: sqlite3.Row) -> KnowledgeFolder:
        try:
            ignore_globs = tuple(json.loads(str(row["ignore_globs_json"])))
        except (KeyError, TypeError, json.JSONDecodeError):
            ignore_globs = ()
        return KnowledgeFolder(
            folder_id=str(row["folder_id"]),
            root_path=str(row["root_path"]),
            display_name=str(row["display_name"]),
            access_scope=str(row["access_scope"]),
            ignore_globs=ignore_globs,
            status=KnowledgeFolderStatus(str(row["status"])),
            scan_generation=int(row["scan_generation"]),
            last_scan_status=str(row["last_scan_status"]),
            last_scan_at=(str(row["last_scan_at"]) if row["last_scan_at"] else None),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _folder_entry(row: sqlite3.Row) -> KnowledgeFolderEntry:
        return KnowledgeFolderEntry(
            entry_id=str(row["entry_id"]),
            folder_id=str(row["folder_id"]),
            relative_path=str(row["relative_path"]),
            content_hash=str(row["content_hash"]),
            byte_size=int(row["byte_size"]),
            modified_ns=int(row["modified_ns"]),
            media_type=str(row["media_type"]),
            index_status=KnowledgeFolderEntryStatus(str(row["index_status"])),
            index_text=str(row["index_text"]),
            index_error=str(row["index_error"]),
            indexer_revision=str(row["indexer_revision"]),
            snapshot_asset_id=(
                str(row["snapshot_asset_id"])
                if row["snapshot_asset_id"] is not None
                else None
            ),
            revision=int(row["revision"]),
            last_seen_generation=int(row["last_seen_generation"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def register_knowledge_folder(
        self,
        *,
        root_path: str,
        display_name: str,
        access_scope: str,
        ignore_globs: tuple[str, ...] = (),
    ) -> tuple[KnowledgeFolder, bool]:
        root_path = root_path.strip()
        display_name = display_name.strip()
        access_scope = access_scope.strip()
        if not root_path or len(root_path.encode("utf-8")) > 4096:
            raise ValueError("Knowledge Folder root path is invalid")
        if not display_name or len(display_name.encode("utf-8")) > 1024:
            raise ValueError("Knowledge Folder display name is invalid")
        if not access_scope or len(access_scope.encode("utf-8")) > 256:
            raise ValueError("Knowledge Folder access scope is invalid")
        now = self._folder_now()
        with self._transaction() as conn:
            ignore_json = json.dumps(list(ignore_globs), ensure_ascii=False, separators=(",", ":"))
            existing = conn.execute(
                "SELECT * FROM knowledge_folders WHERE root_path = ?",
                (root_path,),
            ).fetchone()
            if existing is not None:
                if str(existing["access_scope"]) != access_scope:
                    raise ValueError("Knowledge Folder is already registered with another scope")
                if str(existing["ignore_globs_json"]) != ignore_json:
                    raise ValueError("Knowledge Folder is already registered with other ignore rules")
                return self._folder(existing), True
            folder_id = f"folder-{uuid.uuid4()}"
            conn.execute(
                """
                INSERT INTO knowledge_folders(
                    folder_id, root_path, display_name, access_scope, ignore_globs_json, status,
                    scan_generation, last_scan_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', 0, 'NEVER', ?, ?)
                """,
                (folder_id, root_path, display_name, access_scope, ignore_json, now, now),
            )
            self._event(
                conn,
                "KNOWLEDGE_FOLDER_REGISTERED",
                "knowledge_folder",
                folder_id,
                {"display_name": display_name, "access_scope": access_scope, "ignore_glob_count": len(ignore_globs)},
            )
            row = conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?",
                (folder_id,),
            ).fetchone()
            assert row is not None
            return self._folder(row), False

    def knowledge_folder(self, folder_id: str) -> KnowledgeFolder | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?",
                (folder_id,),
            ).fetchone()
        return None if row is None else self._folder(row)

    def list_knowledge_folders(self, *, limit: int = 100) -> tuple[KnowledgeFolder, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Knowledge Folder list limit must be between 1 and 1000")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_folders ORDER BY updated_at DESC, folder_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._folder(row) for row in rows)

    def set_knowledge_folder_status(
        self,
        folder_id: str,
        *,
        status: KnowledgeFolderStatus,
    ) -> KnowledgeFolder:
        """Pause or resume indexing without touching the user's raw files."""

        now = self._folder_now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?", (folder_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Knowledge Folder was not found: {folder_id}")
            if str(row["status"]) != status.value:
                conn.execute(
                    "UPDATE knowledge_folders SET status = ?, updated_at = ? WHERE folder_id = ?",
                    (status.value, now, folder_id),
                )
                self._event(
                    conn,
                    "KNOWLEDGE_FOLDER_STATUS_CHANGED",
                    "knowledge_folder",
                    folder_id,
                    {"status": status.value},
                )
            updated = conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?", (folder_id,)
            ).fetchone()
            assert updated is not None
        return self._folder(updated)

    def set_knowledge_folder_ignore_globs(
        self,
        folder_id: str,
        *,
        ignore_globs: tuple[str, ...],
    ) -> KnowledgeFolder:
        """Persist explicit user-owned path exclusions without reading raw files."""

        encoded = json.dumps(list(ignore_globs), ensure_ascii=False, separators=(",", ":"))
        now = self._folder_now()
        with self._transaction() as conn:
            current = conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?", (folder_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"Knowledge Folder was not found: {folder_id}")
            if str(current["ignore_globs_json"]) != encoded:
                conn.execute(
                    "UPDATE knowledge_folders SET ignore_globs_json = ?, updated_at = ? WHERE folder_id = ?",
                    (encoded, now, folder_id),
                )
                self._event(
                    conn,
                    "KNOWLEDGE_FOLDER_IGNORE_RULES_CHANGED",
                    "knowledge_folder",
                    folder_id,
                    {"ignore_glob_count": len(ignore_globs)},
                )
            updated = conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?", (folder_id,)
            ).fetchone()
            assert updated is not None
        return self._folder(updated)

    def relink_knowledge_folder(
        self,
        folder_id: str,
        *,
        root_path: str,
        display_name: str | None = None,
    ) -> KnowledgeFolder:
        """Point one registration at a new validated raw-folder location.

        Existing indexed entries are retained only as a historical local index;
        the next explicit scan reconciles them against the newly selected raw
        folder.  No user file is copied, moved, or removed here.
        """

        normalized_root = root_path.strip()
        if not normalized_root or len(normalized_root.encode("utf-8")) > 4096:
            raise ValueError("Knowledge Folder root path is invalid")
        normalized_name = display_name.strip() if display_name is not None else None
        if normalized_name is not None and (
            not normalized_name or len(normalized_name.encode("utf-8")) > 1024
        ):
            raise ValueError("Knowledge Folder display name is invalid")
        now = self._folder_now()
        with self._transaction() as conn:
            current = conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?", (folder_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"Knowledge Folder was not found: {folder_id}")
            duplicate = conn.execute(
                "SELECT folder_id FROM knowledge_folders WHERE root_path = ? AND folder_id != ?",
                (normalized_root, folder_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("Knowledge Folder root is already registered")
            changed = (
                str(current["root_path"]) != normalized_root
                or (normalized_name is not None and str(current["display_name"]) != normalized_name)
            )
            if changed:
                conn.execute(
                    """
                    UPDATE knowledge_folders
                    SET root_path = ?, display_name = ?, last_scan_status = 'RELINKED', updated_at = ?
                    WHERE folder_id = ?
                    """,
                    (
                        normalized_root,
                        normalized_name if normalized_name is not None else str(current["display_name"]),
                        now,
                        folder_id,
                    ),
                )
                self._event(
                    conn,
                    "KNOWLEDGE_FOLDER_RELINKED",
                    "knowledge_folder",
                    folder_id,
                    {"root_path": normalized_root},
                )
            updated = conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?", (folder_id,)
            ).fetchone()
            assert updated is not None
        return self._folder(updated)

    def remove_knowledge_folder(self, folder_id: str) -> bool:
        """Forget a folder registration and derived index, never raw files."""

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT display_name, root_path FROM knowledge_folders WHERE folder_id = ?", (folder_id,)
            ).fetchone()
            if row is None:
                return False
            if self._folder_fts5_available_on(conn):
                conn.execute(
                    f"DELETE FROM {self._FOLDER_FTS5_TABLE} WHERE folder_id = ?",
                    (folder_id,),
                )
            conn.execute("DELETE FROM knowledge_folders WHERE folder_id = ?", (folder_id,))
            self._event(
                conn,
                "KNOWLEDGE_FOLDER_REMOVED",
                "knowledge_folder",
                folder_id,
                {"display_name": str(row["display_name"]), "root_path": str(row["root_path"])},
            )
        return True

    def list_knowledge_folder_entries(
        self,
        folder_id: str,
        *,
        include_deleted: bool = False,
        limit: int = 1000,
    ) -> tuple[KnowledgeFolderEntry, ...]:
        if limit < 1 or limit > 10_000:
            raise ValueError("Knowledge Folder entry limit must be between 1 and 10000")
        query = "SELECT * FROM knowledge_folder_entries WHERE folder_id = ?"
        parameters: list[object] = [folder_id]
        if not include_deleted:
            query += " AND index_status != ?"
            parameters.append(KnowledgeFolderEntryStatus.DELETED.value)
        query += " ORDER BY relative_path LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(parameters)).fetchall()
        return tuple(self._folder_entry(row) for row in rows)

    def reconcile_knowledge_folder(
        self,
        folder_id: str,
        files: Sequence[ScannedKnowledgeFile],
        *,
        truncated: bool,
    ) -> tuple[KnowledgeFolder, dict[str, int]]:
        if len(files) > 10_000:
            raise ValueError("Knowledge Folder scan exceeds its persistence bound")
        paths = [item.relative_path for item in files]
        if len(paths) != len(set(paths)):
            raise ValueError("Knowledge Folder scan contains duplicate relative paths")
        now = self._folder_now()
        counts = {"created": 0, "updated": 0, "renamed": 0, "unchanged": 0, "deleted": 0}
        with self._transaction() as conn:
            folder = conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?",
                (folder_id,),
            ).fetchone()
            if folder is None:
                raise ValueError(f"Knowledge Folder was not found: {folder_id}")
            if str(folder["status"]) != KnowledgeFolderStatus.ACTIVE.value:
                raise ValueError("Knowledge Folder is paused")
            generation = int(folder["scan_generation"]) + 1
            existing_rows = conn.execute(
                "SELECT * FROM knowledge_folder_entries WHERE folder_id = ?",
                (folder_id,),
            ).fetchall()
            by_path = {str(row["relative_path"]): row for row in existing_rows}
            scanned_paths = set(paths)
            unseen = {
                str(row["entry_id"]): row
                for row in existing_rows
                if str(row["index_status"]) != KnowledgeFolderEntryStatus.DELETED.value
                and str(row["relative_path"]) not in scanned_paths
            }
            pending_new = [item for item in files if item.relative_path not in by_path]
            new_hash_counts: dict[str, int] = {}
            for item in pending_new:
                new_hash_counts[item.content_hash] = new_hash_counts.get(item.content_hash, 0) + 1

            for item in files:
                row = by_path.get(item.relative_path)
                if row is None:
                    rename_candidates = [
                        candidate
                        for candidate in unseen.values()
                        if str(candidate["content_hash"]) == item.content_hash
                        and bool(item.content_hash)
                        and new_hash_counts[item.content_hash] == 1
                    ]
                    if len(rename_candidates) == 1:
                        row = rename_candidates[0]
                        conn.execute(
                            """
                            UPDATE knowledge_folder_entries
                            SET relative_path = ?, content_hash = ?, byte_size = ?, modified_ns = ?, media_type = ?,
                                index_status = ?, index_text = ?, index_error = ?, indexer_revision = ?,
                                revision = revision + 1, last_seen_generation = ?, updated_at = ?
                            WHERE entry_id = ?
                            """,
                            (
                                item.relative_path,
                                item.content_hash,
                                item.byte_size,
                                item.modified_ns,
                                item.media_type,
                                item.index_status.value,
                                item.index_text,
                                item.index_error,
                                item.indexer_revision,
                                generation,
                                now,
                                row["entry_id"],
                            ),
                        )
                        unseen.pop(str(row["entry_id"]), None)
                        counts["renamed"] += 1
                        continue
                    entry_id = f"folder-entry-{uuid.uuid4()}"
                    conn.execute(
                        """
                        INSERT INTO knowledge_folder_entries(
                            entry_id, folder_id, relative_path, content_hash, byte_size,
                            modified_ns, media_type, index_status, index_text, index_error,
                            indexer_revision, revision, last_seen_generation, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            entry_id,
                            folder_id,
                            item.relative_path,
                            item.content_hash,
                            item.byte_size,
                            item.modified_ns,
                            item.media_type,
                            item.index_status.value,
                            item.index_text,
                            item.index_error,
                            item.indexer_revision,
                            generation,
                            now,
                            now,
                        ),
                    )
                    counts["created"] += 1
                    continue

                unseen.pop(str(row["entry_id"]), None)
                unchanged = (
                    str(row["content_hash"]) == item.content_hash
                    and int(row["byte_size"]) == item.byte_size
                    and int(row["modified_ns"]) == item.modified_ns
                    and str(row["index_status"]) == item.index_status.value
                    and str(row["index_text"]) == item.index_text
                    and str(row["index_error"]) == item.index_error
                    and str(row["indexer_revision"]) == item.indexer_revision
                )
                if unchanged:
                    conn.execute(
                        "UPDATE knowledge_folder_entries SET last_seen_generation = ? WHERE entry_id = ?",
                        (generation, row["entry_id"]),
                    )
                    counts["unchanged"] += 1
                    continue
                conn.execute(
                    """
                    UPDATE knowledge_folder_entries
                    SET content_hash = ?, byte_size = ?, modified_ns = ?, media_type = ?,
                        index_status = ?, index_text = ?, index_error = ?, snapshot_asset_id = NULL,
                        indexer_revision = ?,
                        revision = revision + 1, last_seen_generation = ?, updated_at = ?
                    WHERE entry_id = ?
                    """,
                    (
                        item.content_hash,
                        item.byte_size,
                        item.modified_ns,
                        item.media_type,
                        item.index_status.value,
                        item.index_text,
                        item.index_error,
                        item.indexer_revision,
                        generation,
                        now,
                        row["entry_id"],
                    ),
                )
                counts["updated"] += 1

            if not truncated:
                for row in unseen.values():
                    conn.execute(
                        """
                        UPDATE knowledge_folder_entries
                        SET index_status = 'DELETED', index_text = '', index_error = '',
                            snapshot_asset_id = NULL, revision = revision + 1, updated_at = ?
                        WHERE entry_id = ?
                        """,
                        (now, row["entry_id"]),
                    )
                    counts["deleted"] += 1

            scan_status = "TRUNCATED" if truncated else "COMPLETE"
            conn.execute(
                """
                UPDATE knowledge_folders
                SET scan_generation = ?, last_scan_status = ?, last_scan_at = ?, updated_at = ?
                WHERE folder_id = ?
                """,
                (generation, scan_status, now, now, folder_id),
            )
            self._rebuild_folder_fts5(conn, folder_id=folder_id)
            self._rebuild_folder_cjk_candidates(conn, folder_id=folder_id)
            self._event(
                conn,
                "KNOWLEDGE_FOLDER_SCANNED",
                "knowledge_folder",
                folder_id,
                {"generation": generation, "status": scan_status, **counts},
            )
            updated = conn.execute(
                "SELECT * FROM knowledge_folders WHERE folder_id = ?",
                (folder_id,),
            ).fetchone()
            assert updated is not None
        return self._folder(updated), counts

    def bind_folder_entry_snapshot(self, entry_id: str, asset_id: str) -> KnowledgeFolderEntry:
        with self._transaction() as conn:
            binding = conn.execute(
                """
                SELECT e.*, f.access_scope AS folder_access_scope,
                       a.content_hash AS asset_content_hash,
                       a.access_scope AS asset_access_scope
                FROM knowledge_folder_entries e
                JOIN knowledge_folders f ON f.folder_id = e.folder_id
                LEFT JOIN knowledge_assets a ON a.asset_id = ?
                WHERE e.entry_id = ? AND e.index_status != 'DELETED'
                """,
                (asset_id, entry_id),
            ).fetchone()
            if binding is None:
                raise ValueError(f"Current Knowledge Folder entry was not found: {entry_id}")
            if binding["asset_content_hash"] is None:
                raise ValueError(f"Knowledge snapshot Asset was not found: {asset_id}")
            if str(binding["content_hash"]) != str(binding["asset_content_hash"]):
                raise ValueError("Knowledge snapshot content does not match the folder entry")
            if str(binding["folder_access_scope"]) != str(binding["asset_access_scope"]):
                raise ValueError("Knowledge snapshot access scope does not match the folder")
            now = self._folder_now()
            changed = conn.execute(
                """
                UPDATE knowledge_folder_entries
                SET snapshot_asset_id = ?, updated_at = ?
                WHERE entry_id = ? AND index_status != 'DELETED'
                """,
                (asset_id, now, entry_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"Current Knowledge Folder entry was not found: {entry_id}")
            row = conn.execute(
                "SELECT * FROM knowledge_folder_entries WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            assert row is not None
            self._event(
                conn,
                "KNOWLEDGE_FOLDER_SNAPSHOT_BOUND",
                "knowledge_folder_entry",
                entry_id,
                {
                    "folder_id": str(row["folder_id"]),
                    "asset_id": asset_id,
                    "content_hash": str(row["content_hash"]),
                },
            )
        return self._folder_entry(row)

    def folder_entry(self, entry_id: str) -> KnowledgeFolderEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_folder_entries WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
        return None if row is None else self._folder_entry(row)

    @staticmethod
    def _folder_now() -> str:
        return datetime.now(UTC).isoformat()
