from __future__ import annotations

import hashlib
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.knowledge import KnowledgePageService, KnowledgeStore


class KnowledgePageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.folder_root = self.root / "knowledge-folder"
        self.folder_root.mkdir()
        self.store = KnowledgeStore(self.root / "knowledge.db")
        self.folder, _ = self.store.register_knowledge_folder(
            root_path=str(self.folder_root.resolve()),
            display_name="Fixture Knowledge",
            access_scope="private",
        )
        self.pages = KnowledgePageService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _candidate(self, *, job_id: str = "job-page"):
        candidate = self.store.create_write_candidate(
            job_id=job_id,
            kind="SYNTHESIS",
            statement=(
                "A durable synthesis remains a candidate until the user accepts it."
            ),
        )
        return self.store.resolve_write_candidate(
            candidate.candidate_id,
            accept=True,
        )

    def test_accepted_candidate_is_previewed_then_exclusively_published(self) -> None:
        candidate = self._candidate()
        preview = self.pages.preview_candidate_page(
            candidate_id=candidate.candidate_id,
            folder_id=self.folder.folder_id,
            relative_path="pages/durable-synthesis.md",
            title="Durable Synthesis",
        )
        self.assertEqual(preview.target_state, "NEW")
        self.assertTrue(preview.publishable)
        self.assertIn("schema: noruct.knowledge-page.v1", preview.markdown)
        self.assertIn(candidate.candidate_id, preview.markdown)
        self.assertEqual(
            preview.content_sha256,
            hashlib.sha256(preview.markdown.encode("utf-8")).hexdigest(),
        )

        publication = self.pages.publish_candidate_page(
            candidate_id=candidate.candidate_id,
            folder_id=self.folder.folder_id,
            relative_path="pages/durable-synthesis.md",
            title="Durable Synthesis",
            expected_content_sha256=preview.content_sha256,
            confirm=True,
        )
        target = self.folder_root / "pages" / "durable-synthesis.md"
        self.assertEqual(target.read_text(encoding="utf-8"), preview.markdown)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(publication.content_sha256, preview.content_sha256)
        self.assertEqual(
            self.store.page_publication(candidate.candidate_id), publication
        )

        self.store.close()
        self.store = KnowledgeStore(self.root / "knowledge.db")
        self.pages = KnowledgePageService(self.store)
        self.assertEqual(
            self.store.page_publication(candidate.candidate_id), publication
        )

    def test_pending_rejected_scope_mismatch_and_unsafe_paths_fail_closed(self) -> None:
        pending = self.store.create_write_candidate(
            job_id="job-pending",
            statement="Pending result",
        )
        with self.assertRaisesRegex(ValueError, "accepted candidate"):
            self.pages.preview_candidate_page(
                candidate_id=pending.candidate_id,
                folder_id=self.folder.folder_id,
                relative_path="pages/pending.md",
                title="Pending",
            )
        rejected = self.store.create_write_candidate(
            job_id="job-rejected",
            statement="Rejected result",
        )
        self.store.resolve_write_candidate(rejected.candidate_id, accept=False)
        with self.assertRaisesRegex(ValueError, "accepted candidate"):
            self.pages.preview_candidate_page(
                candidate_id=rejected.candidate_id,
                folder_id=self.folder.folder_id,
                relative_path="pages/rejected.md",
                title="Rejected",
            )

        candidate = self._candidate(job_id="job-scope")
        public_root = self.root / "public-knowledge"
        public_root.mkdir()
        public_folder, _ = self.store.register_knowledge_folder(
            root_path=str(public_root.resolve()),
            display_name="Public",
            access_scope="public",
        )
        with self.assertRaisesRegex(ValueError, "match the accepted record scope"):
            self.pages.preview_candidate_page(
                candidate_id=candidate.candidate_id,
                folder_id=public_folder.folder_id,
                relative_path="pages/scope.md",
                title="Scope",
            )
        for unsafe in (
            "../escape.md",
            "/absolute.md",
            "notes/not-pages.md",
            "pages/.hidden.md",
            "pages/sub/../../escape.md",
            "pages/not-markdown.txt",
        ):
            with self.subTest(path=unsafe), self.assertRaisesRegex(
                ValueError, "Knowledge page path"
            ):
                self.pages.preview_candidate_page(
                    candidate_id=candidate.candidate_id,
                    folder_id=self.folder.folder_id,
                    relative_path=unsafe,
                    title="Unsafe",
                )

    def test_confirmation_digest_and_existing_content_are_never_bypassed(self) -> None:
        candidate = self._candidate()
        values = {
            "candidate_id": candidate.candidate_id,
            "folder_id": self.folder.folder_id,
            "relative_path": "pages/confirmed.md",
            "title": "Confirmed",
        }
        preview = self.pages.preview_candidate_page(**values)
        with self.assertRaisesRegex(ValueError, "explicit confirmation"):
            self.pages.publish_candidate_page(
                **values,
                expected_content_sha256=preview.content_sha256,
                confirm=False,
            )
        with self.assertRaisesRegex(ValueError, "digest changed"):
            self.pages.publish_candidate_page(
                **values,
                expected_content_sha256="0" * 64,
                confirm=True,
            )
        target = self.folder_root / "pages" / "confirmed.md"
        target.parent.mkdir()
        target.write_text("user-owned existing content\n", encoding="utf-8")
        blocked = self.pages.preview_candidate_page(**values)
        self.assertEqual(blocked.target_state, "CONFLICT_CONTENT")
        self.assertFalse(blocked.publishable)
        with self.assertRaisesRegex(ValueError, "not publishable"):
            self.pages.publish_candidate_page(
                **values,
                expected_content_sha256=blocked.content_sha256,
                confirm=True,
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "user-owned existing content\n")

    def test_exact_file_recovers_receipt_and_later_user_edits_are_preserved(self) -> None:
        candidate = self._candidate()
        values = {
            "candidate_id": candidate.candidate_id,
            "folder_id": self.folder.folder_id,
            "relative_path": "pages/recoverable.md",
            "title": "Recoverable",
        }
        preview = self.pages.preview_candidate_page(**values)
        target = self.folder_root / "pages" / "recoverable.md"
        target.parent.mkdir()
        target.write_text(preview.markdown, encoding="utf-8")
        recovered = self.pages.preview_candidate_page(**values)
        self.assertEqual(recovered.target_state, "EXACT_MATCH_RECOVERABLE")
        publication = self.pages.publish_candidate_page(
            **values,
            expected_content_sha256=recovered.content_sha256,
            confirm=True,
        )

        target.write_text("user edited this page\n", encoding="utf-8")
        after_edit = self.pages.preview_candidate_page(**values)
        self.assertEqual(after_edit.target_state, "PUBLISHED_USER_CONTROLLED")
        self.assertFalse(after_edit.publishable)
        replayed = self.pages.publish_candidate_page(
            **values,
            expected_content_sha256=after_edit.content_sha256,
            confirm=True,
        )
        self.assertEqual(replayed, publication)
        self.assertEqual(target.read_text(encoding="utf-8"), "user edited this page\n")

    def test_store_authority_rejects_publication_service_bypasses(self) -> None:
        candidate = self._candidate(job_id="job-store-authority")
        values = {
            "candidate_id": candidate.candidate_id,
            "folder_id": self.folder.folder_id,
            "relative_path": "pages/authority.md",
            "title": "Authority",
        }
        preview = self.pages.preview_candidate_page(**values)
        mutation = {
            "candidate_id": candidate.candidate_id,
            "accepted_record_id": candidate.accepted_record_id,
            "folder_id": self.folder.folder_id,
            "relative_path": preview.relative_path,
            "title": preview.title,
            "content_sha256": preview.content_sha256,
            "byte_size": preview.byte_size,
        }
        with self.assertRaisesRegex(ValueError, "page parent"):
            self.store.record_page_publication(**mutation)

        target = self.folder_root / preview.relative_path
        target.parent.mkdir(parents=True)
        target.write_text(preview.markdown, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity is not normalized"):
            self.store.record_page_publication(
                **{**mutation, "relative_path": "pages//authority.md"}
            )
        with self.assertRaisesRegex(ValueError, "does not match accepted content"):
            self.store.record_page_publication(
                **{**mutation, "content_sha256": "0" * 64}
            )

        public_root = self.root / "public-store-bypass"
        public_root.mkdir()
        public_folder, _ = self.store.register_knowledge_folder(
            root_path=str(public_root.resolve()),
            display_name="Public Store Bypass",
            access_scope="public",
        )
        public_target = public_root / preview.relative_path
        public_target.parent.mkdir(parents=True)
        public_target.write_text(preview.markdown, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "match the record scope"):
            self.store.record_page_publication(
                **{**mutation, "folder_id": public_folder.folder_id}
            )

        publication = self.pages.publish_candidate_page(
            **values,
            expected_content_sha256=preview.content_sha256,
            confirm=True,
        )
        self.assertEqual(publication.relative_path, preview.relative_path)

    def test_store_authority_rejects_symlinked_page_parent(self) -> None:
        candidate = self._candidate(job_id="job-store-symlink")
        values = {
            "candidate_id": candidate.candidate_id,
            "folder_id": self.folder.folder_id,
            "relative_path": "pages/linked/authority.md",
            "title": "Linked Authority",
        }
        preview = self.pages.preview_candidate_page(**values)
        outside = self.root / "outside-pages"
        outside.mkdir()
        (outside / "authority.md").write_text(preview.markdown, encoding="utf-8")
        pages_root = self.folder_root / "pages"
        pages_root.mkdir()
        try:
            (pages_root / "linked").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available on this platform")
        with self.assertRaisesRegex(ValueError, "non-symlink directory"):
            self.store.record_page_publication(
                candidate_id=candidate.candidate_id,
                accepted_record_id=candidate.accepted_record_id,
                folder_id=self.folder.folder_id,
                relative_path=preview.relative_path,
                title=preview.title,
                content_sha256=preview.content_sha256,
                byte_size=preview.byte_size,
            )

    def test_untrusted_provenance_values_cannot_inject_frontmatter(self) -> None:
        candidate = self._candidate(job_id="job-value\nmalicious: true")
        preview = self.pages.preview_candidate_page(
            candidate_id=candidate.candidate_id,
            folder_id=self.folder.folder_id,
            relative_path="pages/quoted-provenance.md",
            title="Quoted Provenance",
        )
        frontmatter = preview.markdown.split("---", 2)[1]
        self.assertIn(
            'source_job_id: "job-value\\nmalicious: true"', frontmatter
        )
        self.assertNotIn("\nmalicious: true\n", frontmatter)

    def test_schema_seven_database_adds_page_receipts_on_reopen(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.root / "knowledge.db")
        connection.execute("DROP TABLE knowledge_page_publications")
        connection.execute(
            "UPDATE knowledge_meta SET value = '7' WHERE key = 'schema_version'"
        )
        connection.commit()
        connection.close()

        self.store = KnowledgeStore(self.root / "knowledge.db")
        version = self.store._conn.execute(  # noqa: SLF001 - migration regression
            "SELECT value FROM knowledge_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        table = self.store._conn.execute(  # noqa: SLF001 - migration regression
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='knowledge_page_publications'"
        ).fetchone()
        self.assertEqual(version, "8")
        self.assertIsNotNone(table)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
