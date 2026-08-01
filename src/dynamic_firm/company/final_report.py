"""One truthful Company-level terminal report for every execution shape.

The Manager remains responsible for reporting the Company outcome, but this
module never creates a second model loop.  When a specialist owns an effectful
or one-task final action, the report is a deterministic operational envelope
over the already authoritative terminal result.  When the Manager executed the
existing final task, the same contract marks that model-backed integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from dynamic_firm.kernel.models import JobResult, JobStatus


COMPANY_FINAL_REPORT_SCHEMA = "noruct.company-final-report.v1"


class CompanyFinalReportMode(StrEnum):
    """How the user-facing result became a Company report."""

    MANAGER_MODEL_INTEGRATED = "MANAGER_MODEL_INTEGRATED"
    MANAGER_OPERATIONAL_ENVELOPE = "MANAGER_OPERATIONAL_ENVELOPE"
    LEGACY_FINAL_OWNER = "LEGACY_FINAL_OWNER"


@dataclass(frozen=True, slots=True)
class CompanyFinalReport:
    """Ephemeral product projection; never a new state authority or ledger.

    ``summary`` is the terminal owner output already selected by the Firm
    Kernel.  The report adds only observable responsibility and lifecycle
    facts, so a Manager label cannot fabricate a second synthesis, hidden
    reasoning, approval, or effect receipt.
    """

    schema: str
    mode: CompanyFinalReportMode
    status: JobStatus
    summary: str
    manager_employee_id: str
    reporting_owner_employee_id: str
    execution_owner_employee_id: str
    company_work_mode: str
    requested_effect: str
    acceptance_evidence: tuple[str, ...]
    unresolved_issues: tuple[str, ...]
    requires_attention: bool

    def operator_line(self) -> str:
        """Return one bounded, truthful line for CLI/TUI status surfaces."""

        if self.mode is CompanyFinalReportMode.MANAGER_MODEL_INTEGRATED:
            if self.company_work_mode == "DIRECT":
                return (
                    "Company report · Manager produced the direct terminal result "
                    f"(execution owner: {self.execution_owner_employee_id or 'manager'})."
                )
            return (
                "Company report · Manager integrated the final result "
                f"(execution owner: {self.execution_owner_employee_id or 'manager'})."
            )
        if self.mode is CompanyFinalReportMode.MANAGER_OPERATIONAL_ENVELOPE:
            return (
                "Company report · Manager recorded the terminal execution and verification "
                f"(execution owner: {self.execution_owner_employee_id or 'unavailable'}; "
                "no second model call)."
            )
        return (
            "Company report · Final execution owner reported directly "
            f"({self.execution_owner_employee_id or 'unavailable'}; legacy Manager unavailable)."
        )


def _final_execution_owner(result: JobResult) -> str:
    """Resolve the terminal task owner without reading output content."""

    if result.final_task_id:
        for task_result in result.task_results:
            if task_result.task_id == result.final_task_id:
                return task_result.employee_id
    if len(result.task_results) == 1:
        return result.task_results[0].employee_id
    return ""


def company_final_report(result: JobResult) -> CompanyFinalReport:
    """Project one Company report without changing the terminal result.

    The projection is deliberately constructed after the Kernel/direct runtime
    returns. It cannot run tools, reserve a budget, mutate a graph, or persist
    the underlying summary beyond the normal caller-controlled session result.
    """

    manager_employee_id = result.manager_employee_id.strip()
    execution_owner_employee_id = _final_execution_owner(result)
    if manager_employee_id and execution_owner_employee_id == manager_employee_id:
        mode = CompanyFinalReportMode.MANAGER_MODEL_INTEGRATED
        reporting_owner_employee_id = manager_employee_id
    elif manager_employee_id:
        mode = CompanyFinalReportMode.MANAGER_OPERATIONAL_ENVELOPE
        reporting_owner_employee_id = manager_employee_id
    else:
        mode = CompanyFinalReportMode.LEGACY_FINAL_OWNER
        reporting_owner_employee_id = execution_owner_employee_id
    return CompanyFinalReport(
        schema=COMPANY_FINAL_REPORT_SCHEMA,
        mode=mode,
        status=result.status,
        summary=result.summary or f"Job ended with status {result.status.value}.",
        manager_employee_id=manager_employee_id,
        reporting_owner_employee_id=reporting_owner_employee_id,
        execution_owner_employee_id=execution_owner_employee_id,
        company_work_mode=result.company_work_mode,
        requested_effect=result.requested_effect,
        acceptance_evidence=tuple(result.acceptance_evidence[:8]),
        unresolved_issues=tuple(result.unresolved_issues[:8]),
        requires_attention=(
            result.status is not JobStatus.SUCCEEDED
            or bool(result.unresolved_issues)
        ),
    )
