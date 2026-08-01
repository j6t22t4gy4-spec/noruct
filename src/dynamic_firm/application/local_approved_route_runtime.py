"""Fresh local approval adapter for an already-frozen EmployeeRun route plan.

The adapter deliberately owns no provider resolution, credential access, egress
decision, or route selection.  It rereads the non-secret local approval table
for every request, then re-admits the immutable plan before exposing its exact
binding to the runtime service.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dynamic_firm.company.approved_route_plan_admission import (
    require_fresh_approved_route_plan,
)
from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.frozen_route_admission import FrozenRouteAdmission
from dynamic_firm.company.multi_route_runtime_policy import MultiRouteRuntimePolicy
from dynamic_firm.company.route_selection_receipt import RouteSelectionReceipt
from dynamic_firm.product.local_routing_settings import load_local_routing_settings
from dynamic_firm.runtime.models import EmployeeRunRequest


@dataclass(frozen=True, slots=True)
class PreFrozenSelectionReceipt:
    """Receipt evidence supplied by the caller for one exact frozen binding.

    The adapter is intentionally not a selection authority.  This narrow value
    only keys a receipt which was selected and frozen elsewhere to the durable
    binding that it is permitted to admit.
    """

    binding_digest: str
    selection_receipt: RouteSelectionReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.binding_digest, str) or len(self.binding_digest) != 64:
            raise ValueError("binding_digest must be an exact SHA-256 digest")
        if any(character not in "0123456789abcdef" for character in self.binding_digest):
            raise ValueError("binding_digest must be an exact SHA-256 digest")
        if not isinstance(self.selection_receipt, RouteSelectionReceipt):
            raise TypeError("selection_receipt must be a RouteSelectionReceipt")


@dataclass(frozen=True, slots=True)
class LocalApprovedRouteRuntime:
    """Resolve only an exact binding admitted by current local TOML settings."""

    config_path: Path
    frozen_runtime_policy: MultiRouteRuntimePolicy
    frozen_selection_receipts: tuple[PreFrozenSelectionReceipt, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config_path, Path):
            raise TypeError("config_path must be a pathlib.Path")
        if not isinstance(self.frozen_runtime_policy, MultiRouteRuntimePolicy):
            raise TypeError("frozen_runtime_policy must be a MultiRouteRuntimePolicy")
        if self.frozen_selection_receipts is not None:
            if not isinstance(self.frozen_selection_receipts, tuple) or not all(
                isinstance(item, PreFrozenSelectionReceipt)
                for item in self.frozen_selection_receipts
            ):
                raise TypeError("frozen_selection_receipts must be a typed immutable tuple")
            expected = {binding.digest for binding in self.frozen_runtime_policy.bindings}
            actual = tuple(item.binding_digest for item in self.frozen_selection_receipts)
            if len(actual) != len(set(actual)):
                raise ValueError("frozen selection receipts must have unique binding digests")
            if set(actual) != expected:
                raise ValueError("frozen selection receipts must exactly cover frozen bindings")
        object.__setattr__(self, "config_path", self.config_path.expanduser().resolve())

    def __call__(self, request: EmployeeRunRequest) -> ExecutionRouteBinding:
        if not isinstance(request, EmployeeRunRequest):
            raise TypeError("request must be an EmployeeRunRequest")
        settings = load_local_routing_settings(self.config_path)
        approved_plan = require_fresh_approved_route_plan(
            settings.policy,
            settings.approved_routes,
            self.frozen_runtime_policy,
        )
        return approved_plan.binding_for(request)

    def require_frozen_selection_closure(self) -> None:
        """Validate all caller-frozen selection evidence before assembly.

        This is a semantic closure check only.  It neither reads the local
        approval file nor resolves a provider: every receipt must already
        select the exact binding route under that binding's frozen policy and
        match the digest carried by its task assignment.
        """
        receipts = self.frozen_selection_receipts
        if receipts is None:
            raise ValueError("frozen selection receipt closure is required")
        if not isinstance(receipts, tuple) or not all(
            isinstance(item, PreFrozenSelectionReceipt) for item in receipts
        ):
            raise ValueError("frozen selection receipt closure is malformed")

        policy = self.frozen_runtime_policy
        bindings_by_digest = {binding.digest: binding for binding in policy.bindings}
        receipts_by_binding = {item.binding_digest: item.selection_receipt for item in receipts}
        if len(receipts_by_binding) != len(receipts):
            raise ValueError("frozen selection receipt closure has duplicate bindings")
        if set(receipts_by_binding) != set(bindings_by_digest):
            raise ValueError("frozen selection receipt closure does not cover exact bindings")

        assignments_by_binding = {
            assignment.route_binding_digest: assignment
            for assignment in policy.plan.assignments
        }
        if set(assignments_by_binding) != set(bindings_by_digest):
            raise ValueError("frozen assignment closure does not cover exact bindings")
        for binding_digest, binding in bindings_by_digest.items():
            receipt = receipts_by_binding[binding_digest]
            assignment = assignments_by_binding[binding_digest]
            if not isinstance(receipt, RouteSelectionReceipt):
                raise ValueError("frozen selection receipt closure is malformed")
            if receipt.selected_route_id != binding.route_id:
                raise ValueError("frozen selection receipt selected the wrong route")
            if receipt.policy_digest != binding.orchestration_policy_digest:
                raise ValueError("frozen selection receipt used the wrong policy")
            if (
                assignment.expected_selection_receipt_digest is None
                or assignment.expected_selection_receipt_digest != receipt.digest
            ):
                raise ValueError("frozen assignment selection receipt digest mismatches")

    def admission_for(self, request: EmployeeRunRequest) -> FrozenRouteAdmission:
        """Re-admit current local approval and pair its binding with frozen evidence."""
        binding = self(request)
        if self.frozen_selection_receipts is None:
            raise ValueError("frozen selection receipts are required for admission resolution")
        assignment = next(
            (
                item
                for item in self.frozen_runtime_policy.plan.assignments
                if item.task_id == request.task.task_id
                and item.employee_id == request.employee.employee_id
            ),
            None,
        )
        if assignment is None:
            raise ValueError("EmployeeRunRequest does not match the frozen multi-route plan")
        if assignment.expected_selection_receipt_digest is None:
            raise ValueError("frozen assignment must bind a selection receipt for admission resolution")
        receipt = next(
            item.selection_receipt
            for item in self.frozen_selection_receipts
            if item.binding_digest == binding.digest
        )
        if receipt.digest != assignment.expected_selection_receipt_digest:
            raise ValueError("frozen selection receipt does not match the frozen assignment")
        return FrozenRouteAdmission(binding=binding, selection_receipt=receipt)
