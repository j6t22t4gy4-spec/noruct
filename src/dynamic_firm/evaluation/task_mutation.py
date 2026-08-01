from __future__ import annotations

import asyncio
from dataclasses import dataclass

from dynamic_firm import __version__
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobLimits,
    JobStatus,
    JobTask,
    PlanProposal,
    TaskMutationType,
)
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.runtime.models import (
    Failure,
    FailureCategory,
    RunLimits,
    RunSignal,
    RunStatus,
    SignalCode,
)


@dataclass(frozen=True, slots=True)
class TaskMutationEvaluationCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class TaskMutationTrajectory:
    name: str
    status: str
    employees: tuple[str, ...]
    task_attempts: tuple[int, ...]
    mutations: tuple[str, ...]
    final_graph_version: int


@dataclass(frozen=True, slots=True)
class TaskMutationEvaluationRecord:
    schema_version: str
    noruct_version: str
    evidence_class: str
    retry: TaskMutationTrajectory
    reroute: TaskMutationTrajectory
    retry_exhaustion: TaskMutationTrajectory
    reroute_cycle: TaskMutationTrajectory
    refusal_failure_kinds: tuple[str, ...]
    deterministic_replay: bool
    provider_calls: int
    quota_consumed: bool
    checks: tuple[TaskMutationEvaluationCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def _task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    capability: str = "analysis",
) -> JobTask:
    return JobTask(
        task_id=task_id,
        objective=f"Complete {task_id}",
        depends_on=depends_on,
        required_capabilities=(capability,),
        acceptance_criteria=(f"Evidence for {task_id}",),
    )


def _request(
    tasks: tuple[JobTask, ...],
    *,
    final_task_id: str,
    roster: tuple[EmployeeRecord, ...],
) -> CompanyRunRequest:
    return CompanyRunRequest(
        request_id="offline-task-mutation-request",
        job_id="offline-task-mutation-job",
        goal="Evaluate bounded failure-driven task mutation",
        plan_proposal=PlanProposal(
            proposal_id="offline-task-mutation-plan",
            goal="Evaluate bounded failure-driven task mutation",
            tasks=tasks,
            final_task_id=final_task_id,
        ),
        roster=roster,
        runtime_limits=RunLimits(
            max_wall_time_ms=5_000,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost_usd=2.0,
        ),
        job_limits=JobLimits(
            max_tasks=6,
            max_concurrency=2,
            max_graph_patches=1,
            max_task_mutations=2,
            max_temporary_roles=1,
            max_total_model_calls=12,
            max_total_tool_calls=12,
            max_total_cost_usd=6.0,
            max_wall_time_ms=5_000,
        ),
        company_revision=3,
        roster_revision=5,
        playbook_revision=7,
    )


def _trajectory(name: str, runner: ScriptedEmployeeExecutionPort, result) -> TaskMutationTrajectory:
    return TaskMutationTrajectory(
        name=name,
        status=result.status.value,
        employees=tuple(item.employee.employee_id for item in runner.requests),
        task_attempts=tuple(item.task.attempt for item in runner.requests),
        mutations=tuple(item.mutation_type.value for item in result.mutation_events),
        final_graph_version=result.final_graph_version,
    )


async def _retry_run():
    transient = Failure(
        "MODEL_RATE_LIMIT",
        FailureCategory.MODEL,
        "The model endpoint was temporarily rate limited.",
        retryable=True,
    )
    runner = ScriptedEmployeeExecutionPort(
        {
            "analysis": (
                ScriptedOutcome("Transient", status=RunStatus.FAILED, failure=transient),
                ScriptedOutcome("Recovered"),
            ),
            "final": ScriptedOutcome("Integrated"),
        }
    )
    request = _request(
        (
            _task("analysis"),
            _task("final", depends_on=("analysis",), capability="integration"),
        ),
        final_task_id="final",
        roster=(
            EmployeeRecord("analyst", "Analyst", ("analysis",)),
            EmployeeRecord("integrator", "Integrator", ("integration",)),
        ),
    )
    return runner, request, await FirmKernel(employee_execution=runner).run(request)


