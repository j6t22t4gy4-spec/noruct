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
    ManagerSupervisionDecision,
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


class FirmKernelResultMixin:

    @staticmethod
    def _validate_result_boundary(
        request: CompanyRunRequest,
        record: _RunningTask,
        result: EmployeeRunResult,
    ) -> EmployeeRunResult:
        if (
            result.job_id == request.job_id
            and result.task_id == record.task_id
            and result.employee_id == record.employee.employee_id
        ):
            return result
        return replace(
            result,
            job_id=request.job_id,
            task_id=record.task_id,
            employee_id=record.employee.employee_id,
            status=RunStatus.FAILED,
            summary="Employee runtime returned a mismatched result envelope.",
            failure=Failure(
                code="EMPLOYEE_RESULT_IDENTITY_MISMATCH",
                category=FailureCategory.INTERNAL,
                message_safe="Employee runtime result identity did not match the dispatch.",
            ),
        )

    @staticmethod
    def _with_terminal_semantic_signals(
        result: EmployeeRunResult,
    ) -> EmployeeRunResult:
        """Expose only exhausted local validation as a bounded graph signal.

        The Employee Runtime already gives its completion validator one local
        repair.  Turning the first failed check into topology churn would make
        ordinary harness recovery look like organizational intelligence.  A
        terminal completion/coding validation failure is different: a
        configured Replanner may inspect it, but the existing Kernel still
        validates policy, graph shape, incremental budget lease, and patch
        count before anything changes.
        """

        failure = result.failure
        if (
            result.status is not RunStatus.FAILED
            or failure is None
            or failure.code not in {
                "COMPLETION_VALIDATION_FAILED",
                "CODING_VALIDATION_FAILED",
            }
            or any(signal.code is SignalCode.VALIDATION_FAILED for signal in result.signals)
        ):
            return result
        return replace(
            result,
            signals=(
                *result.signals,
                RunSignal(
                    SignalCode.VALIDATION_FAILED,
                    value=failure.code,
                    evidence=(f"runtime-failure:{failure.code}",),
                ),
            ),
        )

    async def _apply_manager_supervision(
        self,
        *,
        request: CompanyRunRequest,
        graph: JobGraph,
        task: JobTask,
        result: EmployeeRunResult,
        remaining_wall_time_ms: int,
    ) -> EmployeeRunResult:
        """Project a Manager's bounded advisory signal, never its authority."""

        # DIRECT is intentionally free of portfolio/supervision overhead.
        if (
            self.manager_supervisor is None
            or request.company_work_mode == "DIRECT"
            or not request.manager_employee_id
        ):
            return result, None
        context = supervision_context(
            request=request,
            graph=graph,
            task=task,
            result=result,
            remaining_wall_time_ms=remaining_wall_time_ms,
        )
        try:
            decision = await self.manager_supervisor.assess(
                context
            )
            if not isinstance(decision, ManagerSupervisionDecision):
                raise TypeError("Manager supervisor returned an untyped decision")
            # Revalidate at the Kernel boundary instead of trusting that an
            # adapter constructed (and did not corrupt) a frozen dataclass.
            decision = ManagerSupervisionDecision(
                action=decision.action,
                rationale=decision.rationale,
                signal=decision.signal,
            )
            observation = (
                decision.action.value,
                None if decision.signal is None else decision.signal.code.value,
                context.priority,
                context.remaining_wall_time_ms,
                len(context.capability_shortage),
                context.conflicting_outcome,
            )
        except Exception:
            # The current graph and completed result remain authoritative if a
            # Manager call fails or returns an invalid decision.
            return result, None
        if decision.action.value != "SIGNAL" or decision.signal is None:
            return result, observation
        if any(signal.code is decision.signal.code for signal in result.signals):
            return result, observation
        return replace(result, signals=(*result.signals, decision.signal)), observation

    @staticmethod
    def _within_structural_mutation_distance(
        before: JobGraph,
        after: JobGraph,
        request: CompanyRunRequest,
    ) -> bool:
        """Reject automatic rewrites whose structural delta is too broad.

        Graph engineering is a bounded control loop, not an open-ended graph
        generator. Patch count alone cannot prevent one accepted rewrite from
        replacing the entire executable topology, so this checks node, edge,
        and capability-set distance before budget reservation. The limits are
        intentionally derived from the already-frozen Job task cap rather
        than adding mutable product configuration to a running request.
        """

        before_tasks = {task.task_id: task for task in before.tasks}
        after_tasks = {task.task_id: task for task in after.tasks}
        node_distance = len(set(before_tasks).symmetric_difference(after_tasks))
        before_edges = {
            (task.task_id, dependency)
            for task in before.tasks
            for dependency in task.depends_on
        }
        after_edges = {
            (task.task_id, dependency)
            for task in after.tasks
            for dependency in task.depends_on
        }
        edge_distance = len(before_edges.symmetric_difference(after_edges))
        capability_distance = sum(
            len(
                set(before_tasks[task_id].required_capabilities).symmetric_difference(
                    after_tasks[task_id].required_capabilities
                )
            )
            for task_id in set(before_tasks).intersection(after_tasks)
        )
        capability_distance += sum(
            len(task.required_capabilities)
            for task_id, task in after_tasks.items()
            if task_id not in before_tasks
        )
        # At least one standard SPLIT/JOIN/MERGE remains possible in a small
        # Job, while a broadly replacing revision is never auto-admitted.
        node_limit = max(3, min(8, request.job_limits.max_tasks // 2))
        edge_limit = max(6, min(16, request.job_limits.max_tasks))
        capability_limit = max(6, min(24, request.job_limits.max_tasks * 2))
        return (
            node_distance <= node_limit
            and edge_distance <= edge_limit
            and capability_distance <= capability_limit
        )

    @staticmethod
    def _task_status(status: RunStatus) -> TaskStatus:
        if status == RunStatus.SUCCEEDED:
            return TaskStatus.SUCCEEDED
        if status == RunStatus.CANCELLED:
            return TaskStatus.CANCELLED
        return TaskStatus.FAILED

    @staticmethod
    def _exceeds_reservation(usage: Usage, reservation: _Reservation) -> bool:
        return (
            usage.model_calls > reservation.model_calls
            or usage.tool_calls > reservation.tool_calls
            or usage.cost_usd > reservation.cost_usd + 1e-12
        )

    @staticmethod
    def _result(
        *,
        request: CompanyRunRequest,
        graph: JobGraph,
        roster: tuple[EmployeeRecord, ...],
        results: dict[str, EmployeeRunResult],
        assignees_used: set[str],
        usage: Usage,
        status: JobStatus,
        graph_patch_count: int,
        organization_admission_count: int,
        task_mutation_count: int,
        maximum_parallelism: int,
        failure_reason: str,
        attempt_records: tuple[TaskAttemptRecord, ...],
        mutation_events: tuple[JobMutationEvent, ...],
        graph_patch_events: tuple[GraphPatchEvent, ...],
        graph_patch_proposal_events: tuple[GraphPatchProposalEvent, ...] = (),
        prior_specialist_material_profiles: frozenset[str] = frozenset(),
        continuation_preserves_graph_shape: bool = False,
    ) -> JobResult:
        ordered_results = tuple(results[task_id] for task_id in sorted(results))
        final_result = results.get(graph.final_task_id)
        evidence = tuple(
            dict.fromkeys(
                item
                for result in ordered_results
                for item in result.acceptance_evidence
            )
        )
        unresolved = tuple(
            dict.fromkeys(
                item
                for result in ordered_results
                for item in result.unresolved_issues
            )
        )
        roster_by_id = {employee.employee_id: employee for employee in roster}
        temporary_used = sum(
            1 for employee_id in assignees_used if roster_by_id[employee_id].temporary
        )
        summary = (
            final_result.summary
            if final_result is not None and final_result.status == RunStatus.SUCCEEDED
            else failure_reason or f"Job ended with status {status.value}."
        )
        specialist_material_profiles = {
            record.capability_material_digest
            for record in attempt_records
            if record.employee_id != request.manager_employee_id
        }
        specialist_material_profiles.update(prior_specialist_material_profiles)
        replica_groups = {
            record.replica_group_id
            for record in attempt_records
            if record.replica_group_id
        }
        replica_count = sum(
            1 for record in attempt_records if record.replica_group_id
        )
        return JobResult(
            job_id=request.job_id,
            request_id=request.request_id,
            status=status,
            summary=summary,
            acceptance_evidence=evidence,
            unresolved_issues=unresolved,
            task_results=ordered_results,
            final_graph_version=graph.version,
            final_tasks=graph.tasks,
            metrics=JobMetrics(
                unique_employee_count=len(assignees_used),
                temporary_role_count=temporary_used,
                maximum_parallelism=maximum_parallelism,
                graph_patch_count=graph_patch_count,
                usage=usage,
                task_mutation_count=task_mutation_count,
                organization_admission_count=organization_admission_count,
                manager_integration_count=(
                    1
                    if request.manager_employee_id
                    and final_result is not None
                    and final_result.employee_id == request.manager_employee_id
                    else 0
                ),
                execution_replica_count=replica_count,
                replica_group_count=len(replica_groups),
            ),
            final_task_id=graph.final_task_id,
            failure_reason=failure_reason,
            planning_mode=request.planning_mode,
            planning_reason=request.planning_reason,
            compiler_usage=request.compiler_usage,
            compiler_provider_request_id=request.compiler_provider_request_id,
            manager_employee_id=request.manager_employee_id,
            work_order_id=request.work_order_id,
            work_order_digest=request.work_order_digest,
            work_order_authority_digest=request.work_order_authority_digest,
            firm_admission_digest=request.firm_admission_digest,
            initial_company_work_mode=request.company_work_mode,
            company_work_mode=(
                "DIRECT"
                if request.company_work_mode == "DIRECT"
                else "TEAM_JOB"
                if continuation_preserves_graph_shape and len(graph.tasks) > 1
                else "SOLO_JOB"
                if continuation_preserves_graph_shape
                else "TEAM_JOB"
                if len(specialist_material_profiles) > 1 or replica_count > 1
                else "SOLO_JOB"
            ),
            coordination_policy=request.coordination_policy,
            requested_effect=request.requested_effect,
            operating_reason=request.operating_reason,
            attempt_records=attempt_records,
            mutation_events=mutation_events,
            graph_patch_events=graph_patch_events,
            graph_patch_proposal_events=graph_patch_proposal_events,
            graph_blueprint_id=request.graph_blueprint_id,
            graph_blueprint_version=request.graph_blueprint_version,
            graph_blueprint_digest=request.graph_blueprint_digest,
            graph_mutation_policy=request.graph_mutation_policy,
            graph_constraints_digest=request.graph_constraints_digest,
            graph_pinned_employee_ids=request.graph_pinned_employee_ids,
            graph_excluded_employee_ids=request.graph_excluded_employee_ids,
            graph_require_independent_review=request.graph_require_independent_review,
            graph_max_concurrency=request.graph_max_concurrency,
            graph_max_cost_usd=request.graph_max_cost_usd,
            graph_max_wall_time_ms=request.graph_max_wall_time_ms,
        )
