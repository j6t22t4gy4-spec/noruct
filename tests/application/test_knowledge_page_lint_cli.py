from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.knowledge import KnowledgeStore, knowledge_state_path


def _page(
    body: str,
    *,
    title: str = "Page",
    created: str = "2026-07-01",
    updated: str = "2026-07-01",
    extra: str = "",
) -> str:
    lines = [
        "---",
        f"title: {title}",
        f"created: {created}",
        f"updated: {updated}",
        "type: synthesis",
    ]
    if extra:
        lines.extend(extra.rstrip().splitlines())
    lines.extend(("---", "", body.rstrip(), ""))
    return "\n".join(lines)


class KnowledgePageLintCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "runtime.db"
        self.database = knowledge_state_path(self.state)
        self.folder_root = self.root / "knowledge"
        self.folder_root.mkdir()
        with KnowledgeStore(self.database) as store:
            folder, _duplicate = store.register_knowledge_folder(
                root_path=str(self.folder_root.resolve()),
                display_name="Knowledge Lint CLI",
                access_scope="private",
            )
        self.folder_id = folder.folder_id

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, relative_path: str, content: str) -> Path:
        target = self.folder_root / "pages" / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def _run_cli(
        self,
        *arguments: str,
        json_output: bool = True,
    ) -> tuple[int, str, str]:
        argv = [
            "knowledge",
            "page-lint",
            "--folder-id",
            self.folder_id,
            *arguments,
            "--state",
            str(self.state),
        ]
        if json_output:
            argv.append("--json")
        output = io.StringIO()
        error = io.StringIO()
        code = main(
            argv,
            provider_factory=lambda _config: (_ for _ in ()).throw(
                AssertionError("read-only Knowledge page lint constructed a provider")
            ),
            stdin=io.StringIO(),
            stdout=output,
            stderr=error,
        )
        return code, output.getvalue(), error.getvalue()

    def _state_snapshot(self) -> dict[str, object]:
        pages: dict[str, tuple[str, int, int]] = {}
        pages_root = self.folder_root / "pages"
        if pages_root.exists():
            for path in sorted(item for item in pages_root.rglob("*") if item.is_file()):
                details = path.stat()
                pages[path.relative_to(self.folder_root).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    details.st_mtime_ns,
                    stat.S_IMODE(details.st_mode),
                )
        connection = sqlite3.connect(
            f"{self.database.as_uri()}?mode=ro",
            uri=True,
        )
        try:
            receipts = connection.execute(
                "SELECT publication_id, candidate_id, folder_id, relative_path, "
                "content_sha256, byte_size, published_at "
                "FROM knowledge_page_publications ORDER BY publication_id"
            ).fetchall()
            events = connection.execute(
                "SELECT COUNT(*) FROM knowledge_events"
            ).fetchone()[0]
        finally:
            connection.close()
        return {
            "database_sha256": hashlib.sha256(self.database.read_bytes()).hexdigest(),
            "events": int(events),
            "pages": pages,
            "receipts": receipts,
        }

    def test_healthy_pages_have_json_and_text_output_without_any_write(self) -> None:
        self._write("index.md", "# Index\n\n- [[alpha]]\n- [[nested/beta]]\n")
        self._write(
            "alpha.md",
            _page("# Alpha\n\nSee [[nested/beta]].", title="Alpha"),
        )
        self._write(
            "nested/beta.md",
            _page("# Beta\n\nSee [[alpha]].", title="Beta"),
        )
        before = self._state_snapshot()
        bounds = (
            "--as-of",
            "2026-07-31",
            "--stale-after-days",
            "120",
            "--max-pages",
            "10",
            "--max-entries",
            "20",
            "--max-page-bytes",
            "10000",
            "--max-total-bytes",
            "50000",
        )

        code, output, error = self._run_cli(*bounds)
        self.assertEqual(code, EXIT_OK, error)
        report = json.loads(output)
        self.assertTrue(report["passed"])
        self.assertFalse(report["truncated"])
        self.assertEqual(report["scanned_pages"], 3)
        self.assertEqual(report["issues"], [])

        code, output, error = self._run_cli(*bounds, json_output=False)
        self.assertEqual(code, EXIT_OK, error)
        self.assertIn("Knowledge page lint · PASS", output)
        self.assertIn("No Knowledge page health issues", output)
        self.assertEqual(self._state_snapshot(), before)

    def test_broken_link_and_frontmatter_errors_are_visible_and_fail(self) -> None:
        self._write(
            "broken.md",
            "---\ntitle:\ncreated: not-a-date\ntype: note\n---\n\n[[missing]]\n",
        )
        before = self._state_snapshot()

        code, output, error = self._run_cli("--as-of", "2026-07-31")
        self.assertEqual(code, EXIT_OK, error)
        report = json.loads(output)
        self.assertFalse(report["passed"])
        codes = {issue["code"] for issue in report["issues"]}
        self.assertTrue(
            {
                "BROKEN_WIKILINK",
                "CREATED_DATE_INVALID",
                "FRONTMATTER_FIELD_EMPTY",
                "FRONTMATTER_FIELD_MISSING",
                "INDEX_MISSING",
            }.issubset(codes)
        )
        self.assertEqual(self._state_snapshot(), before)

    def test_warning_only_health_is_reported_without_becoming_failure(self) -> None:
        self._write("index.md", "# Index\n\n[[primary]]\n[[other]]\n")
        self._write(
            "primary.md",
            _page(
                "# Primary\n\nSee [[other]].",
                title="Primary",
                updated="2025-01-01",
                extra="confidence: low\ncontested: true\ncontradictions: [other]",
            ),
        )
        self._write(
            "other.md",
            _page("# Other\n\nSee [[primary]].", title="Other"),
        )
        before = self._state_snapshot()

        code, output, error = self._run_cli("--as-of", "2026-07-31")
        self.assertEqual(code, EXIT_OK, error)
        report = json.loads(output)
        self.assertTrue(report["passed"])
        warning_codes = {
            issue["code"]
            for issue in report["issues"]
            if issue["severity"] == "WARNING"
        }
        self.assertTrue(
            {
                "CONTESTED_PAGE",
                "DECLARED_CONTRADICTION",
                "LOW_CONFIDENCE_PAGE",
                "STALE_PAGE",
            }.issubset(warning_codes)
        )
        self.assertEqual(self._state_snapshot(), before)

    def test_truncation_is_an_explicit_error_and_never_changes_pages(self) -> None:
        self._write("index.md", "# Index\n\n[[a]]\n[[b]]\n")
        self._write("a.md", _page("# A", title="A"))
        self._write("b.md", _page("# B", title="B"))
        before = self._state_snapshot()

        code, output, error = self._run_cli(
            "--max-pages",
            "1",
            "--max-entries",
            "10",
        )
        self.assertEqual(code, EXIT_OK, error)
        report = json.loads(output)
        self.assertFalse(report["passed"])
        self.assertTrue(report["truncated"])
        self.assertIn("SCAN_TRUNCATED", {item["code"] for item in report["issues"]})
        self.assertEqual(self._state_snapshot(), before)

    def test_invalid_dates_and_bounds_fail_closed_without_db_or_page_write(self) -> None:
        self._write("index.md", "# Index\n")
        before = self._state_snapshot()
        invalid_options = (
            ("--as-of", "not-a-date"),
            ("--stale-after-days", "0"),
            ("--max-pages", "0"),
            ("--max-pages", "2", "--max-entries", "1"),
            ("--max-page-bytes", "0"),
            ("--max-total-bytes", "0"),
        )

        for options in invalid_options:
            with self.subTest(options=options):
                code, _output, error = self._run_cli(*options)
                self.assertEqual(code, EXIT_INPUT)
                self.assertTrue(error.strip())
                self.assertEqual(self._state_snapshot(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
