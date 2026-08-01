from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

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

@dataclass(frozen=True)
class _ManagedCompletion:
    graph: JobGraph
    usage: Usage
    terminal_status: JobStatus | None
    failure_reason: str
    graph_patch_count: int
    organization_admission_count: int
    task_mutation_count: int
    pending_graph_proposal_hold: bool


class FirmKernelManagedCompletionMixin:
    """Consume one completed Employee attempt and apply bounded adaptations."""

    async def _consume_managed_completion(
        self,
        *,
        request: CompanyRunRequest,
        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask],
        done: Iterable[asyncio.Task[EmployeeRunResult]],
        graph: JobGraph,
        roster: list[EmployeeRecord],
        results: dict[str, EmployeeRunResult],
        usage: Usage,
        loop: asyncio.AbstractEventLoop,
        deadline: float,
        frozen_snapshot_hash: str,
        attempt_records: list[TaskAttemptRecord],
        mutation_events: list[JobMutationEvent],
        graph_patch_events: list[GraphPatchEvent],
        graph_patch_proposal_events: list[GraphPatchProposalEvent],
        graph_patch_count: int,
        organization_admission_count: int,
        task_mutation_count: int,
        terminal_status: JobStatus | None,
        failure_reason: str,
        retry_counts: dict[str, int],
        reroute_counts: dict[str, int],
        attempted_assignees: Mapping[str, list[str]],
        forced_assignees: dict[str, EmployeeRecord],
        pending_reservations: dict[str, _Reservation],
        pending_source_attempts: dict[str, str],
        pending_retry_instructions: dict[str, str],
        pending_graph_proposal_hold: bool,
    ) -> _ManagedCompletion:
        # Consume one result, then recompute the entire ready set. Other
        # completed futures remain in `running` and are consumed next.
        future = min(done, key=lambda item: running[item].task_id)
        record = running.pop(future)
        result = await future
        result = self._validate_result_boundary(request, record, result)
        task = task_map(graph)[record.task_id]
        result, _ = enforce_employee_completion_liveness(
            objective=task.objective,
            result=result,
        )
        result = self._with_terminal_semantic_signals(result)
        consume_operator_signals = getattr(
            self.active_job_ledger,
            "consume_operator_signals",
            None,
        )
        if callable(consume_operator_signals):
            try:
                operator_signals = consume_operator_signals(
                    job_id=request.job_id,
                    task_id=task.task_id,
                )
            except Exception:
                operator_signals = ()
            if operator_signals:
                existing_codes = {signal.code for signal in result.signals}
                result = replace(
                    result,
                    signals=(
                        *result.signals,
                        *(
                            signal
                            for signal in operator_signals
                            if signal.code not in existing_codes
                        ),
                    ),
                )
        result, supervision = await self._apply_manager_supervision(
            request=request,
            graph=graph,
            task=task,
            result=result,
            remaining_wall_time_ms=max(0, int((deadline - loop.time()) * 1000)),
        )
        results[record.task_id] = result
        usage = usage.plus(result.usage)
        task_status = self._task_status(result.status)
        graph = replace_task(
            graph,
            replace(task, status=task_status, runtime_result=result),
        )
        completed_attempt = attempt_record(
            attempt_id=record.attempt_id,
            request=request,
            task=task,
            employee=record.employee,
            source_attempt_id=record.source_attempt_id,
            graph_version=graph.version,
            result=result,
            frozen_snapshot_hash=frozen_snapshot_hash,
            capability_profile_digest=record.capability_profile_digest,
            capability_material_digest=record.capability_material_digest,
        )
        if self.active_job_ledger is not None:
            try:
                self.active_job_ledger.append_attempt(request.job_id, completed_attempt)
                append_dependency_receipt = getattr(
                    self.active_job_ledger,
                    "append_dependency_result_receipt",
                    None,
                )
                if callable(append_dependency_receipt) and result.status is RunStatus.SUCCEEDED:
                    append_dependency_receipt(
                        request.job_id,
                        completed_attempt,
                        result,
                    )
            except Exception:
                if running:
                    await self._cancel_running(
                        running,
                        "ACTIVE JOB attempt ledger append failed",
                        request,
                    )
                raise
        if supervision is not None and self.active_job_ledger is not None:
            append_supervision = getattr(
                self.active_job_ledger,
                "append_supervision",
                None,
            )
            if callable(append_supervision):
                try:
                    append_supervision(
                        job_id=request.job_id,
                        attempt_id=completed_attempt.attempt_id,
                        task_id=task.task_id,
                        manager_employee_id=request.manager_employee_id,
                        action=supervision[0],
                        signal_code=supervision[1],
                        priority=supervision[2],
                        remaining_wall_time_ms=supervision[3],
                        capability_shortage_count=supervision[4],
                        conflicting_outcome=supervision[5],
                    )
                except Exception:
                    # The Manager is advisory. A failed optional operator
                    # projection must not invalidate the completed task or
                    # mutate the current valid Job graph.
                    pass
        attempt_records.append(completed_attempt)
        if self._exceeds_reservation(result.usage, record.reservation):
            terminal_status = JobStatus.BUDGET_EXHAUSTED
            failure_reason = "Employee execution exceeded its reserved job budget."
            return _ManagedCompletion(
                graph=graph,
                usage=usage,
                terminal_status=terminal_status,
                failure_reason=failure_reason,
                graph_patch_count=graph_patch_count,
                organization_admission_count=organization_admission_count,
                task_mutation_count=task_mutation_count,
                pending_graph_proposal_hold=pending_graph_proposal_hold,
            )

        candidate = self._mutation_candidate(
            request=request,
            graph=graph,
            task=task,
            record=record,
            result=result,
            usage=usage,
            running=running,
            committed_reservations=pending_reservations.values(),
            task_mutation_count=task_mutation_count,
            retry_count=retry_counts.get(task.task_id, 0),
            reroute_count=reroute_counts.get(task.task_id, 0),
            attempted_employee_ids=set(attempted_assignees.get(task.task_id, ())),
        )
        if candidate is not None:
            target_task = replace(
                task,
                status=TaskStatus.PENDING,
                assignee_id=None,
                attempt=task.attempt + 1,
                runtime_result=None,
            )
            target_attempt_id = attempt_identity(
                request=request,
                task=target_task,
                employee_id=candidate.employee.employee_id,
                graph_version=graph.version,
                frozen_snapshot_hash=frozen_snapshot_hash,
            )
            event = mutation_event(
                sequence=task_mutation_count + 1,
                mutation_type=candidate.mutation_type,
                task=task,
                source_attempt_id=record.attempt_id,
                source_attempt_content_hash=completed_attempt.content_hash,
                target_attempt_id=target_attempt_id,
                from_employee_id=record.employee.employee_id,
                to_employee_id=candidate.employee.employee_id,
                failure_kind=candidate.failure_kind,
                downstream_task_ids=candidate.downstream_task_ids,
                mutation_budget_before=(
                    request.job_limits.max_task_mutations - task_mutation_count
                ),
                reservation=AttemptBudgetEvidence(
                    model_calls=candidate.reservation.model_calls,
                    tool_calls=candidate.reservation.tool_calls,
                    cost_usd=candidate.reservation.cost_usd,
                    wall_time_ceiling_ms=min(
                        request.runtime_limits.max_wall_time_ms,
                        request.job_limits.max_wall_time_ms,
                    ),
                ),
                frozen_snapshot_hash=frozen_snapshot_hash,
            )
            if self.active_job_ledger is not None:
                try:
                    self.active_job_ledger.append_mutation(request.job_id, event)
                except Exception:
                    if running:
                        await self._cancel_running(
                            running,
                            "ACTIVE JOB mutation ledger append failed",
                            request,
                        )
                    raise
            graph = replace_task(graph, target_task)
            results.pop(record.task_id, None)
            forced_assignees[record.task_id] = candidate.employee
            pending_reservations[record.task_id] = candidate.reservation
            pending_source_attempts[record.task_id] = record.attempt_id
            if candidate.failure_kind == AttemptFailureKind.RECOVERABLE_LIVENESS:
                pending_retry_instructions[record.task_id] = (
                    LIVENESS_CONTINUATION_INSTRUCTION
                )
            task_mutation_count += 1
            if candidate.mutation_type == TaskMutationType.RETRY:
                retry_counts[record.task_id] = retry_counts.get(record.task_id, 0) + 1
            else:
                reroute_counts[record.task_id] = reroute_counts.get(record.task_id, 0) + 1
            mutation_events.append(event)
            self._emit_mutation(event)
            return _ManagedCompletion(
                graph=graph,
                usage=usage,
                terminal_status=terminal_status,
                failure_reason=failure_reason,
                graph_patch_count=graph_patch_count,
                organization_admission_count=organization_admission_count,
                task_mutation_count=task_mutation_count,
                pending_graph_proposal_hold=pending_graph_proposal_hold,
            )

        # Blueprint policy applies at the sole Kernel rewrite authority.
        # LOCKED preserves the selected structure. PROPOSE may obtain one
        # bounded candidate but can commit it only after a one-time
        # operator decision; BOUNDED_AUTO uses the same validation path
        # without that interactive checkpoint.
        if (
            self.replanner is not None
            and request.graph_mutation_policy in {"BOUNDED_AUTO", "PROPOSE"}
        ):
            for signal in result.signals:
                # A Replanner may now answer any typed execution signal.
                # The default capability replanner still declines every
                # non-capability signal; only a separately configured,
                # bounded replanner can propose SPLIT/JOIN/MERGE/CANCEL.
                # Whatever it returns is atomically validated below by
                # the Kernel rather than trusted as model authority.
                try:
                    patch = await self.replanner.propose(
                        ReplanContext(
                            request=request,
                            graph=graph,
                            trigger_task=task_map(graph)[record.task_id],
                            signal=signal,
                            roster=tuple(roster),
                        )
                    )
                except Exception:
                    # A Replanner is a bounded proposal source, not Job
                    # authority.  Its failure cannot invalidate the current
                    # graph or strand already-running employee attempts.
                    continue
                if patch is None:
                    continue
                if graph_patch_count >= request.job_limits.max_graph_patches:
                    # The patch budget limits organization adaptation, not
                    # the already-valid employee work.  A second proposed
                    # expansion is safely declined and the current graph
                    # keeps running through its final integration task.
                    # Treating this as a job budget failure stranded valid
                    # final tasks and made a harmless model signal appear
                    # as an execution failure to the user.
                    continue
                before_patch = graph
                try:
                    rewritten_graph = apply_patch(
                        graph,
                        patch,
                        max_tasks=request.job_limits.max_tasks,
                    )
                except GraphValidationError:
                    # Invalid or stale proposals are nonfatal rejections.
                    # Continue deterministic scheduling from the last valid
                    # graph without consuming the graph-patch budget.
                    continue
                if not self._within_structural_mutation_distance(
                    graph,
                    rewritten_graph,
                    request,
                ):
                    # A valid primitive rewrite can still be too broad for
                    # automatic supervision. It remains inspectable in the
                    # replanner's own evidence, but never enters the Job
                    # audit or consumes budget as an accepted revision.
                    continue
                # Reserve only the concrete newly-added work before the
                # topology changes. Existing pending tasks retain their
                # normal dispatch reservation path; cancelling or merely
                # rewiring a task never creates synthetic budget credit.
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
                if len(patch_reservations) != len(added_tasks):
                    # A proposal without a bounded delta lease is simply
                    # declined. The current valid graph stays executable.
                    continue
                mutation_lease = GraphMutationLease(
                    model_calls=sum(item.model_calls for item in patch_reservations),
                    tool_calls=sum(item.tool_calls for item in patch_reservations),
                    cost_usd=round(
                        sum(item.cost_usd for item in patch_reservations),
                        12,
                    ),
                )
                if request.graph_mutation_policy == "PROPOSE":
                    proposal_status = await self._request_graph_patch_approval(
                        request=request,
                        patch=patch,
                        before=before_patch,
                        after=rewritten_graph,
                        proposed_lease=mutation_lease,
                    )
                    proposal_event = graph_patch_proposal_event(
                        patch=patch,
                        before=before_patch,
                        after=rewritten_graph,
                        proposed_lease=mutation_lease,
                        status=proposal_status,
                    )
                    graph_patch_proposal_events.append(proposal_event)
                    if self.active_job_ledger is not None:
                        self.active_job_ledger.append_graph_proposal(
                            request.job_id,
                            proposal_event,
                        )
                    self._emit_graph_patch_proposal(proposal_event)
                    if proposal_status is GraphPatchProposalStatus.PENDING:
                        hold_graph_proposal = getattr(
                            self.active_job_ledger,
                            "hold_graph_proposal",
                            None,
                        )
                        if not callable(hold_graph_proposal):
                            raise RuntimeError(
                                "Durable Graph approval requires a lifecycle-aware ACTIVE JOB ledger"
                            )
                        # The receipt is durable before the lifecycle is
                        # paused.  No graph mutation, dispatch or lease
                        # settlement occurs while the operator decides.
                        hold_graph_proposal(request.job_id, proposal_event)
                        pending_graph_proposal_hold = True
                        terminal_status = JobStatus.STALLED
                        failure_reason = (
                            "Job is paused for an explicit Graph proposal decision."
                        )
                        break
                    if proposal_status is not GraphPatchProposalStatus.APPROVED:
                        # A rejected or unavailable decision leaves the
                        # current valid graph and every lease untouched.
                        continue
                patch_event = graph_patch_event(
                    sequence=graph_patch_count + 1,
                    patch=patch,
                    before=before_patch,
                    after=rewritten_graph,
                    mutation_lease=mutation_lease,
                )
                if self.active_job_ledger is not None:
                    try:
                        self.active_job_ledger.append_graph_patch(
                            request.job_id,
                            patch_event,
                        )
                    except Exception:
                        if running:
                            await self._cancel_running(
                                running,
                                "ACTIVE JOB graph patch ledger append failed",
                                request,
                            )
                        raise
                graph = rewritten_graph
                for task, reservation in zip(added_tasks, patch_reservations):
                    pending_reservations[task.task_id] = reservation
                graph_patch_count += 1
                graph_patch_events.append(patch_event)
                if patch.semantic_operation == SemanticOperation.INSERT:
                    organization_admission_count += 1
                self._emit_graph_patch(patch_event)
                break

        return _ManagedCompletion(
            graph=graph,
            usage=usage,
            terminal_status=terminal_status,
            failure_reason=failure_reason,
            graph_patch_count=graph_patch_count,
            organization_admission_count=organization_admission_count,
            task_mutation_count=task_mutation_count,
            pending_graph_proposal_hold=pending_graph_proposal_hold,
        )
