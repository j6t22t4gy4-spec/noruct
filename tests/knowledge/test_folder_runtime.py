from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from dynamic_firm.knowledge import (
    KnowledgeFolderScanControl,
    KnowledgeFolderEntryStatus,
    KnowledgeFolderService,
    KnowledgeFolderWatcher,
    KnowledgeStore,
    KnowledgeVault,
)
from dynamic_firm.knowledge.service import UserKnowledgeService
from dynamic_firm.knowledge.lifecycle import (
    export_knowledge_archive,
    restore_knowledge_archive,
)


class KnowledgeFolderRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.notes = self.root / "notes"
        self.notes.mkdir()
        self.store = KnowledgeStore(self.root / "knowledge.db")
        self.vault = KnowledgeVault(self.root / "vault")
        self.service = KnowledgeFolderService(self.store, self.vault)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _register(self):
        folder, duplicate = self.service.register(self.notes)
        self.assertFalse(duplicate)
        return folder

    def test_scan_indexes_raw_text_without_copying_every_file_to_the_vault(self) -> None:
        (self.notes / "pricing.md").write_text(
            "Pricing review is scheduled for August 20.", encoding="utf-8"
        )
        folder = self._register()

        report = self.service.scan(folder.folder_id)

        self.assertEqual((report.scanned_files, report.ready_files), (1, 1))
        self.assertEqual(self.store.list_assets(), ())
        entry = self.store.list_knowledge_folder_entries(folder.folder_id)[0]
        self.assertEqual(entry.relative_path, "pricing.md")
        self.assertEqual(entry.index_status, KnowledgeFolderEntryStatus.READY)

        pack = UserKnowledgeService(self.store, self.vault).build_evidence_pack(
            "pricing August"
        )
        self.assertEqual(len(pack.items), 1)
        self.assertEqual(pack.items[0].source_type, "folder_file")
        self.assertEqual(pack.items[0].location["relative_path"], "pricing.md")
        self.assertIsNotNone(pack.items[0].asset_id)
        self.assertEqual(len(self.store.list_assets()), 1)

    def test_retrieval_candidate_bound_does_not_starve_folder_authority(self) -> None:
        (self.notes / "folder.md").write_text("folder evidence", encoding="utf-8")
        folder = self._register()
        self.service.scan(folder.folder_id)
        managed = self.root / "managed.md"
        managed.write_text("managed evidence", encoding="utf-8")
        UserKnowledgeService(self.store, self.vault).ingest(managed)
        self.store.create_record(kind="NOTE", statement="first record")
        self.store.create_record(kind="NOTE", statement="second record")

        rows = self.store.retrieval_rows(access_scope="private", limit=3)

        self.assertEqual(
            {str(item["source_type"]) for item in rows},
            {"representation_chunk", "knowledge_record", "folder_file"},
        )

    def test_current_folder_entry_replaces_its_derived_snapshot_in_retrieval_candidates(self) -> None:
        (self.notes / "strategy.md").write_text("Pricing strategy evidence.", encoding="utf-8")
        folder = self._register()
        self.service.scan(folder.folder_id)
        entry = self.store.list_knowledge_folder_entries(folder.folder_id)[0]
        self.service.open_entry(entry.entry_id)

        rows = self.store.retrieval_rows(access_scope="private", limit=10_000)

        self.assertEqual([str(row["source_type"]) for row in rows], ["folder_file"])
        pack = UserKnowledgeService(self.store, self.vault).build_evidence_pack(
            "pricing strategy", persist=False
        )
        self.assertEqual(pack.items[0].source_type, "folder_file")

    def test_fts5_folder_projection_is_safe_optional_and_reconciled(self) -> None:
        (self.notes / "pricing-strategy-target-007.md").write_text(
            "Pricing strategy evidence for target-007.", encoding="utf-8"
        )
        (self.notes / "unrelated.md").write_text(
            "A separate engineering note.", encoding="utf-8"
        )
        (self.notes / "korean-pricing.md").write_text(
            "가격 전략 검토 근거입니다.", encoding="utf-8"
        )
        folder = self._register()
        self.service.scan(folder.folder_id)

        if not self.store.folder_fts5_available():
            self.assertIsNone(
                self.store.indexed_folder_retrieval_rows(
                    access_scope="private", query="pricing strategy target-007"
                )
            )
            return

        indexed = self.store.indexed_folder_retrieval_rows(
            access_scope="private", query="pricing strategy target-007"
        )
        assert indexed is not None
        self.assertEqual([row["relative_path"] for row in indexed], ["pricing-strategy-target-007.md"])
        # Operators embedded in a user query are converted into inert quoted
        # terms before SQLite receives MATCH syntax.
        safe = self.store.indexed_folder_retrieval_rows(
            access_scope="private", query='pricing OR *'
        )
        self.assertIsNotNone(safe)
        # unicode61 handles whitespace-delimited CJK and the local compact
        # candidate path bridges a continuous compound to spaced source text.
        cjk_indexed = self.store.indexed_folder_retrieval_rows(
            access_scope="private", query="가격 전략"
        )
        assert cjk_indexed is not None
        self.assertEqual([row["relative_path"] for row in cjk_indexed], ["korean-pricing.md"])
        compact = self.store.indexed_folder_retrieval_rows(
            access_scope="private", query="가격전략"
        )
        assert compact is not None
        self.assertEqual([row["relative_path"] for row in compact], ["korean-pricing.md"])
        postposition = self.store.indexed_folder_retrieval_rows(
            access_scope="private", query="가격전략을"
        )
        assert postposition is not None
        self.assertEqual([row["relative_path"] for row in postposition], ["korean-pricing.md"])
        korean_pack = UserKnowledgeService(self.store, self.vault).build_evidence_pack(
            "가격전략을", persist=False
        )
        self.assertEqual(korean_pack.query, "가격전략을")
        self.assertEqual(korean_pack.items[0].location["relative_path"], "korean-pricing.md")

        pack = UserKnowledgeService(self.store, self.vault).build_evidence_pack(
            "pricing strategy target-007", persist=False
        )
        self.assertEqual(pack.items[0].location["relative_path"], "pricing-strategy-target-007.md")

        (self.notes / "pricing-strategy-target-007.md").unlink()
        self.service.scan(folder.folder_id)
        self.assertEqual(
            self.store.indexed_folder_retrieval_rows(
                access_scope="private", query="pricing strategy target-007"
            ),
            (),
        )

    def test_cjk_candidate_index_handles_one_connected_compound_surface_variant(self) -> None:
        (self.notes / "korean-pricing-change.md").write_text(
            "가격 전략 변경 검토 근거입니다.", encoding="utf-8"
        )
        folder = self._register()
        self.service.scan(folder.folder_id)
        if not self.store.folder_fts5_available():
            self.assertIsNone(
                self.store.indexed_folder_retrieval_rows(
                    access_scope="private", query="가격전략의변경을"
                )
            )
            return
        candidates = self.store.indexed_folder_retrieval_rows(
            access_scope="private", query="가격전략의변경을"
        )
        assert candidates is not None
        self.assertEqual(
            [row["relative_path"] for row in candidates],
            ["korean-pricing-change.md"],
        )

    def test_preview_is_redacted_structured_and_watcher_only_scans_after_change(self) -> None:
        source = self.notes / "plan.md"
        source.write_text(
            "# Plan\n\n| A | B |\n|---|---|\npassword=secret-value\n",
            encoding="utf-8",
        )
        folder = self._register()
        self.service.scan(folder.folder_id)
        entry = self.store.list_knowledge_folder_entries(folder.folder_id)[0]
        preview = self.service.preview_entry(entry.entry_id)
        self.assertTrue(preview.redacted)
        self.assertEqual(preview.structure.table_count, 1)
        watcher = KnowledgeFolderWatcher(self.service, interval_seconds=0.25)
        self.assertFalse(watcher.poll_once(folder.folder_id).changed)
        source.write_text("# Plan\n가격 전략\n", encoding="utf-8")
        self.assertTrue(watcher.poll_once(folder.folder_id).changed)

    def test_scan_cancel_keeps_observed_entries_and_never_inferrs_unseen_deletes(self) -> None:
        for index in range(80):
            (self.notes / f"note-{index:03d}.md").write_text(
                f"Local note {index}.", encoding="utf-8"
            )
        folder = self._register()
        control = KnowledgeFolderScanControl()
        progress = []

        def observe(event) -> None:
            progress.append(event)
            if event.phase == "SCANNING" and event.scanned_files >= 25:
                control.cancel()

        cancelled = self.service.scan(
            folder.folder_id,
            control=control,
            progress=observe,
        )

        self.assertTrue(cancelled.cancelled)
        self.assertTrue(cancelled.truncated)
        self.assertEqual(cancelled.scanned_files, 25)
        self.assertEqual(cancelled.deleted_entries, 0)
        self.assertEqual(self.store.counts()["knowledge_folder_entries"], 25)
        self.assertEqual(progress[0].phase, "STARTED")
        self.assertEqual(progress[-1].phase, "CANCELLED")

        completed = self.service.scan(folder.folder_id)
        self.assertFalse(completed.cancelled)
        self.assertFalse(completed.truncated)
        self.assertEqual(completed.scanned_files, 80)
        self.assertEqual(completed.deleted_entries, 0)

    def test_rename_preserves_entry_identity_and_change_invalidates_snapshot(self) -> None:
        original = self.notes / "draft.md"
        original.write_text("The launch date is September 1.", encoding="utf-8")
        folder = self._register()
        self.service.scan(folder.folder_id)
        before = self.store.list_knowledge_folder_entries(folder.folder_id)[0]
        self.service.open_entry(before.entry_id)
        snapshotted = self.store.folder_entry(before.entry_id)
        assert snapshotted is not None
        self.assertIsNotNone(snapshotted.snapshot_asset_id)

        renamed = self.notes / "launch.md"
        original.rename(renamed)
        rename_report = self.service.scan(folder.folder_id)
        after_rename = self.store.list_knowledge_folder_entries(folder.folder_id)[0]
        self.assertEqual(rename_report.renamed_entries, 1)
        self.assertEqual(after_rename.entry_id, before.entry_id)
        self.assertEqual(after_rename.relative_path, "launch.md")

        renamed.write_text("The launch date is September 8.", encoding="utf-8")
        update_report = self.service.scan(folder.folder_id)
        after_update = self.store.folder_entry(before.entry_id)
        assert after_update is not None
        self.assertEqual(update_report.updated_entries, 1)
        self.assertGreater(after_update.revision, after_rename.revision)
        self.assertIsNone(after_update.snapshot_asset_id)

    def test_unchanged_entry_reuses_the_prior_index_without_rehashing_or_extracting(self) -> None:
        source = self.notes / "stable.md"
        source.write_text("Stable evidence should retain its index.", encoding="utf-8")
        folder = self._register()
        self.service.scan(folder.folder_id)
        before = self.store.list_knowledge_folder_entries(folder.folder_id)[0]

        with mock.patch(
            "dynamic_firm.knowledge.folder_service.sha256_file",
            side_effect=AssertionError("unchanged entry must not be rehashed"),
        ), mock.patch.object(
            self.service._text,
            "extract",
            side_effect=AssertionError("unchanged entry must not be re-extracted"),
        ):
            report = self.service.scan(folder.folder_id)

        after = self.store.list_knowledge_folder_entries(folder.folder_id)[0]
        self.assertEqual(report.unchanged_entries, 1)
        self.assertEqual(after.entry_id, before.entry_id)
        self.assertEqual(after.revision, before.revision)
        self.assertTrue(after.indexer_revision.startswith("folder-index-v2|"))

    def test_snapshot_binding_rejects_an_asset_from_different_content(self) -> None:
        (self.notes / "first.md").write_text("first evidence", encoding="utf-8")
        (self.notes / "second.md").write_text("second evidence", encoding="utf-8")
        folder = self._register()
        self.service.scan(folder.folder_id)
        first, second = self.store.list_knowledge_folder_entries(folder.folder_id)
        first_asset = self.service.snapshot_entry(first.entry_id)

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.store.bind_folder_entry_snapshot(second.entry_id, first_asset.asset_id)

    def test_pause_relink_resume_and_remove_only_change_local_registration(self) -> None:
        source = self.notes / "note.md"
        source.write_text("Local folder authority remains with the user.", encoding="utf-8")
        folder = self._register()
        self.service.scan(folder.folder_id)
        replacement = self.root / "replacement"
        replacement.mkdir()
        (replacement / "next.md").write_text("Replacement folder content.", encoding="utf-8")

        paused = self.service.pause(folder.folder_id)
        self.assertEqual(paused.status.name, "PAUSED")
        with self.assertRaisesRegex(ValueError, "paused"):
            self.service.scan(folder.folder_id)

        relinked = self.service.relink(folder.folder_id, replacement)
        self.assertEqual(Path(relinked.root_path), replacement.resolve())
        self.assertEqual(relinked.last_scan_status, "RELINKED")
        resumed = self.service.resume(folder.folder_id)
        self.assertEqual(resumed.status.name, "ACTIVE")
        report = self.service.scan(folder.folder_id)
        self.assertEqual(report.ready_files, 1)
        self.assertTrue(source.exists())
        self.assertTrue((replacement / "next.md").exists())

        self.assertTrue(self.service.remove(folder.folder_id))
        self.assertIsNone(self.store.knowledge_folder(folder.folder_id))
        self.assertTrue(source.exists())
        self.assertTrue((replacement / "next.md").exists())

    def test_delete_is_reconciled_but_incomplete_scan_does_not_delete_unseen_entries(self) -> None:
        first = self.notes / "a.md"
        second = self.notes / "b.md"
        first.write_text("alpha evidence", encoding="utf-8")
        second.write_text("beta evidence", encoding="utf-8")
        folder = self._register()
        self.service.scan(folder.folder_id)

        incomplete = self.service.scan(folder.folder_id, max_files=1)
        self.assertTrue(incomplete.truncated)
        self.assertEqual(
            len(self.store.list_knowledge_folder_entries(folder.folder_id)), 2
        )

        second.unlink()
        complete = self.service.scan(folder.folder_id)
        self.assertFalse(complete.truncated)
        self.assertEqual(complete.deleted_entries, 1)
        entries = self.store.list_knowledge_folder_entries(
            folder.folder_id, include_deleted=True
        )
        deleted = next(item for item in entries if item.relative_path == "b.md")
        self.assertEqual(deleted.index_status, KnowledgeFolderEntryStatus.DELETED)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires optional privileges")
    def test_symlinks_are_never_followed(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("must not be indexed", encoding="utf-8")
        os.symlink(outside, self.notes / "linked.txt")
        folder = self._register()

        report = self.service.scan(folder.folder_id)

        self.assertEqual(report.skipped_symlinks, 1)
        self.assertEqual(
            self.store.list_knowledge_folder_entries(folder.folder_id), ()
        )

    def test_preflight_and_scan_exclude_secret_like_paths_without_reading_content(self) -> None:
        (self.notes / "strategy.md").write_text("usable strategy evidence", encoding="utf-8")
        (self.notes / ".env").write_text("API_KEY=never-index", encoding="utf-8")
        (self.notes / "credentials.json").write_text('{"token":"never-index"}', encoding="utf-8")
        private = self.notes / "secrets"
        private.mkdir()
        (private / "prod.pem").write_text("never-index", encoding="utf-8")

        preflight = KnowledgeFolderService.preview_root(self.notes)
        self.assertEqual(preflight.candidate_files, 1)
        self.assertEqual(preflight.ignored_secret_like, 3)
        self.assertIn("IGNORED_SECRET_LIKE", {item.classification for item in preflight.samples})

        folder = self._register()
        report = self.service.scan(folder.folder_id)
        self.assertEqual(report.scanned_files, 1)
        self.assertEqual(report.skipped_secret_like, 3)
        self.assertEqual(
            [item.relative_path for item in self.store.list_knowledge_folder_entries(folder.folder_id)],
            ["strategy.md"],
        )

    def test_persisted_user_ignore_globs_apply_to_preview_scan_and_future_reconciliation(self) -> None:
        (self.notes / "strategy.md").write_text("usable strategy evidence", encoding="utf-8")
        draft = self.notes / "draft"
        draft.mkdir()
        (draft / "private.md").write_text("must not be indexed", encoding="utf-8")
        (self.notes / "scratch.tmp").write_text("must not be indexed", encoding="utf-8")

        preflight = KnowledgeFolderService.preview_root(
            self.notes,
            ignore_globs=("draft", "*.tmp"),
        )
        self.assertEqual(preflight.candidate_files, 1)
        self.assertEqual(preflight.ignored_user_patterns, 2)
        self.assertIn("IGNORED_USER_PATTERN", {item.classification for item in preflight.samples})

        folder, duplicate = self.service.register(
            self.notes,
            ignore_globs=("draft", "*.tmp"),
        )
        self.assertFalse(duplicate)
        self.assertEqual(folder.ignore_globs, ("draft", "*.tmp"))
        report = self.service.scan(folder.folder_id)
        self.assertEqual(report.skipped_user_ignored, 2)
        self.assertEqual(
            [item.relative_path for item in self.store.list_knowledge_folder_entries(folder.folder_id)],
            ["strategy.md"],
        )

        updated = self.service.set_ignore_globs(folder.folder_id, ignore_globs=("*.md",))
        self.assertEqual(updated.ignore_globs, ("*.md",))
        second = self.service.scan(folder.folder_id)
        self.assertEqual(second.scanned_files, 1)
        self.assertEqual(second.skipped_user_ignored, 2)
        self.assertEqual(
            [item.relative_path for item in self.store.list_knowledge_folder_entries(folder.folder_id)],
            ["scratch.tmp"],
        )

    def test_user_ignore_glob_rejects_absolute_parent_and_overlong_rules(self) -> None:
        for pattern in ("/private", "../private", "folder\\private", "x" * 257):
            with self.assertRaisesRegex(ValueError, "relative POSIX glob"):
                self.service.register(self.notes, ignore_globs=(pattern,))

    def test_document_extraction_is_explicit_bounded_and_reuses_local_worker(self) -> None:
        source = self.notes / "strategy.docx"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr(
                "word/document.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Folder-native DOCX strategy evidence.</w:t></w:r></w:p></w:body>
</w:document>""",
            )
        folder = self._register()

        default_report = self.service.scan(folder.folder_id)
        default_entry = self.store.list_knowledge_folder_entries(folder.folder_id)[0]
        self.assertEqual(default_report.document_files, 0)
        self.assertEqual(default_entry.index_status, KnowledgeFolderEntryStatus.METADATA_ONLY)

        extracted_report = self.service.scan(
            folder.folder_id,
            extract_documents=True,
            max_document_files=1,
        )
        extracted_entry = self.store.folder_entry(default_entry.entry_id)
        assert extracted_entry is not None
        self.assertEqual(extracted_report.document_files, 1)
        self.assertEqual(extracted_entry.index_status, KnowledgeFolderEntryStatus.READY)
        self.assertIn("DOCX strategy evidence", extracted_entry.index_text)

    def test_export_and_restore_preserve_folder_index_and_evidence_snapshot(self) -> None:
        (self.notes / "evidence.md").write_text(
            "Customer evidence is reviewed monthly.", encoding="utf-8"
        )
        folder = self._register()
        self.service.scan(folder.folder_id)
        entry = self.store.list_knowledge_folder_entries(folder.folder_id)[0]
        self.service.open_entry(entry.entry_id)
        archive = self.root / "knowledge.noruct"
        database = self.store.path
        self.store.close()

        export_knowledge_archive(database, self.vault.root, archive)
        restored_database = self.root / "restored.db"
        restored_vault = self.root / "restored.vault"
        restore_knowledge_archive(
            archive,
            restored_database,
            restored_vault,
        )
        self.store = KnowledgeStore(restored_database)

        restored_folder = self.store.knowledge_folder(folder.folder_id)
        self.assertIsNotNone(restored_folder)
        restored_entry = self.store.folder_entry(entry.entry_id)
        self.assertIsNotNone(restored_entry)
        assert restored_entry is not None
        self.assertEqual(restored_entry.relative_path, "evidence.md")
        self.assertIsNotNone(restored_entry.snapshot_asset_id)


if __name__ == "__main__":
    unittest.main()
