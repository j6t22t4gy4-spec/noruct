from __future__ import annotations

from dataclasses import dataclass, replace

from dynamic_firm.compiler import (
    CapabilityInsertReplanner,
    CompilerExecutionProfile,
    CompilerRequest,
    solo_first_decision,
)
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobLimits,
    JobStatus,
)
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import (
    ScriptedEmployeeExecutionPort,
    ScriptedOutcome,
)
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ContextBundle,
    Failure,
    FailureCategory,
    RunLimits,
    RunSignal,
    RunStatus,
    SignalCode,
    VersionedContent,
)


@dataclass(frozen=True, slots=True)
class OrganizationAdmissionFixtureRecord:
    fixture_id: str
    passed: bool
    status: str
    compiler_model_calls: int
    employee_count: int
    employee_attempt_count: int
    organization_admission_count: int
    graph_patch_count: int
    final_graph_version: int
    final_task_id: str
    final_writer_count: int
    same_worker_recovery: bool = False
    specialist_memory_isolated: bool = False


@dataclass(frozen=True, slots=True)
class OrganizationAdmissionEvaluation:
    schema_version: str
    passed: bool
    records: tuple[OrganizationAdmissionFixtureRecord, ...]


def _compiler_request(goal: str) -> CompilerRequest:
    return CompilerRequest(
        request_id="organization-admission-fixture",
        goal=goal,
        workspace_manifest=("fixture.py",),
        available_capabilities=(
            "repository_analysis",
            "sealed_evidence_review",
        ),
        model_profile="scripted",
        execution_profile=CompilerExecutionProfile.READ_ONLY,
        max_tasks=6,
        max_temporary_roles=1,
        max_total_model_calls=4,
    )


def _company_request(
    *,
    fixture_id: str,
    goal: str,
    roster: tuple[EmployeeRecord, ...],
    context: ContextBundle = ContextBundle(),
) -> tuple[CompanyRunRequest, int]:
    decision = solo_first_decision(_compiler_request(goal))
    return (
        CompanyRunRequest(
            request_id=f"request-{fixture_id}",
            job_id=f"job-{fixture_id}",
            goal=goal,
            plan_proposal=decision.proposal,
            roster=roster,
            context_snapshot=context,
            runtime_limits=RunLimits(
                max_model_calls=2,
                max_tool_calls=2,
                max_cost_usd=1.0,
            ),
            action_policy=ActionPolicy(filesystem_policy="READ_ONLY"),
            job_limits=JobLimits(
                max_tasks=6,
                max_concurrency=2,
                max_graph_patches=1,
                max_task_mutations=1,
                max_temporary_roles=1,
                max_total_model_calls=4,
                max_total_tool_calls=8,
                max_total_cost_usd=4.0,
                max_wall_time_ms=5_000,
            ),
        ),
        decision.usage.model_calls,
    )


def _record(
    *,
    fixture_id: str,
    result,
    runner: ScriptedEmployeeExecutionPort,
    compiler_model_calls: int,
    same_worker_recovery: bool = False,
    specialist_memory_isolated: bool = False,
) -> OrganizationAdmissionFixtureRecord:
    final_task_id = runner.requests[-1].task.task_id
    final_writer_count = sum(
        request.task.task_id == final_task_id
        for request in runner.requests
    )
    return OrganizationAdmissionFixtureRecord(
        fixture_id=fixture_id,
        passed=result.status == JobStatus.SUCCEEDED,
        status=result.status.value,
        compiler_model_calls=compiler_model_calls,
        employee_count=result.metrics.unique_employee_count,
        employee_attempt_count=len(result.attempt_records),
        organization_admission_count=(
            result.metrics.organization_admission_count
        ),
        graph_patch_count=result.metrics.graph_patch_count,
        final_graph_version=result.final_graph_version,
        final_task_id=final_task_id,
        final_writer_count=final_writer_count,
        same_worker_recovery=same_worker_recovery,
        specialist_memory_isolated=specialist_memory_isolated,
    )


