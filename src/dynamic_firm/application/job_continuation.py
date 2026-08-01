"""Explicit application service for receipt-bound same-Job continuation.

The service is deliberately surface-neutral: CLI, TUI and a future GUI can
all make the same user-confirmed call.  It does not recover a request from the
ACTIVE JOB audit, create a new Work Order, or relax the Kernel's receipt claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from dynamic_firm.application.frozen_route_goal_composition import (
    FrozenRouteContinuationBundle,
    FrozenRouteContinuationCatalog,
    FrozenRouteGoalComposition,
)
from dynamic_firm.company.work_order_portfolio import WorkOrderPortfolioStore
from dynamic_firm.kernel.models import CompanyRunRequest, JobResult
from dynamic_firm.runtime.job_ledger import (
    ActiveJobInspector,
    ActiveJobEffectRecovery,
    ActiveJobPartialContinuation,
)
from dynamic_firm.runtime.company_budget import (
    CompanyBudgetAuthorityPort,
    CompanyBudgetForfeit,
    CompanyBudgetSettlement,
)


KernelContinuation = Callable[[CompanyRunRequest, str], Awaitable[JobResult]]


def _recovery_references(request: CompanyRunRequest) -> dict[str, str]:
    """Recreate only the immutable references saved at original admission."""

    references = {
        "firm_admission_digest": request.firm_admission_digest,
        "workspace_context_fingerprint": request.workflow_context_fingerprint,
    }
    if request.execution_origin is not None:
        references["knowledge_pack_digest"] = request.execution_origin.pack_digest
    return {key: value for key, value in references.items() if value}


def _fresh_partial_execution_session_key(
    admission: ActiveJobPartialContinuation,
) -> str:
    """Derive an opaque fresh session identity from the one-shot admission.

    This is dispatch-only state, not a change to the frozen Company request.
    It binds the pending-task session to the exact receipt prefix while keeping
    provider-native thread/session state out of continuation authority.
    """

    payload = {
        "completed_results_digest": admission.completed_results_digest,
        "graph_digest": admission.graph_digest,
        "job_id": admission.job_id,
        "request_id": admission.request_id,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"partial-continuation:{digest}"


@dataclass(frozen=True, slots=True)
class PartialContinuationOutcome:
    """One operator-visible result without exposing raw receipt content."""

    admission: ActiveJobPartialContinuation
    result: JobResult


@dataclass(frozen=True, slots=True)
class EffectRecoveryOutcome:
    """Operator-visible closure for a stopped effectful Job.

    ``REPLACEMENT_WORK_ORDER_REQUIRED`` proves that completed effects were
    already terminal and are not replayed.  ``FAIL_CLOSED`` records a full
    reservation forfeit whenever the prior usage cannot be proven.
    """

    recovery: ActiveJobEffectRecovery
    budget_terminal: CompanyBudgetSettlement | CompanyBudgetForfeit | None


class ReceiptBoundContinuationService:
    """Authorise then execute the only supported partial same-Job recovery."""

    def __init__(
        self,
        *,
        work_orders: WorkOrderPortfolioStore,
        inspector: ActiveJobInspector,
        continue_partial: KernelContinuation,
        company_budget_authority: CompanyBudgetAuthorityPort | None = None,
        frozen_route_composition: FrozenRouteGoalComposition | None = None,
        frozen_route_catalog: FrozenRouteContinuationCatalog | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.work_orders = work_orders
        self.inspector = inspector
        self.continue_partial = continue_partial
        self.company_budget_authority = company_budget_authority
        if frozen_route_composition is not None and not isinstance(
            frozen_route_composition, FrozenRouteGoalComposition
        ):
            raise TypeError("frozen route continuation composition must be typed")
        if frozen_route_catalog is not None and not isinstance(
            frozen_route_catalog, FrozenRouteContinuationCatalog
        ):
            raise TypeError("frozen route continuation catalog must be typed")
        if frozen_route_composition is not None and frozen_route_catalog is not None:
            raise ValueError("frozen route continuation accepts one composition source")
        if state_path is not None and not isinstance(state_path, Path):
            raise TypeError("frozen route continuation state path must be pathlib.Path")
        if ((frozen_route_composition is None and frozen_route_catalog is None)
                != (state_path is None)):
            raise ValueError(
                "frozen route continuation requires a composition source and state path"
            )
        self.frozen_route_composition = frozen_route_composition
        self.frozen_route_catalog = frozen_route_catalog
        self.state_path = state_path

    def _require_frozen_route_continuation_closure(
        self,
        request: CompanyRunRequest,
    ) -> None:
        persisted = self.work_orders.frozen_route_continuation_bundle(request.job_id)
        if persisted is None:
            if self.frozen_route_composition is not None:
                raise ValueError("frozen route continuation composition has no retained Job bundle")
            return
        raw, digest = persisted
        expected = FrozenRouteContinuationBundle.from_canonical_json(raw)
        if expected.digest != digest:
            raise ValueError("persisted frozen route continuation bundle digest drifted")
        if self.frozen_route_composition is None and self.frozen_route_catalog is not None:
            composition = self.frozen_route_catalog.reassemble(expected)
        else:
            composition = self.frozen_route_composition
        if composition is None or self.state_path is None:
            raise ValueError(
                "frozen route continuation requires an exact reassembled composition"
            )
        actual = composition.continuation_bundle_for(
            request,
            state_path=self.state_path,
        )
        if actual != expected:
            raise ValueError("reassembled frozen route continuation does not match retained Job bundle")

    async def resume_partial_read_only_job(self, job_id: str) -> PartialContinuationOutcome:
        """Resume only a receipt-proven, read-only, unmodified graph prefix.

        The inspector persists the one-shot local/remote admission immediately
        before this call.  The injected Kernel entry atomically claims it
        before dispatching any remaining task, so duplicate CLI/TUI/GUI clicks
        fail closed rather than start a second continuation.
        """

        request = self.work_orders.continuation_request(job_id)
        order = self.work_orders.work_order(request.work_order_id)
        self._require_frozen_route_continuation_closure(request)
        admission = self.inspector.authorize_partial_read_only_continuation(
            job_id,
            request=request,
            work_order=order,
            source_references=_recovery_references(request),
        )
        if not isinstance(admission, ActiveJobPartialContinuation):
            raise TypeError("Partial continuation inspector returned an invalid admission")
        result = await self.continue_partial(
            request,
            _fresh_partial_execution_session_key(admission),
        )
        return PartialContinuationOutcome(admission=admission, result=result)

    def reconcile_effectful_interrupted_job(self, job_id: str) -> EffectRecoveryOutcome:
        """Settle a proven completed effect prefix or forfeit it fail-closed.

        This method intentionally creates no Work Order and dispatches no
        remaining task.  The caller must make the subsequent replacement
        submission explicitly, so no effectful attempt is silently replayed.
        """

        request = self.work_orders.continuation_request(job_id)
        order = self.work_orders.work_order(request.work_order_id)
        self.inspector.prepare_work_order_recovery(
            job_id,
            work_order=order,
            source_references=_recovery_references(request),
        )
        recovery = self.inspector.assess_effectful_recovery(job_id)
        budget_terminal: CompanyBudgetSettlement | CompanyBudgetForfeit | None = None
        if self.company_budget_authority is not None:
            admission = self.company_budget_authority.admit_job(request)
            if admission.lease is None:
                raise RuntimeError("Interrupted Job has no active Company budget lease to reconcile")
            budget_terminal = self.company_budget_authority.reconcile_interrupted_job(
                admission.lease,
                observed_cost_usd=(
                    recovery.observed_cost_usd
                    if recovery.disposition == "REPLACEMENT_WORK_ORDER_REQUIRED"
                    else None
                ),
            )
        return EffectRecoveryOutcome(recovery=recovery, budget_terminal=budget_terminal)

    def handoff_partial_read_only_job(
        self,
        job_id: str,
        *,
        target_device_id: str,
    ) -> ActiveJobPartialContinuation:
        """Hand off a pre-claim read-only continuation without executing it.

        The target device must have independently retained the same local Work
        Order and receipts.  There is no sync of result content through this
        application boundary.
        """

        request = self.work_orders.continuation_request(job_id)
        order = self.work_orders.work_order(request.work_order_id)
        return self.inspector.handoff_partial_read_only_continuation(
            job_id,
            request=request,
            work_order=order,
            source_references=_recovery_references(request),
            target_device_id=target_device_id,
        )
