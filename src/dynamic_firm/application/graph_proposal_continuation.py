"""Surface-neutral durable decisions for paused Graph mutation proposals.

CLI, terminal TUI and a future GUI share this narrow service.  It never
reconstructs a request from ACTIVE JOB audit content, never accepts a caller
supplied Graph patch, and only resumes after the exact local Work Order and
append-only proposal receipts agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from dynamic_firm.company.work_order_portfolio import WorkOrderPortfolioStore
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    GraphPatchProposalEvent,
    GraphPatchProposalStatus,
    JobResult,
)
from dynamic_firm.kernel.mutation import graph_patch_proposal_resolution_event
from dynamic_firm.runtime.job_ledger import SQLiteActiveJobLedger


KernelContinuation = Callable[[CompanyRunRequest, GraphPatchProposalEvent], Awaitable[JobResult]]


@dataclass(frozen=True, slots=True)
class GraphProposalDecisionOutcome:
    """The only product-level result of an explicit Graph decision."""

    job_id: str
    proposal_id: str
    decision: GraphPatchProposalStatus
    result: JobResult


class GraphProposalContinuationService:
    """Resolve exactly one durable pending proposal and continue its same Job."""

    def __init__(
        self,
        *,
        work_orders: WorkOrderPortfolioStore,
        ledger: SQLiteActiveJobLedger,
        continue_approved: KernelContinuation,
        continue_rejected: KernelContinuation,
    ) -> None:
        self.work_orders = work_orders
        self.ledger = ledger
        self.continue_approved = continue_approved
        self.continue_rejected = continue_rejected

    async def decide(
        self,
        *,
        job_id: str,
        proposal_id: str,
        approve: bool,
    ) -> GraphProposalDecisionOutcome:
        """Apply a one-shot decision using only user-local request authority."""

        request = self.work_orders.continuation_request(job_id)
        pending = self.ledger.pending_graph_proposal(job_id, proposal_id)
        decision = graph_patch_proposal_resolution_event(
            pending,
            status=(
                GraphPatchProposalStatus.APPROVED
                if approve
                else GraphPatchProposalStatus.REJECTED
            ),
        )
        self.ledger.resolve_graph_proposal(job_id, decision)
        result = await (
            self.continue_approved(request, decision)
            if approve
            else self.continue_rejected(request, decision)
        )
        return GraphProposalDecisionOutcome(
            job_id=job_id,
            proposal_id=proposal_id,
            decision=decision.status,
            result=result,
        )
