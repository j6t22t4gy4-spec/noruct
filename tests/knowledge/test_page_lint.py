from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from dynamic_firm.knowledge import KnowledgePageLinter, KnowledgeStore


def _page(
    body: str,
    *,
    title: str = "Page",
    created: str = "2026-07-01",
    updated: str = "2026-07-01",
    extra: str = "",
) -> str:
    values = [
        "---",
        f"title: {title}",
        f"created: {created}",
        f"updated: {updated}",
        "type: synthesis",
    ]
    if extra:
        values.extend(extra.rstrip().splitlines())
    values.extend(("---", "", body.rstrip(), ""))
    return "\n".join(values)


class KnowledgePageLinterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.folder_root = self.root / "knowledge"
        self.folder_root.mkdir()
        self.store = KnowledgeStore(self.root / "knowledge.db")
        self.folder, _ = self.store.register_knowledge_folder(
            root_path=str(self.folder_root.resolve()),
            display_name="Knowledge Lint Fixture",
            access_scope="private",
        )
        self.linter = KnowledgePageLinter(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _write(self, relative: str, content: str) -> Path:
        target = self.folder_root / "pages" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def test_interlinked_indexed_pages_pass_without_mutating_sources(self) -> None:
        index = "# Index\n\n- [[alpha]]\n- [[nested/beta]]\n"
        alpha = _page("# Alpha\n\nSee [[nested/beta]].", title="Alpha")
        beta = _page("# Beta\n\nSee [[alpha]].", title="Beta")
        self._write("index.md", index)
        alpha_path = self._write("alpha.md", alpha)
        self._write("nested/beta.md", beta)

        report = self.linter.lint(
            folder_id=self.folder.folder_id,
            as_of=date(2026, 7, 31),
        )

        self.assertTrue(report.passed)
        self.assertFalse(report.truncated)
        self.assertEqual(report.scanned_pages, 3)
        self.assertEqual(report.issues, ())
        self.assertEqual(alpha_path.read_text(encoding="utf-8"), alpha)

    def test_missing_metadata_broken_links_orphans_and_index_fail_closed(self) -> None:
        self._write(
            "broken.md",
            "---\ntitle:\ncreated: not-a-date\ntype: note\n---\n\n[[missing]]\n",
        )
        report = self.linter.lint(
            folder_id=self.folder.folder_id,
            as_of=date(2026, 7, 31),
        )
        codes = {issue.code for issue in report.issues}
        self.assertFalse(report.passed)
        self.assertIn("FRONTMATTER_FIELD_EMPTY", codes)
        self.assertIn("FRONTMATTER_FIELD_MISSING", codes)
        self.assertIn("CREATED_DATE_INVALID", codes)
        self.assertIn("BROKEN_WIKILINK", codes)
        self.assertIn("ORPHAN_PAGE", codes)
        self.assertIn("INDEX_MISSING", codes)

    def test_staleness_uncertainty_contradiction_and_split_are_explicit(self) -> None:
        self._write("index.md", "# Index\n\n[[primary]]\n[[other]]\n")
        long_body = "# Primary\n\n[[other]]\n" + "\n".join(
            f"line {number}" for number in range(205)
        )
        self._write(
            "primary.md",
            _page(
                long_body,
                title="Primary",
                updated="2025-01-01",
                extra=(
                    "confidence: low\n"
                    "contested: true\n"
                    "contradictions: [other]"
                ),
            ),
        )
        self._write("other.md", _page("# Other\n\n[[primary]].", title="Other"))
        report = self.linter.lint(
            folder_id=self.folder.folder_id,
            as_of=date(2026, 7, 31),
        )
        codes = {issue.code for issue in report.issues}
        self.assertTrue(report.passed)
        self.assertTrue(
            {
                "STALE_PAGE",
                "LOW_CONFIDENCE_PAGE",
                "CONTESTED_PAGE",
                "DECLARED_CONTRADICTION",
                "PAGE_SPLIT_RECOMMENDED",
            }.issubset(codes)
        )

    def test_page_and_entry_limits_make_incomplete_lint_an_error(self) -> None:
        self._write("a.md", _page("# A", title="A"))
        self._write("b.md", _page("# B", title="B"))
        report = self.linter.lint(
            folder_id=self.folder.folder_id,
            as_of=date(2026, 7, 31),
            max_pages=1,
            max_entries=2,
        )
        self.assertTrue(report.truncated)
        self.assertFalse(report.passed)
        self.assertIn("SCAN_TRUNCATED", {issue.code for issue in report.issues})

    def test_symlinked_pages_and_directories_are_never_followed(self) -> None:
        pages = self.folder_root / "pages"
        pages.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text(_page("secret"), encoding="utf-8")
        try:
            (pages / "linked.md").symlink_to(outside / "secret.md")
            (pages / "linked-directory").symlink_to(
                outside, target_is_directory=True
            )
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available on this platform")

        report = self.linter.lint(folder_id=self.folder.folder_id)
        codes = {issue.code for issue in report.issues}
        self.assertFalse(report.passed)
        self.assertEqual(report.scanned_pages, 0)
        self.assertIn("SYMLINK_PAGE_REJECTED", codes)
        self.assertIn("SYMLINK_DIRECTORY_REJECTED", codes)

    def test_invalid_limits_and_unknown_folder_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "was not found"):
            self.linter.lint(folder_id="missing")
        with self.assertRaisesRegex(ValueError, "entry limit"):
            self.linter.lint(
                folder_id=self.folder.folder_id,
                max_pages=10,
                max_entries=9,
            )
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.linter.lint(
                folder_id=self.folder.folder_id,
                max_page_bytes=0,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
