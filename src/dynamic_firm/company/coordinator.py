"""Roleplay-free coordination proposals for one Company WorkOrder.

The coordinator is deliberately not an Employee, manager persona, execution
loop, or state authority.  It decides whether the existing bounded workflow
compiler may be called and exposes the typed runtime replanner only to managed
Company Jobs.  Firm admission, graph validation, budgets, permissions, and
execution remain deterministic authorities outside this module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from dynamic_firm.compiler import (
    CapabilityInsertReplanner,
    CompilerDecision,
    CompilerRequest,
    DynamicWorkflowCompiler,
    ManagerFollowUpReplanner,
    SemanticSignalReplanner,
    WorkflowPrior,
)
from dynamic_firm.compiler.admission import OrganizationAdmissionDecision
from dynamic_firm.kernel.models import JobLimits

from .frontdoor import WorkOrder
from .graph_blueprints import (
    BlueprintResolution,
    BlueprintResolutionReason,
    GraphBlueprintRegistry,
    GraphUserConstraints,
    resolve_blueprint,
)
from .operating import InitialCoordinationPolicy


FIRM_COORDINATOR_SCHEMA = "noruct.firm-coordinator.v1"


class FirmCoordinatorPhase(StrEnum):
    INITIAL = "INITIAL"
    RUNTIME = "RUNTIME"


class FirmCoordinatorAction(StrEnum):
    SKIP_DIRECT = "SKIP_DIRECT"
    RUN_SOLO_PROBE = "RUN_SOLO_PROBE"
    REQUEST_PLAN_PROPOSAL = "REQUEST_PLAN_PROPOSAL"
    ENABLE_TYPED_REPLAN = "ENABLE_TYPED_REPLAN"


class FirmCoordinatorReason(StrEnum):
    DIRECT_SUFFICIENT = "DIRECT_SUFFICIENT"
    SOLO_FIRST_ECONOMY = "SOLO_FIRST_ECONOMY"
    PLAN_FIRST_REQUIRED = "PLAN_FIRST_REQUIRED"
    MANAGED_JOB_TYPED_REPLAN = "MANAGED_JOB_TYPED_REPLAN"


@dataclass(frozen=True, slots=True)
class FirmCoordinatorDecision:
    """Auditable coordination proposal with no execution authority."""

    phase: FirmCoordinatorPhase
    action: FirmCoordinatorAction
    reason: FirmCoordinatorReason
    work_order_id: str
    work_order_digest: str
    model_call_allowed: bool
    authority_granted: bool = False
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.work_order_id.strip() or not self.work_order_digest.strip():
            raise ValueError("Firm Coordinator decisions require a WorkOrder binding")
        if self.authority_granted:
            raise ValueError("Firm Coordinator cannot grant execution authority")
        if self.model_call_allowed != (
            self.action == FirmCoordinatorAction.REQUEST_PLAN_PROPOSAL
        ):
            raise ValueError("model_call_allowed does not match coordinator action")
        object.__setattr__(self, "content_digest", self.computed_digest())

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": FIRM_COORDINATOR_SCHEMA,
            "phase": self.phase.value,
            "action": self.action.value,
            "reason": self.reason.value,
            "work_order_id": self.work_order_id,
            "work_order_digest": self.work_order_digest,
            "model_call_allowed": self.model_call_allowed,
            "authority_granted": self.authority_granted,
        }

    def computed_digest(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def verify(self) -> None:
        if self.content_digest != self.computed_digest():
            raise ValueError("Firm Coordinator decision digest is invalid")


class ManagerProposalAdapter:
    """Adapt one frozen Manager WorkOrder into bounded compiler/replan proposals.

    This is deliberately not a second Company actor: it owns neither state,
    authority, budget nor an Employee session.  The compatibility alias below
    preserves historical imports while Product ingress uses this Manager-facing
    name so the runtime cannot be mistaken for two independent planners.
    """

    def __init__(
        self,
        provider: object | None = None,
        *,
        graph_blueprints: GraphBlueprintRegistry | None = None,
    ) -> None:
        self._compiler = DynamicWorkflowCompiler(provider)
        self._graph_blueprints = graph_blueprints

    def initial_decision(self, work_order: WorkOrder) -> FirmCoordinatorDecision:
        work_order.verify()
        policy = work_order.operating_decision.coordination_policy
        if policy == InitialCoordinationPolicy.DIRECT:
            action = FirmCoordinatorAction.SKIP_DIRECT
            reason = FirmCoordinatorReason.DIRECT_SUFFICIENT
        elif policy == InitialCoordinationPolicy.SOLO_FIRST:
            action = FirmCoordinatorAction.RUN_SOLO_PROBE
            reason = FirmCoordinatorReason.SOLO_FIRST_ECONOMY
        else:
            action = FirmCoordinatorAction.REQUEST_PLAN_PROPOSAL
            reason = FirmCoordinatorReason.PLAN_FIRST_REQUIRED
        return FirmCoordinatorDecision(
            phase=FirmCoordinatorPhase.INITIAL,
            action=action,
            reason=reason,
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            model_call_allowed=(
                action == FirmCoordinatorAction.REQUEST_PLAN_PROPOSAL
            ),
        )

    def runtime_decision(self, work_order: WorkOrder) -> FirmCoordinatorDecision:
        """Enable only the typed replanner for a non-DIRECT WorkOrder."""

        work_order.verify()
        if (
            work_order.operating_decision.coordination_policy
            == InitialCoordinationPolicy.DIRECT
        ):
            action = FirmCoordinatorAction.SKIP_DIRECT
            reason = FirmCoordinatorReason.DIRECT_SUFFICIENT
        else:
            action = FirmCoordinatorAction.ENABLE_TYPED_REPLAN
            reason = FirmCoordinatorReason.MANAGED_JOB_TYPED_REPLAN
        return FirmCoordinatorDecision(
            phase=FirmCoordinatorPhase.RUNTIME,
            action=action,
            reason=reason,
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            model_call_allowed=False,
        )

    async def propose_initial_plan(
        self,
        work_order: WorkOrder,
        decision: FirmCoordinatorDecision,
        request: CompilerRequest,
    ) -> CompilerDecision:
        """Request one bounded plan proposal only after deterministic admission."""

        self._verify_binding(work_order, decision)
        if decision.phase != FirmCoordinatorPhase.INITIAL:
            raise ValueError("Initial planning requires an INITIAL decision")
        if decision.action != FirmCoordinatorAction.REQUEST_PLAN_PROPOSAL:
            raise ValueError("Coordinator decision does not permit a planning call")
        if request.goal != work_order.objective:
            raise ValueError("Compiler request is not bound to the WorkOrder objective")
        return await self._compiler.compile(request)

    def resolve_initial_blueprint(
        self,
        work_order: WorkOrder,
        decision: FirmCoordinatorDecision,
        request: CompilerRequest,
        *,
        limits: JobLimits,
        objective_class: str = "general",
        pin_slot: str | None = None,
        constraints: GraphUserConstraints = GraphUserConstraints(),
    ) -> BlueprintResolution:
        """Return a provider-free reusable plan candidate before Compiler use.

        DIRECT requests intentionally retain their graphless path.  A registry
        miss is not an error: callers continue to the existing bounded model
        proposal only when the coordinator already permits it.
        """

        self._verify_binding(work_order, decision)
        if decision.action == FirmCoordinatorAction.SKIP_DIRECT:
            return BlueprintResolution(
                reason=BlueprintResolutionReason.SKIPPED_DIRECT,
                detail="DIRECT Company work does not create a managed Job Graph.",
            )
        if self._graph_blueprints is None:
            return BlueprintResolution(
                reason=BlueprintResolutionReason.NO_COMPATIBLE_BLUEPRINT,
                detail="No local Graph Blueprint registry is configured.",
            )
        if request.goal != work_order.objective:
            raise ValueError("Compiler request is not bound to the WorkOrder objective")
        return resolve_blueprint(
            self._graph_blueprints,
            work_order=work_order,
            objective_class=objective_class,
            execution_profile=request.execution_profile.value.lower(),
            available_capabilities=request.available_capabilities,
            limits=limits,
            pin_slot=pin_slot,
            constraints=constraints,
        )

    def runtime_replanner(
        self,
        work_order: WorkOrder,
        decision: FirmCoordinatorDecision,
        *,
        managed_job: bool,
        decision_sink: Callable[[OrganizationAdmissionDecision], None] | None = None,
        workflow_priors: tuple[WorkflowPrior, ...] = (),
        manager_employee_id: str = "",
    ) -> SemanticSignalReplanner | None:
        """Expose typed bounded replan proposals only inside a managed Job."""

        self._verify_binding(work_order, decision)
        if (
            not managed_job
            or decision.phase != FirmCoordinatorPhase.RUNTIME
            or decision.action != FirmCoordinatorAction.ENABLE_TYPED_REPLAN
        ):
            return None
        replanner = CapabilityInsertReplanner(
            decision_sink=decision_sink,
            workflow_priors=workflow_priors,
        )
        manager_or_capability = (
            ManagerFollowUpReplanner(
                replanner,
                manager_employee_id=manager_employee_id,
            )
            if manager_employee_id.strip()
            else replanner
        )
        return SemanticSignalReplanner(manager_or_capability)

    @staticmethod
    def _verify_binding(
        work_order: WorkOrder,
        decision: FirmCoordinatorDecision,
    ) -> None:
        work_order.verify()
        decision.verify()
        if (
            decision.work_order_id != work_order.work_order_id
            or decision.work_order_digest != work_order.content_digest
        ):
            raise ValueError("Firm Coordinator decision is not bound to this WorkOrder")


# Historical private API compatibility only. Product ingress must construct
# ``ManagerProposalAdapter``; neither name owns Company authority.
FirmCoordinator = ManagerProposalAdapter
