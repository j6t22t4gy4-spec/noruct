from __future__ import annotations

import asyncio
import unittest

from dynamic_firm.company.multi_route_job_plan import (
    DependencyArtifactHandoff,
    MultiRouteAssignmentGuard,
    MultiRouteJobPlan,
    TaskRouteAssignment,
)
from dynamic_firm.kernel.models import EmployeeRecord, JobStatus, TaskAssignmentEvent
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from tests.kernel.helpers import company_request, task


class MultiRouteJobPlanTests(unittest.TestCase):
    def plan(self, **changes: object) -> MultiRouteJobPlan:
        values: dict[str, object] = {
            "graph_digest": "a" * 64,
            "assignments": (
                TaskRouteAssignment("explore", "employee-a", "b" * 64),
                TaskRouteAssignment(
                    "verify", "employee-b", "c" * 64, ("explore",)
                ),
                TaskRouteAssignment(
                    "integrate", "employee-c", "d" * 64, ("verify",), True
                ),
            ),
            "handoffs": (
                DependencyArtifactHandoff("explore", "verify", "e" * 64),
                DependencyArtifactHandoff("verify", "integrate", "f" * 64),
            ),
            "acting_integrator_id": "employee-c",
        }
        values.update(changes)
        return MultiRouteJobPlan(**values)  # type: ignore[arg-type]

    def test_heterogeneous_three_task_plan_has_typed_handoffs_and_one_owner(self) -> None:
        value = self.plan()
        self.assertEqual(
            {item.route_binding_digest for item in value.assignments},
            {"b" * 64, "c" * 64, "d" * 64},
        )
        self.assertEqual(value.acting_integrator_id, "employee-c")

    def test_selection_receipt_digest_is_optional_but_exact_when_supplied(self) -> None:
        legacy = TaskRouteAssignment("explore", "employee-a", "b" * 64)
        self.assertIsNone(legacy.expected_selection_receipt_digest)
        expected = TaskRouteAssignment(
            "explore",
            "employee-a",
            "b" * 64,
            expected_selection_receipt_digest="c" * 64,
        )
        self.assertEqual(expected.expected_selection_receipt_digest, "c" * 64)
        for invalid in ("C" * 64, "a" * 63, "not-a-digest"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    TaskRouteAssignment(
                        "explore",
                        "employee-a",
                        "b" * 64,
                        expected_selection_receipt_digest=invalid,
                    )

    def test_missing_dependency_or_single_owner_violation_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            self.plan(handoffs=())
        with self.assertRaises(ValueError):
            self.plan(
                handoffs=(
                    DependencyArtifactHandoff("explore", "integrate", "e" * 64),
                )
            )
        assignments = (
            TaskRouteAssignment("one", "employee-a", "b" * 64, final=True),
            TaskRouteAssignment("two", "employee-b", "c" * 64, final=True),
        )
        with self.assertRaises(ValueError):
            self.plan(assignments=assignments, handoffs=())

    def test_kernel_assignment_guard_admits_each_frozen_task_once_and_returns_exact_digest(self) -> None:
        guard = MultiRouteAssignmentGuard(self.plan())
        events = (
            TaskAssignmentEvent(
                "job", "explore", 1, "employee-a", "role", False, (), (), 1,
                False, "match", 1,
            ),
            TaskAssignmentEvent(
                "job", "verify", 1, "employee-b", "role", False, (),
                ("explore",), 1, False, "match", 1,
            ),
            TaskAssignmentEvent(
                "job", "integrate", 1, "employee-c", "role", False, (),
                ("verify",), 1, True, "match", 1,
            ),
        )
        self.assertEqual(
            tuple(guard.accept(event) for event in events),
            ("b" * 64, "c" * 64, "d" * 64),
        )
        with self.assertRaises(ValueError):
            guard.accept(events[0])

    def test_kernel_assignment_guard_rejects_wrong_employee_final_or_dependencies(self) -> None:
        assignments = (
            ("explore", "employee-z", False, ()),
            ("verify", "employee-b", True, ("explore",)),
            ("verify", "employee-b", False, ()),
        )
        for task_id, employee_id, final, depends_on in assignments:
            with self.subTest(
                task_id=task_id,
                employee_id=employee_id,
                final=final,
                depends_on=depends_on,
            ):
                guard = MultiRouteAssignmentGuard(self.plan())
                with self.assertRaises(ValueError):
                    guard.accept(
                        TaskAssignmentEvent(
                            "job", task_id, 1, employee_id, "role", False, (),
                            depends_on, 1, final, "match", 1,
                        )
                    )

    def test_kernel_pre_dispatch_admission_fails_closed_before_employee_start_and_product_sink_remains_best_effort(self) -> None:
        request = company_request(
            (task("only"),),
            final_task_id="only",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        rejected_plan = MultiRouteJobPlan(
            "a" * 64,
            (TaskRouteAssignment("only", "different-employee", "b" * 64, final=True),),
            (),
            "different-employee",
        )
        rejected_runner = ScriptedEmployeeExecutionPort({"only": ScriptedOutcome("done")})
        emitted: list[TaskAssignmentEvent] = []
        rejected = asyncio.run(
            FirmKernel(
                employee_execution=rejected_runner,
                assignment_admission=MultiRouteAssignmentGuard(rejected_plan).accept,
                assignment_sink=emitted.append,
            ).run(request)
        )
        self.assertEqual(rejected.status, JobStatus.FAILED)
        self.assertEqual(
            rejected.failure_reason,
            "Frozen route admission rejected task dispatch.",
        )
        self.assertEqual(rejected_runner.requests, [])
        self.assertEqual(emitted, [])

        admitted_plan = MultiRouteJobPlan(
            "a" * 64,
            (TaskRouteAssignment("only", "analyst", "c" * 64, final=True),),
            (),
            "analyst",
        )
        admitted_runner = ScriptedEmployeeExecutionPort({"only": ScriptedOutcome("done")})

        def broken_product_sink(event: TaskAssignmentEvent) -> None:
            raise RuntimeError("projection unavailable")

        admitted = asyncio.run(
            FirmKernel(
                employee_execution=admitted_runner,
                assignment_admission=MultiRouteAssignmentGuard(admitted_plan).accept,
                assignment_sink=broken_product_sink,
            ).run(request)
        )
        self.assertEqual(admitted.status, JobStatus.SUCCEEDED)
        self.assertEqual(len(admitted_runner.requests), 1)
