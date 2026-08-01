from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from dynamic_firm.runtime.models import (
    ApprovalDecision,
    ApprovalRequest,
    ActionPolicy,
    ContextBundle,
    EmployeeCapabilityProfile,
    EmployeeRunRequest,
    EmployeeRunResult,
    EmployeeSessionRetention,
    EmployeeSnapshot,
    Failure,
    FailureCategory,
    RunHandle,
    RunLimits,
    RunSignal,
    RunStatus,
    SignalCode,
    TaskEnvelope,
    TaskEvidencePack,
    ToolEffect,
    ToolRisk,
    Usage,
    VersionedContent,
)
from dynamic_firm.runtime.employee_capability import (
    build_employee_capability_profile,
    material_profile_difference,
    materially_equivalent,
)
from dynamic_firm.runtime.knowledge_retrieval import BoundedKnowledgeRetriever
from dynamic_firm.runtime.liveness import (
    LIVENESS_CONTINUATION_INSTRUCTION,
    enforce_employee_completion_liveness,
)
from dynamic_firm.runtime.manager_tool_policy import is_manager_tool
from dynamic_firm.runtime.ports import ApprovalPort, CancellationToken, EmployeeExecutionPort
from dynamic_firm.runtime.redaction import redact_prompt_text
from dynamic_firm.runtime.company_budget import (
    CompanyBudgetAdmission,
    CompanyBudgetAuthorityPort,
    CompanyBudgetForfeit,
    CompanyBudgetLease,
    CompanyBudgetSettlement,
)

from .graph import (
    GraphValidationError,
    apply_patch,
    graph_from_proposal,
    ready_tasks,
    replace_task,
    task_map,
)
from .ledger import ActiveJobLedgerPort
from .models import (
    AttemptBudgetEvidence,
    AttemptFailureKind,
    CompanyRunRequest,
    EmployeeRecord,
    GraphPatch,
    GraphPatchEvent,
    GraphPatchProposalEvent,
    GraphPatchProposalStatus,
    GraphMutationLease,
    JobGraph,
    JobMetrics,
    JobMutationEvent,
    JobResult,
    JobStatus,
    JobTask,
    ReplanContext,
    SemanticOperation,
    TaskAssignmentEvent,
    TaskStatus,
    TaskAttemptRecord,
    TaskMutationType,
)
from .mutation import (
    RECOVERABLE_FAILURE_KINDS,
    attempt_identity,
    attempt_record,
    classify_attempt_failure,
    content_digest,
    frozen_snapshot_digest,
    graph_patch_event,
    graph_patch_proposal_event,
    mutation_event,
    reroute_candidate,
    structurally_replica_safe,
    structurally_read_only,
)
from .staffing import staff_task
from .supervision import (
    ManagerSupervisionPort,
    supervision_context,
)
from .primitives import (
    ReplannerPort,
    _MutationCandidate,
    _Reservation,
    _RunningTask,
    _TrackedCompanyBudgetAuthority,
    _dependency_result_projection,
)
from .mutation_execution import FirmKernelMutationExecutionMixin
from .policy_request import FirmKernelPolicyMixin
from .result_supervision import FirmKernelResultMixin
from .ingress import FirmKernelIngressMixin
from .managed_continuation import FirmKernelManagedContinuationMixin
from .managed_terminal import FirmKernelManagedTerminalMixin
from .managed_completion import FirmKernelManagedCompletionMixin


AssignmentAdmission = Callable[[TaskAssignmentEvent], str]
TaskActionPolicyOverride = Callable[[JobTask, EmployeeRecord, ActionPolicy], ActionPolicy]


