from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.knowledge import KnowledgeStore, build_knowledge_lineage


class KnowledgeLineageTests(unittest.TestCase):
    def test_content_free_job_and_intent_lineage_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with KnowledgeStore(Path(temporary) / "knowledge.db") as store:
                candidate = store.create_write_candidate(
                    job_id="job-lineage", kind="SYNTHESIS", statement="private candidate body"
                )
                accepted = store.resolve_write_candidate(candidate.candidate_id, accept=True)
                by_job = build_knowledge_lineage(store, job_id="job-lineage")
                self.assertEqual(by_job["schema"], "noruct.knowledge-lineage.v1")
                self.assertFalse(by_job["network_request_performed"])
                self.assertIn(
                    {"from": f"candidate:{candidate.candidate_id}", "to": f"record:{accepted.accepted_record_id}", "relation": "ACCEPTED_AS"},
                    by_job["edges"],
                )
                self.assertNotIn("private candidate body", repr(by_job))

                intent = store.create_intent(goal="private intent goal")
                decision = store.create_decision(
                    statement="private decision", rationale="private rationale", intent_id=intent.intent_id
                )
                by_intent = build_knowledge_lineage(store, intent_id=intent.intent_id)
                self.assertIn(
                    {"from": f"intent:{intent.intent_id}", "to": f"decision:{decision.decision_id}", "relation": "INFORMS"},
                    by_intent["edges"],
                )
                self.assertNotIn("private intent goal", repr(by_intent))
                self.assertNotIn("private decision", repr(by_intent))

    def test_lineage_rejects_invalid_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with KnowledgeStore(Path(temporary) / "knowledge.db") as store:
                with self.assertRaisesRegex(ValueError, "limit"):
                    build_knowledge_lineage(store, limit=0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
