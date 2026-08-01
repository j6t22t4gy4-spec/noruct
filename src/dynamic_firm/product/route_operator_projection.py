"""Provider-free route facts for identical CLI and TUI operator projections.

This module consumes only facts frozen before execution.  It deliberately does
not accept a provider, a model identifier, configuration, credentials, or an
egress grant, so an operator surface cannot accidentally turn route facts into
provider authority.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.model_invocation_receipt import ModelInvocationReceipt
from dynamic_firm.company.route_selection_receipt import RouteSelectionReceipt


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{field} must be a bounded opaque identifier")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


class CompatibilityStatus(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class EgressPolicyState(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    OFFLINE = "OFFLINE"
    UNVERIFIED = "UNVERIFIED"


class FallbackOperatorState(StrEnum):
    """What durable invocation evidence can prove about fallback use."""

    NOT_OBSERVED = "NOT_OBSERVED"
    NOT_USED = "NOT_USED"
    FANOUT_UNCLASSIFIED = "FANOUT_UNCLASSIFIED"


@dataclass(frozen=True, slots=True)
class OperatorTaskIdentity:
    """Public opaque identities, never an execution-provider identity."""

    employee_id: str
    task_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "employee_id", _token(self.employee_id, "employee_id"))
        object.__setattr__(self, "task_id", _token(self.task_id, "task_id"))


@dataclass(frozen=True, slots=True)
class CompatibilityPoint:
    """A bounded named compatibility point, separate from the evidence digest."""

    point_id: str
    status: CompatibilityStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "point_id", _token(self.point_id, "point_id"))
        object.__setattr__(self, "status", CompatibilityStatus(self.status))


@dataclass(frozen=True, slots=True)
class EgressOperatorState:
    """Read-only policy state; this is not an authorization grant."""

    state: EgressPolicyState

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", EgressPolicyState(self.state))


@dataclass(frozen=True, slots=True)
class RouteOperatorProjection:
    """Canonical, content-free payload shared by CLI and TUI renderers."""

    employee_id: str
    task_id: str
    route_id: str
    selection_reasons: tuple[str, ...]
    selection_policy_digest: str
    intelligence_snapshot_digest: str
    compatibility_evidence_digest: str
    compatibility_point_id: str
    compatibility_status: CompatibilityStatus
    egress_policy_digest: str
    egress_policy_state: EgressPolicyState
    fallback_policy_digest: str
    fallback_state: FallbackOperatorState
    selected_uncertainty: float
    actual_receipt: dict[str, object] | None

    def __post_init__(self) -> None:
        for field in ("employee_id", "task_id", "route_id", "compatibility_point_id"):
            object.__setattr__(self, field, _token(getattr(self, field), field))
        for field in (
            "selection_policy_digest",
            "intelligence_snapshot_digest",
            "compatibility_evidence_digest",
            "egress_policy_digest",
            "fallback_policy_digest",
        ):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        reasons = tuple(self.selection_reasons)
        if not reasons or any(not isinstance(reason, str) or not reason for reason in reasons):
            raise ValueError("selection_reasons must be a nonempty tuple of labels")
        object.__setattr__(self, "selection_reasons", reasons)
        object.__setattr__(self, "compatibility_status", CompatibilityStatus(self.compatibility_status))
        object.__setattr__(self, "egress_policy_state", EgressPolicyState(self.egress_policy_state))
        object.__setattr__(self, "fallback_state", FallbackOperatorState(self.fallback_state))
        if isinstance(self.selected_uncertainty, bool) or not isinstance(self.selected_uncertainty, (int, float)) or not 0 <= self.selected_uncertainty <= 1:
            raise ValueError("selected_uncertainty must be finite and in [0, 1]")
        object.__setattr__(self, "selected_uncertainty", float(self.selected_uncertainty))
        if self.actual_receipt is not None:
            expected = {"terminal_status", "usage_availability", "cost_availability", "cost_usd", "latency_ms"}
            if not isinstance(self.actual_receipt, dict) or set(self.actual_receipt) != expected:
                raise ValueError("actual_receipt summary is invalid")

    def canonical_payload(self) -> dict[str, object]:
        payload = {
            "employee_id": self.employee_id,
            "task_id": self.task_id,
            "route_id": self.route_id,
            "selection_reasons": list(self.selection_reasons),
            "selection_policy_digest": self.selection_policy_digest,
            "intelligence_snapshot_digest": self.intelligence_snapshot_digest,
            "compatibility_evidence_digest": self.compatibility_evidence_digest,
            "compatibility_point_id": self.compatibility_point_id,
            "compatibility_status": self.compatibility_status.value,
            "egress_policy_digest": self.egress_policy_digest,
            "egress_policy_state": self.egress_policy_state.value,
            "fallback_policy_digest": self.fallback_policy_digest,
            "fallback_state": self.fallback_state.value,
            "selected_uncertainty": self.selected_uncertainty,
            "actual_receipt": self.actual_receipt,
        }
        return payload

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_canonical_json(cls, raw: object) -> "RouteOperatorProjection":
        try:
            payload = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError as exc:
            raise ValueError("projection JSON is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != set(cls.__dataclass_fields__):
            raise ValueError("projection JSON has unknown or missing fields")
        if not isinstance(payload["selection_reasons"], list):
            raise ValueError("projection selection_reasons must be a list")
        projection = cls(
            **{**payload, "selection_reasons": tuple(payload["selection_reasons"])}
        )
        if raw != projection.canonical_json():
            raise ValueError("projection JSON is not canonical")
        return projection

    def render_cli_lines(self, width: int = 80) -> tuple[str, ...]:
        """Thin CLI form.  First line always retains route and execution state labels."""
        if isinstance(width, bool) or not isinstance(width, int) or width < 16:
            raise ValueError("width must be an integer of at least 16")
        terminal = "OFFLINE" if self.actual_receipt is None else str(self.actual_receipt["terminal_status"])
        suffix = f" 상태={terminal}"
        route_width = width - len("경로=") - len(suffix)
        priority = f"경로={_truncate(self.route_id, route_width)}{suffix}"
        lines = (
            priority,
            f"직원={self.employee_id} 작업={self.task_id}",
            f"선택={','.join(self.selection_reasons)} 불확실성={self.selected_uncertainty:g}",
            f"호환={self.compatibility_point_id}:{self.compatibility_status.value}",
            f"송신={self.egress_policy_state.value}",
            f"대체={self.fallback_state.value}",
        )
        return tuple(_truncate(line, width) for line in lines)

    def render_tui_rows(self) -> tuple[tuple[str, str], ...]:
        """Thin TUI form derived directly from the same canonical payload."""
        payload = self.canonical_payload()
        terminal = "OFFLINE" if payload["actual_receipt"] is None else str(payload["actual_receipt"]["terminal_status"])
        return (
            ("route_id", str(payload["route_id"])),
            ("terminal_status", terminal),
            ("employee_id", str(payload["employee_id"])),
            ("task_id", str(payload["task_id"])),
            ("selection_reasons", ",".join(payload["selection_reasons"])),
            ("selected_uncertainty", f"{payload['selected_uncertainty']:g}"),
            ("compatibility", f"{payload['compatibility_point_id']}:{payload['compatibility_status']}"),
            ("egress_policy_state", str(payload["egress_policy_state"])),
            ("fallback_state", str(payload["fallback_state"])),
        )


def _truncate(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"


def build_route_operator_projection(
    identity: OperatorTaskIdentity,
    binding: ExecutionRouteBinding,
    selection: RouteSelectionReceipt,
    compatibility: CompatibilityPoint,
    egress: EgressOperatorState,
    actual_receipt: ModelInvocationReceipt | None = None,
    fallback_state: FallbackOperatorState = FallbackOperatorState.NOT_OBSERVED,
) -> RouteOperatorProjection:
    """Build display facts without taking any route-selection or egress authority."""
    if not isinstance(identity, OperatorTaskIdentity) or not isinstance(binding, ExecutionRouteBinding):
        raise ValueError("typed identity and frozen binding are required")
    if not isinstance(selection, RouteSelectionReceipt) or selection.selected_route_id != binding.route_id:
        raise ValueError("selection must select the frozen binding route")
    if not isinstance(compatibility, CompatibilityPoint) or not isinstance(egress, EgressOperatorState):
        raise ValueError("typed compatibility and egress state are required")
    candidate = selection.selected_candidate
    if candidate is None:
        raise ValueError("selected route candidate is required")
    receipt_summary = None
    if actual_receipt is not None:
        if not isinstance(actual_receipt, ModelInvocationReceipt):
            raise ValueError("actual receipt must be typed")
        if actual_receipt.route_binding_digest != binding.digest:
            raise ValueError("actual receipt belongs to a different frozen route")
        receipt_summary = {
            "terminal_status": actual_receipt.terminal_status.value,
            "usage_availability": actual_receipt.usage_availability.value,
            "cost_availability": actual_receipt.cost_availability.value,
            "cost_usd": actual_receipt.cost_usd,
            "latency_ms": actual_receipt.latency_ms,
        }
    return RouteOperatorProjection(
        employee_id=identity.employee_id,
        task_id=identity.task_id,
        route_id=binding.route_id,
        selection_reasons=selection.explanation(),
        selection_policy_digest=selection.policy_digest,
        intelligence_snapshot_digest=binding.intelligence_snapshot_digest,
        compatibility_evidence_digest=binding.compatibility_evidence_digest,
        compatibility_point_id=compatibility.point_id,
        compatibility_status=compatibility.status,
        egress_policy_digest=binding.egress_policy_digest,
        egress_policy_state=egress.state,
        fallback_policy_digest=binding.fallback_policy_digest,
        fallback_state=fallback_state,
        selected_uncertainty=candidate.uncertainty,
        actual_receipt=receipt_summary,
    )