async def run_organization_admission_evaluation() -> OrganizationAdmissionEvaluation:
    generalist = EmployeeRecord(
        "employee-generalist",
        "Repository Generalist",
        ("repository_analysis",),
    )

    solo_request, solo_compiler_calls = _company_request(
        fixture_id="bounded-solo",
        goal="Make one bounded repository assessment.",
        roster=(generalist,),
    )
    solo_runner = ScriptedEmployeeExecutionPort(
        {"analyze_goal": ScriptedOutcome("Bounded solo result.")}
    )
    solo_result = await FirmKernel(
        employee_execution=solo_runner,
        replanner=CapabilityInsertReplanner(),
    ).run(solo_request)
    solo = _record(
        fixture_id="bounded-solo",
        result=solo_result,
        runner=solo_runner,
        compiler_model_calls=solo_compiler_calls,
    )
    solo = replace(
        solo,
        passed=(
            solo.passed
            and solo.compiler_model_calls == 0
            and solo.employee_count == 1
            and solo.employee_attempt_count == 1
            and solo.organization_admission_count == 0
            and solo.final_writer_count == 1
        ),
    )

    recovery_request, recovery_compiler_calls = _company_request(
        fixture_id="same-worker-recovery",
        goal="Recover one transient validation-like provider failure.",
        roster=(generalist,),
    )
    transient = Failure(
        code="MODEL_TRANSIENT",
        category=FailureCategory.MODEL,
        message_safe="Transient scripted model failure.",
        retryable=True,
    )
    recovery_runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": (
                ScriptedOutcome(
                    "First attempt failed.",
                    status=RunStatus.FAILED,
                    failure=transient,
                ),
                ScriptedOutcome("Same employee recovered."),
            )
        }
    )
    recovery_result = await FirmKernel(
        employee_execution=recovery_runner,
        replanner=CapabilityInsertReplanner(),
    ).run(recovery_request)
    recovery_employee_ids = tuple(
        request.employee.employee_id for request in recovery_runner.requests
    )
    recovery = _record(
        fixture_id="same-worker-recovery",
        result=recovery_result,
        runner=recovery_runner,
        compiler_model_calls=recovery_compiler_calls,
        same_worker_recovery=(
            len(recovery_employee_ids) == 2
            and len(set(recovery_employee_ids)) == 1
        ),
    )
    recovery = replace(
        recovery,
        passed=(
            recovery.passed
            and recovery.compiler_model_calls == 0
            and recovery.employee_count == 1
            and recovery.employee_attempt_count == 2
            and recovery.organization_admission_count == 0
            and recovery.same_worker_recovery
            and recovery.final_writer_count == 2
        ),
    )

    specialist = EmployeeRecord(
        "employee-sealed-reviewer",
        "Sealed Evidence Reviewer",
        ("sealed_evidence_review",),
    )
    separated_memory = (
        VersionedContent(
            "employee-memory:employee-generalist:public-evidence",
            "1",
            "General repository evidence.",
        ),
        VersionedContent(
            "employee-memory:employee-sealed-reviewer:sealed-evidence",
            "1",
            "Evidence available only to the sealed reviewer.",
        ),
    )
    escalation_request, escalation_compiler_calls = _company_request(
        fixture_id="typed-information-boundary",
        goal="Produce one combined repository report from the supplied sources.",
        roster=(generalist, specialist),
        context=ContextBundle(selected_memory=separated_memory),
    )
    gap = RunSignal(
        SignalCode.CAPABILITY_MISSING,
        "sealed_evidence_review",
        ("solo attempt proved the sealed evidence is outside its memory boundary",),
    )
    escalation_runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": ScriptedOutcome(
                "Public evidence is ready; sealed evidence remains inaccessible.",
                signals=(gap,),
                acceptance_evidence=("public:evidence",),
            ),
            "specialist_sealed_evidence_review": ScriptedOutcome(
                "Sealed specialist evidence resolved.",
                acceptance_evidence=("sealed:evidence",),
            ),
            "integrate_goal": ScriptedOutcome(
                "Public and sealed evidence integrated.",
                acceptance_evidence=("integrated:evidence",),
            ),
        }
    )
    escalation_result = await FirmKernel(
        employee_execution=escalation_runner,
        replanner=CapabilityInsertReplanner(),
    ).run(escalation_request)
    specialist_request = next(
        request
        for request in escalation_runner.requests
        if request.task.task_id == "specialist_sealed_evidence_review"
    )
    solo_request_snapshot = escalation_runner.requests[0]
    specialist_memory_isolated = (
        specialist_request.employee.employee_id == specialist.employee_id
        and specialist_request.employee.selected_memory_refs
        == ("employee-memory:employee-sealed-reviewer:sealed-evidence",)
        and "employee-memory:employee-sealed-reviewer:sealed-evidence"
        not in solo_request_snapshot.employee.selected_memory_refs
    )
    escalation = _record(
        fixture_id="typed-information-boundary",
        result=escalation_result,
        runner=escalation_runner,
        compiler_model_calls=escalation_compiler_calls,
        specialist_memory_isolated=specialist_memory_isolated,
    )
    escalation = replace(
        escalation,
        passed=(
            escalation.passed
            and escalation.compiler_model_calls == 0
            and escalation.employee_count == 2
            and escalation.employee_attempt_count == 3
            and escalation.organization_admission_count == 1
            and escalation.graph_patch_count == 1
            and escalation.final_graph_version == 2
            and escalation.final_writer_count == 1
            and escalation.specialist_memory_isolated
        ),
    )

    records = (solo, recovery, escalation)
    return OrganizationAdmissionEvaluation(
        schema_version="noruct.organization-admission-evaluation.v1",
        passed=all(record.passed for record in records),
        records=records,
    )
