from __future__ import annotations

import unittest
import io
import json
import asyncio

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.organization_admission import (
    run_organization_admission_evaluation,
)


class OrganizationAdmissionEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_provider_free_solo_first_trajectories(self) -> None:
        evaluation = await run_organization_admission_evaluation()

        self.assertTrue(evaluation.passed)
        self.assertEqual(
            evaluation.schema_version,
            "noruct.organization-admission-evaluation.v1",
        )
        records = {record.fixture_id: record for record in evaluation.records}

        solo = records["bounded-solo"]
        self.assertEqual(solo.compiler_model_calls, 0)
        self.assertEqual(solo.employee_count, 1)
        self.assertEqual(solo.organization_admission_count, 0)

        recovery = records["same-worker-recovery"]
        self.assertEqual(recovery.compiler_model_calls, 0)
        self.assertTrue(recovery.same_worker_recovery)
        self.assertEqual(recovery.employee_attempt_count, 2)
        self.assertEqual(recovery.organization_admission_count, 0)

        escalation = records["typed-information-boundary"]
        self.assertEqual(escalation.compiler_model_calls, 0)
        self.assertEqual(escalation.employee_count, 2)
        self.assertEqual(escalation.organization_admission_count, 1)
        self.assertEqual(escalation.graph_patch_count, 1)
        self.assertEqual(escalation.final_graph_version, 2)
        self.assertEqual(escalation.final_writer_count, 1)
        self.assertTrue(escalation.specialist_memory_isolated)

    async def test_cli_exposes_provider_free_admission_evaluation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        exit_code = await asyncio.to_thread(
            main,
            ["eval", "organization-admission", "--json"],
            stdout=output,
            stderr=error,
        )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertTrue(payload["passed"])
        self.assertEqual(len(payload["records"]), 3)
        self.assertEqual(
            sum(item["compiler_model_calls"] for item in payload["records"]),
            0,
        )


if __name__ == "__main__":
    unittest.main()
