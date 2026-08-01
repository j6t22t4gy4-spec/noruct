from __future__ import annotations

import unittest
from datetime import UTC, datetime

from dynamic_firm.company.final_report import (
    COMPANY_FINAL_REPORT_SCHEMA,
    CompanyFinalReportMode,
    company_final_report,
)
from dynamic_firm.kernel.models import JobMetrics, JobResult, JobStatus
from dynamic_firm.runtime.models import EmployeeRunResult, RunStatus, Usage


def _employee_result(*, employee_id: str, task_id: str = "final") -> EmployeeRunResult:
    return EmployeeRunResult(
        run_id=f"run-{employee_id}",
        request_id="request-1",
        job_id="job-1",
        task_id=task_id,
        employee_id=employee_id,
        status=RunStatus.SUCCEEDED,
        summary="The final result is already authoritative.",
        output_artifact_refs=(),
        acceptance_evidence=("verified: artifact",),
        unresolved_issues=(),
        observations=(),
        suggested_followups=(),
        signals=(),
        partial_result=False,
        usage=Usage(model_calls=1),
        last_event_seq=1,
        started_at=None,
        finished_at=datetime.now(UTC),
    )


def _job_result(
    *,
    manager_employee_id: str = "",
    owner: str = "specialist",
    effect: str = "READ",
    company_work_mode: str = "TEAM_JOB",
) -> JobResult:
    return JobResult(
        job_id="job-1",
        request_id="request-1",
        status=JobStatus.SUCCEEDED,
        summary="The final result is already authoritative.",
        acceptance_evidence=("verified: artifact",),
        unresolved_issues=(),
        task_results=(_employee_result(employee_id=owner),),
        final_graph_version=1,
        final_tasks=(),
        metrics=JobMetrics(
            unique_employee_count=1,
            temporary_role_count=0,
            maximum_parallelism=1,
            graph_patch_count=0,
            usage=Usage(model_calls=1),
        ),
        final_task_id="final",
        manager_employee_id=manager_employee_id,
        company_work_mode=company_work_mode,
        requested_effect=effect,
    )


class CompanyFinalReportTests(unittest.TestCase):
    def test_manager_final_execution_is_marked_as_model_integrated(self) -> None:
        report = company_final_report(
            _job_result(manager_employee_id="manager", owner="manager")
        )

        self.assertEqual(report.schema, COMPANY_FINAL_REPORT_SCHEMA)
        self.assertEqual(report.mode, CompanyFinalReportMode.MANAGER_MODEL_INTEGRATED)
        self.assertEqual(report.reporting_owner_employee_id, "manager")
        self.assertEqual(report.execution_owner_employee_id, "manager")
        self.assertNotIn("second model", report.operator_line())

    def test_manager_direct_result_does_not_claim_specialist_integration(self) -> None:
        report = company_final_report(
            _job_result(
                manager_employee_id="manager",
                owner="manager",
                company_work_mode="DIRECT",
            )
        )

        self.assertIn("direct terminal result", report.operator_line())
        self.assertNotIn("integrated", report.operator_line())

    def test_effectful_specialist_uses_manager_operational_envelope(self) -> None:
        report = company_final_report(
            _job_result(
                manager_employee_id="manager",
                owner="workspace-specialist",
                effect="WORKSPACE_CHANGE",
            )
        )

        self.assertEqual(report.mode, CompanyFinalReportMode.MANAGER_OPERATIONAL_ENVELOPE)
        self.assertEqual(report.reporting_owner_employee_id, "manager")
        self.assertEqual(report.execution_owner_employee_id, "workspace-specialist")
        self.assertEqual(report.summary, "The final result is already authoritative.")
        self.assertIn("no second model call", report.operator_line())

    def test_without_manager_final_owner_reports_directly(self) -> None:
        report = company_final_report(_job_result(owner="conversation-specialist"))

        self.assertEqual(report.mode, CompanyFinalReportMode.LEGACY_FINAL_OWNER)
        self.assertEqual(report.reporting_owner_employee_id, "conversation-specialist")
        self.assertEqual(report.execution_owner_employee_id, "conversation-specialist")
