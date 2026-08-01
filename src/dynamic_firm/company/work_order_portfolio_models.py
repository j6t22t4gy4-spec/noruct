"""Typed portfolio records and canonical Work Order payload decoding."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping

from dynamic_firm.kernel.models import (
    ExecutionReplicaPreference,
    ExecutionReplicaStrategy,
    GraphMutationLease,
)

from .frontdoor import AuthoritySnapshotIdentity, WorkOrder, WorkOrderBudgetSnapshot
from .operating import (
    CompanyOperatingDecision,
    CompanyWorkMode,
    InitialCoordinationPolicy,
    OperatingReason,
    RequestedEffect,
)


class PortfolioStatus(StrEnum):
    QUEUED = "QUEUED"
    ADMITTED = "ADMITTED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class PortfolioLeaseStatus(StrEnum):
    """Lifecycle of a local cross-Job mutation reservation."""

    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"
    FORFEITED = "FORFEITED"


class PortfolioSettlementStatus(StrEnum):
    """Terminal local accounting projection for one bound Company Job."""

    SETTLED = "SETTLED"
    FORFEITED = "FORFEITED"


class PortfolioReestimateChoice(StrEnum):
    """An explicit operator response to a changed local planning estimate."""

    CONTINUE = "CONTINUE"
    REDUCE = "REDUCE"
    CANCEL = "CANCEL"


class PortfolioLifecycleState(StrEnum):
    """Replayable operator lifecycle, independent from admission bookkeeping."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    TERMINAL = "TERMINAL"


_CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def normalize_portfolio_capabilities(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > 32:
        raise ValueError("Portfolio capabilities must be a bounded tuple")
    normalized: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not _CAPABILITY.fullmatch(raw):
            raise ValueError("Portfolio capability identity is invalid")
        if raw not in normalized:
            normalized.append(raw)
    return tuple(sorted(normalized))


def normalize_capability_slots(
    values: tuple[tuple[str, int], ...],
) -> tuple[tuple[str, int], ...]:
    if not isinstance(values, tuple) or len(values) > 32:
        raise ValueError("Portfolio capability slots must be a bounded tuple")
    slots: dict[str, int] = {}
    for item in values:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("Portfolio capability slot is invalid")
        capability, count = item
        normalized = normalize_portfolio_capabilities((capability,))[0]
        if normalized in slots or type(count) is not int or not 1 <= count <= 32:
            raise ValueError("Portfolio capability slot is invalid")
        slots[normalized] = count
    return tuple(sorted(slots.items()))


@dataclass(frozen=True, slots=True)
class PortfolioPolicy:
    """Local pre-admission bounds; the runtime budget remains authoritative."""

    max_active_jobs: int = 1
    max_reserved_cost_usd: float = 0.0
    max_incremental_model_calls: int = 0
    max_incremental_tool_calls: int = 0
    max_incremental_cost_usd: float = 0.0
    capability_slots: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if type(self.max_active_jobs) is not int or not 1 <= self.max_active_jobs <= 32:
            raise ValueError("Portfolio max_active_jobs must be between 1 and 32")
        if (
            isinstance(self.max_reserved_cost_usd, bool)
            or not isinstance(self.max_reserved_cost_usd, (int, float))
            or not math.isfinite(float(self.max_reserved_cost_usd))
            or float(self.max_reserved_cost_usd) < 0
        ):
            raise ValueError("Portfolio max_reserved_cost_usd must be finite and non-negative")
        for name in ("max_incremental_model_calls", "max_incremental_tool_calls"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"Portfolio {name} must be a non-negative integer")
        if (
            isinstance(self.max_incremental_cost_usd, bool)
            or not isinstance(self.max_incremental_cost_usd, (int, float))
            or not math.isfinite(float(self.max_incremental_cost_usd))
            or float(self.max_incremental_cost_usd) < 0
        ):
            raise ValueError("Portfolio max_incremental_cost_usd must be finite and non-negative")
        normalized_slots = normalize_capability_slots(self.capability_slots)
        if normalized_slots != self.capability_slots:
            raise ValueError("Portfolio capability slots must be normalized and sorted")

    @property
    def capability_capacity(self) -> dict[str, int]:
        return dict(self.capability_slots)


@dataclass(frozen=True, slots=True)
class PortfolioSchedulingEnvelope:
    """Immutable local scheduling facts that never grant runtime authority."""

    work_order_id: str
    dependency_work_order_ids: tuple[str, ...] = ()
    deadline_at: str | None = None
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.work_order_id, str) or not self.work_order_id.strip():
            raise ValueError("Portfolio scheduling Work Order identity is invalid")
        if (
            not isinstance(self.dependency_work_order_ids, tuple)
            or len(self.dependency_work_order_ids) > 64
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 256
                for item in self.dependency_work_order_ids
            )
            or len(set(self.dependency_work_order_ids)) != len(self.dependency_work_order_ids)
            or self.work_order_id in self.dependency_work_order_ids
        ):
            raise ValueError("Portfolio dependencies are invalid")
        if tuple(sorted(self.dependency_work_order_ids)) != self.dependency_work_order_ids:
            raise ValueError("Portfolio dependencies must be normalized and sorted")
        if self.deadline_at is not None:
            parsed = datetime.fromisoformat(self.deadline_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Portfolio deadline must be timezone-aware")
        normalized = normalize_portfolio_capabilities(self.required_capabilities)
        if normalized != self.required_capabilities:
            raise ValueError("Portfolio required capabilities must be normalized and sorted")


@dataclass(frozen=True, slots=True)
class PortfolioEntry:
    work_order_id: str
    work_order_digest: str
    job_id: str | None
    priority: int
    reserved_cost_usd: float
    status: PortfolioStatus
    reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PortfolioIncrementalLease:
    """Content-free local reservation for one future graph-mutation delta."""

    lease_id: str
    work_order_id: str
    job_id: str
    mutation_lease: GraphMutationLease
    status: PortfolioLeaseStatus
    reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PortfolioJobSettlement:
    """Content-free, idempotent terminal accounting for a portfolio Job."""

    job_id: str
    status: PortfolioSettlementStatus
    terminal_status: str
    actual_model_calls: int
    actual_tool_calls: int
    actual_cost_usd: float
    reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PortfolioReestimate:
    """Content-free estimate-change notice with an immutable user decision."""

    reestimate_id: str
    work_order_id: str
    job_id: str | None
    prior_reserved_cost_usd: float
    proposed_reserved_cost_usd: float
    reason: str
    created_at: str
    choice: PortfolioReestimateChoice | None = None
    choice_reason: str | None = None
    decided_at: str | None = None


def parse_work_order_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Work Order requested_at is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Work Order requested_at is not timezone-aware")
    return parsed.astimezone(UTC)


def work_order_from_payload(payload: Mapping[str, Any]) -> WorkOrder:
    """Recreate a canonical Work Order solely in its user-owned authority."""

    if str(payload.get("schema", "")) != "noruct.work-order.v1":
        raise ValueError("Unsupported Work Order schema")
    authority = payload.get("authority_snapshot")
    budget = payload.get("budget_snapshot")
    decision = payload.get("operating_decision")
    if not isinstance(authority, Mapping) or not isinstance(budget, Mapping) or not isinstance(decision, Mapping):
        raise ValueError("Work Order payload is incomplete")
    order = WorkOrder(
        work_order_id=str(payload["work_order_id"]),
        objective=str(payload["objective"]),
        requested_outcome=str(payload["requested_outcome"]),
        constraints=tuple(str(value) for value in payload.get("constraints", ())),
        acceptance_criteria=tuple(str(value) for value in payload.get("acceptance_criteria", ())),
        context_refs=tuple(str(value) for value in payload.get("context_refs", ())),
        workspace_ref=(None if payload.get("workspace_ref") is None else str(payload["workspace_ref"])),
        authority_snapshot=AuthoritySnapshotIdentity(
            company_id=str(authority["company_id"]),
            company_revision=int(authority["company_revision"]),
            roster_revision=int(authority["roster_revision"]),
            playbook_revision=int(authority["playbook_revision"]),
            action_policy_digest=str(authority["action_policy_digest"]),
        ),
        budget_snapshot=WorkOrderBudgetSnapshot(
            max_model_calls=int(budget["max_model_calls"]),
            max_tool_calls=int(budget["max_tool_calls"]),
            max_cost_usd=float(budget["max_cost_usd"]),
            max_wall_time_ms=int(budget["max_wall_time_ms"]),
        ),
        requested_at=parse_work_order_time(payload["requested_at"]),
        operating_decision=CompanyOperatingDecision(
            work_mode=CompanyWorkMode(str(decision["work_mode"])),
            coordination_policy=InitialCoordinationPolicy(str(decision["coordination_policy"])),
            requested_effect=RequestedEffect(str(decision["requested_effect"])),
            reason=OperatingReason(str(decision["reason"])),
            requires_independent_review=bool(decision.get("requires_independent_review", False)),
            execution_replica_preference=ExecutionReplicaPreference(
                str(decision.get("execution_replica_preference", "PERFORMANCE_FIRST"))
            ),
            suggested_execution_replica_strategy=(
                None
                if decision.get("suggested_execution_replica_strategy") is None
                else ExecutionReplicaStrategy(str(decision["suggested_execution_replica_strategy"]))
            ),
        ),
    )
    order.verify()
    if payload.get("authority_snapshot_identity") != order.authority_snapshot.identity_digest:
        raise ValueError("Work Order authority identity digest is invalid")
    return order
