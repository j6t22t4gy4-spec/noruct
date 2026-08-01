from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.knowledge.models import (
    DecisionStatus,
    IntentStatus,
    QuestionStatus,
    ResearchRequestStatus,
)
from dynamic_firm.knowledge.store import KnowledgeStore


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = KnowledgeStore(self.root / "knowledge.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _asset(self, *, content: bytes = b"source", scope: str = "private") -> str:
        digest = hashlib.sha256(content).hexdigest()
        asset, duplicate = self.store.create_asset(
            content_hash=digest,
            original_name="source.txt",
            title="Source",
            media_type="text/plain",
            byte_size=len(content),
            vault_relative_path=f"objects/{scope}/{digest}",
            origin="test",
            access_scope=scope,
        )
        self.assertFalse(duplicate)
        return asset.asset_id

    def test_database_is_private_and_symlink_database_is_rejected(self) -> None:
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.path.parent.stat().st_mode), 0o700)
        self.store.create_record(kind="NOTE", statement="force a WAL write")
        for sidecar in (
            Path(f"{self.store.path}-wal"),
            Path(f"{self.store.path}-shm"),
        ):
            if sidecar.exists():
                self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)

        actual = self.root / "actual.db"
        actual.touch()
        link = self.root / "linked.db"
        os.symlink(actual, link)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            KnowledgeStore(link)

    def test_selective_forget_rewrites_deleted_content_and_truncates_wal(self) -> None:
        sentinel = "PHYSICAL-FORGET-SECRET-77f6b239"
        record = self.store.create_record(kind="NOTE", statement=sentinel)
        before = b"".join(
            path.read_bytes()
            for path in (
                self.store.path,
                Path(f"{self.store.path}-wal"),
                Path(f"{self.store.path}-shm"),
            )
            if path.exists()
        )
        self.assertIn(sentinel.encode(), before)

        self.assertTrue(self.store.forget_record(record.record_id))

        after = b"".join(
            path.read_bytes()
            for path in (
                self.store.path,
                Path(f"{self.store.path}-wal"),
                Path(f"{self.store.path}-shm"),
            )
            if path.exists()
        )
        self.assertNotIn(sentinel.encode(), after)

    def test_future_schema_fails_closed_without_mutating_the_database(self) -> None:
        future_path = self.root / "future.db"
        connection = sqlite3.connect(future_path)
        connection.executescript(
            """
            CREATE TABLE knowledge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO knowledge_meta(key, value) VALUES ('schema_version', '999');
            CREATE TABLE future_sentinel (value TEXT NOT NULL);
            INSERT INTO future_sentinel(value) VALUES ('preserve-me');
            """
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(ValueError, "Unsupported Knowledge DB schema 999"):
            KnowledgeStore(future_path)

        inspection = sqlite3.connect(future_path)
        try:
            value = inspection.execute("SELECT value FROM future_sentinel").fetchone()
            self.assertEqual(value, ("preserve-me",))
            tables = {
                row[0]
                for row in inspection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertEqual(tables, {"knowledge_meta", "future_sentinel"})
        finally:
            inspection.close()

    def test_schema_v2_migrates_additively_to_current_knowledge_schema(self) -> None:
        record = self.store.create_record(kind="FACT", statement="Legacy fact")
        path = self.store.path
        self.store.close()
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(
                """
                DROP TABLE knowledge_outcome_observations;
                DROP TABLE knowledge_oracle_contracts;
                DROP TABLE knowledge_decision_contexts;
                DROP TABLE knowledge_epistemic_annotations;
                DROP TABLE knowledge_folder_entries;
                DROP TABLE knowledge_folders;
                UPDATE knowledge_meta SET value = '2' WHERE key = 'schema_version';
                """
            )
            connection.commit()
        finally:
            connection.close()

        self.store = KnowledgeStore(path)
        annotation = self.store.epistemic_annotation("RECORD", record.record_id)
        self.assertIsNotNone(annotation)
        assert annotation is not None
        self.assertEqual(annotation.epistemic_status.value, "UNKNOWN")
        self.assertEqual(annotation.trust_class.value, "UNSPECIFIED")
        self.assertEqual(self.store.counts()["knowledge_oracle_contracts"], 0)
        self.assertEqual(self.store.counts()["knowledge_folders"], 0)
        self.assertEqual(self.store.counts()["knowledge_folder_entries"], 0)

    def test_schema_v4_adds_the_folder_indexer_revision_without_losing_entries(self) -> None:
        path = self.store.path
        self.store.close()
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "ALTER TABLE knowledge_folder_entries DROP COLUMN indexer_revision"
            )
            connection.execute(
                "UPDATE knowledge_meta SET value = '4' WHERE key = 'schema_version'"
            )
            connection.commit()
        finally:
            connection.close()

        self.store = KnowledgeStore(path)
        columns = {
            str(row[1])
            for row in self.store._conn.execute(
                "PRAGMA table_info(knowledge_folder_entries)"
            ).fetchall()
        }
        self.assertIn("indexer_revision", columns)
        version = self.store._conn.execute(
            "SELECT value FROM knowledge_meta WHERE key = 'schema_version'"
        ).fetchone()
        self.assertIsNotNone(version)
        assert version is not None
        self.assertEqual(str(version[0]), "8")

    def test_representation_revisions_are_immutable_and_only_latest_is_retrievable(self) -> None:
        asset_id = self._asset()
        first_text = "The launch codename is Cedar."
        second_text = "The launch codename is Birch."
        first = self.store.create_representation(
            asset_id=asset_id,
            kind="normalized_markdown",
            media_type="text/markdown",
            content_hash=hashlib.sha256(first_text.encode()).hexdigest(),
            byte_size=len(first_text),
            vault_relative_path="derived/first.md",
            processor="fixture",
            processor_version="1",
            chunks=(
                {
                    "content": first_text,
                    "content_hash": hashlib.sha256(first_text.encode()).hexdigest(),
                    "char_start": 0,
                    "char_end": len(first_text),
                    "location": {"page": 1},
                },
            ),
        )
        second = self.store.create_representation(
            asset_id=asset_id,
            kind="normalized_markdown",
            media_type="text/markdown",
            content_hash=hashlib.sha256(second_text.encode()).hexdigest(),
            byte_size=len(second_text),
            vault_relative_path="derived/second.md",
            processor="fixture",
            processor_version="2",
            chunks=(
                {
                    "content": second_text,
                    "content_hash": hashlib.sha256(second_text.encode()).hexdigest(),
                    "char_start": 0,
                    "char_end": len(second_text),
                    "location": {"page": 1},
                },
            ),
        )
        from dynamic_firm.knowledge.models import AssetStatus

        self.store.set_asset_processing(asset_id, status=AssetStatus.READY)

        self.assertEqual((first.revision, second.revision), (1, 2))
        self.assertEqual(
            [item.representation_id for item in self.store.list_representations(asset_id)],
            [second.representation_id, first.representation_id],
        )
        rows = self.store.retrieval_rows(access_scope="private")
        self.assertEqual({row["representation_id"] for row in rows}, {second.representation_id})
        self.assertEqual({row["content"] for row in rows}, {second_text})

    def test_record_correction_and_forget_preserve_an_explicit_chain(self) -> None:
        original = self.store.create_record(
            kind="claim",
            statement="The release is Monday.",
            source_span={"page": 2},
        )
        corrected = self.store.create_record(
            kind="claim",
            statement="The release is Tuesday.",
            supersedes_record_id=original.record_id,
            source_span={"page": 3},
        )

        previous = self.store.record(original.record_id)
        self.assertIsNotNone(previous)
        assert previous is not None
        self.assertEqual(previous.status, "SUPERSEDED")
        self.assertEqual(corrected.revision, 2)
        self.assertEqual(corrected.supersedes_record_id, original.record_id)
        self.assertEqual(
            [item.record_id for item in self.store.list_records()], [corrected.record_id]
        )

        self.assertTrue(self.store.forget_record(corrected.record_id))
        self.assertIsNone(self.store.record(corrected.record_id))
        self.assertIsNotNone(self.store.record(original.record_id))
        self.assertFalse(self.store.forget_record(corrected.record_id))
        self.assertTrue(self.store.forget_record(original.record_id))
        self.assertEqual(self.store.list_records(include_superseded=True), ())

    def test_intent_history_is_append_only_and_content_addressed(self) -> None:
        intent = self.store.create_intent(
            goal="Ship the local knowledge path",
            priority=91,
            constraints=("offline", "bounded context"),
            acceptance_criteria=("tests pass",),
            knowledge_query="knowledge runtime",
        )
        paused = self.store.set_intent_status(intent.intent_id, IntentStatus.PAUSED)
        completed = self.store.set_intent_status(intent.intent_id, IntentStatus.COMPLETED)

        self.assertEqual((paused.revision, completed.revision), (2, 3))
        history = self.store.intent_history(intent.intent_id)
        self.assertEqual([item["revision"] for item in history], [1, 2, 3])
        self.assertEqual(
            [item["status"] for item in history], ["ACTIVE", "PAUSED", "COMPLETED"]
        )
        for item in history:
            self.assertRegex(str(item["content_hash"]), r"^[0-9a-f]{64}$")
        self.assertEqual(
            self.store.list_intents(status=IntentStatus.COMPLETED), (completed,)
        )

    def test_intent_current_row_tamper_cannot_be_laundered_by_status_change(self) -> None:
        intent = self.store.create_intent(goal="Original authenticated goal")
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute(
                "UPDATE knowledge_intents SET goal = ? WHERE intent_id = ?",
                ("Tampered unauthenticated goal", intent.intent_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ValueError, "does not match its immutable revision"):
            self.store.set_intent_status(intent.intent_id, IntentStatus.PAUSED)
        with self.assertRaisesRegex(ValueError, "does not match its immutable revision"):
            self.store.list_intents()
        self.assertEqual(len(self.store.intent_history(intent.intent_id)), 1)

    def test_decision_current_row_tamper_cannot_be_laundered_or_superseded(self) -> None:
        decision = self.store.create_decision(
            statement="Original authenticated decision",
            rationale="Original rationale",
        )
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute(
                "UPDATE knowledge_decisions SET statement = ? WHERE decision_id = ?",
                ("Tampered unauthenticated decision", decision.decision_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ValueError, "does not match its immutable revision"):
            self.store.set_decision_status(decision.decision_id, DecisionStatus.ACCEPTED)
        with self.assertRaisesRegex(ValueError, "does not match its immutable revision"):
            self.store.create_decision(
                statement="Replacement",
                rationale="Must not supersede a tampered row",
                supersedes_decision_id=decision.decision_id,
            )
        with self.assertRaisesRegex(ValueError, "does not match its immutable revision"):
            self.store.list_decisions()
        self.assertEqual(len(self.store.decision_history(decision.decision_id)), 1)

    def test_decision_due_dates_and_supersession_are_revisioned(self) -> None:
        intent = self.store.create_intent(goal="Choose a release channel")
        original = self.store.create_decision(
            statement="Use the stable channel.",
            rationale="It limits operator risk.",
            status=DecisionStatus.ACCEPTED,
            intent_id=intent.intent_id,
            review_at="2025-01-01T00:00:00Z",
        )
        future = self.store.create_decision(
            statement="Review hosted sync later.",
            rationale="The server is not deployed.",
            review_at="2035-01-01T00:00:00+00:00",
        )
        due = self.store.due_decisions(as_of="2030-01-01T00:00:00+00:00")
        self.assertEqual([item.decision_id for item in due], [original.decision_id])
        self.assertNotIn(future.decision_id, {item.decision_id for item in due})
        with self.assertRaisesRegex(ValueError, "explicit timezone"):
            self.store.create_decision(
                statement="Invalid timestamp",
                rationale="Naive local time is ambiguous.",
                review_at="2025-01-01T00:00:00",
            )

        replacement = self.store.create_decision(
            statement="Use a staged channel.",
            rationale="The evidence now favors a progressive rollout.",
            status=DecisionStatus.ACCEPTED,
            intent_id=intent.intent_id,
            supersedes_decision_id=original.decision_id,
        )
        superseded = self.store.decision(original.decision_id)
        self.assertIsNotNone(superseded)
        assert superseded is not None
        self.assertEqual(superseded.status, DecisionStatus.SUPERSEDED)
        self.assertEqual(superseded.revision, 2)
        self.assertEqual(replacement.revision, 2)
        self.assertEqual(
            [item["status"] for item in self.store.decision_history(original.decision_id)],
            ["ACCEPTED", "SUPERSEDED"],
        )
        self.assertEqual(
            self.store.due_decisions(as_of="2030-01-01T00:00:00+00:00"), ()
        )

    def test_question_research_acceptance_compiles_but_never_starts_an_intent(self) -> None:
        decision = self.store.create_decision(
            statement="Hold the current price.",
            rationale="Recheck with current market evidence.",
            status=DecisionStatus.ACCEPTED,
            review_at="2026-08-20T00:00:00Z",
        )
        question = self.store.create_question(
            prompt="What evidence would change the price decision?",
            decision_id=decision.decision_id,
            answer_criteria=("State support, conflict, or uncertainty.",),
            knowledge_query="price decision",
        )
        self.assertEqual(question.status, QuestionStatus.OPEN)
        request = self.store.create_research_request(
            title="Price review",
            objective="Research only the current price decision.",
            question_id=question.question_id,
            decision_id=decision.decision_id,
            knowledge_query="price decision",
            required_evidence=("Current competitor pricing.",),
            counterargument_required=True,
            max_cost_units=0,
            max_duration_minutes=45,
        )
        self.assertEqual(request.status, ResearchRequestStatus.DRAFT)
        accepted, intent = self.store.accept_research_request(request.request_id, priority=77)
        self.assertEqual(accepted.status, ResearchRequestStatus.ACCEPTED)
        self.assertEqual(accepted.compiled_intent_id, intent.intent_id)
        self.assertEqual(intent.status, IntentStatus.ACTIVE)
        self.assertEqual(intent.priority, 77)
        self.assertIn("counterargument", " ".join(intent.constraints).lower())
        self.assertEqual(self.store.list_execution_bindings(), ())
        self.assertEqual(
            [item["status"] for item in self.store.research_history(request.request_id)],
            ["DRAFT", "ACCEPTED"],
        )

    def test_due_decision_review_proposal_is_idempotent_and_does_not_start_work(self) -> None:
        decision = self.store.create_decision(
            statement="Keep the current packaging.",
            rationale="Validate the market before changing it.",
            status=DecisionStatus.ACCEPTED,
            review_at="2026-08-20T00:00:00Z",
        )
        first_question, first_request = self.store.propose_review_research(decision.decision_id)
        second_question, second_request = self.store.propose_review_research(decision.decision_id)
        self.assertEqual(first_question.question_id, second_question.question_id)
        self.assertEqual(first_request.request_id, second_request.request_id)
        self.assertEqual(first_request.status, ResearchRequestStatus.DRAFT)
        self.assertEqual(self.store.list_execution_bindings(), ())


if __name__ == "__main__":
    unittest.main()
