from __future__ import annotations

import io
import json
import unittest

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.hiring import run_hiring_recommendation_evaluation


class HiringEvaluationTests(unittest.TestCase):
    def test_offline_hiring_evaluation_closes_recommendation_authority(self) -> None:
        record = run_hiring_recommendation_evaluation()

        self.assertTrue(record.passed)
        self.assertEqual(record.first_decision, "NO_PATCH")
        self.assertEqual(record.second_decision, "CANDIDATE_AVAILABLE")
        self.assertEqual(record.initial_roster_revision, 2)
        self.assertEqual(record.recommendation_roster_revision, 2)
        self.assertEqual(record.applied_roster_revision, 3)
        self.assertEqual(record.restarted_roster_revision, 3)
        self.assertTrue(record.offline_approval_rejected)
        self.assertEqual(record.provider_calls, 0)
        self.assertFalse(record.quota_consumed)
        self.assertFalse(record.automatic_approve)
        self.assertFalse(record.automatic_apply)

    def test_cli_json_exposes_stable_hiring_record(self) -> None:
        output = io.StringIO()

        code = main(["eval", "hiring", "--json"], stdout=output)
        payload = json.loads(output.getvalue())

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            payload["schema_version"],
            "noruct.hiring-recommendation-evaluation.v1",
        )
        self.assertTrue(all(check["passed"] for check in payload["checks"]))
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["automatic_apply"])


if __name__ == "__main__":
    unittest.main()
