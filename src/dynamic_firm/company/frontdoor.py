"""Immutable Company Front Door work-order boundary.

A WorkOrder fixes what the operator asked for, which frozen Company authority
and budget apply, and the initial operating decision.  It is intentionally not
a task, plan, workflow, employee assignment, permission grant, or ACTIVE JOB.
Those structures may be created only after this boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .operating import CompanyOperatingDecision, classify_company_input


WORK_ORDER_SCHEMA = "noruct.work-order.v1"
_MAX_OBJECTIVE_BYTES = 256_000
_MAX_LIST_ITEMS = 64
_MAX_ITEM_BYTES = 16_000
_MAX_REFERENCE_BYTES = 2_048
_MAX_ID_BYTES = 256


def _required_text(value: str, label: str, *, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError(f"{label} must be non-empty")
    if "\x00" in normalized:
        raise ValueError(f"{label} cannot contain NUL")
    if len(normalized.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its UTF-8 byte bound")
    return normalized


def _optional_text(
    value: str | None,
    label: str,
    *,
    maximum_bytes: int,
) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, maximum_bytes=maximum_bytes)


def _bounded_items(
    values: tuple[str, ...],
    label: str,
    *,
    maximum_bytes: int,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} must be a tuple")
    if len(values) > _MAX_LIST_ITEMS:
        raise ValueError(f"{label} exceeds its item bound")
    normalized: list[str] = []
    for value in values:
        item = _required_text(value, label, maximum_bytes=maximum_bytes)
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class AuthoritySnapshotIdentity:
    """Identity of the frozen authority used to interpret one WorkOrder."""

    company_id: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    action_policy_digest: str

    def __post_init__(self) -> None:
        if self.company_id != _required_text(
            self.company_id,
            "company_id",
            maximum_bytes=_MAX_ID_BYTES,
        ):
            raise ValueError("company_id must already be normalized")
        if self.action_policy_digest != _required_text(
            self.action_policy_digest,
            "action_policy_digest",
            maximum_bytes=_MAX_ID_BYTES,
        ):
            raise ValueError("action_policy_digest must already be normalized")
        for label, value in (
            ("company_revision", self.company_revision),
            ("roster_revision", self.roster_revision),
            ("playbook_revision", self.playbook_revision),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "company_id": self.company_id,
            "company_revision": self.company_revision,
            "roster_revision": self.roster_revision,
            "playbook_revision": self.playbook_revision,
            "action_policy_digest": self.action_policy_digest,
        }

    @property
    def identity_digest(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class WorkOrderBudgetSnapshot:
    """Effective upper bounds frozen before planning or employee dispatch."""

    max_model_calls: int
    max_tool_calls: int
    max_cost_usd: float
    max_wall_time_ms: int

    def __post_init__(self) -> None:
        for label, value in (
            ("max_model_calls", self.max_model_calls),
            ("max_tool_calls", self.max_tool_calls),
            ("max_wall_time_ms", self.max_wall_time_ms),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if (
            isinstance(self.max_cost_usd, bool)
            or not isinstance(self.max_cost_usd, (int, float))
            or not math.isfinite(float(self.max_cost_usd))
            or self.max_cost_usd < 0
        ):
            raise ValueError("max_cost_usd must be a finite non-negative number")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "max_model_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_cost_usd": float(self.max_cost_usd),
            "max_wall_time_ms": self.max_wall_time_ms,
        }


@dataclass(frozen=True, slots=True)
class WorkOrder:
    """One normalized Company request, fixed before workflow compilation."""

    work_order_id: str
    objective: str
    requested_outcome: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    context_refs: tuple[str, ...]
    workspace_ref: str | None
    authority_snapshot: AuthoritySnapshotIdentity
    budget_snapshot: WorkOrderBudgetSnapshot
    requested_at: datetime
    # Frozen initial coordination candidate. The validated plan shape may
    # still collapse TEAM_JOB to SOLO_JOB; this is neither an execution grant
    # nor the final runtime admission result.
    operating_decision: CompanyOperatingDecision
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.work_order_id != _required_text(
            self.work_order_id,
            "work_order_id",
            maximum_bytes=_MAX_ID_BYTES,
        ):
            raise ValueError("work_order_id must already be normalized")
        if self.objective != _required_text(
            self.objective,
            "objective",
            maximum_bytes=_MAX_OBJECTIVE_BYTES,
        ):
            raise ValueError("objective must already be normalized")
        if self.requested_outcome != _required_text(
            self.requested_outcome,
            "requested_outcome",
            maximum_bytes=_MAX_OBJECTIVE_BYTES,
        ):
            raise ValueError("requested_outcome must already be normalized")
        if self.constraints != _bounded_items(
            self.constraints,
            "constraints",
            maximum_bytes=_MAX_ITEM_BYTES,
        ):
            raise ValueError("constraints must already be normalized and unique")
        if self.acceptance_criteria != _bounded_items(
            self.acceptance_criteria,
            "acceptance_criteria",
            maximum_bytes=_MAX_ITEM_BYTES,
        ):
            raise ValueError("acceptance_criteria must already be normalized and unique")
        if self.context_refs != _bounded_items(
            self.context_refs,
            "context_refs",
            maximum_bytes=_MAX_REFERENCE_BYTES,
        ):
            raise ValueError("context_refs must already be normalized and unique")
        if self.workspace_ref != _optional_text(
            self.workspace_ref,
            "workspace_ref",
            maximum_bytes=_MAX_REFERENCE_BYTES,
        ):
            raise ValueError("workspace_ref must already be normalized")
        if not isinstance(self.authority_snapshot, AuthoritySnapshotIdentity):
            raise TypeError("authority_snapshot must be AuthoritySnapshotIdentity")
        if not isinstance(self.budget_snapshot, WorkOrderBudgetSnapshot):
            raise TypeError("budget_snapshot must be WorkOrderBudgetSnapshot")
        if not isinstance(self.operating_decision, CompanyOperatingDecision):
            raise TypeError("operating_decision must be CompanyOperatingDecision")
        if not isinstance(self.requested_at, datetime):
            raise TypeError("requested_at must be a datetime")
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        object.__setattr__(self, "content_digest", self.computed_digest())

    def canonical_payload(self) -> dict[str, object]:
        decision = self.operating_decision
        return {
            "schema": WORK_ORDER_SCHEMA,
            "work_order_id": self.work_order_id,
            "objective": self.objective,
            "requested_outcome": self.requested_outcome,
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "context_refs": list(self.context_refs),
            "workspace_ref": self.workspace_ref,
            "authority_snapshot": self.authority_snapshot.canonical_payload(),
            "authority_snapshot_identity": self.authority_snapshot.identity_digest,
            "budget_snapshot": self.budget_snapshot.canonical_payload(),
            "requested_at": self.requested_at.astimezone(UTC).isoformat().replace(
                "+00:00", "Z"
            ),
            "operating_decision": {
                "work_mode": decision.work_mode.value,
                "coordination_policy": decision.coordination_policy.value,
                "requested_effect": decision.requested_effect.value,
                "reason": decision.reason.value,
                "requires_independent_review": decision.requires_independent_review,
                "execution_replica_preference": (
                    decision.execution_replica_preference.value
                ),
                "suggested_execution_replica_strategy": (
                    None
                    if decision.suggested_execution_replica_strategy is None
                    else decision.suggested_execution_replica_strategy.value
                ),
                "company_owned": decision.company_owned,
            },
        }

    def computed_digest(self) -> str:
        return _digest(self.canonical_payload())

    def verify(self) -> None:
        """Fail if an in-memory or deserialized order no longer matches its digest."""

        if self.content_digest != self.computed_digest():
            raise ValueError("WorkOrder content digest is invalid")


def normalize_work_order(
    raw_company_input: str,
    *,
    work_order_id: str,
    authority_snapshot: AuthoritySnapshotIdentity,
    budget_snapshot: WorkOrderBudgetSnapshot,
    requested_at: datetime,
    requested_outcome: str | None = None,
    constraints: tuple[str, ...] = (),
    acceptance_criteria: tuple[str, ...] = (),
    context_refs: tuple[str, ...] = (),
    workspace_ref: str | None = None,
    operating_decision: CompanyOperatingDecision | None = None,
) -> WorkOrder:
    """Normalize raw input without creating a plan, graph, team, or authority."""

    objective = _required_text(
        raw_company_input,
        "raw_company_input",
        maximum_bytes=_MAX_OBJECTIVE_BYTES,
    )
    outcome = (
        objective
        if requested_outcome is None
        else _required_text(
            requested_outcome,
            "requested_outcome",
            maximum_bytes=_MAX_OBJECTIVE_BYTES,
        )
    )
    normalized_requested_at = _utc_datetime(requested_at)
    return WorkOrder(
        work_order_id=_required_text(
            work_order_id,
            "work_order_id",
            maximum_bytes=_MAX_ID_BYTES,
        ),
        objective=objective,
        requested_outcome=outcome,
        constraints=_bounded_items(
            constraints,
            "constraints",
            maximum_bytes=_MAX_ITEM_BYTES,
        ),
        acceptance_criteria=_bounded_items(
            acceptance_criteria,
            "acceptance_criteria",
            maximum_bytes=_MAX_ITEM_BYTES,
        ),
        context_refs=_bounded_items(
            context_refs,
            "context_refs",
            maximum_bytes=_MAX_REFERENCE_BYTES,
        ),
        workspace_ref=_optional_text(
            workspace_ref,
            "workspace_ref",
            maximum_bytes=_MAX_REFERENCE_BYTES,
        ),
        authority_snapshot=authority_snapshot,
        budget_snapshot=budget_snapshot,
        requested_at=normalized_requested_at,
        operating_decision=operating_decision or classify_company_input(objective),
    )


def verify_work_order_binding(
    work_order: WorkOrder,
    *,
    authority_snapshot: AuthoritySnapshotIdentity,
    budget_snapshot: WorkOrderBudgetSnapshot,
) -> None:
    """Verify the order against the exact frozen Company admission inputs.

    The workflow compiler is a proposal source and never receives enough
    authority state to validate this binding. The Company Front Door owns the
    check before direct or managed execution is dispatched.
    """

    if not isinstance(work_order, WorkOrder):
        raise TypeError("work_order must be WorkOrder")
    if not isinstance(authority_snapshot, AuthoritySnapshotIdentity):
        raise TypeError("authority_snapshot must be AuthoritySnapshotIdentity")
    if not isinstance(budget_snapshot, WorkOrderBudgetSnapshot):
        raise TypeError("budget_snapshot must be WorkOrderBudgetSnapshot")
    work_order.verify()
    if work_order.authority_snapshot != authority_snapshot:
        raise ValueError("WorkOrder authority snapshot does not match Company admission")
    if work_order.budget_snapshot != budget_snapshot:
        raise ValueError("WorkOrder budget snapshot does not match Company admission")


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("requested_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("requested_at must be timezone-aware")
    return value.astimezone(UTC)


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AuthoritySnapshotIdentity",
    "WORK_ORDER_SCHEMA",
    "WorkOrder",
    "WorkOrderBudgetSnapshot",
    "normalize_work_order",
    "verify_work_order_binding",
]
