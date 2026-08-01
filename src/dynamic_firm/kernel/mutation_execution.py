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



class FirmKernelMutationExecutionMixin:
    def _mutation_candidate(
        self,
        *,
        request: CompanyRunRequest,
        graph: JobGraph,
        task: JobTask,
        record: _RunningTask,
        result: EmployeeRunResult,
        usage: Usage,
        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask],
        committed_reservations: Iterable[_Reservation],
        task_mutation_count: int,
        retry_count: int,
        reroute_count: int,
        attempted_employee_ids: set[str],
    ) -> _MutationCandidate | None:
        if result.status != RunStatus.FAILED:
            return None
        if task_mutation_count >= request.job_limits.max_task_mutations:
            return None
        if not structurally_read_only(record.action_policy):
            return None
        downstream = self._downstream_task_ids(graph, task.task_id)
        tasks = task_map(graph)
        if any(
            tasks[task_id].status not in {TaskStatus.PENDING, TaskStatus.CANCELLED}
            for task_id in downstream
        ):
            return None
        reservation = self._reserve_budget(
            request,
            usage,
            running,
            committed_reservations=committed_reservations,
        )
        if reservation is None:
            return None
        failure_kind = classify_attempt_failure(result)
        if failure_kind in RECOVERABLE_FAILURE_KINDS:
            if retry_count >= 1:
                return None
            return _MutationCandidate(
                mutation_type=TaskMutationType.RETRY,
                employee=record.employee,
                failure_kind=failure_kind,
                reservation=reservation,
                downstream_task_ids=downstream,
            )
        if failure_kind != AttemptFailureKind.ASSIGNEE_MISMATCH or reroute_count >= 1:
            return None
        employee = reroute_candidate(
            task,
            request.roster,
            current_employee_id=record.employee.employee_id,
            attempted_employee_ids=attempted_employee_ids,
            pinned_employee_ids=set(request.graph_pinned_employee_ids),
            excluded_employee_ids=(
                set(request.graph_excluded_employee_ids)
                | self._independent_review_employee_ids(graph, task)
            ),
        )
        if employee is None:
            return None
        return _MutationCandidate(
            mutation_type=TaskMutationType.REROUTE,
            employee=employee,
            failure_kind=failure_kind,
            reservation=reservation,
            downstream_task_ids=downstream,
        )

    @staticmethod
    def _downstream_task_ids(graph: JobGraph, source_task_id: str) -> tuple[str, ...]:
        downstream: set[str] = set()
        pending = [source_task_id]
        while pending:
            current = pending.pop()
            for candidate in graph.tasks:
                if current in candidate.depends_on and candidate.task_id not in downstream:
                    downstream.add(candidate.task_id)
                    pending.append(candidate.task_id)
        return tuple(sorted(downstream))

    @staticmethod
    def _independent_review_employee_ids(
        graph: JobGraph,
        task: JobTask,
    ) -> set[str]:
        """Keep an independent reviewer distinct from the final result owner.

        The compiler can require a review capability and a direct dependency,
        but employee identity is known only after staffing.  Enforcing the
        separation here prevents a broad-capability employee from reviewing
        and accepting its own work.  Ordinary non-review dependencies do not
        force extra staffing.
        """

        if task.task_id != graph.final_task_id:
            return set()
        tasks = task_map(graph)
        reviewer_ids: set[str] = set()
        for dependency_id in task.depends_on:
            dependency = tasks[dependency_id]
            if dependency.assignee_id and any(
                capability in {
                    "review",
                    "independent_review",
                    "validation",
                    "verification",
                }
                or capability.endswith("_review")
                for capability in dependency.required_capabilities
            ):
                reviewer_ids.add(dependency.assignee_id)
        return reviewer_ids

    @staticmethod
    def _manager_integrator(
        request: CompanyRunRequest,
        graph: JobGraph,
        task: JobTask,
        results: dict[str, EmployeeRunResult],
        *,
        busy_employee_ids: set[str],
    ) -> EmployeeRecord | None:
        """Select the Manager only for an artifact-backed read-only final report.

        This reuses the graph's existing final model call; it does not add a
        second verification/report loop. Effectful final ownership remains
        with an exact-capable specialist until the later G3 writer contract.
        """

        manager = request.manager_employee
        if (
            manager is None
            or not request.manager_delegation_digest
            or request.requested_effect != "READ"
            or task.task_id != graph.final_task_id
            or not task.depends_on
            or manager.employee_id in busy_employee_ids
            or manager.employee_id in request.graph_excluded_employee_ids
        ):
            return None
        if any(
            dependency_id not in results
            or results[dependency_id].status != RunStatus.SUCCEEDED
            for dependency_id in task.depends_on
        ):
            return None
        return manager

    @staticmethod
    def _validate_graph_constraints_against_graph(
        request: CompanyRunRequest,
        graph: JobGraph,
    ) -> None:
        """Require an actual review edge when the user asked for one.

        The Kernel does not infer a reviewer from a label or create a hidden
        role-play loop.  A selected Blueprint must explicitly contain a
        review/verification dependency of the final integration task.  At
        dispatch the reviewer identity is then excluded from that final task.
        """

        if not request.graph_require_independent_review:
            return
        tasks = task_map(graph)
        final_task = tasks[graph.final_task_id]
        for dependency_id in final_task.depends_on:
            dependency = tasks[dependency_id]
            if any(
                capability in {
                    "review",
                    "independent_review",
                    "validation",
                    "verification",
                }
                or capability.endswith("_review")
                for capability in dependency.required_capabilities
            ):
                return
        raise ValueError(
            "Graph independent review requires an explicit review dependency of the final task"
        )
    @staticmethod
    def _effective_max_concurrency(request: CompanyRunRequest) -> int:
        """Return the never-widening graph-specific dispatch ceiling."""

        if request.graph_max_concurrency is None:
            return request.job_limits.max_concurrency
        return min(request.job_limits.max_concurrency, request.graph_max_concurrency)

    def _emit_mutation(self, event: JobMutationEvent) -> None:
        if self.mutation_sink is None:
            return
        try:
            self.mutation_sink(event)
        except Exception:
            # Product projection cannot change Kernel execution semantics.
            return

    def _emit_assignment(self, event: TaskAssignmentEvent) -> None:
        if self.assignment_sink is None:
            return
        try:
            self.assignment_sink(event)
        except Exception:
            # Product projection cannot change Kernel execution semantics.
            return

    def _emit_graph_patch(self, event: GraphPatchEvent) -> None:
        if self.graph_patch_sink is None:
            return
        try:
            self.graph_patch_sink(event)
        except Exception:
            # Product projection cannot change Kernel execution semantics.
            return

    def _emit_graph_patch_proposal(self, event: GraphPatchProposalEvent) -> None:
        if self.graph_patch_proposal_sink is None:
            return
        try:
            self.graph_patch_proposal_sink(event)
        except Exception:
            # Product projection cannot change Kernel execution semantics.
            return

    async def _request_graph_patch_approval(
        self,
        *,
        request: CompanyRunRequest,
        patch: GraphPatch,
        before: JobGraph,
        after: JobGraph,
        proposed_lease: GraphMutationLease,
    ) -> GraphPatchProposalStatus:
        """Ask for one explicit decision without creating reusable authority.

        The shared approval transport is only a modal decision channel here:
        no tool is executed, no session-wide grant is retained, and a normal
        append-only GraphPatchEvent still validates and commits any approved
        rewrite after this method returns.
        """

        if self.approval_port is None:
            # PROPOSE is an explicit user authority boundary.  When the
            # process has a durable ACTIVE JOB ledger but no live modal port,
            # retain the exact candidate and pause rather than silently
            # treating a missing UI as a rejection or auto-approval.
            if callable(getattr(self.active_job_ledger, "hold_graph_proposal", None)):
                return GraphPatchProposalStatus.PENDING
            return GraphPatchProposalStatus.UNAVAILABLE
        proposal_identity = content_digest(
            {
                "job_id": request.job_id,
                "patch_id": patch.patch_id,
                "before_graph_digest": frozen_snapshot_digest(request),
                "base_graph_version": before.version,
                "target_graph_version": after.version,
            }
        )[:24]
        approval_request = ApprovalRequest(
            action_id=f"graph-patch-proposal-{proposal_identity}",
            run_id=f"firm-kernel:{request.job_id}",
            job_id=request.job_id,
            task_id=patch.trigger_task_id,
            employee_id="firm-kernel",
            tool_name="propose_graph_patch",
            effect=ToolEffect.WRITE,
            risk=ToolRisk.MEDIUM,
            resource_key=(
                f"job-graph:{request.job_id}:v{before.version}->v{after.version}"
            ),
            preview=(
                f"Apply {patch.semantic_operation.value} graph proposal "
                f"({len(patch.operations)} operation(s), "
                f"{len(after.tasks) - len(before.tasks):+d} task(s), "
                f"lease Δ${proposed_lease.cost_usd:.4f})"
            ),
            allow_session=False,
        )
        try:
            decision = await self.approval_port.request(
                approval_request,
                CancellationToken(),
            )
        except Exception:
            return GraphPatchProposalStatus.UNAVAILABLE
        if decision is ApprovalDecision.ALLOW_ONCE:
            return GraphPatchProposalStatus.APPROVED
        if decision is ApprovalDecision.DENY:
            return GraphPatchProposalStatus.REJECTED
        return GraphPatchProposalStatus.UNAVAILABLE

    async def _execute(
        self,
        record: _RunningTask,
        request: EmployeeRunRequest,
        semaphore: asyncio.Semaphore,
    ) -> tuple[EmployeeRunResult, tuple[str, str | None, str, int, int, bool] | None]:
        started_at = datetime.now(UTC)
        try:
            async with semaphore:
                record.handle = await self.employee_execution.start(request)
                return await self.employee_execution.collect(record.handle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return EmployeeRunResult(
                run_id=f"kernel-failure:{request.request_id}",
                request_id=request.request_id,
                job_id=request.task.job_id,
                task_id=request.task.task_id,
                employee_id=request.employee.employee_id,
                status=RunStatus.FAILED,
                summary="Employee execution failed at the runtime boundary.",
                output_artifact_refs=(),
                acceptance_evidence=(),
                unresolved_issues=(f"Runtime boundary failure: {type(exc).__name__}",),
                observations=(),
                suggested_followups=(),
                signals=(),
                partial_result=False,
                usage=Usage(),
                last_event_seq=0,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                failure=Failure(
                    code="EMPLOYEE_EXECUTION_BOUNDARY_FAILED",
                    category=FailureCategory.INTERNAL,
                    message_safe=f"Employee runtime failed with {type(exc).__name__}.",
                ),
            )

    async def _cancel_running(
        self,
        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask],
        reason: str,
        request: CompanyRunRequest,
    ) -> dict[asyncio.Task[EmployeeRunResult], EmployeeRunResult]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._CANCEL_COLLECTION_GRACE_SECONDS
        cancellation_requests: set[asyncio.Task[object]] = set()
        for future, record in running.items():
            if record.handle is not None:
                cancellation_requests.add(
                    asyncio.create_task(
                        self.employee_execution.cancel(record.handle, reason)
                    )
                )
            else:
                # A collector which has not acquired the execution semaphore
                # has no external run to cancel.
                future.cancel()

        if cancellation_requests:
            completed_cancels, pending_cancels = await asyncio.wait(
                cancellation_requests,
                timeout=max(0.0, deadline - loop.time()),
            )
            for completed in completed_cancels:
                self._consume_task_terminal(completed)
            for pending in pending_cancels:
                pending.cancel()
                pending.add_done_callback(self._consume_task_terminal)

        collectors = set(running)
        completed_collectors = {future for future in collectors if future.done()}
        pending_collectors = collectors - completed_collectors
        if pending_collectors and loop.time() < deadline:
            completed, pending_collectors = await asyncio.wait(
                pending_collectors,
                timeout=max(0.0, deadline - loop.time()),
            )
            completed_collectors.update(completed)
        for pending in pending_collectors:
            pending.cancel()
            pending.add_done_callback(self._consume_task_terminal)

        outcomes: dict[asyncio.Task[EmployeeRunResult], EmployeeRunResult] = {}
        for future, record in running.items():
            if future in completed_collectors:
                try:
                    value = future.result()
                except BaseException:
                    value = None
                if isinstance(value, EmployeeRunResult):
                    outcomes[future] = value
                    continue
            outcomes[future] = self._kernel_cancelled_result(request, record)
        return outcomes

    @staticmethod
    def _consume_task_terminal(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            return

    @staticmethod
    def _budget_forfeit_reason(
        request: CompanyRunRequest,
        result: JobResult,
    ) -> str | None:
        if (
            request.planning_reason in {
                "COMPILER_WALL_TIME_EXHAUSTED",
                "COMPILER_PROVIDER_FAILURE",
            }
            and request.compiler_usage.model_calls > 0
        ):
            return "COMPILER_USAGE_UNCERTAIN"
        for task_result in result.task_results:
            if task_result.status == RunStatus.CANCELLED:
                return "MANAGED_RUN_USAGE_UNCERTAIN"
            failure = task_result.failure
            if failure is None:
                continue
            if failure.category in {FailureCategory.TIMEOUT, FailureCategory.CANCEL}:
                return "MANAGED_RUN_USAGE_UNCERTAIN"
            if failure.code in {
                "EMPLOYEE_EXECUTION_BOUNDARY_FAILED",
                "MODEL_PROVIDER_ERROR",
            }:
                return "MANAGED_RUN_USAGE_UNCERTAIN"
        return None

    @staticmethod
    def _kernel_cancelled_result(
        request: CompanyRunRequest,
        record: _RunningTask,
    ) -> EmployeeRunResult:
        now = datetime.now(UTC)
        return EmployeeRunResult(
            run_id=(
                record.handle.run_id
                if record.handle is not None
                else f"kernel-cancelled:{request.request_id}:{record.task_id}"
            ),
            request_id=request.request_id,
            job_id=request.job_id,
            task_id=record.task_id,
            employee_id=record.employee.employee_id,
            status=RunStatus.CANCELLED,
            summary="Employee execution was cancelled by the Firm terminal control.",
            output_artifact_refs=(),
            acceptance_evidence=(),
            unresolved_issues=("Firm terminal control cancelled this attempt.",),
            observations=(),
            suggested_followups=(),
            signals=(),
            partial_result=False,
            usage=Usage(),
            last_event_seq=0,
            started_at=now,
            finished_at=now,
            failure=Failure(
                code="EMPLOYEE_CANCELLED_BY_FIRM",
                category=FailureCategory.CANCEL,
                message_safe="Firm terminal control cancelled the employee attempt.",
            ),
        )
