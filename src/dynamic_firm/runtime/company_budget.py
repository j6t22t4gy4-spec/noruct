"""Durable local company-cost admission and explicit budget-incident control.

The Company version supplies a policy snapshot.  This module owns only the
runtime projection: conservative job reservations, observed settlement, and a
fail-closed pause that an operator must explicitly resolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import TYPE_CHECKING, Protocol

from dynamic_firm._vendor.paperclip_runtime.budget import resolve_budget_window
from dynamic_firm.runtime.models import utc_now

if TYPE_CHECKING:
    from dynamic_firm.kernel.models import CompanyRunRequest, JobResult
    from .store import RunStore


COMPANY_COST_BUDGET_POLICY_KEY = "company_cost_budget"
DEFAULT_COMPANY_COST_BUDGET_POLICY = {
    "max_total_cost_usd": 0.0,
    "window_kind": "lifetime",
}
_WINDOW_KINDS = frozenset({"lifetime", "calendar_month_utc"})


@dataclass(frozen=True, slots=True)
class CompanyCostBudgetPolicy:
    """One versioned COMPANY policy snapshot; zero disables admission control."""

    max_total_cost_usd: float = 0.0
    window_kind: str = "lifetime"

    @property
    def enabled(self) -> bool:
        return self.max_total_cost_usd > 0

    @classmethod
    def from_mapping(cls, value: object) -> "CompanyCostBudgetPolicy":
        raw = DEFAULT_COMPANY_COST_BUDGET_POLICY if value is None else value
        if not isinstance(raw, dict):
            raise ValueError("Company cost budget policy must be an object")
        allowed = set(DEFAULT_COMPANY_COST_BUDGET_POLICY)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError("Unknown company cost budget policy fields: " + ", ".join(unknown))
        amount = raw.get("max_total_cost_usd", 0.0)
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError("Company cost budget must be a number")
        normalized_amount = float(amount)
        if normalized_amount < 0 or not math.isfinite(normalized_amount):
            raise ValueError("Company cost budget must be finite and non-negative")
        window = str(raw.get("window_kind", "lifetime"))
        if window not in _WINDOW_KINDS:
            raise ValueError("Company cost budget window is invalid")
        return cls(max_total_cost_usd=normalized_amount, window_kind=window)

    def as_mapping(self) -> dict[str, object]:
        return {
            "max_total_cost_usd": self.max_total_cost_usd,
            "window_kind": self.window_kind,
        }


@dataclass(frozen=True, slots=True)
class CompanyBudgetLease:
    job_id: str
    request_id: str
    company_revision: int
    window_start: str
    window_end: str
    reserved_cost_usd: float


@dataclass(frozen=True, slots=True)
class CompanyBudgetIncident:
    incident_id: str
    company_revision: int
    window_kind: str
    window_start: str
    window_end: str
    budget_limit_usd: float
    observed_cost_usd: float
    reserved_cost_usd: float
    requested_cost_usd: float
    status: str
    created_at: str
    resolved_at: str | None = None


@dataclass(frozen=True, slots=True)
class CompanyBudgetAdmission:
    allowed: bool
    lease: CompanyBudgetLease | None
    incident: CompanyBudgetIncident | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CompanyBudgetSettlement:
    lease: CompanyBudgetLease
    actual_cost_usd: float
    incident: CompanyBudgetIncident | None


@dataclass(frozen=True, slots=True)
class CompanyBudgetForfeit:
    """Fail-closed terminalization when a lease has no trustworthy result.

    A forfeited lease charges its full conservative reservation.  This is
    intentionally different from settling an interrupted run at zero: the
    Company must not recover spending authority merely because cancellation or
    an internal exception prevented observed usage from crossing the runtime
    boundary.
    """

    lease: CompanyBudgetLease
    charged_cost_usd: float
    reason: str
    forfeited_at: str
    incident: CompanyBudgetIncident | None


class CompanyBudgetAuthorityPort(Protocol):
    """Runtime port for durable Company cost admission and terminalization."""

    def admit_job(self, request: "CompanyRunRequest") -> CompanyBudgetAdmission: ...

    def settle_job(self, lease: CompanyBudgetLease, result: "JobResult") -> CompanyBudgetSettlement: ...

    def forfeit_job(
        self,
        lease: CompanyBudgetLease,
        *,
        reason: str,
    ) -> CompanyBudgetForfeit: ...

    def reconcile_interrupted_job(
        self,
        lease: CompanyBudgetLease,
        *,
        observed_cost_usd: float | None,
        unknown_reason: str = "INTERRUPTED_USAGE_UNCERTAIN",
    ) -> CompanyBudgetSettlement | CompanyBudgetForfeit: ...


class SQLiteCompanyBudgetAuthority:
    """First-party local implementation over the RunStore transaction owner."""

    def __init__(self, store: "RunStore", policy: CompanyCostBudgetPolicy) -> None:
        self.store = store
        self.policy = policy

    def admit_job(self, request: "CompanyRunRequest") -> CompanyBudgetAdmission:
        if not self.policy.enabled:
            return CompanyBudgetAdmission(True, None, None)
        if request.job_limits.max_total_cost_usd <= 0:
            return CompanyBudgetAdmission(
                False,
                None,
                None,
                "A company budget requires a positive concrete job cost limit.",
            )
        start, end = resolve_budget_window(self.policy.window_kind, utc_now())
        return self.store.admit_company_budget_job(
            job_id=request.job_id,
            request_id=request.request_id,
            company_revision=request.company_revision,
            window_start=start,
            window_end=end,
            budget_limit_usd=self.policy.max_total_cost_usd,
            reserved_cost_usd=request.job_limits.max_total_cost_usd,
        )

    def settle_job(self, lease: CompanyBudgetLease, result: "JobResult") -> CompanyBudgetSettlement:
        return self.store.settle_company_budget_job(
            lease,
            actual_cost_usd=result.metrics.usage.cost_usd,
        )

    def forfeit_job(
        self,
        lease: CompanyBudgetLease,
        *,
        reason: str,
    ) -> CompanyBudgetForfeit:
        return self.store.forfeit_company_budget_job(lease, reason=reason)

    def reconcile_interrupted_job(
        self,
        lease: CompanyBudgetLease,
        *,
        observed_cost_usd: float | None,
        unknown_reason: str = "INTERRUPTED_USAGE_UNCERTAIN",
    ) -> CompanyBudgetSettlement | CompanyBudgetForfeit:
        """Close an interrupted reservation with proven cost or a full forfeit.

        This is intentionally not a resumption grant.  A known receipt prefix
        replaces the reservation with its observed scalar cost; any missing
        provider/action evidence keeps the conservative full charge.
        """

        if observed_cost_usd is None:
            return self.forfeit_job(lease, reason=unknown_reason)
        return self.store.settle_company_budget_job(
            lease,
            actual_cost_usd=observed_cost_usd,
        )

    def status(self) -> dict[str, object]:
        return self.store.company_budget_status(self.policy)

    def resolve_incident(self, incident_id: str, *, actor: str) -> CompanyBudgetIncident:
        if not actor.strip():
            raise ValueError("Budget incident resolution actor must be explicit")
        if not self.policy.enabled:
            raise ValueError("Set a positive company cost budget before resuming")
        return self.store.resolve_company_budget_incident(
            incident_id,
            actor=actor.strip(),
            policy=self.policy,
            resolved_at=utc_now(),
        )
