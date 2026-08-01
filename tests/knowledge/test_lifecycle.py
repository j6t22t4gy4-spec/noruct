from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path

from dynamic_firm.knowledge import lifecycle
from dynamic_firm.knowledge.lifecycle import (
    ARCHIVE_SCHEMA,
    DATABASE_ARCHIVE_NAME,
    MANIFEST_NAME,
    VAULT_ARCHIVE_PREFIX,
    authorize_knowledge_deletion,
    delete_knowledge_state,
    export_knowledge_archive,
    knowledge_diagnostics,
    restore_knowledge_archive,
)
from dynamic_firm.knowledge.locking import (
    KnowledgeStateBusyError,
    KnowledgeStateLock,
    knowledge_mutation_marker_path,
)
from dynamic_firm.knowledge.models import AssetStatus
from dynamic_firm.knowledge.store import KnowledgeStore
from dynamic_firm.knowledge.vault import KnowledgeVault


class KnowledgeLifecycleTests(unittest.TestCase):
    @staticmethod
    def _fixture(root: Path, *, content: str = "private product thesis\nwith cited evidence") -> tuple[Path, Path, str, str]:
        database = root / "runtime.knowledge.db"
        vault_root = root / "runtime.knowledge.vault"
        vault = KnowledgeVault(vault_root)
        source = root / "source.txt"
        source.write_text(content, encoding="utf-8")
        resolved, digest, size = vault.inspect_source(source)
        stored = vault.store_source(
            resolved,
            content_hash=digest,
            byte_size=size,
            access_scope="private",
        )
        store = KnowledgeStore(database)
        try:
            asset, duplicate = store.create_asset(
                content_hash=digest,
                original_name=source.name,
                title="Private thesis",
                media_type="text/plain",
                byte_size=size,
                vault_relative_path=stored.relative_path,
                origin="test",
                access_scope="private",
            )
            if duplicate:
                raise AssertionError("fixture unexpectedly duplicated its asset")
            derived = vault.write_representation(asset.asset_id, content)
            representation = store.create_representation(
                asset_id=asset.asset_id,
                kind="normalized_markdown",
                media_type="text/markdown",
                content_hash=derived.content_hash,
                byte_size=derived.byte_size,
                vault_relative_path=derived.relative_path,
                processor="fixture",
                processor_version="1",
                chunks=(
                    {
                        "content": content,
                        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "char_start": 0,
                        "char_end": len(content),
                        "location": {"char_start": 0, "char_end": len(content)},
                    },
                ),
            )
            store.set_asset_processing(
                asset.asset_id,
                status=AssetStatus.READY,
                processor="fixture",
                processor_version="1",
            )
            store.create_record(
                kind="NOTE",
                statement="A deliberately private statement",
                source_asset_id=asset.asset_id,
                source_representation_id=representation.representation_id,
            )
        finally:
            store.close()
        return database, vault_root, stored.relative_path, derived.relative_path

    @staticmethod
    def _rewrite_archive(
        source: Path,
        destination: Path,
        mutate,
    ) -> None:
        with zipfile.ZipFile(source, "r") as archive:
            values = {info.filename: archive.read(info) for info in archive.infolist()}
        values, special = mutate(values)
        with zipfile.ZipFile(destination, "w", allowZip64=True) as archive:
            for name, payload in values.items():
                info = lifecycle._zip_info(name)
                if name in special:
                    info = special[name]
                archive.writestr(info, payload)

    @classmethod
    def _mutate_archived_database(
        cls,
        source: Path,
        destination: Path,
        statements: tuple[str, ...],
    ) -> None:
        def mutation(values):
            with tempfile.TemporaryDirectory() as temporary:
                database = Path(temporary) / "knowledge.db"
                database.write_bytes(values[DATABASE_ARCHIVE_NAME])
                connection = sqlite3.connect(database)
                try:
                    connection.execute("PRAGMA trusted_schema = OFF")
                    connection.execute("PRAGMA foreign_keys = OFF")
                    for statement in statements:
                        connection.execute(statement)
                    connection.commit()
                finally:
                    connection.close()
                payload = database.read_bytes()
            manifest = json.loads(values[MANIFEST_NAME])
            manifest["database"]["byte_size"] = len(payload)
            manifest["database"]["sha256"] = hashlib.sha256(payload).hexdigest()
            values[DATABASE_ARCHIVE_NAME] = payload
            values[MANIFEST_NAME] = lifecycle._canonical_json(manifest)
            return values, {}

        cls._rewrite_archive(source, destination, mutation)

    def test_export_is_integrity_checked_canonical_and_referenced_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, source_relative, derived_relative = self._fixture(root)
            orphan = vault / "orphan-private-copy.txt"
            orphan.write_text("must not be exported", encoding="utf-8")
            archive_path = root / "knowledge.zip"

            record = export_knowledge_archive(database, vault, archive_path)

            self.assertEqual(record.schema_version, ARCHIVE_SCHEMA)
            self.assertEqual(record.database_integrity, "ok")
            self.assertEqual(record.vault_object_count, 2)
            self.assertEqual(archive_path.stat().st_mode & 0o777, 0o600)
            with zipfile.ZipFile(archive_path, "r") as archive:
                names = set(archive.namelist())
                manifest_bytes = archive.read(MANIFEST_NAME)
                manifest = json.loads(manifest_bytes)
                self.assertEqual(
                    manifest_bytes,
                    lifecycle._canonical_json(manifest),
                )
                self.assertEqual(manifest["schema_version"], ARCHIVE_SCHEMA)
                self.assertEqual(
                    names,
                    {
                        MANIFEST_NAME,
                        DATABASE_ARCHIVE_NAME,
                        f"{VAULT_ARCHIVE_PREFIX}{source_relative}",
                        f"{VAULT_ARCHIVE_PREFIX}{derived_relative}",
                    },
                )
                self.assertNotIn("orphan-private-copy.txt", "\n".join(names))
                database_payload = archive.read(DATABASE_ARCHIVE_NAME)
                self.assertEqual(
                    hashlib.sha256(database_payload).hexdigest(),
                    manifest["database"]["sha256"],
                )
                for item in manifest["vault"]["objects"]:
                    payload = archive.read(item["archive_path"])
                    self.assertEqual(len(payload), item["byte_size"])
                    self.assertEqual(hashlib.sha256(payload).hexdigest(), item["sha256"])

    def test_export_uses_a_consistent_sqlite_backup_while_wal_store_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root)
            live = KnowledgeStore(database)
            try:
                live.create_record(kind="NOTE", statement="committed while the WAL store is open")
                archive_path = root / "knowledge.zip"
                export_knowledge_archive(database, vault, archive_path)
            finally:
                live.close()
            restored_database = root / "restored.db"
            restored_vault = root / "restored.vault"
            restore_knowledge_archive(archive_path, restored_database, restored_vault)
            connection = sqlite3.connect(restored_database)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM knowledge_records WHERE statement = ?",
                    ("committed while the WAL store is open",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)

    def test_restore_to_absent_targets_and_explicit_overwrite_are_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, source_relative, derived_relative = self._fixture(root / "source")
            archive_path = root / "knowledge.zip"
            export_knowledge_archive(database, vault, archive_path)
            target_database = root / "target" / "knowledge.db"
            target_vault = root / "target" / "knowledge.vault"

            first = restore_knowledge_archive(archive_path, target_database, target_vault)
            self.assertFalse(first.overwritten)
            self.assertEqual(target_database.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target_vault.stat().st_mode & 0o777, 0o700)
            self.assertTrue((target_vault / source_relative).is_file())
            self.assertTrue((target_vault / derived_relative).is_file())
            with self.assertRaisesRegex(ValueError, "overwrite"):
                restore_knowledge_archive(archive_path, target_database, target_vault)

            Path(f"{target_database}-wal").write_bytes(b"stale wal")
            Path(f"{target_database}-shm").write_bytes(b"stale shm")
            (target_vault / "stale.txt").write_text("stale", encoding="utf-8")
            second = restore_knowledge_archive(
                archive_path,
                target_database,
                target_vault,
                overwrite=True,
            )
            self.assertTrue(second.overwritten)
            self.assertFalse(Path(f"{target_database}-wal").exists())
            self.assertFalse(Path(f"{target_database}-shm").exists())
            self.assertFalse((target_vault / "stale.txt").exists())
            self.assertEqual(knowledge_diagnostics(target_database, target_vault).database_integrity, "ok")

    def test_restore_rejects_path_traversal_without_touching_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root / "source")
            valid = root / "valid.zip"
            hostile = root / "hostile.zip"
            export_knowledge_archive(database, vault, valid)

            def mutation(values):
                values["../escape"] = b"escape"
                return values, {}

            self._rewrite_archive(valid, hostile, mutation)
            target_database = root / "target.db"
            target_vault = root / "target.vault"
            with self.assertRaisesRegex(ValueError, "unsafe member path"):
                restore_knowledge_archive(hostile, target_database, target_vault)
            self.assertFalse(target_database.exists())
            self.assertFalse(target_vault.exists())
            self.assertFalse((root.parent / "escape").exists())

    def test_restore_rejects_symlink_members_and_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, source_relative, _ = self._fixture(root / "source")
            valid = root / "valid.zip"
            symlink_archive = root / "symlink.zip"
            hash_archive = root / "hash.zip"
            export_knowledge_archive(database, vault, valid)

            def symlink_mutation(values):
                name = f"{VAULT_ARCHIVE_PREFIX}{source_relative}"
                info = lifecycle._zip_info(name)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                return values, {name: info}

            self._rewrite_archive(valid, symlink_archive, symlink_mutation)
            with self.assertRaisesRegex(ValueError, "unsafe member"):
                restore_knowledge_archive(
                    symlink_archive,
                    root / "symlink-target.db",
                    root / "symlink-target.vault",
                )

            def hash_mutation(values):
                name = f"{VAULT_ARCHIVE_PREFIX}{source_relative}"
                values[name] = b"X" * len(values[name])
                return values, {}

            self._rewrite_archive(valid, hash_archive, hash_mutation)
            with self.assertRaisesRegex(ValueError, "hash or size"):
                restore_knowledge_archive(
                    hash_archive,
                    root / "hash-target.db",
                    root / "hash-target.vault",
                )

    def test_restore_rejects_corrupt_database_even_when_manifest_hash_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root / "source")
            valid = root / "valid.zip"
            corrupt = root / "corrupt.zip"
            export_knowledge_archive(database, vault, valid)

            def mutation(values):
                payload = bytearray(values[DATABASE_ARCHIVE_NAME])
                payload[:16] = b"not a sqlite db!"
                values[DATABASE_ARCHIVE_NAME] = bytes(payload)
                manifest = json.loads(values[MANIFEST_NAME])
                manifest["database"]["sha256"] = hashlib.sha256(payload).hexdigest()
                values[MANIFEST_NAME] = lifecycle._canonical_json(manifest)
                return values, {}

            self._rewrite_archive(valid, corrupt, mutation)
            with self.assertRaisesRegex(ValueError, "integrity|schema|validation"):
                restore_knowledge_archive(
                    corrupt,
                    root / "corrupt-target.db",
                    root / "corrupt-target.vault",
                )
            self.assertFalse((root / "corrupt-target.db").exists())

    def test_export_rejects_missing_tampered_and_symlinked_referenced_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, source_relative, _ = self._fixture(root / "source")
            source_object = vault / source_relative
            source_object.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "does not match"):
                export_knowledge_archive(database, vault, root / "tampered.zip")
            source_object.unlink()
            source_object.symlink_to(root / "outside")
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                export_knowledge_archive(database, vault, root / "symlink.zip")

    def test_export_vacuums_deleted_sqlite_content_out_of_archive(self) -> None:
        secret = "deleted-customer-secret-" + ("z" * 24_000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root)
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA secure_delete = OFF")
                connection.execute(
                    "INSERT INTO knowledge_events "
                    "(event_id, event_type, subject_type, subject_id, metadata_json, created_at) "
                    "VALUES (?, 'TEMP', 'record', 'deleted', ?, '2026-01-01T00:00:00+00:00')",
                    ("event-deleted-secret", json.dumps({"secret": secret})),
                )
                connection.commit()
                connection.execute(
                    "DELETE FROM knowledge_events WHERE event_id = 'event-deleted-secret'"
                )
                connection.commit()
            finally:
                connection.close()
            self.assertIn(b"deleted-customer-secret-", database.read_bytes())

            archive_path = root / "sanitized.zip"
            export_knowledge_archive(database, vault, archive_path)
            with zipfile.ZipFile(archive_path, "r") as archive:
                exported_database = archive.read(DATABASE_ARCHIVE_NAME)
            self.assertNotIn(b"deleted-customer-secret-", exported_database)

    def test_restore_rejects_unexpected_schema_and_foreign_key_damage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root / "source")
            valid = root / "valid.zip"
            export_knowledge_archive(database, vault, valid)

            trigger_archive = root / "trigger.zip"
            self._mutate_archived_database(
                valid,
                trigger_archive,
                (
                    "CREATE TRIGGER unexpected_trigger AFTER INSERT ON knowledge_events "
                    "BEGIN SELECT 1; END",
                ),
            )
            with self.assertRaisesRegex(ValueError, "triggers or views"):
                restore_knowledge_archive(
                    trigger_archive,
                    root / "trigger-target.db",
                    root / "trigger-target.vault",
                )

            table_archive = root / "table.zip"
            self._mutate_archived_database(
                valid,
                table_archive,
                ("CREATE TABLE unexpected_customer_copy(secret TEXT)",),
            )
            with self.assertRaisesRegex(ValueError, "schema surface"):
                restore_knowledge_archive(
                    table_archive,
                    root / "table-target.db",
                    root / "table-target.vault",
                )

            foreign_key_archive = root / "foreign-key.zip"
            self._mutate_archived_database(
                valid,
                foreign_key_archive,
                (
                    "INSERT INTO knowledge_processing_attempts "
                    "(attempt_id, asset_id, status, processor, processor_version, error_code, "
                    "error_summary, created_at) VALUES "
                    "('orphan-attempt', 'missing-asset', 'FAILED', 'test', '1', 'x', 'x', "
                    "'2026-01-01T00:00:00+00:00')",
                ),
            )
            with self.assertRaisesRegex(ValueError, "foreign-key"):
                restore_knowledge_archive(
                    foreign_key_archive,
                    root / "foreign-target.db",
                    root / "foreign-target.vault",
                )

    def test_restore_rejects_semantic_schema_tampering_with_plausible_integrity(self) -> None:
        cases = {
            "check": (
                "knowledge_intents",
                "UPDATE sqlite_master SET sql = replace("
                "sql, 'CHECK(priority >= 0 AND priority <= 100)', 'CHECK(priority >= 0)') "
                "WHERE type = 'table' AND name = 'knowledge_intents'",
            ),
            "unique": (
                "knowledge_write_candidates",
                "UPDATE sqlite_master SET sql = replace("
                "sql, 'UNIQUE(job_id, kind)', 'UNIQUE(kind, job_id)') "
                "WHERE type = 'table' AND name = 'knowledge_write_candidates'",
            ),
            "default": (
                "knowledge_execution_bindings",
                "UPDATE sqlite_master SET sql = replace("
                "sql, \"DEFAULT ''\", \"DEFAULT 'tampered'\") "
                "WHERE type = 'table' AND name = 'knowledge_execution_bindings'",
            ),
            "type": (
                "knowledge_intents",
                "UPDATE sqlite_master SET sql = replace("
                "sql, 'priority INTEGER NOT NULL', 'priority TEXT NOT NULL') "
                "WHERE type = 'table' AND name = 'knowledge_intents'",
            ),
            "index-order": (
                "knowledge_intents_status_priority_idx",
                "UPDATE sqlite_master SET sql = replace("
                "sql, 'priority DESC', 'priority ASC') "
                "WHERE type = 'index' AND name = 'knowledge_intents_status_priority_idx'",
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root / "source")
            valid = root / "valid.zip"
            export_knowledge_archive(database, vault, valid)

            for label, (object_name, mutation) in cases.items():
                with self.subTest(semantic_change=label):
                    hostile = root / f"{label}.zip"
                    self._mutate_archived_database(
                        valid,
                        hostile,
                        ("PRAGMA writable_schema = ON", mutation),
                    )
                    with zipfile.ZipFile(hostile, "r") as archive:
                        tampered_database = root / f"{label}.db"
                        tampered_database.write_bytes(archive.read(DATABASE_ARCHIVE_NAME))
                    connection = sqlite3.connect(tampered_database)
                    try:
                        self.assertEqual(
                            connection.execute("PRAGMA integrity_check").fetchall(),
                            [("ok",)],
                        )
                        self.assertEqual(
                            connection.execute(
                                "SELECT value FROM knowledge_meta WHERE key = 'schema_version'"
                            ).fetchone(),
                            (str(lifecycle.SCHEMA_VERSION),),
                        )
                        changed_sql = connection.execute(
                            "SELECT sql FROM sqlite_master WHERE name = ?",
                            (object_name,),
                        ).fetchone()
                        self.assertIsNotNone(changed_sql)
                    finally:
                        connection.close()

                    with self.assertRaisesRegex(ValueError, "schema semantics"):
                        restore_knowledge_archive(
                            hostile,
                            root / f"{label}-target.db",
                            root / f"{label}-target.vault",
                        )
                    self.assertFalse((root / f"{label}-target.db").exists())

    def test_cross_process_lock_is_nonblocking_and_released(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "knowledge.db"
            source_root = Path(__file__).resolve().parents[2] / "src"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in (str(source_root), environment.get("PYTHONPATH", ""))
                if value
            )
            program = (
                "import sys\n"
                "from dynamic_firm.knowledge.locking import KnowledgeStateBusyError, KnowledgeStateLock\n"
                "try:\n"
                "    with KnowledgeStateLock(sys.argv[1], mode='exclusive'): pass\n"
                "except KnowledgeStateBusyError:\n"
                "    raise SystemExit(23)\n"
            )
            with KnowledgeStateLock(database, mode="shared"):
                blocked = subprocess.run(
                    [sys.executable, "-c", program, str(database)],
                    check=False,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(blocked.returncode, 23, blocked.stderr)
            released = subprocess.run(
                [sys.executable, "-c", program, str(database)],
                check=False,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(released.returncode, 0, released.stderr)

    def test_open_store_blocks_restore_and_delete_until_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root)
            archive_path = root / "knowledge.zip"
            export_knowledge_archive(database, vault, archive_path)
            authorization = authorize_knowledge_deletion(database, vault, confirmed=True)
            live = KnowledgeStore(database)
            try:
                with self.assertRaises(KnowledgeStateBusyError):
                    restore_knowledge_archive(
                        archive_path,
                        database,
                        vault,
                        overwrite=True,
                    )
                with self.assertRaises(KnowledgeStateBusyError):
                    delete_knowledge_state(database, vault, authorization=authorization)
            finally:
                live.close()
            deleted = delete_knowledge_state(database, vault, authorization=authorization)
            self.assertTrue(deleted.deleted)

    def test_interrupted_restore_and_delete_markers_recover_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root / "restore")
            archive_path = root / "knowledge.zip"
            export_knowledge_archive(database, vault, archive_path)
            transaction = "1" * 32
            os.replace(database, lifecycle._restore_backup_path(database, transaction))
            os.replace(vault, lifecycle._restore_backup_path(vault, transaction))
            database.write_bytes(b"partial restore database")
            vault.mkdir()
            (vault / "partial.txt").write_text("partial restore", encoding="utf-8")
            lifecycle._write_mutation_marker(
                database,
                {
                    "database": str(database),
                    "database_present": True,
                    "operation": "restore",
                    "phase": "prepared",
                    "schema_version": "noruct.knowledge-mutation.v1",
                    "sidecars": [],
                    "transaction": transaction,
                    "vault": str(vault),
                    "vault_present": True,
                },
            )

            restored = restore_knowledge_archive(
                archive_path,
                database,
                vault,
                overwrite=True,
            )
            self.assertTrue(restored.overwritten)
            self.assertFalse(knowledge_mutation_marker_path(database).exists())
            self.assertEqual(knowledge_diagnostics(database, vault).database_integrity, "ok")

            authorization = authorize_knowledge_deletion(database, vault, confirmed=True)
            delete_transaction = "2" * 32
            delete_sidecars = sorted(
                suffix
                for suffix in lifecycle._DATABASE_SIDECARS[1:]
                if Path(f"{database}{suffix}").exists()
            )
            os.replace(database, lifecycle._delete_tombstone_path(database, delete_transaction))
            lifecycle._write_mutation_marker(
                database,
                {
                    "database": str(database),
                    "database_present": True,
                    "operation": "delete",
                    "phase": "prepared",
                    "schema_version": "noruct.knowledge-mutation.v1",
                    "sidecars": delete_sidecars,
                    "transaction": delete_transaction,
                    "vault": str(vault),
                    "vault_present": True,
                },
            )
            deleted = delete_knowledge_state(database, vault, authorization=authorization)
            self.assertTrue(deleted.deleted)
            self.assertEqual(
                set(deleted.deleted_components),
                {"database", "vault"}
                | {f"database_{suffix[1:]}" for suffix in delete_sidecars},
            )
            self.assertFalse(database.exists())
            self.assertFalse(vault.exists())
            self.assertFalse(knowledge_mutation_marker_path(database).exists())

    def test_prepared_delete_recovery_rolls_back_partial_tombstone_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root)
            wal = Path(f"{database}-wal")
            wal.write_bytes(b"committed-wal-sentinel")
            transaction = "3" * 32
            database_tombstone = lifecycle._delete_tombstone_path(database, transaction)
            os.replace(database, database_tombstone)
            lifecycle._write_mutation_marker(
                database,
                {
                    "database": str(database),
                    "database_present": True,
                    "operation": "delete",
                    "phase": "prepared",
                    "schema_version": "noruct.knowledge-mutation.v1",
                    "sidecars": ["-wal"],
                    "transaction": transaction,
                    "vault": str(vault),
                    "vault_present": True,
                },
            )

            recovered = lifecycle._recover_delete_marker(database, vault)

            self.assertEqual(recovered, ())
            self.assertTrue(database.is_file())
            self.assertEqual(wal.read_bytes(), b"committed-wal-sentinel")
            self.assertTrue(vault.is_dir())
            self.assertFalse(database_tombstone.exists())
            self.assertFalse(knowledge_mutation_marker_path(database).exists())
            self.assertEqual(knowledge_diagnostics(database, vault).database_integrity, "ok")

    def test_prepared_delete_recovery_fails_closed_on_both_or_missing_components(self) -> None:
        for state in ("both", "missing"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                database, vault, _, _ = self._fixture(root)
                transaction = "4" * 32
                tombstone = lifecycle._delete_tombstone_path(database, transaction)
                os.replace(database, tombstone)
                if state == "both":
                    database.write_bytes(b"replacement-state")
                else:
                    tombstone.unlink()
                lifecycle._write_mutation_marker(
                    database,
                    {
                        "database": str(database),
                        "database_present": True,
                        "operation": "delete",
                        "phase": "prepared",
                        "schema_version": "noruct.knowledge-mutation.v1",
                        "sidecars": [],
                        "transaction": transaction,
                        "vault": str(vault),
                        "vault_present": True,
                    },
                )

                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    lifecycle._recover_delete_marker(database, vault)
                self.assertTrue(knowledge_mutation_marker_path(database).is_file())
                if state == "both":
                    self.assertEqual(database.read_bytes(), b"replacement-state")
                    self.assertTrue(tombstone.is_file())
                else:
                    self.assertFalse(database.exists())
                    self.assertFalse(tombstone.exists())

    def test_published_delete_recovery_never_deletes_a_recreated_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root)
            authorization = authorize_knowledge_deletion(database, vault, confirmed=True)
            transaction = "5" * 32
            database_tombstone = lifecycle._delete_tombstone_path(database, transaction)
            vault_tombstone = lifecycle._delete_tombstone_path(vault, transaction)
            os.replace(database, database_tombstone)
            os.replace(vault, vault_tombstone)
            lifecycle._write_mutation_marker(
                database,
                {
                    "database": str(database),
                    "database_present": True,
                    "operation": "delete",
                    "phase": "published",
                    "schema_version": "noruct.knowledge-mutation.v1",
                    "sidecars": [],
                    "transaction": transaction,
                    "vault": str(vault),
                    "vault_present": True,
                },
            )
            database_tombstone.unlink()
            replacement = b"newly-created-replacement-state"
            database.write_bytes(replacement)

            with self.assertRaisesRegex(ValueError, "replacement target"):
                delete_knowledge_state(database, vault, authorization=authorization)

            self.assertEqual(database.read_bytes(), replacement)
            self.assertTrue(vault_tombstone.is_dir())
            self.assertFalse(vault.exists())
            self.assertTrue(knowledge_mutation_marker_path(database).is_file())

    def test_published_delete_recovery_removes_only_remaining_expected_tombstones(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root)
            authorization = authorize_knowledge_deletion(database, vault, confirmed=True)
            transaction = "6" * 32
            database_tombstone = lifecycle._delete_tombstone_path(database, transaction)
            vault_tombstone = lifecycle._delete_tombstone_path(vault, transaction)
            os.replace(database, database_tombstone)
            os.replace(vault, vault_tombstone)
            lifecycle._write_mutation_marker(
                database,
                {
                    "database": str(database),
                    "database_present": True,
                    "operation": "delete",
                    "phase": "published",
                    "schema_version": "noruct.knowledge-mutation.v1",
                    "sidecars": [],
                    "transaction": transaction,
                    "vault": str(vault),
                    "vault_present": True,
                },
            )
            database_tombstone.unlink()

            deleted = delete_knowledge_state(database, vault, authorization=authorization)

            self.assertEqual(set(deleted.deleted_components), {"database", "vault"})
            self.assertFalse(database.exists())
            self.assertFalse(vault.exists())
            self.assertFalse(vault_tombstone.exists())
            self.assertFalse(knowledge_mutation_marker_path(database).exists())

    def test_deletion_requires_target_bound_authorization_and_removes_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root)
            Path(f"{database}-wal").write_bytes(b"wal")
            Path(f"{database}-shm").write_bytes(b"shm")
            with self.assertRaisesRegex(ValueError, "explicit authorization"):
                delete_knowledge_state(database, vault, authorization=None)
            with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                authorize_knowledge_deletion(database, vault, confirmed=False)
            wrong = authorize_knowledge_deletion(database, root / "other.vault", confirmed=True)
            with self.assertRaisesRegex(ValueError, "these targets"):
                delete_knowledge_state(database, vault, authorization=wrong)
            authorization = authorize_knowledge_deletion(database, vault, confirmed=True)

            record = delete_knowledge_state(database, vault, authorization=authorization)

            self.assertTrue(record.deleted)
            self.assertEqual(
                record.deleted_components,
                ("database", "database_wal", "database_shm", "vault"),
            )
            self.assertFalse(database.exists())
            self.assertFalse(Path(f"{database}-wal").exists())
            self.assertFalse(Path(f"{database}-shm").exists())
            self.assertFalse(vault.exists())

    def test_deletion_rejects_a_symlink_anywhere_in_the_vault(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, _, _ = self._fixture(root)
            (vault / "unsafe-link").symlink_to(root / "outside")
            authorization = authorize_knowledge_deletion(database, vault, confirmed=True)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                delete_knowledge_state(database, vault, authorization=authorization)
            self.assertTrue(database.exists())
            self.assertTrue(vault.exists())

    def test_diagnostics_are_content_free_and_report_missing_or_invalid_objects(self) -> None:
        secret = "secret-customer-omega-roadmap"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, vault, source_relative, derived_relative = self._fixture(root, content=secret)
            healthy = knowledge_diagnostics(database, vault)
            serialized = json.dumps(asdict(healthy), sort_keys=True)
            self.assertNotIn(secret, serialized)
            self.assertNotIn(source_relative, serialized)
            self.assertNotIn(derived_relative, serialized)
            self.assertEqual(healthy.database_integrity, "ok")
            self.assertEqual(healthy.referenced_object_count, 2)
            self.assertEqual(healthy.present_object_count, 2)
            (vault / source_relative).unlink()
            (vault / derived_relative).write_text("wrong", encoding="utf-8")
            damaged = knowledge_diagnostics(database, vault)
            self.assertEqual(damaged.missing_object_count, 1)
            self.assertEqual(damaged.invalid_object_count, 1)
            self.assertEqual(damaged.present_object_count, 0)


if __name__ == "__main__":
    unittest.main()
