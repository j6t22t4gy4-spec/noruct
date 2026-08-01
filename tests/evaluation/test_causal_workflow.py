from __future__ import annotations

import asyncio
import io
import json
import unittest

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.causal_workflow import (
    QUALITY_GAIN_THRESHOLD,
    run_causal_workflow_evaluation,
)


class CausalWorkflowEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_job_cohort_applies_attributes_isolates_and_rolls_back(self) -> None:
        evaluation = await run_causal_workflow_evaluation()

        self.assertTrue(evaluation.passed)
        self.assertEqual(
            evaluation.schema_version,
            "noruct.causal-workflow-evaluation.v1",
        )
        self.assertEqual(evaluation.cohort_job_count, 4)
        self.assertEqual(evaluation.external_model_calls, 0)
        self.assertFalse(evaluation.quota_consumed)
        self.assertEqual(evaluation.final_patch_status, "ROLLED_BACK")
        self.assertEqual(evaluation.playbook_revision, 3)

        baseline, observation, patched, rollback = evaluation.records
        self.assertGreaterEqual(baseline.quality_gain, QUALITY_GAIN_THRESHOLD)
        self.assertGreaterEqual(observation.quality_gain, QUALITY_GAIN_THRESHOLD)
        self.assertTrue(patched.prior_exposed)
        self.assertTrue(patched.prior_aligned)
        self.assertFalse(patched.unrelated_control_exposed)
        self.assertFalse(rollback.prior_exposed)
        self.assertEqual(rollback.final_task_id, "integrate_goal")
        self.assertTrue(all(check.passed for check in evaluation.checks))

    async def test_cli_exposes_provider_free_causal_workflow_evaluation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        exit_code = await asyncio.to_thread(
            main,
            ["eval", "causal-workflow", "--json"],
            stdout=output,
            stderr=error,
        )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["cohort_job_count"], 4)
        self.assertEqual(payload["external_model_calls"], 0)
        self.assertFalse(payload["quota_consumed"])


if __name__ == "__main__":
    unittest.main()
