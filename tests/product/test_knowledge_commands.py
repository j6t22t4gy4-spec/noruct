from __future__ import annotations

import tempfile
import unittest
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dynamic_firm.knowledge.models import DecisionStatus
from dynamic_firm.knowledge import KnowledgePageService
from dynamic_firm.knowledge.store import KnowledgeStore, knowledge_state_path
from dynamic_firm.product.knowledge_commands import execute_local_knowledge_command


class LocalKnowledgeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime_state = Path(self.temporary.name) / "runtime.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_remember_and_retrieve_are_provider_free_local_state(self) -> None:
        remembered = execute_local_knowledge_command(
            self.runtime_state,
            "/remember",
            "The release codename is Cedar.",
        )
        retrieved = execute_local_knowledge_command(
            self.runtime_state,
            "/knowledge",
            "codename",
        )

        self.assertIn("Remembered locally", remembered[0])
        self.assertIn("no Company Job", remembered[0])
        self.assertIn("1 match", retrieved[0])
        self.assertTrue(any("Cedar" in message for message in retrieved))
        self.assertIn("not appended to Company or employee history", retrieved[-1])
        self.assertFalse(self.runtime_state.exists())
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            records = store.list_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].statement, "The release codename is Cedar.")

    def test_status_intent_and_decision_views_are_bounded_and_local(self) -> None:
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            intent = store.create_intent(
                goal="Ship the local Knowledge Runtime",
                priority=90,
                knowledge_query="Knowledge Runtime",
            )
            decision = store.create_decision(
                statement="Keep customer knowledge local by default.",
                rationale="Company and employee state are separate authorities.",
                status=DecisionStatus.ACCEPTED,
                intent_id=intent.intent_id,
                review_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            )

        summary = execute_local_knowledge_command(self.runtime_state, "/knowledge")
        intents = execute_local_knowledge_command(self.runtime_state, "/intent")
        intent_detail = execute_local_knowledge_command(
            self.runtime_state, "/intent", intent.intent_id
        )
        due = execute_local_knowledge_command(self.runtime_state, "/decision", "due")
        decision_detail = execute_local_knowledge_command(
            self.runtime_state, "/decision", decision.decision_id
        )

        self.assertIn("1 active intent", summary[1])
        self.assertIn(intent.intent_id, "\n".join(intents))
        self.assertIn("Ship the local Knowledge Runtime", "\n".join(intent_detail))
        self.assertIn(decision.decision_id, "\n".join(due))
        self.assertIn("Keep customer knowledge local", "\n".join(decision_detail))

    def test_workbench_projects_intent_decision_relations_without_reading_content(self) -> None:
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            intent = store.create_intent(
                goal="Decide the pricing direction",
                knowledge_query="pricing evidence",
            )
            decision = store.create_decision(
                statement="Keep the current price until review.",
                rationale="The decision remains user-owned.",
                status=DecisionStatus.ACCEPTED,
                intent_id=intent.intent_id,
            )

        view = execute_local_knowledge_command(self.runtime_state, "/workbench", intent.intent_id)

        rendered = "\n".join(view)
        self.assertIn("Knowledge Workbench", rendered)
        self.assertIn(intent.intent_id, rendered)
        self.assertIn(decision.decision_id, rendered)
        self.assertIn("no provider or Job created", rendered)
        self.assertIn("JOB · none", rendered)

    def test_workbench_actions_require_explicit_lifecycle_and_never_start_job(self) -> None:
        created = execute_local_knowledge_command(
            self.runtime_state,
            "/intent",
            "create Review competitor pricing --query 'competitor pricing' --priority 70",
        )
        intent_id = created[0].split(" · ")[1]
        self.assertIn("draft", created[0])
        activated = execute_local_knowledge_command(
            self.runtime_state, "/intent", f"activate {intent_id}"
        )
        self.assertIn("active", activated[0])
        readiness = execute_local_knowledge_command(
            self.runtime_state, "/workbench", f"ready {intent_id}"
        )
        self.assertIn("Execution readiness", readiness[0])
        self.assertIn("No binding", "\n".join(readiness))
        decision = execute_local_knowledge_command(
            self.runtime_state,
            "/decision",
            f"record Keep current price --rationale 'Need evidence first' --intent {intent_id}",
        )
        decision_id = decision[0].split(" · ")[1]
        review = execute_local_knowledge_command(
            self.runtime_state, "/decision", f"review {decision_id}"
        )
        self.assertIn("Review proposal", review[0])
        self.assertIn("no Job was created", "\n".join(review))
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            self.assertEqual(store.list_execution_bindings(), ())

    def test_workbench_candidate_review_requires_explicit_accept_or_reject(self) -> None:
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            candidate = store.create_write_candidate(
                job_id="job-pricing",
                statement="Competitor price evidence should be reviewed before changing strategy.",
                evidence_pack_id=None,
            )

        listing = execute_local_knowledge_command(self.runtime_state, "/workbench", "candidates")
        detail = execute_local_knowledge_command(
            self.runtime_state, "/workbench", f"candidate {candidate.candidate_id}"
        )
        accepted = execute_local_knowledge_command(
            self.runtime_state, "/workbench", f"accept {candidate.candidate_id}"
        )

        self.assertIn(candidate.candidate_id, "\n".join(listing))
        self.assertIn("proposed Knowledge record", "\n".join(detail))
        self.assertIn("accepted", accepted[0])
        self.assertIn("No Intent, Decision, Company Patch, provider call, or Job changed", accepted[1])
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            resolved = store.write_candidate(candidate.candidate_id)
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.status, "ACCEPTED")
            self.assertIsNotNone(resolved.accepted_record_id)
            self.assertEqual(store.list_execution_bindings(), ())

    def test_review_queue_projects_exact_and_high_threshold_lexical_leads_with_page_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pages = root / "pages"
            pages.mkdir()
            (pages / "price.md").write_text(
                "---\ntitle: Price\ncreated: 2025-01-01\nupdated: 2025-01-01\ntype: note\ncontested: true\ncontradictions: [other]\n---\nPrice evidence.\n",
                encoding="utf-8",
            )
            with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
                folder, _ = store.register_knowledge_folder(
                    root_path=str(root), display_name="review fixture", access_scope="private"
                )
                store.create_write_candidate(job_id="job-one", statement="Review the current price before changing strategy.")
                store.create_write_candidate(job_id="job-two", statement=" review the current PRICE before changing strategy. ")
                lexical_secret = "WORKBENCH-LEXICAL-SENTINEL"
                store.create_write_candidate(
                    job_id="job-lexical-a",
                    statement=(
                        f"{lexical_secret} alpha beta gamma delta epsilon zeta eta theta iota "
                        "mu nu xi omicron pi rho sigma tau upsilon kappa"
                    ),
                )
                store.create_write_candidate(
                    job_id="job-lexical-b",
                    statement=(
                        f"{lexical_secret} alpha beta gamma delta epsilon zeta eta theta iota "
                        "mu nu xi omicron pi rho sigma tau upsilon lambda"
                    ),
                )
                store.create_write_candidate(job_id="job-three", statement="Different conclusion.")
                published = store.create_write_candidate(
                    job_id="job-published", statement="Published page remains user-owned."
                )
                published = store.resolve_write_candidate(published.candidate_id, accept=True)
                pages_service = KnowledgePageService(store)
                preview = pages_service.preview_candidate_page(
                    candidate_id=published.candidate_id,
                    folder_id=folder.folder_id,
                    relative_path="pages/published.md",
                    title="Published",
                )
                pages_service.publish_candidate_page(
                    candidate_id=published.candidate_id,
                    folder_id=folder.folder_id,
                    relative_path="pages/published.md",
                    title="Published",
                    expected_content_sha256=preview.content_sha256,
                    confirm=True,
                )
                (pages / "published.md").write_text("user-owned edit\n", encoding="utf-8")
            review = execute_local_knowledge_command(self.runtime_state, "/workbench", "review")

        rendered = "\n".join(review)
        self.assertIn("synthesis leads=1", rendered)
        self.assertIn("lexical leads=1", rendered)
        self.assertIn("similarity=90.47%", rendered)
        self.assertNotIn("WORKBENCH-LEXICAL-SENTINEL", rendered)
        self.assertIn("jobs=2", rendered)
        self.assertIn("CONTESTED_PAGE", rendered)
        self.assertIn("DECLARED_CONTRADICTION", rendered)
        self.assertIn("PUBLISHED_PAGE_DRIFT", rendered)
        self.assertIn(folder.folder_id, rendered)
        self.assertIn("No candidate, page, Folder, Intent, Decision, Company state, provider call, or Job changed", rendered)

    def test_question_and_research_views_never_start_company_work(self) -> None:
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            decision = store.create_decision(
                statement="Hold the current price.",
                rationale="Review current evidence first.",
                status=DecisionStatus.ACCEPTED,
            )
            question, request = store.propose_review_research(decision.decision_id)

        questions = execute_local_knowledge_command(self.runtime_state, "/question")
        question_detail = execute_local_knowledge_command(
            self.runtime_state, "/question", question.question_id
        )
        research = execute_local_knowledge_command(self.runtime_state, "/research")
        research_detail = execute_local_knowledge_command(
            self.runtime_state, "/research", request.request_id
        )

        self.assertIn(question.question_id, "\n".join(questions))
        self.assertIn("What evidence", "\n".join(question_detail))
        self.assertIn(request.request_id, "\n".join(research))
        self.assertIn("no Job started", "\n".join(research_detail))
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            self.assertEqual(store.list_execution_bindings(), ())

    def test_command_validation_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be non-empty"):
            execute_local_knowledge_command(self.runtime_state, "/remember", "  ")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            execute_local_knowledge_command(self.runtime_state, "/unknown")
        with self.assertRaisesRegex(ValueError, "was not found"):
            execute_local_knowledge_command(
                self.runtime_state,
                "/intent",
                "intent-missing",
            )

    def test_empty_read_commands_do_not_create_local_state(self) -> None:
        summary = execute_local_knowledge_command(self.runtime_state, "/knowledge")
        intents = execute_local_knowledge_command(self.runtime_state, "/intent")
        decisions = execute_local_knowledge_command(self.runtime_state, "/decision", "due")

        self.assertIn("0 asset", summary[1])
        self.assertEqual(intents, ("Active intents · none",))
        self.assertEqual(decisions, ("Decisions due for review · none",))
        self.assertFalse(knowledge_state_path(self.runtime_state).exists())

    def test_terminal_control_sequences_are_stored_as_data_but_never_rendered(self) -> None:
        hostile = "Terminal note \x1b]52;c;ZXhmaWx0cmF0ZQ==\x07 \x1b[31msecret\x1b[0m"
        execute_local_knowledge_command(self.runtime_state, "/remember", hostile)

        rendered = "\n".join(
            execute_local_knowledge_command(self.runtime_state, "/knowledge", "Terminal")
        )

        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertNotIn("52;c", rendered)
        self.assertIn("secret", rendered)
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            self.assertEqual(store.list_records()[0].statement, hostile)

    def test_folder_commands_register_scan_retrieve_and_freeze_evidence(self) -> None:
        raw = Path(self.temporary.name) / "My Knowledge Folder"
        raw.mkdir()
        (raw / "pricing.md").write_text(
            "Competitor A raised its annual price.",
            encoding="utf-8",
        )

        registered = execute_local_knowledge_command(
            self.runtime_state,
            "/knowledge",
            f"folder add {shlex.quote(str(raw))}",
        )
        folders = execute_local_knowledge_command(
            self.runtime_state,
            "/knowledge",
            "folder list",
        )
        recalled = execute_local_knowledge_command(
            self.runtime_state,
            "/knowledge",
            "annual price",
        )

        self.assertIn("Registered Knowledge Folder", registered[0])
        self.assertIn(str(raw), "\n".join(folders))
        self.assertIn("Competitor A", "\n".join(recalled))
        with KnowledgeStore(knowledge_state_path(self.runtime_state)) as store:
            folder = store.list_knowledge_folders()[0]
            entry = store.list_knowledge_folder_entries(folder.folder_id)[0]
            self.assertIsNotNone(entry.snapshot_asset_id)

        opened = execute_local_knowledge_command(
            self.runtime_state,
            "/knowledge",
            f"folder open {entry.entry_id}",
        )
        self.assertIn("Competitor A", "\n".join(opened))


if __name__ == "__main__":
    unittest.main()
