from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.knowledge import KnowledgeStore, knowledge_state_path


class KnowledgeCandidatePageCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "runtime.db"
        self.folder_root = self.root / "user-knowledge"
        self.folder_root.mkdir()
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            folder, _duplicate = store.register_knowledge_folder(
                root_path=str(self.folder_root.resolve()),
                display_name="User Knowledge",
                access_scope="private",
            )
            candidate = store.create_write_candidate(
                job_id="job-page-cli",
                kind="SYNTHESIS",
                statement="Accepted synthesis becomes a page only after digest confirmation.",
            )
            accepted = store.resolve_write_candidate(
                candidate.candidate_id,
                accept=True,
            )
            second = store.create_write_candidate(
                job_id="job-page-cli-second",
                kind="SYNTHESIS",
                statement="A second accepted synthesis stays an independent Knowledge record.",
            )
            second_accepted = store.resolve_write_candidate(
                second.candidate_id,
                accept=True,
            )
            pending = store.create_write_candidate(
                job_id="job-page-cli-pending",
                kind="SYNTHESIS",
                statement="Pending synthesis must remain unpublished.",
            )
        self.folder_id = folder.folder_id
        self.candidate_id = accepted.candidate_id
        self.accepted_record_id = accepted.accepted_record_id
        self.second_candidate_id = second_accepted.candidate_id
        self.second_accepted_record_id = second_accepted.accepted_record_id
        self.pending_candidate_id = pending.candidate_id

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(
        self,
        *arguments: str,
        json_output: bool = True,
    ) -> tuple[int, str, str]:
        output = io.StringIO()
        error = io.StringIO()
        argv = [*arguments, "--state", str(self.state)]
        if json_output:
            argv.append("--json")
        code = main(
            argv,
            provider_factory=lambda _config: (_ for _ in ()).throw(
                AssertionError("provider-free Knowledge page command constructed a provider")
            ),
            stdin=io.StringIO(),
            stdout=output,
            stderr=error,
        )
        return code, output.getvalue(), error.getvalue()

    def page_arguments(self, *, relative_path: str = "pages/accepted-synthesis.md") -> tuple[str, ...]:
        return (
            self.candidate_id,
            "--folder-id",
            self.folder_id,
            "--path",
            relative_path,
            "--title",
            "Accepted Synthesis",
        )

    def test_preview_and_publish_have_json_and_text_surfaces_without_overwrite(self) -> None:
        relative_path = "pages/accepted-synthesis.md"
        target = self.folder_root / relative_path
        code, output, error = self.run_cli(
            "knowledge",
            "candidate-page-preview",
            *self.page_arguments(relative_path=relative_path),
        )
        self.assertEqual(code, EXIT_OK, error)
        preview = json.loads(output)
        self.assertEqual(preview["candidate_id"], self.candidate_id)
        self.assertEqual(preview["accepted_record_id"], self.accepted_record_id)
        self.assertEqual(preview["target_state"], "NEW")
        self.assertTrue(preview["publishable"])
        self.assertIn("schema: noruct.knowledge-page.v1", preview["markdown"])
        self.assertFalse(target.exists())
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            self.assertIsNone(store.page_publication(self.candidate_id))

        code, human, error = self.run_cli(
            "knowledge",
            "candidate-page-preview",
            *self.page_arguments(relative_path=relative_path),
            json_output=False,
        )
        self.assertEqual(code, EXIT_OK, error)
        self.assertIn("Knowledge page preview", human)
        self.assertIn("publishable=yes", human)
        self.assertIn("# Accepted Synthesis", human)
        self.assertFalse(target.exists())

        code, output, error = self.run_cli(
            "knowledge",
            "candidate-page-publish",
            *self.page_arguments(relative_path=relative_path),
            "--expected-sha256",
            preview["content_sha256"].upper(),
            "--confirm",
        )
        self.assertEqual(code, EXIT_OK, error)
        publication = json.loads(output)
        self.assertEqual(publication["content_sha256"], preview["content_sha256"])
        self.assertEqual(target.read_text(encoding="utf-8"), preview["markdown"])

        target.write_text("user-controlled edit\n", encoding="utf-8")
        code, human, error = self.run_cli(
            "knowledge",
            "candidate-page-publish",
            *self.page_arguments(relative_path=relative_path),
            "--expected-sha256",
            preview["content_sha256"],
            "--confirm",
            json_output=False,
        )
        self.assertEqual(code, EXIT_OK, error)
        self.assertIn("Knowledge page published", human)
        self.assertIn("future file edits remain user-controlled", human)
        self.assertEqual(target.read_text(encoding="utf-8"), "user-controlled edit\n")

    def test_review_exposes_only_repeat_metadata_without_accepting_or_publishing(self) -> None:
        secret = "REVIEW-SYNTHESIS-SENTINEL"
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            first = store.create_write_candidate(
                job_id="job-review-a",
                kind="SYNTHESIS",
                statement=secret,
            )
            second = store.create_write_candidate(
                job_id="job-review-b",
                kind="SYNTHESIS",
                statement=secret.upper(),
            )

        code, output, error = self.run_cli("knowledge", "review")

        self.assertEqual(code, EXIT_OK, error)
        payload = json.loads(output)
        self.assertEqual(len(payload["syntheses"]), 1)
        self.assertEqual(payload["lexical_near_duplicates"], [])
        lead = payload["syntheses"][0]
        self.assertEqual(set(lead["candidate_ids"]), {first.candidate_id, second.candidate_id})
        self.assertEqual(set(lead["job_ids"]), {"job-review-a", "job-review-b"})
        self.assertNotIn(secret, output)
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            self.assertEqual(store.write_candidate(first.candidate_id).status, "PENDING")  # type: ignore[union-attr]
            self.assertEqual(store.write_candidate(second.candidate_id).status, "PENDING")  # type: ignore[union-attr]
            self.assertIsNone(store.page_publication(first.candidate_id))

        code, human, error = self.run_cli("knowledge", "review", json_output=False)
        self.assertEqual(code, EXIT_OK, error)
        self.assertIn("Knowledge review", human)
        self.assertIn("Read-only review", human)

    def test_review_emits_only_high_threshold_lexical_leads_from_distinct_jobs(self) -> None:
        secret = "LEXICAL-REVIEW-SENTINEL"
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            first = store.create_write_candidate(
                job_id="job-lexical-a",
                kind="SYNTHESIS",
                statement=f"{secret} alpha beta gamma delta epsilon zeta eta theta iota mu nu xi omicron pi rho sigma tau upsilon kappa",
            )
            second = store.create_write_candidate(
                job_id="job-lexical-b",
                kind="SYNTHESIS",
                statement=f"{secret} alpha beta gamma delta epsilon zeta eta theta iota mu nu xi omicron pi rho sigma tau upsilon lambda",
            )
            store.create_write_candidate(
                job_id="job-lexical-c",
                kind="SYNTHESIS",
                statement="unrelated candidate with different local wording entirely",
            )

        code, output, error = self.run_cli("knowledge", "review")

        self.assertEqual(code, EXIT_OK, error)
        payload = json.loads(output)
        self.assertEqual(len(payload["lexical_near_duplicates"]), 1)
        lead = payload["lexical_near_duplicates"][0]
        self.assertEqual(set(lead["candidate_ids"]), {first.candidate_id, second.candidate_id})
        self.assertEqual(set(lead["job_ids"]), {"job-lexical-a", "job-lexical-b"})
        self.assertGreaterEqual(lead["similarity_basis_points"], 9000)
        self.assertNotIn(secret, output)

    def test_explicit_multi_candidate_bundle_never_merges_records_or_overwrites_page(self) -> None:
        target = self.folder_root / "pages" / "bundle.md"
        arguments = (
            "--candidate-id", self.second_candidate_id,
            "--candidate-id", self.candidate_id,
            "--folder-id", self.folder_id,
            "--path", "pages/bundle.md",
            "--title", "Accepted Bundle",
        )
        code, output, error = self.run_cli(
            "knowledge", "candidate-page-bundle-preview", *arguments,
        )
        self.assertEqual(code, EXIT_OK, error)
        preview = json.loads(output)
        self.assertEqual(preview["candidate_ids"], sorted((self.candidate_id, self.second_candidate_id)))
        self.assertEqual(
            set(preview["accepted_record_ids"]),
            {self.accepted_record_id, self.second_accepted_record_id},
        )
        self.assertIn("does not merge or replace", preview["markdown"])
        self.assertFalse(target.exists())

        code, output, error = self.run_cli(
            "knowledge", "candidate-page-bundle-publish", *arguments,
            "--expected-sha256", preview["content_sha256"], "--confirm",
        )
        self.assertEqual(code, EXIT_OK, error)
        self.assertEqual(json.loads(output)["content_sha256"], preview["content_sha256"])
        self.assertEqual(target.read_text(encoding="utf-8"), preview["markdown"])
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            self.assertEqual(store.write_candidate(self.candidate_id).accepted_record_id, self.accepted_record_id)  # type: ignore[union-attr]
            self.assertEqual(store.write_candidate(self.second_candidate_id).accepted_record_id, self.second_accepted_record_id)  # type: ignore[union-attr]

        target.write_text("user controlled bundle\n", encoding="utf-8")
        code, _output, error = self.run_cli(
            "knowledge", "candidate-page-bundle-publish", *arguments,
            "--expected-sha256", preview["content_sha256"], "--confirm",
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("not publishable", error)
        self.assertEqual(target.read_text(encoding="utf-8"), "user controlled bundle\n")

    def test_bundle_requires_two_distinct_accepted_candidates(self) -> None:
        arguments = (
            "--candidate-id", self.candidate_id,
            "--folder-id", self.folder_id,
            "--path", "pages/invalid-bundle.md",
            "--title", "Invalid Bundle",
        )
        code, _output, error = self.run_cli(
            "knowledge", "candidate-page-bundle-preview", *arguments,
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("between 2 and 16", error)

    def test_publish_requires_confirmation_and_exact_well_formed_digest(self) -> None:
        relative_path = "pages/confirmation.md"
        target = self.folder_root / relative_path
        code, output, error = self.run_cli(
            "knowledge",
            "candidate-page-preview",
            *self.page_arguments(relative_path=relative_path),
        )
        self.assertEqual(code, EXIT_OK, error)
        preview = json.loads(output)

        code, _output, error = self.run_cli(
            "knowledge",
            "candidate-page-publish",
            *self.page_arguments(relative_path=relative_path),
            "--expected-sha256",
            preview["content_sha256"],
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("requires --confirm", error)
        self.assertFalse(target.exists())

        code, _output, error = self.run_cli(
            "knowledge",
            "candidate-page-publish",
            *self.page_arguments(relative_path=relative_path),
            "--expected-sha256",
            "not-a-digest",
            "--confirm",
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("expected SHA-256", error)
        self.assertFalse(target.exists())

        code, _output, error = self.run_cli(
            "knowledge",
            "candidate-page-publish",
            *self.page_arguments(relative_path=relative_path),
            "--expected-sha256",
            "0" * 64,
            "--confirm",
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("digest changed", error)
        self.assertFalse(target.exists())
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            self.assertIsNone(store.page_publication(self.candidate_id))

    def test_unsafe_path_and_pending_candidate_fail_before_any_page_write(self) -> None:
        code, _output, error = self.run_cli(
            "knowledge",
            "candidate-page-preview",
            *self.page_arguments(relative_path="../escape.md"),
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("Knowledge page path", error)
        self.assertFalse((self.root / "escape.md").exists())

        pending_arguments = (
            self.pending_candidate_id,
            "--folder-id",
            self.folder_id,
            "--path",
            "pages/pending.md",
            "--title",
            "Pending",
        )
        code, _output, error = self.run_cli(
            "knowledge",
            "candidate-page-preview",
            *pending_arguments,
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("explicitly accepted candidate", error)
        self.assertFalse((self.folder_root / "pages" / "pending.md").exists())

    def test_existing_user_content_is_reported_and_never_overwritten(self) -> None:
        relative_path = "pages/conflict.md"
        target = self.folder_root / relative_path
        target.parent.mkdir()
        target.write_text("user-owned existing page\n", encoding="utf-8")

        code, output, error = self.run_cli(
            "knowledge",
            "candidate-page-preview",
            *self.page_arguments(relative_path=relative_path),
        )
        self.assertEqual(code, EXIT_OK, error)
        preview = json.loads(output)
        self.assertEqual(preview["target_state"], "CONFLICT_CONTENT")
        self.assertFalse(preview["publishable"])

        code, _output, error = self.run_cli(
            "knowledge",
            "candidate-page-publish",
            *self.page_arguments(relative_path=relative_path),
            "--expected-sha256",
            preview["content_sha256"],
            "--confirm",
        )
        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("not publishable", error)
        self.assertEqual(target.read_text(encoding="utf-8"), "user-owned existing page\n")
        with KnowledgeStore(knowledge_state_path(self.state)) as store:
            self.assertIsNone(store.page_publication(self.candidate_id))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
