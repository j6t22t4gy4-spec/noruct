from __future__ import annotations

from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobLimits,
    JobTask,
    PlanProposal,
)
from dynamic_firm.runtime.models import RunLimits


def task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = ("analysis",),
) -> JobTask:
    return JobTask(
        task_id=task_id,
        objective=f"Complete {task_id}",
        depends_on=depends_on,
        required_capabilities=capabilities,
        acceptance_criteria=(f"Evidence for {task_id}",),
    )


def company_request(
    tasks: tuple[JobTask, ...],
    *,
    final_task_id: str,
    roster: tuple[EmployeeRecord, ...],
    limits: JobLimits | None = None,
) -> CompanyRunRequest:
    return CompanyRunRequest(
        request_id="company-request-1",
        job_id="fixture-job",
        goal="Complete the fixture goal",
        plan_proposal=PlanProposal(
            proposal_id="proposal-1",
            goal="Complete the fixture goal",
            tasks=tasks,
            final_task_id=final_task_id,
        ),
        roster=roster,
        runtime_limits=RunLimits(max_model_calls=4, max_tool_calls=4, max_cost_usd=2.0),
        job_limits=limits or JobLimits(max_wall_time_ms=5_000),
    )
