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



class FirmKernelIngressMixin:
    def _has_pending_receipt_bound_settlement(self, request: CompanyRunRequest) -> bool:
        """Keep a proven prefix reservation fail-closed for operator recovery.

        An exception after a successful dependency receipt can occur before a
        terminal aggregate is written.  Charging it at the full ceiling would
        make the receipt unusable for exact settlement.  We retain the active
        reservation only when at least one durable result receipt exists; it
        remains unavailable to every other Job until the recovery service
        settles known cost or forfeits it.  All other aborts preserve the
        existing immediate full-forfeit behavior.
        """

        ledger = self.active_job_ledger
        store = None if ledger is None else getattr(ledger, "store", None)
        receipts = None if store is None else getattr(store, "list_job_dependency_result_receipts", None)
        if not callable(receipts):
            return False
        try:
            return bool(receipts(request.job_id))
        except Exception:
            return False

    async def run(self, request: CompanyRunRequest) -> JobResult:
        """Run one managed Company Job with fail-closed budget cleanup."""

        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask] = {}
        tracked_budget = (
            None
            if self.company_budget_authority is None
            else _TrackedCompanyBudgetAuthority(self.company_budget_authority)
        )
        try:
            return await self._run_managed(
                request,
                company_budget_authority=tracked_budget,
                running=running,
            )
        except BaseException as error:
            if running:
                try:
                    await self._cancel_running(
                        running,
                        "Managed Company Job was cancelled or aborted",
                        request,
                    )
                except BaseException as cancellation_error:
                    error.add_note(
                        "Dispatched Employee cleanup failed. "
                        f"{type(cancellation_error).__name__}: {cancellation_error}"
                    )
            if tracked_budget is not None and not self._has_pending_receipt_bound_settlement(request):
                reason = (
                    "MANAGED_JOB_CANCELLED"
                    if isinstance(error, asyncio.CancelledError)
                    else "MANAGED_JOB_ABORTED"
                )
                try:
                    tracked_budget.forfeit_unsettled(reason=reason)
                except BaseException as finalization_error:
                    error.add_note(
                        "Company budget lease forfeiture failed; the durable "
                        "reservation remains fail-closed. "
                        f"{type(finalization_error).__name__}: {finalization_error}"
                    )
            raise

    async def continue_same_job(self, request: CompanyRunRequest) -> JobResult:
        """Consume an explicit continuation receipt for an untouched Job.

        Callers must first obtain the receipt from the ACTIVE JOB inspector.
        This method is intentionally separate from :meth:`run`: ordinary Job
        submission must never accidentally become a same-Job restart path.
        """

        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask] = {}
        tracked_budget = (
            None
            if self.company_budget_authority is None
            else _TrackedCompanyBudgetAuthority(self.company_budget_authority)
        )
        try:
            return await self._run_managed(
                request,
                company_budget_authority=tracked_budget,
                running=running,
                same_job_continuation=True,
            )
        except BaseException as error:
            if running:
                try:
                    await self._cancel_running(
                        running,
                        "Managed same-Job continuation was cancelled or aborted",
                        request,
                    )
                except BaseException as cancellation_error:
                    error.add_note(
                        "Dispatched Employee cleanup failed. "
                        f"{type(cancellation_error).__name__}: {cancellation_error}"
                    )
            if tracked_budget is not None:
                reason = (
                    "SAME_JOB_CONTINUATION_CANCELLED"
                    if isinstance(error, asyncio.CancelledError)
                    else "SAME_JOB_CONTINUATION_ABORTED"
                )
                try:
                    tracked_budget.forfeit_unsettled(reason=reason)
                except BaseException as finalization_error:
                    error.add_note(
                        "Company budget lease forfeiture failed; the durable "
                        "reservation remains fail-closed. "
                        f"{type(finalization_error).__name__}: {finalization_error}"
                    )
            raise

    async def continue_partial_read_only_job(
        self,
        request: CompanyRunRequest,
        *,
        pending_execution_session_key: str,
    ) -> JobResult:
        """Resume only receipt-proven completed work in an interrupted read-only Job.

        This is intentionally separate from ordinary submission and fresh-start
        continuation.  The ledger must atomically consume a prior operator
        admission before any new Employee can be dispatched.  That Employee
        always gets the supplied fresh execution session: it is deliberately
        outside the frozen Company request so the retained snapshot, route and
        admission can still be compared exactly before dispatch.
        """

        if not isinstance(pending_execution_session_key, str) or not pending_execution_session_key:
            raise ValueError("Partial continuation requires a fresh execution session key")
        if pending_execution_session_key in {
            request.session_key,
            request.manager_session_key,
        }:
            raise ValueError("Partial continuation cannot retain an existing execution session")

        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask] = {}
        tracked_budget = (
            None
            if self.company_budget_authority is None
            else _TrackedCompanyBudgetAuthority(self.company_budget_authority)
        )
        try:
            return await self._run_managed(
                request,
                company_budget_authority=tracked_budget,
                running=running,
                partial_read_only_continuation=True,
                execution_session_key=pending_execution_session_key,
            )
        except BaseException as error:
            if running:
                try:
                    await self._cancel_running(
                        running,
                        "Managed partial read-only continuation was cancelled or aborted",
                        request,
                    )
                except BaseException as cancellation_error:
                    error.add_note(
                        "Dispatched Employee cleanup failed. "
                        f"{type(cancellation_error).__name__}: {cancellation_error}"
                    )
            if tracked_budget is not None:
                reason = (
                    "PARTIAL_CONTINUATION_CANCELLED"
                    if isinstance(error, asyncio.CancelledError)
                    else "PARTIAL_CONTINUATION_ABORTED"
                )
                try:
                    tracked_budget.forfeit_unsettled(reason=reason)
                except BaseException as finalization_error:
                    error.add_note(
                        "Company budget lease forfeiture failed; the durable "
                        "reservation remains fail-closed. "
                        f"{type(finalization_error).__name__}: {finalization_error}"
                    )
            raise

    async def continue_approved_graph_proposal(
        self,
        request: CompanyRunRequest,
        proposal: GraphPatchProposalEvent,
    ) -> JobResult:
        """Resume one paused Job from an exact, durably approved graph proposal.

        This is deliberately not a generic ``resume``.  The ACTIVE JOB ledger
        must reconstruct the original frozen graph, prove the proposed patch's
        before/after digests, retain only receipt-proven successful work, and
        atomically consume the exact approval before this method can dispatch.
        """

        ledger = self.active_job_ledger
        prepare = None if ledger is None else getattr(
            ledger,
            "prepare_claimed_graph_proposal_continuation",
            None,
        )
        claim = None if ledger is None else getattr(
            ledger,
            "claim_approved_graph_proposal",
            None,
        )
        if not callable(prepare) or not callable(claim):
            raise RuntimeError(
                "Approved Graph continuation requires a receipt-aware ACTIVE JOB ledger"
            )
        if proposal.status is not GraphPatchProposalStatus.APPROVED:
            raise ValueError("Only an approved Graph proposal can resume a Job")

        # Reconstruct before consuming the durable admission.  A malformed or
        # stale candidate therefore leaves the paused job recoverable.
        continuation = prepare(request, proposal)
        claim(request.job_id, proposal)
        patch_event = graph_patch_event(
            sequence=continuation.prior_graph_patch_count + 1,
            patch=proposal.patch,
            before=continuation.before_graph,
            after=continuation.graph,
            mutation_lease=proposal.proposed_lease,
        )
        append_claimed = getattr(ledger, "append_claimed_graph_proposal_patch", None)
        activate_claimed = getattr(ledger, "activate_claimed_graph_proposal", None)
        if not callable(append_claimed) or not callable(activate_claimed):
            raise RuntimeError(
                "Approved Graph continuation requires an atomic lifecycle-aware ledger"
            )
        # Claim, paused lease reservation, append-only patch and activation
        # are individually idempotent.  A crash before activation remains
        # paused and retryable; dispatch cannot observe an admitted Job until
        # the matching patch record exists.
        append_claimed(request.job_id, proposal, patch_event)
        activate_claimed(request.job_id, proposal, patch_event)

        pending = graph_patch_proposal_event(
            patch=proposal.patch,
            before=continuation.before_graph,
            after=continuation.graph,
            proposed_lease=proposal.proposed_lease,
            status=GraphPatchProposalStatus.PENDING,
        )
        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask] = {}
        tracked_budget = (
            None
            if self.company_budget_authority is None
            else _TrackedCompanyBudgetAuthority(self.company_budget_authority)
        )
        try:
            return await self._run_managed(
                request,
                company_budget_authority=tracked_budget,
                running=running,
                approved_graph_proposal_continuation=True,
                resumed_graph=continuation.graph,
                resumed_results=continuation.completed_results,
                initial_graph_patch_events=(patch_event,),
                initial_graph_patch_proposal_events=(pending, proposal),
                initial_graph_patch_count=continuation.prior_graph_patch_count + 1,
                prior_specialist_material_profiles=(
                    continuation.prior_specialist_material_profiles
                ),
                continuation_preserves_graph_shape=True,
            )
        except BaseException as error:
            if running:
                try:
                    await self._cancel_running(
                        running,
                        "Managed approved Graph continuation was cancelled or aborted",
                        request,
                    )
                except BaseException as cancellation_error:
                    error.add_note(
                        "Dispatched Employee cleanup failed. "
                        f"{type(cancellation_error).__name__}: {cancellation_error}"
                    )
            if tracked_budget is not None:
                reason = (
                    "GRAPH_PROPOSAL_CONTINUATION_CANCELLED"
                    if isinstance(error, asyncio.CancelledError)
                    else "GRAPH_PROPOSAL_CONTINUATION_ABORTED"
                )
                try:
                    tracked_budget.forfeit_unsettled(reason=reason)
                except BaseException as finalization_error:
                    error.add_note(
                        "Company budget lease forfeiture failed; the durable "
                        "reservation remains fail-closed. "
                        f"{type(finalization_error).__name__}: {finalization_error}"
                    )
            raise

    async def continue_rejected_graph_proposal(
        self,
        request: CompanyRunRequest,
        proposal: GraphPatchProposalEvent,
    ) -> JobResult:
        """Resume the exact prior Graph after one durable user rejection.

        Rejecting a proposal is not a generic lifecycle ``RESUME``: it must
        consume the same pending candidate exactly once, rehydrate only
        receipt-proven completed work, and prove that no proposed Graph patch
        became executable before dispatch restarts.
        """

        ledger = self.active_job_ledger
        prepare = None if ledger is None else getattr(
            ledger,
            "prepare_rejected_graph_proposal_continuation",
            None,
        )
        claim = None if ledger is None else getattr(
            ledger,
            "claim_rejected_graph_proposal",
            None,
        )
        activate = None if ledger is None else getattr(
            ledger,
            "activate_rejected_graph_proposal",
            None,
        )
        if not callable(prepare) or not callable(claim) or not callable(activate):
            raise RuntimeError(
                "Rejected Graph continuation requires a receipt-aware ACTIVE JOB ledger"
            )
        if proposal.status is not GraphPatchProposalStatus.REJECTED:
            raise ValueError("Only a rejected Graph proposal can restore the prior Job")
        continuation = prepare(request, proposal)
        claim(request.job_id, proposal)
        activate(request.job_id, proposal)
        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask] = {}
        tracked_budget = (
            None
            if self.company_budget_authority is None
            else _TrackedCompanyBudgetAuthority(self.company_budget_authority)
        )
        # Preserve the immutable candidate identities from the durable ledger;
        # ``graph_patch_proposal_event`` cannot recreate a rejected candidate
        # from the prior Graph because its candidate's after-digest is the
        # rejected topology rather than the resumed one.
        pending = replace(
            proposal,
            status=GraphPatchProposalStatus.PENDING,
            event_id="",
            content_hash="",
        )
        from .mutation import content_digest

        unsigned = {
            "patch": pending.patch,
            "before_graph_digest": pending.before_graph_digest,
            "after_graph_digest": pending.after_graph_digest,
            "proposed_lease": pending.proposed_lease,
            "status": pending.status.value,
        }
        pending = replace(
            pending,
            event_id=f"graph-proposal-event-{content_digest(unsigned)[:24]}",
        )
        pending = replace(pending, content_hash=content_digest(pending))
        try:
            return await self._run_managed(
                request,
                company_budget_authority=tracked_budget,
                running=running,
                approved_graph_proposal_continuation=True,
                resumed_graph=continuation.before_graph,
                resumed_results=continuation.completed_results,
                initial_graph_patch_proposal_events=(pending, proposal),
                initial_graph_patch_count=continuation.prior_graph_patch_count,
                prior_specialist_material_profiles=(
                    continuation.prior_specialist_material_profiles
                ),
                continuation_preserves_graph_shape=True,
            )
        except BaseException as error:
            if running:
                try:
                    await self._cancel_running(
                        running,
                        "Managed rejected Graph continuation was cancelled or aborted",
                        request,
                    )
                except BaseException as cancellation_error:
                    error.add_note(
                        "Dispatched Employee cleanup failed. "
                        f"{type(cancellation_error).__name__}: {cancellation_error}"
                    )
            if tracked_budget is not None:
                reason = (
                    "GRAPH_PROPOSAL_REJECTION_CANCELLED"
                    if isinstance(error, asyncio.CancelledError)
                    else "GRAPH_PROPOSAL_REJECTION_ABORTED"
                )
                try:
                    tracked_budget.forfeit_unsettled(reason=reason)
                except BaseException as finalization_error:
                    error.add_note(
                        "Company budget lease forfeiture failed; the durable "
                        "reservation remains fail-closed. "
                        f"{type(finalization_error).__name__}: {finalization_error}"
                    )
            raise
