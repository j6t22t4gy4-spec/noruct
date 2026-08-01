"""Pure mapping from a frozen multi-route plan to exact route bindings."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from dynamic_firm.runtime.models import EmployeeRunRequest

from .execution_route_binding import ExecutionRouteBinding
from .multi_route_job_plan import MultiRouteJobPlan


@dataclass(frozen=True, slots=True)
class MultiRouteRuntimePolicy:
    """Read-only route callback for a plan already admitted by the Kernel.

    The policy cannot choose a route: every assignment is paired with exactly
    one supplied binding before an EmployeeRunRequest can be dispatched.
    """

    plan: MultiRouteJobPlan
    bindings: tuple[ExecutionRouteBinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MultiRouteJobPlan):
            raise TypeError("multi-route plan is required")
        if not isinstance(self.bindings, tuple) or not all(
            isinstance(binding, ExecutionRouteBinding) for binding in self.bindings
        ):
            raise TypeError("route bindings must be a typed immutable tuple")

        expected = tuple(item.route_binding_digest for item in self.plan.assignments)
        actual = tuple(binding.digest for binding in self.bindings)
        if len(actual) != len(expected) or set(actual) != set(expected):
            raise ValueError("route bindings must exactly cover the frozen plan")
        if any(actual.count(digest) != 1 for digest in expected):
            raise ValueError("each frozen assignment requires one exact route binding")

    def binding_for(self, request: EmployeeRunRequest) -> ExecutionRouteBinding:
        """Return the frozen binding or reject task/Employee drift before dispatch."""
        if not isinstance(request, EmployeeRunRequest):
            raise TypeError("EmployeeRunRequest is required")
        assignment = next(
            (
                item
                for item in self.plan.assignments
                if item.task_id == request.task.task_id
            ),
            None,
        )
        if assignment is None or assignment.employee_id != request.employee.employee_id:
            raise ValueError("EmployeeRunRequest does not match the frozen multi-route plan")
        return next(
            binding
            for binding in self.bindings
            if binding.digest == assignment.route_binding_digest
        )

    def __call__(self, request: EmployeeRunRequest) -> ExecutionRouteBinding:
        return self.binding_for(request)

    def canonical_summary(self) -> str:
        """Return content-free route evidence, never provider configuration or model IDs."""
        payload = {
            "acting_integrator_id": self.plan.acting_integrator_id,
            "assignment_bindings": [
                {
                    "depends_on": list(assignment.depends_on),
                    "employee_id": assignment.employee_id,
                    "expected_selection_receipt_digest": assignment.expected_selection_receipt_digest,
                    "final": assignment.final,
                    "route_binding_digest": assignment.route_binding_digest,
                    "task_id": assignment.task_id,
                }
                for assignment in sorted(self.plan.assignments, key=lambda item: item.task_id)
            ],
            "graph_digest": self.plan.graph_digest,
            "handoffs": [
                {
                    "artifact_digest": handoff.artifact_digest,
                    "source_task_id": handoff.source_task_id,
                    "target_task_id": handoff.target_task_id,
                }
                for handoff in sorted(
                    self.plan.handoffs,
                    key=lambda item: (item.source_task_id, item.target_task_id, item.artifact_digest),
                )
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @property
    def summary_digest(self) -> str:
        return hashlib.sha256(self.canonical_summary().encode("utf-8")).hexdigest()
