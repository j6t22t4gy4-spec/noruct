from __future__ import annotations

import unittest

from dynamic_firm.kernel.models import (
    GraphPatch,
    GraphPatchEvent,
    SemanticOperation,
    TaskAssignmentEvent,
)
from dynamic_firm.product.events import (
    ProductEvent,
    ProductEventType,
    product_event_from_assignment,
    product_event_from_graph_patch,
)
from dynamic_firm.product.terminal_activity import TerminalFlowProjector


class TerminalFlowProjectorTests(unittest.TestCase):
    def test_direct_company_turn_never_projects_a_plan_or_organization_stage(self) -> None:
        projector = TerminalFlowProjector()
        projector.record_event(
            ProductEvent(
                ProductEventType.INPUT_ROUTED,
                "Company direct assignment",
                data={"company_work_mode": "DIRECT"},
            )
        )
        projector.record_event(
            ProductEvent(
                ProductEventType.PLAN_ACCEPTED,
                "direct plan",
                data={"mode": "DIRECT", "company_work_mode": "DIRECT"},
            )
        )
        projector.record_event(
            ProductEvent(
                ProductEventType.TASK_ASSIGNED,
                "Generalist assigned",
                data={"employee_role": "Noruct Generalist"},
            )
        )
        projector.record_event(
            ProductEvent(ProductEventType.EMPLOYEE_STARTED, "Generalist started")
        )

        snapshot = projector.snapshot()
        self.assertEqual(snapshot.stage, "RESPONDING")
        labels = [item.label for item in snapshot.items]
        self.assertNotIn("Execution plan accepted", labels)
        self.assertNotIn("Ready work assigned", labels)
        self.assertIn("Persistent employee assigned", labels)
        self.assertIn("Employee answering directly", labels)

    def test_projects_company_lifecycle_without_recording_answer_tokens(self) -> None:
        projector = TerminalFlowProjector()
        projector.record_event(
            ProductEvent(ProductEventType.COMPILER_STARTED, "Compile a bounded company")
        )
        projector.record_event(
            ProductEvent(ProductEventType.TOOL_RUNNING, "Running workspace_search")
        )
        projector.record_event(
            ProductEvent(
                ProductEventType.MODEL_STREAMING,
                "private answer token",
                data={"stream_kind": "text_delta"},
            )
        )
        snapshot = projector.snapshot()

        self.assertEqual(snapshot.stage, "RESPONDING")
        self.assertEqual(snapshot.next_step, "Return the composer to ready")
        self.assertEqual([item.label for item in snapshot.items], [
            "Company is scoping work",
            "Tool action running",
        ])
        self.assertNotIn("private answer token", "\n".join(item.detail for item in snapshot.items))

    def test_approval_denial_is_visible_as_a_blocked_guard(self) -> None:
        projector = TerminalFlowProjector(limit=2)
        projector.record_event(
            ProductEvent(ProductEventType.APPROVAL_REQUIRED, "Apply a protected workspace change")
        )
        projector.record_event(
            ProductEvent(ProductEventType.APPROVAL_RESOLVED, "DENY")
        )
        snapshot = projector.snapshot()

        self.assertEqual(snapshot.stage, "BLOCKED")
        self.assertEqual(snapshot.guard, "protected action remains unexecuted")
        self.assertEqual(snapshot.items[-1].label, "Protected action was not approved")

    def test_activity_history_is_bounded_and_system_events_are_labeled(self) -> None:
        projector = TerminalFlowProjector(limit=2)
        projector.record_system("first")
        projector.record_system("second")
        projector.record_system("third", stage="COMMAND")
        snapshot = projector.snapshot()

        self.assertEqual(snapshot.stage, "COMMAND")
        self.assertEqual([item.sequence for item in snapshot.items], [2, 3])
        self.assertTrue(all(item.label == "Local operator surface" for item in snapshot.items))

    def test_requires_positive_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            TerminalFlowProjector(limit=0)

    def test_organization_claim_follows_applied_patch_and_final_metrics(self) -> None:
        projector = TerminalFlowProjector()
        projector.record_event(
            ProductEvent(
                ProductEventType.ORGANIZATION_ADMISSION,
                "Typed specialist need admitted",
                data={"admitted": True, "capability": "security"},
            )
        )
        self.assertNotIn("expanded", projector.snapshot().items[-1].detail.lower())

        assignment = product_event_from_assignment(
            TaskAssignmentEvent(
                job_id="job-1",
                task_id="security-review",
                graph_version=2,
                employee_id="security-reviewer",
                employee_role="Security Reviewer",
                employee_temporary=False,
                required_capabilities=("security",),
                depends_on=("discovery",),
                attempt=1,
                final_task=False,
                selection_reason="PERSISTENT_CAPABILITY_MATCH",
                active_task_count=1,
            )
        )
        projector.record_event(assignment)
        self.assertIn("Security Reviewer", projector.snapshot().items[-1].detail)
        self.assertIn("persistent", projector.snapshot().items[-1].detail)

        patch = GraphPatch(
            patch_id="insert-security",
            base_graph_version=1,
            trigger_task_id="discovery",
            semantic_operation=SemanticOperation.INSERT,
            rationale="Typed security gap",
            expected_gain="Add security evidence",
            operations=(),
        )
        applied = product_event_from_graph_patch(
            GraphPatchEvent(
                event_id="graph-event-1",
                sequence=1,
                patch=patch,
                target_graph_version=2,
                before_graph_digest="before",
                after_graph_digest="after",
                added_task_ids=("security-review",),
                cancelled_task_ids=(),
                content_hash="event-hash",
            ),
            job_id="job-1",
        )
        projector.record_event(applied)
        self.assertIn("Organization expanded", projector.snapshot().items[-1].detail)
        self.assertEqual(applied.data["mutation_lease"]["model_calls"], 0)
        self.assertEqual(applied.data["mutation_lease"]["cost_usd"], 0.0)

        projector.record_event(
            ProductEvent(
                ProductEventType.JOB_FINISHED,
                "Company job succeeded",
                data={
                    "status": "SUCCEEDED",
                    "unique_employee_count": 2,
                    "maximum_parallelism": 2,
                    "graph_patch_count": 1,
                    "task_mutation_count": 0,
                },
            )
        )
        detail = projector.snapshot().items[-1].detail
        self.assertIn("2 employees", detail)
        self.assertIn("1 workflow revisions", detail)