class FirmKernel(
    FirmKernelIngressMixin,
    FirmKernelManagedContinuationMixin,
    FirmKernelManagedTerminalMixin,
    FirmKernelManagedCompletionMixin,
    FirmKernelResultMixin,
    FirmKernelPolicyMixin,
    FirmKernelMutationExecutionMixin,
):
    """Process-local owner of graph state, staffing, concurrency, and patch bounds."""

    # A terminal Firm decision must not leave an already-dispatched Employee
    # attempt outside of the ACTIVE JOB audit chain.  Runtime ports are asked
    # to cancel first and get a brief chance to return their own terminal
    # result; only then does the Kernel cancel a stuck collector and emit its
    # own safe cancellation record.
    _CANCEL_COLLECTION_GRACE_SECONDS = 0.25

    def __init__(
        self,
        *,
        employee_execution: EmployeeExecutionPort,
        replanner: ReplannerPort | None = None,
        mutation_sink: Callable[[JobMutationEvent], None] | None = None,
        assignment_admission: AssignmentAdmission | None = None,
        task_action_policy_override: TaskActionPolicyOverride | None = None,
        assignment_sink: Callable[[TaskAssignmentEvent], None] | None = None,
        graph_patch_sink: Callable[[GraphPatchEvent], None] | None = None,
        graph_patch_proposal_sink: Callable[[GraphPatchProposalEvent], None] | None = None,
        approval_port: ApprovalPort | None = None,
        active_job_ledger: ActiveJobLedgerPort | None = None,
        company_budget_authority: CompanyBudgetAuthorityPort | None = None,
        manager_supervisor: ManagerSupervisionPort | None = None,
    ) -> None:
        self.employee_execution = employee_execution
        self.replanner = replanner
        self.mutation_sink = mutation_sink
        self.assignment_admission = assignment_admission
        self.task_action_policy_override = task_action_policy_override
        self.assignment_sink = assignment_sink
        self.graph_patch_sink = graph_patch_sink
        self.graph_patch_proposal_sink = graph_patch_proposal_sink
        self.approval_port = approval_port
        self.active_job_ledger = active_job_ledger
        self.company_budget_authority = company_budget_authority
        self.manager_supervisor = manager_supervisor

    async def _run_managed(
        self,
        request: CompanyRunRequest,
        *,
        company_budget_authority: CompanyBudgetAuthorityPort | None,
        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask],
        same_job_continuation: bool = False,
        partial_read_only_continuation: bool = False,
        approved_graph_proposal_continuation: bool = False,
        resumed_graph: JobGraph | None = None,
        resumed_results: Mapping[str, EmployeeRunResult] | None = None,
        initial_graph_patch_events: tuple[GraphPatchEvent, ...] = (),
        initial_graph_patch_proposal_events: tuple[GraphPatchProposalEvent, ...] = (),
        initial_graph_patch_count: int | None = None,
        prior_specialist_material_profiles: frozenset[str] = frozenset(),
        continuation_preserves_graph_shape: bool = False,
        execution_session_key: str | None = None,
    ) -> JobResult:
        self._validate_request(request)
        graph = resumed_graph or graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        self._validate_graph_constraints_against_graph(request, graph)
        roster = list(request.roster)
        results: dict[str, EmployeeRunResult] = {}
        assignees_used: set[str] = set()
        # Compiler work occurs before Employee dispatch, but it is still Job
        # work.  Starting the aggregate here makes reservation, terminal audit,
        # and Company-budget settlement account for it exactly once.
        usage = request.compiler_usage
        graph_patch_count = (
            len(initial_graph_patch_events)
            if initial_graph_patch_count is None
            else initial_graph_patch_count
        )
        organization_admission_count = 0
        task_mutation_count = 0
        temporary_roles_created = 0
        maximum_parallelism = 0
        effective_max_concurrency = self._effective_max_concurrency(request)
        semaphore = asyncio.Semaphore(effective_max_concurrency)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.job_limits.max_wall_time_ms / 1000
        terminal_status: JobStatus | None = None
        failure_reason = ""
        pending_graph_proposal_hold = False
        frozen_snapshot_hash = frozen_snapshot_digest(request)
        graph, results, assignees_used = self._restore_managed_ledger(
            request=request,
            graph=graph,
            results=results,
            assignees_used=assignees_used,
            same_job_continuation=same_job_continuation,
            partial_read_only_continuation=partial_read_only_continuation,
            approved_graph_proposal_continuation=approved_graph_proposal_continuation,
            resumed_results=resumed_results,
            frozen_snapshot_hash=frozen_snapshot_hash,
        )
        if partial_read_only_continuation and not execution_session_key:
            raise ValueError("Partial continuation requires a fresh execution session key")
        usage = request.compiler_usage
        for resumed_result in results.values():
            usage = usage.plus(resumed_result.usage)
        company_budget_lease: CompanyBudgetLease | None = None
        if company_budget_authority is not None:
            admission = company_budget_authority.admit_job(request)
            if not admission.allowed:
                detail = admission.reason or "Company cost budget denied this job."
                if admission.incident is not None:
                    detail += " Explicit operator budget resolution is required."
                denied = self._result(
                    request=request,
                    graph=graph,
                    roster=tuple(roster),
                    results=results,
                    assignees_used=assignees_used,
                    usage=usage,
                    status=JobStatus.BUDGET_EXHAUSTED,
                    graph_patch_count=graph_patch_count,
                    organization_admission_count=organization_admission_count,
                    task_mutation_count=task_mutation_count,
                    maximum_parallelism=maximum_parallelism,
                    failure_reason=detail,
                    attempt_records=(),
                    mutation_events=(),
                    graph_patch_events=(),
                )
                if self.active_job_ledger is not None:
                    self.active_job_ledger.finish_job(request.job_id, denied)
                return denied
            company_budget_lease = admission.lease
        attempt_records: list[TaskAttemptRecord] = []
        mutation_events: list[JobMutationEvent] = []
        graph_patch_events: list[GraphPatchEvent] = list(initial_graph_patch_events)
        graph_patch_proposal_events: list[GraphPatchProposalEvent] = list(
            initial_graph_patch_proposal_events
        )
        attempted_assignees: dict[str, list[str]] = {}
        retry_counts: dict[str, int] = {}
        reroute_counts: dict[str, int] = {}
        forced_assignees: dict[str, EmployeeRecord] = {}
        pending_reservations: dict[str, _Reservation] = {}
        pending_source_attempts: dict[str, str] = {}
        pending_retry_instructions: dict[str, str] = {}
        assigned_profiles: dict[str, EmployeeCapabilityProfile] = {}
        collapsed_profile_owners: dict[str, str] = {}
        replica_group_owners: dict[str, str] = {}

        if self._compiler_consumed_job_budget(request):
            terminal_status = JobStatus.BUDGET_EXHAUSTED
            failure_reason = (
                "The Company Job wall-time budget expired before Employee dispatch."
                if request.planning_reason
                == "JOB_WALL_TIME_EXHAUSTED_BEFORE_DISPATCH"
                else (
                    "The workflow compiler consumed or exceeded the Job budget before "
                    "Employee execution could start."
                )
            )

        while terminal_status is None:
            dispatch_state = getattr(self.active_job_ledger, "dispatch_state", None)
            if callable(dispatch_state):
                state = str(dispatch_state(request.job_id))
                if state != "ADMITTED":
                    terminal_status = (
                        JobStatus.FAILED if state == "CANCELLED" else JobStatus.STALLED
                    )
                    failure_reason = f"Job lifecycle holds further dispatch: {state}."
                    break
            final_task = task_map(graph)[graph.final_task_id]
            if final_task.status == TaskStatus.SUCCEEDED:
                terminal_status = JobStatus.SUCCEEDED
                break
            if final_task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                terminal_status = JobStatus.FAILED
                failure_reason = "The final integration task did not succeed."
                break
            if loop.time() >= deadline:
                terminal_status = JobStatus.BUDGET_EXHAUSTED
                failure_reason = "Job wall-time limit was exhausted."
                break

            busy = {item.employee.employee_id for item in running.values()}
            open_slots = effective_max_concurrency - len(running)
            homogeneous_profile_blocked = False
            if open_slots > 0:
                ready = tuple(
                    sorted(
                        ready_tasks(graph),
                        key=lambda item: (
                            0 if item.task_id in forced_assignees else 1,
                            item.task_id,
                        ),
                    )
                )
                allocation_slots = max(1, min(open_slots, len(ready)))
                for pending in ready:
                    if open_slots <= 0:
                        break
                    forced_employee = forced_assignees.get(pending.task_id)
                    assignment_reason = "PERSISTENT_CAPABILITY_MATCH"
                    if forced_employee is not None:
                        if forced_employee.employee_id in busy:
                            # Keep the pre-reserved attempt budget intact until
                            # its frozen assignee becomes available.
                            break
                        employee = forced_employee
                        reservation = pending_reservations[pending.task_id]
                        assignment_reason = "FORCED_MUTATION"
                    else:
                        reservation = pending_reservations.get(pending.task_id)
                        if reservation is None:
                            reservation = self._reserve_budget(
                                request,
                                usage,
                                running,
                                committed_reservations=pending_reservations.values(),
                                allocation_slots=allocation_slots,
                            )
                        if reservation is None:
                            break
                        manager_integrator = self._manager_integrator(
                            request,
                            graph,
                            pending,
                            results,
                            busy_employee_ids=busy,
                        )
                        collapsed_owner_id = collapsed_profile_owners.get(
                            pending.task_id,
                            "",
                        )
                        replica = pending.execution_replica
                        replica_owner_id = (
                            ""
                            if replica is None
                            else replica_group_owners.get(replica.group_id, "")
                        )
                        if replica_owner_id:
                            employee = next(
                                (
                                    item
                                    for item in roster
                                    if item.employee_id == replica_owner_id and item.active
                                ),
                                None,
                            )
                            if employee is None:
                                raise ValueError(
                                    "Execution replica owner left the frozen ROSTER"
                                )
                            assignment_reason = "VALUE_GATED_EXECUTION_REPLICA"
                        elif collapsed_owner_id:
                            if collapsed_owner_id in busy:
                                continue
                            employee = next(
                                (
                                    item
                                    for item in roster
                                    if item.employee_id == collapsed_owner_id
                                    and item.active
                                ),
                                None,
                            )
                            if employee is None:
                                raise ValueError(
                                    "Collapsed capability profile owner left the frozen ROSTER"
                                )
                            assignment_reason = "HOMOGENEOUS_CLONE_COLLAPSED"
                        elif manager_integrator is not None:
                            employee = manager_integrator
                            if all(
                                item.employee_id != employee.employee_id
                                for item in roster
                            ):
                                roster.append(employee)
                            assignment_reason = "PERSISTENT_MANAGER_FINAL_INTEGRATION"
                        else:
                            decision = staff_task(
                                pending,
                                tuple(roster),
                                busy_employee_ids=busy,
                                pinned_employee_ids=set(request.graph_pinned_employee_ids),
                                excluded_employee_ids=(
                                    set(request.graph_excluded_employee_ids)
                                    | self._independent_review_employee_ids(graph, pending)
                                ),
                                job_id=request.job_id,
                                temporary_roles_created=temporary_roles_created,
                                max_temporary_roles=request.job_limits.max_temporary_roles,
                                model_profile=request.roster[0].model_profile,
                            )
                            if decision.employee is None:
                                continue
                            employee = decision.employee
                            if decision.created_temporary:
                                roster.append(employee)
                                temporary_roles_created += 1
                                assignment_reason = "TEMPORARY_CAPABILITY_GAP"
                    remaining_wall_ms = max(1, int((deadline - loop.time()) * 1000))
                    runtime_request = self._employee_request(
                        request,
                        graph,
                        pending,
                        employee,
                        results,
                        reservation,
                        remaining_wall_ms,
                        retry_instruction=pending_retry_instructions.get(
                            pending.task_id,
                            "",
                        ),
                        task_action_policy_override=self.task_action_policy_override,
                        execution_session_key=execution_session_key,
                    )
                    capability_profile = runtime_request.employee.capability_profile
                    if capability_profile is None:
                        raise ValueError("Employee dispatch lacks a capability profile")
                    capability_profile.verify()
                    replica = pending.execution_replica
                    if replica is not None:
                        if not structurally_replica_safe(runtime_request.action_policy):
                            raise ValueError(
                                "Execution replicas require a structurally read-only action policy"
                            )
                        owner_id = replica_group_owners.setdefault(
                            replica.group_id,
                            employee.employee_id,
                        )
                        if owner_id != employee.employee_id:
                            raise ValueError(
                                "Execution replica group changed Employee profile during admission"
                            )
                        assignment_reason = "VALUE_GATED_EXECUTION_REPLICA"
                    equivalent_owner_id = next(
                        (
                            owner_id
                            for owner_id, owner_profile in sorted(assigned_profiles.items())
                            if owner_id != employee.employee_id
                            and materially_equivalent(owner_profile, capability_profile)
                            and set(pending.required_capabilities).issubset(
                                owner_profile.capability_ids
                            )
                        ),
                        None,
                    )
                    replaced_clone_id = ""
                    if equivalent_owner_id is not None and replica is None:
                        excluded = (
                            set(request.graph_excluded_employee_ids)
                            | self._independent_review_employee_ids(graph, pending)
                        )
                        if equivalent_owner_id in excluded:
                            # A clone does not supply independent review merely
                            # because its label differs.
                            homogeneous_profile_blocked = True
                            continue
                        if forced_employee is not None:
                            # Reroute can still make one bounded fresh attempt
                            # (for example a clean session), but it is audited
                            # as a homogeneous retry and never counts as team
                            # diversity.
                            assignment_reason = "HOMOGENEOUS_REROUTE_RETRY"
                        elif equivalent_owner_id in busy:
                            # Wait for and reuse the existing Employee instead
                            # of manufacturing parallelism from identity alone.
                            collapsed_profile_owners[pending.task_id] = equivalent_owner_id
                            continue
                        else:
                            owner = next(
                                (
                                    item
                                    for item in roster
                                    if item.employee_id == equivalent_owner_id
                                ),
                                None,
                            )
                            if owner is None or not owner.active:
                                raise ValueError(
                                    "Capability profile owner is absent from the frozen ROSTER"
                                )
                            replaced_clone_id = employee.employee_id
                            employee = owner
                            assignment_reason = "HOMOGENEOUS_CLONE_COLLAPSED"
                            runtime_request = self._employee_request(
                                request,
                                graph,
                                pending,
                                employee,
                                results,
                                reservation,
                                remaining_wall_ms,
                                retry_instruction=pending_retry_instructions.get(
                                    pending.task_id,
                                    "",
                                ),
                                task_action_policy_override=self.task_action_policy_override,
                                execution_session_key=execution_session_key,
                            )
                            capability_profile = runtime_request.employee.capability_profile
                            assert capability_profile is not None
                            capability_profile.verify()
                    source_attempt_id = pending_source_attempts.get(pending.task_id)
                    execution_instance_id = self._execution_instance_id(
                        request,
                        pending,
                    )
                    attempt_id = attempt_identity(
                        request=request,
                        task=pending,
                        employee_id=employee.employee_id,
                        graph_version=graph.version,
                        frozen_snapshot_hash=frozen_snapshot_hash,
                    )
                    record = _RunningTask(
                        task_id=pending.task_id,
                        employee=employee,
                        reservation=reservation,
                        attempt_id=attempt_id,
                        source_attempt_id=source_attempt_id,
                        action_policy=runtime_request.action_policy,
                        capability_profile_digest=capability_profile.profile_digest,
                        capability_material_digest=capability_profile.material_digest,
                        execution_instance_id=execution_instance_id,
                        replica_group_id="" if replica is None else replica.group_id,
                    )
                    prior_material_profiles = tuple(
                        profile
                        for owner_id, profile in assigned_profiles.items()
                        if owner_id != employee.employee_id
                    )
                    profile_difference = tuple(
                        sorted(
                            {
                                dimension
                                for prior in prior_material_profiles
                                for dimension in material_profile_difference(
                                    prior,
                                    capability_profile,
                                )
                            }
                        )
                    ) or (("initial_profile",) if not assigned_profiles else ())
                    alternatives = {
                        item.employee_id
                        for item in roster
                        if item.active
                        and item.employee_id != employee.employee_id
                        and set(pending.required_capabilities).issubset(item.capabilities)
                    }
                    if replaced_clone_id:
                        alternatives.add(replaced_clone_id)
                    assignment_event = TaskAssignmentEvent(
                        job_id=request.job_id,
                        task_id=pending.task_id,
                        graph_version=graph.version,
                        employee_id=employee.employee_id,
                        employee_role=employee.role,
                        employee_temporary=employee.temporary,
                        required_capabilities=pending.required_capabilities,
                        depends_on=pending.depends_on,
                        attempt=pending.attempt,
                        final_task=pending.task_id == graph.final_task_id,
                        selection_reason=assignment_reason,
                        active_task_count=len(running) + 1,
                        capability_profile_digest=capability_profile.profile_digest,
                        capability_material_digest=capability_profile.material_digest,
                        task_relevance=tuple(
                            sorted(
                                set(pending.required_capabilities)
                                & set(employee.capabilities)
                            )
                        ),
                        chosen_over_employee_ids=tuple(sorted(alternatives)),
                        profile_difference=profile_difference,
                        execution_instance_id=execution_instance_id,
                        replica_group_id="" if replica is None else replica.group_id,
                        replica_id="" if replica is None else replica.replica_id,
                        replica_strategy=(
                            "" if replica is None else replica.strategy.value
                        ),
                        replica_scope="" if replica is None else replica.scope,
                        replica_value_reason=(
                            "" if replica is None else replica.marginal_value_reason
                        ),
                    )
                    if self.assignment_admission is not None:
                        try:
                            self.assignment_admission(assignment_event)
                        except Exception:
                            terminal_status = JobStatus.FAILED
                            failure_reason = (
                                "Frozen route admission rejected task dispatch."
                            )
                            break
                    future = asyncio.create_task(
                        self._execute(record, runtime_request, semaphore),
                        name=f"firm-task:{request.job_id}:{pending.task_id}",
                    )
                    running[future] = record
                    graph = replace_task(
                        graph,
                        replace(
                            pending,
                            status=TaskStatus.RUNNING,
                            assignee_id=employee.employee_id,
                        ),
                    )
                    busy.add(employee.employee_id)
                    assignees_used.add(employee.employee_id)
                    attempted_assignees.setdefault(pending.task_id, []).append(
                        employee.employee_id
                    )
                    forced_assignees.pop(pending.task_id, None)
                    pending_reservations.pop(pending.task_id, None)
                    pending_source_attempts.pop(pending.task_id, None)
                    pending_retry_instructions.pop(pending.task_id, None)
                    collapsed_profile_owners.pop(pending.task_id, None)
                    open_slots -= 1
                    allocation_slots = max(1, allocation_slots - 1)
                    maximum_parallelism = max(maximum_parallelism, len(running))
                    assigned_profiles[employee.employee_id] = capability_profile
                    self._emit_assignment(assignment_event)

            if terminal_status is not None:
                break
            if not running:
                tasks = task_map(graph)
                pending_tasks = [task for task in tasks.values() if task.status == TaskStatus.PENDING]
                dependency_failed = any(
                    any(
                        tasks[dependency_id].status in {TaskStatus.FAILED, TaskStatus.CANCELLED}
                        for dependency_id in task.depends_on
                    )
                    for task in pending_tasks
                )
                if dependency_failed:
                    terminal_status = JobStatus.FAILED
                    failure_reason = "A failed dependency made the final task unreachable."
                elif self._reserve_budget(
                    request,
                    usage,
                    running,
                    committed_reservations=pending_reservations.values(),
                ) is None:
                    terminal_status = JobStatus.BUDGET_EXHAUSTED
                    failure_reason = "Job model, tool, or cost budget was exhausted."
                else:
                    # A graph can be structurally valid yet have no runnable
                    # Employee assignment. Give the bounded replanner one
                    # typed opportunity to repair that condition before
                    # terminalizing it as stalled. This is not an authority
                    # bypass: the exact same graph/lease admission checks as
                    # result-triggered patches apply below.
                    if (
                        self.replanner is not None
                        and request.graph_mutation_policy == "BOUNDED_AUTO"
                        and graph_patch_count < request.job_limits.max_graph_patches
                        and pending_tasks
                    ):
                        trigger_task = min(pending_tasks, key=lambda item: item.task_id)
                        try:
                            stalled_patch = await self.replanner.propose(
                                ReplanContext(
                                    request=request,
                                    graph=graph,
                                    trigger_task=trigger_task,
                                    signal=RunSignal(
                                        SignalCode.GRAPH_STALLED,
                                        value="NO_RUNNABLE_ASSIGNMENT",
                                        evidence=("scheduler:no-runnable-assignment",),
                                    ),
                                    roster=tuple(roster),
                                )
                            )
                            if stalled_patch is not None:
                                before_patch = graph
                                rewritten_graph = apply_patch(
                                    graph,
                                    stalled_patch,
                                    max_tasks=request.job_limits.max_tasks,
                                )
                                if self._within_structural_mutation_distance(
                                    graph,
                                    rewritten_graph,
                                    request,
                                ):
                                    before_task_ids = {
                                        item.task_id for item in before_patch.tasks
                                    }
                                    added_tasks = tuple(
                                        item
                                        for item in rewritten_graph.tasks
                                        if item.task_id not in before_task_ids
                                    )
                                    patch_reservations: list[_Reservation] = []
                                    for _task in added_tasks:
                                        reservation = self._reserve_budget(
                                            request,
                                            usage,
                                            running,
                                            committed_reservations=(
                                                *pending_reservations.values(),
                                                *patch_reservations,
                                            ),
                                            allocation_slots=max(
                                                1,
                                                len(added_tasks) - len(patch_reservations),
                                            ),
                                        )
                                        if reservation is None:
                                            patch_reservations = []
                                            break
                                        patch_reservations.append(reservation)
                                    if len(patch_reservations) == len(added_tasks):
                                        mutation_lease = GraphMutationLease(
                                            model_calls=sum(item.model_calls for item in patch_reservations),
                                            tool_calls=sum(item.tool_calls for item in patch_reservations),
                                            cost_usd=round(sum(item.cost_usd for item in patch_reservations), 12),
                                        )
                                        patch_event = graph_patch_event(
                                            sequence=graph_patch_count + 1,
                                            patch=stalled_patch,
                                            before=before_patch,
                                            after=rewritten_graph,
                                            mutation_lease=mutation_lease,
                                        )
                                        if self.active_job_ledger is not None:
                                            self.active_job_ledger.append_graph_patch(
                                                request.job_id,
                                                patch_event,
                                            )
                                        graph = rewritten_graph
                                        for task, reservation in zip(added_tasks, patch_reservations):
                                            pending_reservations[task.task_id] = reservation
                                        graph_patch_count += 1
                                        graph_patch_events.append(patch_event)
                                        if stalled_patch.semantic_operation == SemanticOperation.INSERT:
                                            organization_admission_count += 1
                                        self._emit_graph_patch(patch_event)
                                        continue
                        except Exception:
                            # Replanner or admission errors never erase the
                            # current valid graph; normal stalled handling
                            # below remains the safe terminal outcome.
                            pass
                    terminal_status = JobStatus.STALLED
                    failure_reason = (
                        "No materially independent Employee profile could satisfy the "
                        "pending review or reroute."
                        if homogeneous_profile_blocked
                        else "Pending tasks exist, but none can be staffed and scheduled."
                    )
                break

            remaining_seconds = max(0.001, deadline - loop.time())
            done, _ = await asyncio.wait(
                set(running),
                timeout=remaining_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                terminal_status = JobStatus.BUDGET_EXHAUSTED
                failure_reason = "Job wall-time limit was exhausted."
                break

            completion = await self._consume_managed_completion(
                request=request,
                running=running,
                done=done,
                graph=graph,
                roster=roster,
                results=results,
                usage=usage,
                loop=loop,
                deadline=deadline,
                frozen_snapshot_hash=frozen_snapshot_hash,
                attempt_records=attempt_records,
                mutation_events=mutation_events,
                graph_patch_events=graph_patch_events,
                graph_patch_proposal_events=graph_patch_proposal_events,
                graph_patch_count=graph_patch_count,
                organization_admission_count=organization_admission_count,
                task_mutation_count=task_mutation_count,
                terminal_status=terminal_status,
                failure_reason=failure_reason,
                retry_counts=retry_counts,
                reroute_counts=reroute_counts,
                attempted_assignees=attempted_assignees,
                forced_assignees=forced_assignees,
                pending_reservations=pending_reservations,
                pending_source_attempts=pending_source_attempts,
                pending_retry_instructions=pending_retry_instructions,
                pending_graph_proposal_hold=pending_graph_proposal_hold,
            )
            graph = completion.graph
            usage = completion.usage
            terminal_status = completion.terminal_status
            failure_reason = completion.failure_reason
            graph_patch_count = completion.graph_patch_count
            organization_admission_count = completion.organization_admission_count
            task_mutation_count = completion.task_mutation_count
            pending_graph_proposal_hold = completion.pending_graph_proposal_hold
            if terminal_status is not None:
                break

        return await self._terminalize_managed_run(
            request=request,
            running=running,
            failure_reason=failure_reason,
            graph=graph,
            results=results,
            usage=usage,
            frozen_snapshot_hash=frozen_snapshot_hash,
            attempt_records=attempt_records,
            roster=roster,
            assignees_used=assignees_used,
            terminal_status=terminal_status,
            graph_patch_count=graph_patch_count,
            organization_admission_count=organization_admission_count,
            task_mutation_count=task_mutation_count,
            maximum_parallelism=maximum_parallelism,
            mutation_events=mutation_events,
            graph_patch_events=graph_patch_events,
            graph_patch_proposal_events=graph_patch_proposal_events,
            pending_graph_proposal_hold=pending_graph_proposal_hold,
            company_budget_lease=company_budget_lease,
            company_budget_authority=company_budget_authority,
            prior_specialist_material_profiles=prior_specialist_material_profiles,
            continuation_preserves_graph_shape=continuation_preserves_graph_shape,
        )