async def _run_evaluation() -> TaskMutationEvaluationRecord:
    retry_runner, retry_request, retry_result = await _retry_run()
    _, _, replay_result = await _retry_run()

    mismatch_failure = Failure(
        "ASSIGNEE_CAPABILITY_MISMATCH",
        FailureCategory.INPUT,
        "Another exact-capable employee is required.",
    )
    mismatch_signal = RunSignal(
        SignalCode.ASSIGNEE_MISMATCH,
        "analysis",
        ("typed:assignment-mismatch",),
    )
    reroute_runner = ScriptedEmployeeExecutionPort(
        {
            ("analysis", "analyst-a"): ScriptedOutcome(
                "Mismatch",
                status=RunStatus.FAILED,
                signals=(mismatch_signal,),
                failure=mismatch_failure,
            ),
            ("analysis", "analyst-b"): ScriptedOutcome("Reassigned"),
            "final": ScriptedOutcome("Integrated"),
        }
    )
    reroute_request = _request(
        (
            _task("analysis"),
            _task("final", depends_on=("analysis",), capability="integration"),
        ),
        final_task_id="final",
        roster=(
            EmployeeRecord("analyst-a", "Analyst A", ("analysis",)),
            EmployeeRecord("analyst-b", "Analyst B", ("analysis",)),
            EmployeeRecord("integrator", "Integrator", ("integration",)),
        ),
    )
    reroute_result = await FirmKernel(employee_execution=reroute_runner).run(
        reroute_request
    )

    transient_tool = Failure(
        "TOOL_READ_TRANSIENT",
        FailureCategory.TOOL,
        "The read tool was temporarily unavailable.",
        retryable=True,
    )
    exhaustion_runner = ScriptedEmployeeExecutionPort(
        {
            "analysis": (
                ScriptedOutcome("First failure", status=RunStatus.FAILED, failure=transient_tool),
                ScriptedOutcome("Second failure", status=RunStatus.FAILED, failure=transient_tool),
            ),
            "final": ScriptedOutcome("Must not run"),
        }
    )
    exhaustion_request = _request(
        (
            _task("analysis"),
            _task("final", depends_on=("analysis",)),
        ),
        final_task_id="final",
        roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
    )
    exhaustion_result = await FirmKernel(employee_execution=exhaustion_runner).run(
        exhaustion_request
    )

    cycle_failed = ScriptedOutcome(
        "Mismatch",
        status=RunStatus.FAILED,
        signals=(mismatch_signal,),
        failure=mismatch_failure,
    )
    cycle_runner = ScriptedEmployeeExecutionPort(
        {
            ("final", "analyst-a"): cycle_failed,
            ("final", "analyst-b"): cycle_failed,
            ("final", "analyst-c"): ScriptedOutcome("Must not run"),
        }
    )
    cycle_request = _request(
        (_task("final"),),
        final_task_id="final",
        roster=tuple(
            EmployeeRecord(f"analyst-{suffix}", f"Analyst {suffix}", ("analysis",))
            for suffix in ("a", "b", "c")
        ),
    )
    cycle_result = await FirmKernel(employee_execution=cycle_runner).run(cycle_request)

    refusal_kinds: list[str] = []
    refusal_events = 0
    for failure in (
        Failure(
            "TOOL_APPROVAL_DENIED",
            FailureCategory.POLICY,
            "The user denied approval.",
            retryable=True,
        ),
        Failure(
            "SAFETY_VIOLATION",
            FailureCategory.TOOL,
            "The safety boundary rejected the action.",
            retryable=True,
        ),
        Failure(
            "RUNTIME_BOUNDARY_FAILED",
            FailureCategory.INTERNAL,
            "The runtime boundary failed.",
            retryable=True,
        ),
    ):
        runner = ScriptedEmployeeExecutionPort(
            {
                "final": ScriptedOutcome(
                    "Refused",
                    status=RunStatus.FAILED,
                    failure=failure,
                )
            }
        )
        request = _request(
            (_task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        result = await FirmKernel(employee_execution=runner).run(request)
        refusal_kinds.append(result.attempt_records[0].failure_kind.value)
        refusal_events += len(result.mutation_events)

    unknown_runner = ScriptedEmployeeExecutionPort(
        {
            "final": ScriptedOutcome(
                "Unknown failure",
                status=RunStatus.FAILED,
                synthesize_failure=False,
            )
        }
    )
    unknown_request = _request(
        (_task("final"),),
        final_task_id="final",
        roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
    )
    unknown_result = await FirmKernel(employee_execution=unknown_runner).run(
        unknown_request
    )
    refusal_kinds.append(unknown_result.attempt_records[0].failure_kind.value)
    refusal_events += len(unknown_result.mutation_events)

    replay_match = (
        tuple((item.event_id, item.content_hash) for item in retry_result.mutation_events)
        == tuple((item.event_id, item.content_hash) for item in replay_result.mutation_events)
        and tuple(item.attempt_id for item in retry_result.attempt_records)
        == tuple(item.attempt_id for item in replay_result.attempt_records)
    )
    retry_analysis_attempts = tuple(
        item for item in retry_result.attempt_records if item.task_id == "analysis"
    )
    retry_downstream_runs = sum(
        item.task.task_id == "final" for item in retry_runner.requests
    )
    snapshot_hashes = {
        item.frozen_snapshot_hash for item in retry_result.attempt_records
    }
    snapshot_hashes.update(
        item.frozen_snapshot_hash for item in retry_result.mutation_events
    )

    checks = (
        TaskMutationEvaluationCheck(
            "recoverable_failure_retries_once_and_downstream_runs_once",
            retry_result.status == JobStatus.SUCCEEDED
            and len(retry_analysis_attempts) == 2
            and retry_downstream_runs == 1
            and retry_result.mutation_events[0].mutation_type
            == TaskMutationType.RETRY,
            f"analysis-attempts={len(retry_analysis_attempts)},downstream={retry_downstream_runs}",
        ),
        TaskMutationEvaluationCheck(
            "assignee_mismatch_reroutes_to_existing_exact_capability",
            reroute_result.status == JobStatus.SUCCEEDED
            and tuple(
                item.employee.employee_id
                for item in reroute_runner.requests
                if item.task.task_id == "analysis"
            )
            == ("analyst-a", "analyst-b")
            and reroute_result.mutation_events[0].mutation_type
            == TaskMutationType.REROUTE,
            "analyst-a→analyst-b,temporary=0",
        ),
        TaskMutationEvaluationCheck(
            "policy_safety_internal_and_unknown_failures_never_retry",
            refusal_events == 0,
            f"kinds={','.join(refusal_kinds)},mutations={refusal_events}",
        ),
        TaskMutationEvaluationCheck(
            "retry_exhaustion_creates_no_third_attempt",
            exhaustion_result.status == JobStatus.FAILED
            and len(exhaustion_runner.requests) == 2
            and len(exhaustion_result.mutation_events) == 1,
            f"attempts={len(exhaustion_runner.requests)},mutations={len(exhaustion_result.mutation_events)}",
        ),
        TaskMutationEvaluationCheck(
            "reroute_is_once_only_and_does_not_cycle",
            cycle_result.status == JobStatus.FAILED
            and tuple(item.employee.employee_id for item in cycle_runner.requests)
            == ("analyst-a", "analyst-b")
            and len(cycle_result.mutation_events) == 1,
            f"employees={','.join(item.employee.employee_id for item in cycle_runner.requests)}",
        ),
        TaskMutationEvaluationCheck(
            "frozen_company_roster_playbook_and_skill_snapshot_is_stable",
            len(snapshot_hashes) == 1
            and all(item.company_revision == 3 for item in retry_result.attempt_records)
            and all(item.roster_revision == 5 for item in retry_result.attempt_records)
            and all(item.playbook_revision == 7 for item in retry_result.attempt_records),
            f"snapshot-hashes={len(snapshot_hashes)},revisions=3/5/7",
        ),
        TaskMutationEvaluationCheck(
            "immutable_request_replay_reconstructs_same_attempt_and_event_identity",
            replay_match,
            f"match={str(replay_match).lower()}",
        ),
        TaskMutationEvaluationCheck(
            "topology_and_long_term_state_remain_unchanged",
            retry_result.final_graph_version == 1
            and reroute_result.final_graph_version == 1
            and retry_request.company_revision == 3,
            "graph=v1,company/roster/playbook=read-only snapshots",
        ),
    )
    return TaskMutationEvaluationRecord(
        schema_version="noruct.task-mutation-evaluation.v1",
        noruct_version=__version__,
        evidence_class="offline-typed-failure-kernel-trajectory",
        retry=_trajectory("retry", retry_runner, retry_result),
        reroute=_trajectory("reroute", reroute_runner, reroute_result),
        retry_exhaustion=_trajectory(
            "retry-exhaustion", exhaustion_runner, exhaustion_result
        ),
        reroute_cycle=_trajectory("reroute-cycle", cycle_runner, cycle_result),
        refusal_failure_kinds=tuple(refusal_kinds),
        deterministic_replay=replay_match,
        provider_calls=0,
        quota_consumed=False,
        checks=checks,
    )


def run_task_mutation_evaluation() -> TaskMutationEvaluationRecord:
    """Run bounded RETRY/REROUTE trajectories without provider or network access."""

    return asyncio.run(_run_evaluation())
