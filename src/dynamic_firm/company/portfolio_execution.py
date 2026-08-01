"""Evidence-gated, operator-driven execution planning for a local portfolio.

This is intentionally not a worker pool or background scheduler.  It turns
the next already-admitted Work Order into one explicit operator plan and
records the Kernel's terminal aggregate result back into the same local
portfolio.  The Company budget, active Job ledger, and effect approvals keep
their existing authorities.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable, Iterable, Mapping

from dynamic_firm.kernel.models import CompanyRunRequest, JobResult, JobStatus
from dynamic_firm.runtime.company_budget import CompanyBudgetAuthorityPort

from .manager_outcomes import (
    ManagerOutcomeAssessment,
    ManagerOutcomeDecision,
    assess_manager_outcomes,
)
from .organization_outcomes import (
    OrganizationEvidenceDecision,
    OrganizationOutcomeAssessment,
    assess_organization_outcomes,
)
from .workflow_models import OrganizationEpisode
from .work_order_portfolio import (
    PortfolioEntry,
    PortfolioJobSettlement,
    PortfolioPolicy,
    PortfolioStatus,
    WorkOrderPortfolioStore,
)
from .frontdoor import WorkOrder


class PortfolioReuseDecision(StrEnum):
    """Whether an automatic Manager/Blueprint topology may be proposed."""

    SOLO_ONLY = "SOLO_ONLY"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    AUTOMATIC_REUSE_ALLOWED = "AUTOMATIC_REUSE_ALLOWED"


class PortfolioRecoveryDecision(StrEnum):
    """Operator-facing continuation boundary for a stopped Job."""

    RECEIPT_BOUND_READ_ONLY = "RECEIPT_BOUND_READ_ONLY"
    REPLACEMENT_WORK_ORDER_REQUIRED = "REPLACEMENT_WORK_ORDER_REQUIRED"


@dataclass(frozen=True, slots=True)
class PortfolioDispatchPlan:
    """One content-free next-Job decision; callers still explicitly dispatch."""

    entry: PortfolioEntry | None
    reuse_decision: PortfolioReuseDecision
    organization_assessment: OrganizationOutcomeAssessment
    manager_assessment: ManagerOutcomeAssessment | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortfolioRecoveryPlan:
    """A safe recovery route, never an automatic retry instruction."""

    job_id: str
    decision: PortfolioRecoveryDecision
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagerCampaignQualification:
    """Read-only four-way evidence required before automatic Manager reuse.

    This is deliberately a projection rather than a campaign writer.  The
    campaign remains the source of truth and a passing projection cannot edit
    a Blueprint, Company, or ROSTER by itself.
    """

    qualified: bool
    automatic_reuse_allowed: bool
    reasons: tuple[str, ...]

    @classmethod
    def from_report(cls, report: object | None) -> "ManagerCampaignQualification":
        if report is None:
            return cls(False, False, ("manager_campaign_report_missing",))
        if not bool(getattr(report, "qualified", False)):
            return cls(False, False, ("manager_campaign_report_not_qualified",))
        outcomes = {
            str(getattr(item, "arm", "")): item
            for item in tuple(getattr(report, "outcomes", ()))
        }
        manager = outcomes.get("MANAGER_LED_FIRM")
        heterogeneous = outcomes.get("HETEROGENEOUS_GRAPH")
        required = {"SINGLE_EMPLOYEE", "HOMOGENEOUS_GRAPH", "HETEROGENEOUS_GRAPH", "MANAGER_LED_FIRM"}
        if set(outcomes) != required or any(int(getattr(item, "run_count", 0)) != 4 for item in outcomes.values()):
            return cls(False, False, ("manager_campaign_requires_complete_16_slot_matrix",))
        if manager is None or heterogeneous is None:
            return cls(False, False, ("manager_campaign_comparison_arm_missing",))
        if any(
            float(getattr(item, "complete_failure_rate", 1.0)) > 0
            or float(getattr(item, "safety_failure_rate", 1.0)) > 0
            for item in outcomes.values()
        ):
            return cls(False, False, ("manager_campaign_contains_failure_or_safety_loss",))
        if (
            float(getattr(manager, "lower_decile_quality", 0.0))
            <= float(getattr(heterogeneous, "lower_decile_quality", 0.0))
        ):
            return cls(False, False, ("manager_campaign_lower_decile_not_improved",))
        if float(getattr(manager, "mean_model_calls", float("inf"))) > float(getattr(heterogeneous, "mean_model_calls", 0.0)):
            return cls(False, False, ("manager_campaign_model_call_cost_not_improved",))
        if getattr(manager, "cost_accounting_mode", "") != getattr(heterogeneous, "cost_accounting_mode", ""):
            return cls(False, False, ("manager_campaign_cost_accounting_not_comparable",))
        return cls(True, True, ("sealed_same_budget_16_slot_manager_gain",))


@dataclass(frozen=True, slots=True)
class PortfolioBatchExecution:
    """Terminal facts from one bounded, explicitly invoked portfolio drain."""

    dispatched_job_ids: tuple[str, ...]
    settled_job_ids: tuple[str, ...]
    blocked_job_ids: tuple[str, ...]
    deferred_work_order_ids: tuple[str, ...]
    waves: int


PortfolioRequestFactory = Callable[[WorkOrder], CompanyRunRequest]
PortfolioKernelDispatch = Callable[[CompanyRunRequest], Awaitable[JobResult]]
PortfolioWorkOrderDispatch = Callable[[WorkOrder, str], Awaitable[JobResult]]


class PortfolioExecutionService:
    """Coordinate deterministic local admission with existing outcome gates."""

    def __init__(self, store: WorkOrderPortfolioStore) -> None:
        self.store = store

    def next_dispatch_plan(
        self,
        *,
        policy: PortfolioPolicy,
        episodes: Iterable[OrganizationEpisode],
        context_fingerprint: str,
        manager_employee_id: str = "",
        automatic_blueprint_requested: bool = False,
        manager_campaign_report: object | None = None,
    ) -> PortfolioDispatchPlan:
        """Choose the next admitted unbound order and its reuse boundary.

        A Manager-shaped or Blueprint-shaped plan remains an observation until
        the *same* workflow context has both qualified organization evidence
        and, when present, a qualified Manager cohort.  This prevents a
        successful Job in one context from silently becoming a portfolio-wide
        automatic template.
        """

        all_episodes = tuple(episodes)
        organization = assess_organization_outcomes(
            all_episodes, context_fingerprint=context_fingerprint
        )
        manager = self._manager_assessment(
            all_episodes,
            manager_employee_id=manager_employee_id,
            context_fingerprint=context_fingerprint,
        )
        entries = self.store.reconcile(policy)
        entry = next(
            (
                item
                for item in entries
                if item.status is PortfolioStatus.ADMITTED and item.job_id is None
            ),
            None,
        )
        campaign = ManagerCampaignQualification.from_report(manager_campaign_report)
        reuse_decision, reasons = self._reuse_decision(
            organization=organization,
            manager=manager,
            campaign=campaign,
            automatic_blueprint_requested=automatic_blueprint_requested,
        )
        if entry is None:
            reasons = (*reasons, "no_unbound_admitted_work_order")
        return PortfolioDispatchPlan(
            entry=entry,
            reuse_decision=reuse_decision,
            organization_assessment=organization,
            manager_assessment=manager,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def bind_dispatched_job(self, plan: PortfolioDispatchPlan, *, job_id: str) -> PortfolioEntry:
        """Bind the exact operator-selected next order after request freezing."""

        if plan.entry is None:
            raise ValueError("No admitted Work Order is available for dispatch")
        return self.store.bind_job(plan.entry.work_order_id, job_id=job_id)

    def record_terminal_result(self, result: JobResult) -> PortfolioJobSettlement:
        """Persist actual aggregate usage and release local portfolio capacity."""

        return self.store.settle_job_result(result)

    async def execute_until_idle(
        self,
        *,
        policy: PortfolioPolicy,
        prepare_request: PortfolioRequestFactory,
        dispatch: PortfolioKernelDispatch,
        company_budget_authority: CompanyBudgetAuthorityPort | None = None,
    ) -> PortfolioBatchExecution:
        """Run deterministic admitted waves through the caller's Kernel port.

        This is an explicit bounded call, never a daemon.  ``prepare_request``
        must freeze the ordinary Front Door request and ``dispatch`` must be
        composed with the authoritative Company-budget-enabled Firm Kernel.
        A thrown dispatch leaves its bound reservation active and blocks later
        admission; it is never treated as zero spend or silently retried.
        """

        dispatched: list[str] = []
        settled: list[str] = []
        blocked: list[str] = []
        deferred: list[str] = []
        waves = 0
        while True:
            entries = self.store.reconcile(policy)
            candidates = tuple(
                item for item in entries
                if item.status is PortfolioStatus.ADMITTED and item.job_id is None
            )
            if not candidates:
                break
            prepared: list[CompanyRunRequest] = []
            for entry in candidates:
                order = self.store.work_order(entry.work_order_id)
                request = prepare_request(order)
                if (
                    request.work_order_id != entry.work_order_id
                    or request.work_order_digest != entry.work_order_digest
                    or not request.job_id.strip()
                ):
                    raise ValueError("Portfolio request factory returned an invalid frozen Work Order binding")
                if company_budget_authority is not None:
                    admission = company_budget_authority.admit_job(request)
                    if not admission.allowed:
                        self.store.defer_work_order(
                            entry.work_order_id,
                            reason="COMPANY_BUDGET_ADMISSION_DEFERRED",
                        )
                        deferred.append(entry.work_order_id)
                        continue
                self.store.retain_continuation_request(request)
                self.store.bind_job(entry.work_order_id, job_id=request.job_id)
                prepared.append(request)
                dispatched.append(request.job_id)
            if not prepared:
                break
            waves += 1
            outcomes = await asyncio.gather(
                *(dispatch(request) for request in prepared),
                return_exceptions=True,
            )
            made_terminal_progress = False
            for request, outcome in zip(prepared, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    blocked.append(request.job_id)
                    continue
                if outcome.job_id != request.job_id:
                    raise RuntimeError("Portfolio Kernel dispatch returned another Job identity")
                self.record_terminal_result(outcome)
                settled.append(request.job_id)
                made_terminal_progress = True
            if blocked or deferred or not made_terminal_progress:
                break
        return PortfolioBatchExecution(
            dispatched_job_ids=tuple(dispatched),
            settled_job_ids=tuple(settled),
            blocked_job_ids=tuple(blocked),
            deferred_work_order_ids=tuple(deferred),
            waves=waves,
        )

    async def execute_work_orders_until_idle(
        self,
        *,
        policy: PortfolioPolicy,
        job_id_for: Callable[[WorkOrder], str],
        dispatch: PortfolioWorkOrderDispatch,
    ) -> PortfolioBatchExecution:
        """Drain current admissions through the ordinary Company Front Door.

        This variant exists for product ingress where the Front Door itself
        owns request freezing and Company-budget admission.  It allocates a
        stable Job id *before* invoking that path, binds the caller-held
        canonical Work Order, then waits for the terminal Kernel result.  It
        intentionally has no local budget pre-admission: the composed Front
        Door remains the sole authority for that live decision.
        """

        dispatched: list[str] = []
        settled: list[str] = []
        blocked: list[str] = []
        deferred: list[str] = []
        waves = 0
        while True:
            entries = self.store.reconcile(policy)
            candidates = tuple(
                item for item in entries
                if item.status is PortfolioStatus.ADMITTED and item.job_id is None
            )
            if not candidates:
                break
            prepared: list[tuple[WorkOrder, str]] = []
            for entry in candidates:
                order = self.store.work_order(entry.work_order_id)
                job_id = job_id_for(order)
                if not job_id.strip():
                    raise ValueError("Portfolio dispatcher produced an invalid Job identity")
                self.store.bind_job(order.work_order_id, job_id=job_id)
                prepared.append((order, job_id))
                dispatched.append(job_id)
            waves += 1
            outcomes = await asyncio.gather(
                *(dispatch(order, job_id) for order, job_id in prepared),
                return_exceptions=True,
            )
            made_terminal_progress = False
            for (order, job_id), outcome in zip(prepared, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    blocked.append(job_id)
                    continue
                if outcome.job_id != job_id:
                    raise RuntimeError("Portfolio Front Door returned another Job identity")
                if outcome.status is JobStatus.BUDGET_EXHAUSTED:
                    self.store.defer_bound_budget_denial(
                        work_order_id=order.work_order_id,
                        job_id=job_id,
                    )
                    deferred.append(order.work_order_id)
                    continue
                self.record_terminal_result(outcome)
                settled.append(job_id)
                made_terminal_progress = True
            if blocked or deferred or not made_terminal_progress:
                break
        return PortfolioBatchExecution(
            dispatched_job_ids=tuple(dispatched),
            settled_job_ids=tuple(settled),
            blocked_job_ids=tuple(blocked),
            deferred_work_order_ids=tuple(deferred),
            waves=waves,
        )

    @staticmethod
    def recovery_plan(
        *,
        job_id: str,
        requested_effect: str,
        has_receipt_proven_read_only_prefix: bool,
        has_in_flight_or_attempted_pending_work: bool,
        has_graph_revision: bool,
        has_effect_receipt: bool,
    ) -> PortfolioRecoveryPlan:
        """Classify recovery without ever replaying an effectful operation.

        An effect receipt proves only that an external operation may have
        happened; it does not prove an idempotent continuation protocol.  The
        safe route is therefore an explicit replacement Work Order.  Existing
        ``ReceiptBoundContinuationService`` remains the sole execution path
        for the narrowly proven read-only prefix.
        """

        if not job_id.strip():
            raise ValueError("Recovery plan requires a Job identity")
        normalized_effect = requested_effect.strip().upper()
        if (
            normalized_effect == "READ"
            and has_receipt_proven_read_only_prefix
            and not has_in_flight_or_attempted_pending_work
            and not has_graph_revision
            and not has_effect_receipt
        ):
            return PortfolioRecoveryPlan(
                job_id=job_id,
                decision=PortfolioRecoveryDecision.RECEIPT_BOUND_READ_ONLY,
                reasons=("receipt_proven_unmodified_read_only_prefix",),
            )
        reasons: list[str] = []
        if normalized_effect != "READ" or has_effect_receipt:
            reasons.append("effectful_or_effect_receipt_requires_explicit_replacement")
        if has_in_flight_or_attempted_pending_work:
            reasons.append("in_flight_or_attempted_pending_work_cannot_be_replayed")
        if has_graph_revision:
            reasons.append("graph_revision_requires_explicit_replacement")
        if not reasons:
            reasons.append("receipt_bound_continuation_requirements_not_met")
        return PortfolioRecoveryPlan(
            job_id=job_id,
            decision=PortfolioRecoveryDecision.REPLACEMENT_WORK_ORDER_REQUIRED,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _manager_assessment(
        episodes: tuple[OrganizationEpisode, ...],
        *,
        manager_employee_id: str,
        context_fingerprint: str,
    ) -> ManagerOutcomeAssessment | None:
        if not manager_employee_id:
            return None
        assessments = assess_manager_outcomes(
            episodes,
            manager_employee_id=manager_employee_id,
            context_fingerprint=context_fingerprint,
        )
        return assessments[0] if assessments else None

    @staticmethod
    def _reuse_decision(
        *,
        organization: OrganizationOutcomeAssessment,
        manager: ManagerOutcomeAssessment | None,
        campaign: ManagerCampaignQualification,
        automatic_blueprint_requested: bool,
    ) -> tuple[PortfolioReuseDecision, tuple[str, ...]]:
        if not automatic_blueprint_requested:
            return PortfolioReuseDecision.SOLO_ONLY, ("automatic_blueprint_not_requested",)
        if organization.decision not in {
            OrganizationEvidenceDecision.TEAM_ELIGIBLE,
            OrganizationEvidenceDecision.REPLICA_ELIGIBLE,
        }:
            return PortfolioReuseDecision.SOLO_ONLY, (
                "organization_outcome_not_qualified_for_automatic_reuse",
                *organization.reasons,
            )
        if manager is None:
            return PortfolioReuseDecision.OBSERVE_ONLY, (
                "manager_provenance_missing_for_manager_led_reuse",
            )
        if manager.decision is not ManagerOutcomeDecision.KEEP_UNDER_OBSERVATION:
            return PortfolioReuseDecision.SOLO_ONLY, (
                "manager_outcome_not_qualified_for_automatic_reuse",
                *manager.reasons,
            )
        if not campaign.automatic_reuse_allowed:
            return PortfolioReuseDecision.OBSERVE_ONLY, campaign.reasons
        return PortfolioReuseDecision.AUTOMATIC_REUSE_ALLOWED, (
            "same_context_organization_manager_and_16_slot_value_reproduced",
        )


__all__ = [
    "PortfolioDispatchPlan",
    "PortfolioBatchExecution",
    "PortfolioExecutionService",
    "ManagerCampaignQualification",
    "PortfolioRecoveryDecision",
    "PortfolioRecoveryPlan",
    "PortfolioReuseDecision",
]
