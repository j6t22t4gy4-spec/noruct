from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, _resolve_evolution_artifacts_for_job, main
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.runtime.job_ledger import SQLiteActiveJobLedger
from dynamic_firm.runtime.store import RunStore
from tests.kernel.helpers import company_request, task
from tests.evolution.test_typed_artifact_proposals import _artifact


class RuntimeCatalogResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = (
            EmployeeRecord(
                "employee-generalist",
                "Generalist",
                ("conversation",),
            ),
        )

    def test_absent_optional_catalog_is_not_created_by_a_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.evolution.db"

            resolution = _resolve_evolution_artifacts_for_job(
                state_path=path,
                job_id="job-no-catalog",
                roster=self.roster,
            )

            self.assertFalse(path.exists())
            self.assertEqual(resolution.pins, ())
            self.assertEqual(
                resolution.effects[0]["decision"],
                "LOCAL_BASELINE_NO_EVOLUTION_STATE",
            )

    def test_corrupt_optional_catalog_is_excluded_from_the_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.evolution.db"
            path.write_bytes(b"not a sqlite database")

            resolution = _resolve_evolution_artifacts_for_job(
                state_path=path,
                job_id="job-corrupt-catalog",
                roster=self.roster,
            )

            self.assertEqual(resolution.pins, ())
            self.assertEqual(resolution.employee_skills, {})
            self.assertEqual(
                resolution.effects[0]["decision"],
                "LOCAL_BASELINE_EVOLUTION_STATE_EXCLUDED",
            )

    def test_active_job_projection_keeps_only_bounded_effect_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runtime.db")
            try:
                ledger = SQLiteActiveJobLedger(
                    store,
                    evolution_artifact_effects=(
                        {
                            "kind": "SKILL_PACKAGE",
                            "artifact_id": "repository_skill",
                            "version": "1.0.0",
                            "scope_key": "company_default",
                            "decision": "PROJECTED_TO_EMPLOYEE_SKILL_SNAPSHOT",
                            "procedure": "must never enter ACTIVE JOB",
                        },
                    ),
                )
                self.assertEqual(
                    ledger.evolution_artifact_effects,
                    (
                        {
                            "kind": "SKILL_PACKAGE",
                            "artifact_id": "repository_skill",
                            "version": "1.0.0",
                            "scope_key": "company_default",
                            "decision": "PROJECTED_TO_EMPLOYEE_SKILL_SNAPSHOT",
                        },
                    ),
                )
            finally:
                store.close()

    def test_cli_builds_a_local_capsule_from_verified_terminal_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "runtime.db"
            destination = root / "capsule.json"
            proposal_path = root / "proposal.json"
            proposal_path.write_text(
                json.dumps(
                    {
                        "schema": "noruct.evolution-proposal.v1",
                        "kind": "SKILL_PATCH",
                        "artifact": _artifact("SKILL_PACKAGE"),
                    }
                ),
                encoding="utf-8",
            )
            request = company_request(
                (task("analysis"),),
                final_task_id="analysis",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            )
            store = RunStore(state)
            try:
                asyncio.run(
                    FirmKernel(
                        employee_execution=ScriptedEmployeeExecutionPort(
                            {"analysis": ScriptedOutcome("done")}
                        ),
                        active_job_ledger=SQLiteActiveJobLedger(store),
                    ).run(request)
                )
            finally:
                store.close()

            output = io.StringIO()
            error = io.StringIO()
            result = main(
                [
                    "evolution",
                    "capsule",
                    "build-job",
                    request.job_id,
                    str(destination),
                    "--state",
                    str(state),
                    "--capability",
                    "repository_analysis",
                    "--domain",
                    "software",
                    "--operation",
                    "analyze",
                    "--input-field",
                    "repository_shape",
                    "--tool-class",
                    "workspace_read",
                    "--metric-name",
                    "acceptance_passed",
                    "--risk-level",
                    "LOW",
                    "--quality-score",
                    "0.9",
                    "--cost-bucket",
                    "LOW",
                    "--evaluator-kind",
                    "LOCAL_TEST",
                    "--authority",
                    "ORGANIZATION_OWNER",
                    "--proposal",
                    str(proposal_path),
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )

            self.assertEqual(result, EXIT_OK, error.getvalue())
            capsule = json.loads(destination.read_text(encoding="utf-8"))
            receipt = json.loads(output.getvalue())
            self.assertEqual(capsule["schema"], "noruct.learning-capsule.v2")
            self.assertEqual(capsule["proposal"]["kind"], "SKILL_PATCH")
            self.assertTrue(capsule["execution_summary"]["redaction_applied"])
            self.assertEqual(receipt["preview"]["proposal"]["kind"], "SKILL_PATCH")
            self.assertFalse(receipt["network_request_performed"])
            self.assertFalse(receipt["queued"])


if __name__ == "__main__":
    unittest.main()
