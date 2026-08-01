from __future__ import annotations

import asyncio
import io
import json
import unittest

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.company_learning import run_company_learning_evaluation


class CompanyLearningEvaluationTests(unittest.TestCase):
    def test_repeated_offline_evidence_stops_at_preview_candidate(self) -> None:
        record = asyncio.run(run_company_learning_evaluation())

        self.assertTrue(record.passed)
        self.assertEqual(record.first_decision, "NO_PATCH")
        self.assertEqual(record.second_decision, "CANDIDATE_AVAILABLE")
        self.assertFalse(record.candidate_apply_eligible)
        self.assertTrue(record.synthetic_approval_blocked)
        self.assertTrue(record.replay_matches)
        self.assertEqual(record.final_playbook_revision, 1)
        self.assertEqual(record.final_pattern_count, 0)

    def test_cli_exposes_stable_offline_company_learning_evaluation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        exit_code = main(["eval", "company", "--json"], stdout=output, stderr=error)
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["schema_version"], "noruct.company-learning-evaluation.v1")
        self.assertEqual(payload["evidence_class"], "offline-fixture-preview-only")
        self.assertFalse(payload["candidate_apply_eligible"])
        self.assertTrue(all(item["passed"] for item in payload["checks"]))


if __name__ == "__main__":
    unittest.main()
