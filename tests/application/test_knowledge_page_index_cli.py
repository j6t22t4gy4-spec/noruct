from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.knowledge import KnowledgeStore, knowledge_state_path


def _page(*, page_type: str, tags: str = "") -> str:
    tags_line = f"tags: [{tags}]\n" if tags else ""
    return (
        "---\n"
        "title: Fixture\n"
        "created: 2026-07-31\n"
        "updated: 2026-07-31\n"
        f"type: {page_type}\n"
        f"{tags_line}"
        "---\n\n# Fixture\n"
    )


class KnowledgePageIndexCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "runtime.db"
        self.folder_root = self.root / "knowledge"
        self.folder_root.mkdir()
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            folder, _ = store.register_knowledge_folder(
                root_path=str(self.folder_root.resolve()),
                display_name="Knowledge Index Fixture",
                access_scope="private",
            )
        self.folder_id = folder.folder_id

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, relative: str, content: str) -> None:
        path = self.folder_root / "pages" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _run(self, *arguments: str, json_output: bool = True) -> tuple[int, str, str]:
        output = io.StringIO()
        error = io.StringIO()
        argv = ["knowledge", *arguments, "--state", str(self.state)]
        if json_output:
            argv.append("--json")
        code = main(
            argv,
            provider_factory=lambda _config: (_ for _ in ()).throw(
                AssertionError("Knowledge page index must remain provider-free")
            ),
            stdin=io.StringIO(),
            stdout=output,
            stderr=error,
        )
        return code, output.getvalue(), error.getvalue()

    def test_preview_then_exact_confirmed_exclusive_publish(self) -> None:
        self._write("alpha.md", _page(page_type="synthesis", tags="research, design"))
        self._write("nested/beta.md", _page(page_type="note", tags="research"))
        target = self.folder_root / "pages" / "index.md"

        code, output, error = self._run("page-index-preview", "--folder-id", self.folder_id)

        self.assertEqual(code, EXIT_OK, error)
        preview = json.loads(output)
        self.assertEqual(preview["target_state"], "NEW")
        self.assertEqual(preview["indexed_page_count"], 2)
        self.assertEqual(preview["topic_count"], 2)
        self.assertIn("[[alpha]]", preview["markdown"])
        self.assertIn("[[nested/beta]]", preview["markdown"])
        self.assertFalse(target.exists())

        code, output, error = self._run(
            "page-index-publish", "--folder-id", self.folder_id,
            "--expected-sha256", preview["content_sha256"], "--confirm",
        )
        self.assertEqual(code, EXIT_OK, error)
        publication = json.loads(output)
        self.assertEqual(publication["content_sha256"], preview["content_sha256"])
        self.assertEqual(target.read_text(encoding="utf-8"), preview["markdown"])

        code, output, error = self._run("page-lint", "--folder-id", self.folder_id)
        self.assertEqual(code, EXIT_OK, error)
        lint = json.loads(output)
        self.assertTrue(lint["passed"])
        self.assertNotIn("INDEX_MISSING", {item["code"] for item in lint["issues"]})
        self.assertNotIn("ORPHAN_PAGE", {item["code"] for item in lint["issues"]})

        target.write_text("user controlled\n", encoding="utf-8")
        code, _output, error = self._run(
            "page-index-publish", "--folder-id", self.folder_id,
            "--expected-sha256", preview["content_sha256"], "--confirm",
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("not publishable", error)
        self.assertEqual(target.read_text(encoding="utf-8"), "user controlled\n")

    def test_confirmation_digest_and_unsafe_link_path_fail_without_creating_index(self) -> None:
        self._write("safe.md", _page(page_type="note"))
        target = self.folder_root / "pages" / "index.md"
        code, output, error = self._run("page-index-preview", "--folder-id", self.folder_id)
        self.assertEqual(code, EXIT_OK, error)
        preview = json.loads(output)

        code, _output, error = self._run(
            "page-index-publish", "--folder-id", self.folder_id,
            "--expected-sha256", preview["content_sha256"],
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("requires --confirm", error)
        self.assertFalse(target.exists())

        self._write("unsafe]].md", _page(page_type="note"))
        code, _output, error = self._run("page-index-preview", "--folder-id", self.folder_id)
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("cannot safely link", error)
        self.assertFalse(target.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
