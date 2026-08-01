from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import CompletionEnvelope, ModelResponse


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "tiny_repo"


class ManagerCliIntegrationTests(unittest.TestCase):
    def test_manager_report_and_revision_rollback_keep_running_jobs_pinned(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            from dynamic_firm.company.store import CompanyStateStore
            from dynamic_firm.kernel.models import EmployeeRecord

            with CompanyStateStore(state) as store:
                store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            "employee-executive-manager",
                            "Executive Manager",
                            ("company_management",),
                            model_profile="company-default",
                        ),
                    )
                )
            status_code = main(
                ["company", "manager-report", "--state", str(state), "--json"],
                stdout=output,
                stderr=error,
            )
            initial_report = json.loads(output.getvalue())
            output.seek(0); output.truncate(0)
            revise_code = main(
                [
                    "company", "manager-revise", "--model-profile", "manager-v2",
                    "--rationale", "Controlled next-Job Manager runtime update.",
                    "--state", str(state), "--json",
                ],
                stdout=output,
                stderr=error,
            )
            revision_patch = json.loads(output.getvalue())["proposal"]["patch_id"]
            output.seek(0); output.truncate(0)
            approve_code = main(
                ["company", "roster-approve", revision_patch, "--state", str(state), "--confirm"],
                stdout=output,
                stderr=error,
            )
            apply_code = main(
                ["company", "roster-apply", revision_patch, "--state", str(state), "--confirm"],
                stdout=output,
                stderr=error,
            )
            output.seek(0); output.truncate(0)
            rollback_code = main(
                [
                    "company", "manager-rollback", str(initial_report["roster_revision"]),
                    "--rationale", "Restore the immutable original Manager revision.",
                    "--state", str(state), "--json",
                ],
                stdout=output,
                stderr=error,
            )
            rollback_payload = json.loads(output.getvalue())

        self.assertEqual(status_code, EXIT_OK, error.getvalue())
        self.assertEqual(initial_report["manager_employee_id"], "employee-executive-manager")
        self.assertEqual(initial_report["pending_reason"], "no_manager_attributed_organization_episode")
        self.assertEqual(revise_code, EXIT_OK, error.getvalue())
        self.assertEqual(approve_code, EXIT_OK, error.getvalue())
        self.assertEqual(apply_code, EXIT_OK, error.getvalue())
        self.assertEqual(rollback_code, EXIT_OK, error.getvalue())
        self.assertEqual(
            rollback_payload["restore_from_roster_revision"],
            initial_report["roster_revision"],
        )
        self.assertFalse(rollback_payload["active_jobs_changed"])
        self.assertEqual(rollback_payload["proposal"]["operation"], "UPDATE_EMPLOYEE")

    def test_manager_outcomes_are_read_only_and_expose_negative_transfer(self) -> None:
        from dynamic_firm.company import EvidenceSource, OrganizationEpisode, WorkflowTaskTemplate
        from dynamic_firm.company.store import CompanyStateStore

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            for job_id, quality, digest in (
                ("manager-outcome-one", 1.0, "a" * 64),
                ("manager-outcome-two", 0.6, "b" * 64),
            ):
                with CompanyStateStore(state) as store:
                    store.record_episode(
                        OrganizationEpisode.create(
                            job_id=job_id,
                            source=EvidenceSource.REAL_JOB,
                            task_family="manager-cli",
                            context_fingerprint="manager-cli-context",
                            execution_profile="READ_ONLY",
                            planning_mode="DYNAMIC",
                            plan_template=(
                                WorkflowTaskTemplate("final", ("analysis",), final=True),
                            ),
                            success=True,
                            quality_score=quality,
                            baseline_quality_score=0.8,
                            model_calls=3,
                            baseline_model_calls=4,
                            employee_count=1,
                            maximum_parallelism=1,
                            writer_count=1,
                            approvals_requested=0,
                            approvals_granted=0,
                            preapproval_mutations=0,
                            validation_attempts=(True,),
                            ledger_digest=digest,
                            manager_employee_id="employee-executive-manager",
                            manager_assignment_digest="c" * 64,
                            manager_delegation_digest="d" * 64,
                        )
                    )
            output = io.StringIO()
            error = io.StringIO()
            code = main(
                ["company", "manager-outcomes", "--state", str(state), "--json"],
                stdout=output,
                stderr=error,
            )

        self.assertEqual(code, EXIT_OK, error.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload[0]["decision"], "REVIEW_REQUIRED")
        self.assertEqual(payload[0]["negative_transfer_count"], 1)
        self.assertFalse(payload[0]["promotion_allowed"])

    def test_organization_outcomes_expose_only_context_bound_next_job_admission(self) -> None:
        from dynamic_firm.company import EvidenceSource, OrganizationEpisode, WorkflowTaskTemplate
        from dynamic_firm.company.store import CompanyStateStore

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            for job_id, quality, digest in (
                ("organization-outcome-one", 1.0, "a" * 64),
                ("organization-outcome-two", 0.95, "b" * 64),
            ):
                with CompanyStateStore(state) as store:
                    store.record_episode(
                        OrganizationEpisode.create(
                            job_id=job_id,
                            source=EvidenceSource.REAL_JOB,
                            task_family="organization-cli",
                            context_fingerprint="organization-cli-context",
                            execution_profile="READ_ONLY",
                            planning_mode="DYNAMIC",
                            plan_template=(
                                WorkflowTaskTemplate("analysis", ("analysis",)),
                                WorkflowTaskTemplate(
                                    "final", ("implementation",), ("analysis",), True
                                ),
                            ),
                            success=True,
                            quality_score=quality,
                            baseline_quality_score=0.8,
                            model_calls=3,
                            baseline_model_calls=4,
                            employee_count=2,
                            maximum_parallelism=2,
                            writer_count=1,
                            approvals_requested=0,
                            approvals_granted=0,
                            preapproval_mutations=0,
                            validation_attempts=(True,),
                            ledger_digest=digest,
                        )
                    )
            output = io.StringIO()
            error = io.StringIO()
            code = main(
                ["company", "organization-outcomes", "--state", str(state), "--json"],
                stdout=output,
                stderr=error,
            )

        self.assertEqual(code, EXIT_OK, error.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["automatic_application"], "next_job_only_evidence_gated")
        self.assertEqual(payload["assessments"][0]["context_fingerprint"], "organization-cli-context")
        self.assertEqual(payload["assessments"][0]["decision"], "TEAM_ELIGIBLE")
        self.assertFalse(payload["state_changed"])

    def test_legacy_company_manager_migration_is_explicit_and_uses_roster_lifecycle(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            # Open the real Company store, then seed a legacy non-Manager
            # baseline through its supported API.
            from dynamic_firm.company.store import CompanyStateStore
            from dynamic_firm.kernel.models import EmployeeRecord

            with CompanyStateStore(state) as store:
                store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            "employee-legacy-generalist",
                            "Generalist",
                            ("conversation",),
                            model_profile="fixture",
                        ),
                    )
                )

            status_output = io.StringIO()
            status_code = main(
                ["company", "manager-status", "--state", str(state), "--json"],
                stdout=status_output,
                stderr=error,
            )
            migration_code = main(
                ["company", "manager-migrate", "--state", str(state), "--json"],
                stdout=output,
                stderr=error,
            )
            proposal_id = json.loads(output.getvalue())["proposal"]["patch_id"]
            approve_code = main(
                [
                    "company",
                    "roster-approve",
                    proposal_id,
                    "--state",
                    str(state),
                    "--confirm",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            apply_code = main(
                [
                    "company",
                    "roster-apply",
                    proposal_id,
                    "--state",
                    str(state),
                    "--confirm",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            upgraded_output = io.StringIO()
            upgraded_code = main(
                ["company", "manager-status", "--state", str(state), "--json"],
                stdout=upgraded_output,
                stderr=error,
            )
            provider = ScriptedModelProvider(
                [ModelResponse(completion=CompletionEnvelope(summary="Migrated Manager response."))]
            )
            run_output = io.StringIO()
            run_code = main(
                [
                    "hello",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--provider",
                    "openai-api",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                ],
                provider_factory=lambda _config: provider,
                stdout=run_output,
                stderr=error,
            )
            connection = sqlite3.connect(state)
            try:
                run_employee = connection.execute(
                    "SELECT employee_id FROM employee_runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(status_code, EXIT_OK, error.getvalue())
        self.assertEqual(migration_code, EXIT_OK, error.getvalue())
        self.assertEqual(approve_code, EXIT_OK, error.getvalue())
        self.assertEqual(apply_code, EXIT_OK, error.getvalue())
        self.assertEqual(upgraded_code, EXIT_OK, error.getvalue())
        self.assertEqual(run_code, EXIT_OK, error.getvalue())
        self.assertIn('"manager_capable": false', status_output.getvalue())
        self.assertIn('"proposal"', output.getvalue())
        self.assertIn("roster-approve", output.getvalue())
        self.assertIn("roster-apply", output.getvalue())
        self.assertIn('"manager_capable": true', upgraded_output.getvalue())
        self.assertIsNotNone(run_employee)
        assert run_employee is not None
        self.assertEqual(run_employee[0], "employee-executive-manager")
        self.assertIn("Migrated Manager response.", run_output.getvalue())

    def test_new_company_direct_turn_runs_the_persistent_manager(self) -> None:
        provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="Manager response."))]
        )
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            exit_code = main(
                [
                    "hello",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--provider",
                    "openai-api",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                ],
                provider_factory=lambda _config: provider,
                stdout=output,
                stderr=error,
            )
            connection = sqlite3.connect(state)
            try:
                row = connection.execute(
                    "SELECT employee_id FROM employee_runs ORDER BY created_at LIMIT 1"
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], "employee-executive-manager")
        self.assertIn("Manager response.", output.getvalue())
        self.assertTrue(provider.requests)
        manager_tools = {
            tool.name
            for tool in provider.requests[0].tools
            if tool.name.startswith("manager_")
        }
        self.assertEqual(
            manager_tools,
            {
                "manager_inspect_company",
                "manager_inspect_current_job",
                "manager_read_intent_brief",
                "manager_review_recent_outcomes",
            },
        )


if __name__ == "__main__":
    unittest.main()
