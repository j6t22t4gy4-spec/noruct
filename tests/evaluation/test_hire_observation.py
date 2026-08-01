from __future__ import annotations

import io
import json
import unittest

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.hire_observation import run_hire_observation_evaluation


class HireObservationEvaluationTests(unittest.TestCase):
    def test_evaluation_closes_post_hire_attribution_without_roster_mutation(self) -> None:
        record = run_hire_observation_evaluation()

        self.assertTrue(record.passed)
        self.assertEqual(record.two_observation_decision, "INSUFFICIENT_OBSERVATION")
        self.assertEqual(record.three_observation_decision, "KEEP")
        self.assertEqual(record.safety_decision, "DORMANCY_CANDIDATE")
        self.assertEqual(record.applied_roster_revision, 3)
        self.assertEqual(record.final_roster_revision, 3)
        self.assertTrue(record.employee_active)
        self.assertEqual(record.provider_calls, 0)
        self.assertFalse(record.quota_consumed)
        self.assertFalse(record.automatic_dormancy)
        self.assertFalse(record.automatic_roster_patch)

    def test_cli_json_exposes_stable_hire_observation_record(self) -> None:
        output = io.StringIO()

        code = main(["eval", "hire-observation", "--json"], stdout=output)
        payload = json.loads(output.getvalue())

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            payload["schema_version"],
            "noruct.hire-observation-evaluation.v1",
        )
        self.assertTrue(all(check["passed"] for check in payload["checks"]))
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["automatic_dormancy"])


if __name__ == "__main__":
    unittest.main()
