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
from .mutation_execution import FirmKernelMutationExecutionMixin
from .policy_request import FirmKernelPolicyMixin
from .result_supervision import FirmKernelResultMixin
from .ingress import FirmKernelIngressMixin
from .managed_continuation import FirmKernelManagedContinuationMixin


class FirmKernelManagedTerminalMixin:
    async def _terminalize_managed_run(
        self,
        *,
        request: CompanyRunRequest,
        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask],
        failure_reason: str,
        graph: JobGraph,
        results: dict[str, EmployeeRunResult],
        usage: Usage,
        frozen_snapshot_hash: str,
        attempt_records: list[TaskAttemptRecord],
        roster: list[EmployeeRecord],
        assignees_used: set[str],
        terminal_status: JobStatus | None,
        graph_patch_count: int,
        organization_admission_count: int,
        task_mutation_count: int,
        maximum_parallelism: int,
        mutation_events: list[JobMutationEvent],
        graph_patch_events: list[GraphPatchEvent],
        graph_patch_proposal_events: list[GraphPatchProposalEvent],
        pending_graph_proposal_hold: bool,
        company_budget_lease: CompanyBudgetLease | None,
        company_budget_authority: CompanyBudgetAuthorityPort | None,
        prior_specialist_material_profiles: frozenset[str],
        continuation_preserves_graph_shape: bool,
    ) -> JobResult:
        if running:
            cancelled = await self._cancel_running(
                running,
                failure_reason or "Job reached a terminal state",
                request,
            )
            for future, record in sorted(
                running.items(), key=lambda item: item[1].task_id
            ):
                result = self._validate_result_boundary(
                    request,
                    record,
                    cancelled[future],
                )
                task = task_map(graph)[record.task_id]
                results[record.task_id] = result
                usage = usage.plus(result.usage)
                graph = replace_task(
                    graph,
                    replace(
                        task,
                        status=self._task_status(result.status),
                        runtime_result=result,
                    ),
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
                    # Every dispatched attempt, including an attempt stopped by
                    # the Firm's own terminal decision, is durable before the
                    # terminal aggregate is written.
                    self.active_job_ledger.append_attempt(
                        request.job_id,
                        completed_attempt,
                    )
                attempt_records.append(completed_attempt)
        final_result = self._result(
            request=request,
            graph=graph,
            roster=tuple(roster),
            results=results,
            assignees_used=assignees_used,
            usage=usage,
            status=terminal_status or JobStatus.FAILED,
            graph_patch_count=graph_patch_count,
            organization_admission_count=organization_admission_count,
            task_mutation_count=task_mutation_count,
            maximum_parallelism=maximum_parallelism,
            failure_reason=failure_reason,
            attempt_records=tuple(attempt_records),
            mutation_events=tuple(mutation_events),
            graph_patch_events=tuple(graph_patch_events),
            graph_patch_proposal_events=tuple(graph_patch_proposal_events),
            prior_specialist_material_profiles=prior_specialist_material_profiles,
            continuation_preserves_graph_shape=continuation_preserves_graph_shape,
        )
        if pending_graph_proposal_hold:
            # This is deliberately not a terminal Job result despite using a
            # compact existing status for compatibility with current product
            # projections.  The durable PAUSED lifecycle row is authoritative;
            # it retains the original company-budget lease and prevents a
            # terminal audit or a second dispatch until a later exact resume.
            return final_result
        if company_budget_lease is not None:
            # Settlement replaces the conservative admission reservation before
            # the terminal audit is exposed.  If it cannot persist, the ACTIVE
            # JOB remains interrupted and the reservation fails closed rather
            # than silently allowing another company job to start.
            assert company_budget_authority is not None
            forfeit_reason = self._budget_forfeit_reason(request, final_result)
            if forfeit_reason is None:
                company_budget_authority.settle_job(company_budget_lease, final_result)
            else:
                company_budget_authority.forfeit_job(
                    company_budget_lease,
                    reason=forfeit_reason,
                )
        if self.active_job_ledger is not None:
            self.active_job_ledger.finish_job(request.job_id, final_result)
        return final_result
