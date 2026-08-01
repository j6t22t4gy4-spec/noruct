"""Deterministic, non-dispatching Work Order portfolio scheduler.

The scheduler consumes immutable local facts and returns decisions only.  It
does not open SQLite, create a Job, contact a provider, or grant a capability.
Persistence and explicit dispatch remain separate owner components.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .work_order_portfolio_models import (
    PortfolioLifecycleState,
    PortfolioPolicy,
    PortfolioStatus,
)


@dataclass(frozen=True, slots=True)
class PortfolioScheduleRecord:
    work_order_id: str
    priority: int
    reserved_cost_usd: float
    admission_status: PortfolioStatus
    created_at: str
    dependency_work_order_ids: tuple[str, ...]
    deadline_at: str | None
    required_capabilities: tuple[str, ...]
    lifecycle_state: PortfolioLifecycleState
    defer_count: int
    terminal_status: str | None = None


@dataclass(frozen=True, slots=True)
class PortfolioScheduleDecision:
    work_order_id: str
    admission_status: PortfolioStatus
    admission_reason: str
    lifecycle_state: PortfolioLifecycleState
    lifecycle_reason: str
    defer_count: int
    inherited_priority: int
    effective_deadline_at: str | None


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Portfolio scheduling timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _transitive_effective_constraints(
    records: dict[str, PortfolioScheduleRecord],
) -> tuple[dict[str, int], dict[str, datetime | None]]:
    """Propagate dependent urgency to blockers and reject dependency cycles."""

    dependents: dict[str, list[str]] = {identifier: [] for identifier in records}
    for record in records.values():
        for dependency in record.dependency_work_order_ids:
            if dependency in dependents:
                dependents[dependency].append(record.work_order_id)

    priorities: dict[str, int] = {}
    deadlines: dict[str, datetime | None] = {}
    visiting: set[str] = set()

    def visit(identifier: str) -> tuple[int, datetime | None]:
        if identifier in priorities:
            return priorities[identifier], deadlines[identifier]
        if identifier in visiting:
            raise ValueError("Portfolio dependencies cannot contain cycles")
        visiting.add(identifier)
        record = records[identifier]
        inherited_priority = record.priority + min(record.defer_count, 100)
        inherited_deadline = (
            None if record.deadline_at is None else _time(record.deadline_at)
        )
        for dependent in sorted(dependents[identifier]):
            dependent_priority, dependent_deadline = visit(dependent)
            inherited_priority = max(inherited_priority, dependent_priority)
            if dependent_deadline is not None and (
                inherited_deadline is None or dependent_deadline < inherited_deadline
            ):
                inherited_deadline = dependent_deadline
        visiting.remove(identifier)
        priorities[identifier] = inherited_priority
        deadlines[identifier] = inherited_deadline
        return inherited_priority, inherited_deadline

    for identifier in sorted(records):
        visit(identifier)
    return priorities, deadlines


def plan_portfolio_admission(
    records: tuple[PortfolioScheduleRecord, ...],
    policy: PortfolioPolicy,
    *,
    now: datetime,
) -> tuple[PortfolioScheduleDecision, ...]:
    """Plan one explicit reconcile without executing or mutating state.

    Ordering is deterministic: a dependent's priority/deadline is inherited by
    its blockers, then the earliest effective deadline wins, followed by
    inherited priority, durable starvation age, creation time, and identity.
    Cost never participates in ordering; it remains a user-declared hard guard.
    """

    normalized_now = now.astimezone(UTC)
    by_id = {record.work_order_id: record for record in records}
    if len(by_id) != len(records):
        raise ValueError("Portfolio scheduling records contain duplicate identities")
    inherited_priorities, inherited_deadlines = _transitive_effective_constraints(by_id)
    capacity = policy.capability_capacity

    active = tuple(
        record
        for record in records
        if record.admission_status is PortfolioStatus.ADMITTED
        and record.lifecycle_state
        not in {PortfolioLifecycleState.CANCELLED, PortfolioLifecycleState.TERMINAL}
    )
    active_count = len(active)
    active_cost = sum(record.reserved_cost_usd for record in active)
    capability_use: dict[str, int] = {}
    for record in active:
        for capability in record.required_capabilities:
            capability_use[capability] = capability_use.get(capability, 0) + 1

    decisions: dict[str, PortfolioScheduleDecision] = {}
    ready: list[PortfolioScheduleRecord] = []
    for record in records:
        if record.admission_status not in {
            PortfolioStatus.QUEUED,
            PortfolioStatus.DEFERRED,
        }:
            continue
        if record.lifecycle_state in {
            PortfolioLifecycleState.PAUSED,
            PortfolioLifecycleState.CANCELLED,
            PortfolioLifecycleState.TERMINAL,
        }:
            continue
        missing_dependencies = tuple(
            dependency
            for dependency in record.dependency_work_order_ids
            if dependency not in by_id
        )
        failed_dependencies = tuple(
            dependency
            for dependency in record.dependency_work_order_ids
            if dependency in by_id
            and (
                by_id[dependency].lifecycle_state
                in {PortfolioLifecycleState.CANCELLED, PortfolioLifecycleState.TERMINAL}
            )
            and by_id[dependency].terminal_status != "SUCCEEDED"
        )
        pending_dependencies = tuple(
            dependency
            for dependency in record.dependency_work_order_ids
            if dependency in by_id
            and not (
                by_id[dependency].lifecycle_state is PortfolioLifecycleState.TERMINAL
                and by_id[dependency].terminal_status == "SUCCEEDED"
            )
            and dependency not in failed_dependencies
        )
        if missing_dependencies or failed_dependencies or pending_dependencies:
            if missing_dependencies:
                reason = "PORTFOLIO_DEPENDENCY_MISSING:" + ",".join(missing_dependencies)
            elif failed_dependencies:
                reason = "PORTFOLIO_DEPENDENCY_FAILED:" + ",".join(failed_dependencies)
            else:
                reason = "PORTFOLIO_DEPENDENCY_WAIT:" + ",".join(pending_dependencies)
            decisions[record.work_order_id] = PortfolioScheduleDecision(
                work_order_id=record.work_order_id,
                admission_status=PortfolioStatus.DEFERRED,
                admission_reason=reason,
                lifecycle_state=PortfolioLifecycleState.BLOCKED,
                lifecycle_reason=reason,
                defer_count=record.defer_count,
                inherited_priority=inherited_priorities[record.work_order_id],
                effective_deadline_at=(
                    None
                    if inherited_deadlines[record.work_order_id] is None
                    else inherited_deadlines[record.work_order_id].isoformat()
                ),
            )
            continue
        own_deadline = None if record.deadline_at is None else _time(record.deadline_at)
        if own_deadline is not None and own_deadline <= normalized_now:
            reason = "PORTFOLIO_DEADLINE_MISSED"
            decisions[record.work_order_id] = PortfolioScheduleDecision(
                record.work_order_id,
                PortfolioStatus.DEFERRED,
                reason,
                PortfolioLifecycleState.BLOCKED,
                reason,
                record.defer_count,
                inherited_priorities[record.work_order_id],
                own_deadline.isoformat(),
            )
            continue
        unavailable = tuple(
            capability
            for capability in record.required_capabilities
            if capability not in capacity
        )
        if unavailable:
            reason = "PORTFOLIO_CAPABILITY_UNAVAILABLE:" + ",".join(unavailable)
            decisions[record.work_order_id] = PortfolioScheduleDecision(
                record.work_order_id,
                PortfolioStatus.DEFERRED,
                reason,
                PortfolioLifecycleState.BLOCKED,
                reason,
                record.defer_count,
                inherited_priorities[record.work_order_id],
                None
                if inherited_deadlines[record.work_order_id] is None
                else inherited_deadlines[record.work_order_id].isoformat(),
            )
            continue
        if policy.max_reserved_cost_usd > 0 and (
            record.reserved_cost_usd > policy.max_reserved_cost_usd + 1e-12
        ):
            reason = "PORTFOLIO_RESERVE_EXCEEDS_POLICY"
            decisions[record.work_order_id] = PortfolioScheduleDecision(
                record.work_order_id,
                PortfolioStatus.REJECTED,
                reason,
                PortfolioLifecycleState.BLOCKED,
                reason,
                record.defer_count,
                inherited_priorities[record.work_order_id],
                None
                if inherited_deadlines[record.work_order_id] is None
                else inherited_deadlines[record.work_order_id].isoformat(),
            )
            continue
        ready.append(record)

    ready.sort(
        key=lambda record: (
            inherited_deadlines[record.work_order_id] is None,
            inherited_deadlines[record.work_order_id]
            or datetime.max.replace(tzinfo=UTC),
            -inherited_priorities[record.work_order_id],
            -record.defer_count,
            _time(record.created_at),
            record.work_order_id,
        )
    )
    for record in ready:
        blocked_capability = next(
            (
                capability
                for capability in record.required_capabilities
                if capability_use.get(capability, 0) >= capacity[capability]
            ),
            None,
        )
        cost_blocked = policy.max_reserved_cost_usd > 0 and (
            active_cost + record.reserved_cost_usd
            > policy.max_reserved_cost_usd + 1e-12
        )
        if active_count >= policy.max_active_jobs or cost_blocked or blocked_capability:
            reason = (
                f"PORTFOLIO_CAPABILITY_DEFERRED:{blocked_capability}"
                if blocked_capability
                else "PORTFOLIO_HARD_LIMIT_DEFERRED"
            )
            decisions[record.work_order_id] = PortfolioScheduleDecision(
                record.work_order_id,
                PortfolioStatus.DEFERRED,
                reason,
                PortfolioLifecycleState.QUEUED,
                reason,
                record.defer_count + 1,
                inherited_priorities[record.work_order_id],
                None
                if inherited_deadlines[record.work_order_id] is None
                else inherited_deadlines[record.work_order_id].isoformat(),
            )
            continue
        active_count += 1
        active_cost += record.reserved_cost_usd
        for capability in record.required_capabilities:
            capability_use[capability] = capability_use.get(capability, 0) + 1
        reason = "PORTFOLIO_ADMITTED"
        decisions[record.work_order_id] = PortfolioScheduleDecision(
            record.work_order_id,
            PortfolioStatus.ADMITTED,
            reason,
            PortfolioLifecycleState.QUEUED,
            "READY_FOR_EXPLICIT_DISPATCH",
            record.defer_count,
            inherited_priorities[record.work_order_id],
            None
            if inherited_deadlines[record.work_order_id] is None
            else inherited_deadlines[record.work_order_id].isoformat(),
        )

    return tuple(decisions[identifier] for identifier in sorted(decisions))


__all__ = [
    "PortfolioScheduleDecision",
    "PortfolioScheduleRecord",
    "plan_portfolio_admission",
]
