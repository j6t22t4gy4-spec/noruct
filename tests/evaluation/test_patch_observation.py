from __future__ import annotations

import io
import json
import unittest

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.patch_observation import run_patch_observation_evaluation


class PatchObservationEvaluationTests(unittest.TestCase):
    def test_offline_contract_covers_attribution_keep_and_rollback_recommendation(self) -> None:
        record = run_patch_observation_evaluation()

        self.assertTrue(record.passed)
        self.assertEqual(record.external_model_calls, 0)
        self.assertFalse(record.unaligned_cohort_eligible)
        self.assertEqual(record.two_observation_decision, "INSUFFICIENT_OBSERVATION")
        self.assertEqual(record.three_observation_decision, "KEEP")
        self.assertEqual(record.safety_decision, "ROLLBACK_CANDIDATE")
        self.assertEqual(record.final_patch_status, "APPLIED")
        self.assertFalse(record.automatic_rollback)

    def test_cli_exposes_stable_patch_observation_evaluation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        exit_code = main(
            ["eval", "observation", "--json"], stdout=output, stderr=error
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(
            payload["schema_version"], "noruct.patch-observation-evaluation.v1"
        )
        self.assertEqual(payload["evidence_class"], "offline-contract-only")
        self.assertEqual(payload["external_model_calls"], 0)
        self.assertTrue(all(item["passed"] for item in payload["checks"]))


if __name__ == "__main__":
    unittest.main()
