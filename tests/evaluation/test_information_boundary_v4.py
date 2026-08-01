from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.information_boundary import InformationBoundaryCase
from dynamic_firm.evaluation.information_boundary_v4 import (
    INFORMATION_BOUNDARY_SUITE_REPORT_SCHEMA,
    ReleaseAuthorizationCase,
    materialize_release_authorization_fixture,
    run_information_boundary_suite,
    score_release_authorization_artifact,
)


class InformationBoundarySuiteV4Tests(unittest.IsolatedAsyncioTestCase):
    async def test_two_independent_information_boundaries_pass_provider_free(
        self,
    ) -> None:
        report = await run_information_boundary_suite()

        self.assertTrue(report.passed)
        self.assertTrue(report.ready_for_second_live_control_pair)
        self.assertEqual(report.external_provider_calls, 0)
        self.assertFalse(report.quota_consumed)
        self.assertEqual(
            tuple(item.case for item in report.legacy_fixture.records),
            tuple(InformationBoundaryCase),
        )
        self.assertEqual(
            tuple(item.case for item in report.release_fixture.records),
            tuple(ReleaseAuthorizationCase),
        )
        self.assertNotEqual(
            report.legacy_fixture.fixture_revision,
            report.release_fixture.fixture_revision,
        )
        self.assertNotEqual(
            report.fixture_gains[0].memory_revision,
            report.fixture_gains[1].memory_revision,
        )
        self.assertEqual(
            tuple(item.capability for item in report.fixture_gains),
            ("sealed_policy_review", "release_policy_review"),
        )
        self.assertEqual(
            tuple(item.output_path for item in report.fixture_gains),
            ("REPORT.md", "RELEASE_REVIEW.md"),
        )
        self.assertEqual(
            tuple(item.artifact_quality_gain for item in report.fixture_gains),
            (0.4, 0.5),
        )

    async def test_release_boundary_has_same_workload_and_exact_admission(
        self,
    ) -> None:
        report = await run_information_boundary_suite()
        boundary = next(
            item
            for item in report.release_fixture.records
            if item.case == ReleaseAuthorizationCase.INFORMATION_BOUNDARY
        )

        self.assertTrue(boundary.passed)
        self.assertIsNotNone(boundary.counterfactual)
        self.assertEqual(
            boundary.identity.workload_hash,
            boundary.counterfactual.workload_hash,
        )
        self.assertNotEqual(boundary.identity.run_id, boundary.counterfactual.run_id)
        self.assertEqual(boundary.counterfactual.artifact_quality_score, 0.5)
        self.assertEqual(boundary.artifact.quality_score, 1.0)
        self.assertEqual(boundary.artifact_quality_gain, 0.5)
        self.assertEqual(boundary.admission.organization_admission_count, 1)
        self.assertEqual(
            boundary.admission.admitted_capabilities,
            ("release_policy_review",),
        )
        self.assertEqual(
            boundary.admission.decision_reasons,
            ("TYPED_CAPABILITY_GAP",),
        )
        self.assertEqual(
            (
                boundary.trajectory.attempts[0].task_id,
                boundary.trajectory.attempts[0].employee_id,
            ),
            ("analyze_goal", "employee-release-generalist"),
        )
        self.assertEqual(boundary.admission.final_graph_version, 2)
        self.assertEqual(boundary.trajectory.final_task_id, "integrate_goal")
        self.assertTrue(boundary.safety.employee_memory_isolated)
        self.assertTrue(boundary.safety.no_memory_identifier_leak)
        self.assertEqual(boundary.safety.final_writer_count, 1)

    async def test_invalid_admission_and_memory_leak_are_refused(self) -> None:
        report = await run_information_boundary_suite()
        records = {item.case: item for item in report.release_fixture.records}
        invalid = records[ReleaseAuthorizationCase.INVALID_CAPABILITY_REFUSAL]
        leak = records[ReleaseAuthorizationCase.MEMORY_LEAK_REFUSAL]

        self.assertTrue(invalid.passed)
        self.assertEqual(invalid.admission.organization_admission_count, 0)
        self.assertEqual(invalid.admission.final_graph_version, 1)
        self.assertEqual(
            invalid.admission.decision_reasons,
            ("CAPABILITY_INVALID", "CAPABILITY_ALREADY_ASSIGNED"),
        )
        self.assertTrue(leak.passed)
        self.assertEqual(leak.admission.organization_admission_count, 1)
        self.assertFalse(leak.artifact.passed)
        self.assertFalse(leak.safety.passed)
        self.assertFalse(leak.safety.no_memory_identifier_leak)
        self.assertEqual(leak.safety.final_writer_count, 1)

    async def test_release_artifact_scorer_has_no_topology_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = materialize_release_authorization_fixture(
                Path(directory) / "workspace"
            )
            self.assertNotIn(
                "attestation-green-rule-r2",
                "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in workspace.iterdir()
                ),
            )
            (workspace / "RELEASE_REVIEW.md").write_text(
                "disposition=RELEASE\n"
                "public_basis=tests-128-passed\n"
                "policy_basis=attestation-green-rule-r2\n"
                "required_action=publish-release-notes\n",
                encoding="utf-8",
            )
            score = score_release_authorization_artifact(workspace)

        self.assertTrue(score.passed)
        self.assertEqual(score.quality_score, 1.0)
        self.assertEqual(
            tuple(score_release_authorization_artifact.__annotations__),
            ("workspace", "return"),
        )

    async def test_cli_exposes_provider_free_v4_suite(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = await asyncio.to_thread(
            main,
            ["eval", "information-boundary-v4", "--json"],
            stdout=output,
            stderr=error,
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(
            payload["schema_version"],
            INFORMATION_BOUNDARY_SUITE_REPORT_SCHEMA,
        )
        self.assertTrue(payload["passed"])
        self.assertTrue(payload["ready_for_second_live_control_pair"])
        self.assertEqual(payload["external_provider_calls"], 0)
        self.assertFalse(payload["quota_consumed"])


if __name__ == "__main__":
    unittest.main()
