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


class FirmKernelManagedContinuationMixin:
    def _restore_managed_ledger(
        self,
        *,
        request: CompanyRunRequest,
        graph: JobGraph,
        results: dict[str, EmployeeRunResult],
        assignees_used: set[str],
        same_job_continuation: bool,
        partial_read_only_continuation: bool,
        approved_graph_proposal_continuation: bool,
        resumed_results: Mapping[str, EmployeeRunResult] | None,
        frozen_snapshot_hash: str,
    ) -> tuple[JobGraph, dict[str, EmployeeRunResult], set[str]]:
        if self.active_job_ledger is not None:
            if (
                not same_job_continuation
                and not partial_read_only_continuation
                and not approved_graph_proposal_continuation
            ):
                assert_fresh_entry = getattr(
                    self.active_job_ledger,
                    "assert_fresh_kernel_entry",
                    None,
                )
                if callable(assert_fresh_entry):
                    assert_fresh_entry(request)
            # A continuation must append to the original immutable ACTIVE JOB
            # snapshot.  Re-running ``start_job`` would either create a second
            # authority record or fail on the existing snapshot before the
            # one-shot receipt can be claimed.  Every continuation path below
            # independently validates and consumes its own durable receipt.
            if not (
                same_job_continuation
                or partial_read_only_continuation
                or approved_graph_proposal_continuation
            ):
                self.active_job_ledger.start_job(request, graph, frozen_snapshot_hash)
            if same_job_continuation:
                claim = getattr(self.active_job_ledger, "claim_same_job_continuation", None)
                if not callable(claim):
                    raise RuntimeError(
                        "Same-Job continuation requires a receipt-aware ACTIVE JOB ledger"
                    )
                claim(request, graph, frozen_snapshot_hash)
            elif partial_read_only_continuation:
                claim_partial = getattr(
                    self.active_job_ledger,
                    "claim_partial_read_only_continuation",
                    None,
                )
                if not callable(claim_partial):
                    raise RuntimeError(
                        "Partial continuation requires a receipt-aware ACTIVE JOB ledger"
                    )
                resumed_results = claim_partial(request, graph, frozen_snapshot_hash)
                if not isinstance(resumed_results, dict):
                    raise RuntimeError("Partial continuation ledger returned an invalid result set")
                for task_id, result in resumed_results.items():
                    if not isinstance(result, EmployeeRunResult) or result.status is not RunStatus.SUCCEEDED:
                        raise RuntimeError("Partial continuation result is not a successful Employee receipt")
                    task = task_map(graph).get(task_id)
                    if task is None or result.task_id != task_id:
                        raise RuntimeError("Partial continuation result task does not match the frozen graph")
                    results[task_id] = result
                    graph = replace_task(
                        graph,
                        replace(task, status=TaskStatus.SUCCEEDED, runtime_result=result),
                    )
                    assignees_used.add(result.employee_id)
            elif approved_graph_proposal_continuation:
                if resumed_results is None:
                    raise RuntimeError("Graph continuation is missing durable result receipts")
                for task_id, result in resumed_results.items():
                    if not isinstance(result, EmployeeRunResult) or result.status is not RunStatus.SUCCEEDED:
                        raise RuntimeError("Graph continuation result is not a successful Employee receipt")
                    task = task_map(graph).get(task_id)
                    if task is None or result.task_id != task_id:
                        raise RuntimeError("Graph continuation result task does not match approved graph")
                    results[task_id] = result
                    graph = replace_task(
                        graph,
                        replace(task, status=TaskStatus.SUCCEEDED, runtime_result=result),
                    )
                    assignees_used.add(result.employee_id)
        elif (
            same_job_continuation
            or partial_read_only_continuation
            or approved_graph_proposal_continuation
        ):
            raise RuntimeError("Same-Job continuation requires an ACTIVE JOB ledger")
        return graph, results, assignees_used
