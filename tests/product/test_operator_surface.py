from __future__ import annotations

from types import SimpleNamespace
import unittest

from dynamic_firm.product.operator_surface import (
    OPERATOR_SURFACE_SCHEMA,
    assessment_projection,
    build_operator_surface_snapshot,
)
from dynamic_firm.runtime.operator_attention import (
    CompanyAttention,
    CompanyAttentionItem,
    CompanyAttentionKind,
)
from dynamic_firm.runtime.models import Usage


class OperatorSurfaceTests(unittest.TestCase):
    def test_idle_projection_is_safe_and_has_no_hidden_execution_state(self) -> None:
        snapshot = build_operator_surface_snapshot(manager_report=None, inspection=None)

        self.assertEqual(snapshot.schema, OPERATOR_SURFACE_SCHEMA)
        self.assertEqual(snapshot.execution["status"], "IDLE")
        self.assertEqual(snapshot.hold["reason"], "none")
        self.assertEqual(snapshot.approval["pending_count"], 0)
        self.assertIn("next Work Order", snapshot.execution["decision"])
        self.assertNotIn("prompt", str(snapshot.as_dict()).lower())

    def test_active_projection_separates_decision_hold_approval_budget_and_assignments(self) -> None:
        manager = SimpleNamespace(
            manager_employee_id="employee-executive-manager",
            roster_revision=4,
            model_profile="model-a",
            supervised_job_count=3,
            specialist_job_count=2,
            replanned_job_count=1,
            pending_reason="insufficient_independent_production_outcomes",
        )
        inspection = SimpleNamespace(
            job_id="job-operator-surface",
            job_status=None,
            audit_status=SimpleNamespace(value="TERMINAL"),
            company_work_mode="TEAM_JOB",
            coordination_policy="PLAN_FIRST",
            planning_reason="MANAGER_SPECIALIST_DELEGATION_REQUIRED",
            requested_effect="WORKSPACE_CHANGE",
            graph_patch_count=1,
            graph_proposal_decisions=(
                {
                    "sequence": 1,
                    "status": "REJECTED",
                    "operation": "INSERT",
                    "base_graph_version": 1,
                    "proposed_lease": {"model_calls": 1, "tool_calls": 0, "cost_usd": 0.02},
                },
            ),
            reconstructed_tasks=(
                {"task_id": "implement", "assignee_id": "employee-engineer", "status": "RUNNING", "attempt": 2},
                {"task_id": "review", "assignee_id": "employee-reviewer", "status": "PENDING", "attempt": 1},
            ),
            runtime_runs=(SimpleNamespace(pending_approval_count=1),),
            job_limits={
                "max_total_model_calls": 8,
                "max_total_tool_calls": 6,
                "max_total_cost_usd": 1.5,
            },
            compiler_usage=Usage(model_calls=1, tool_calls=0, cost_usd=0.01),
            errors=(),
        )

        snapshot = build_operator_surface_snapshot(
            manager_report=manager,
            inspection=inspection,
        )

        self.assertEqual(snapshot.manager["employee_id"], "employee-executive-manager")
        self.assertEqual(snapshot.execution["work_mode"], "TEAM_JOB")
        self.assertEqual(snapshot.assignments[0]["employee_id"], "employee-engineer")
        self.assertEqual(snapshot.hold["reason"], "approval pending")
        self.assertEqual(snapshot.approval["status"], "operator decision required")
        self.assertEqual(snapshot.execution["graph_proposal_count"], 1)
        self.assertEqual(snapshot.execution["graph_proposal_status"], "REJECTED")
        self.assertEqual(snapshot.approval["latest_graph_proposal_status"], "REJECTED")
        self.assertEqual(snapshot.budget["max_model_calls"], 8)
        self.assertIn("Resolve the pending approval", snapshot.next_action)
        rendered = "\n".join(snapshot.lines())
        self.assertIn("Graph", rendered)
        self.assertIn("Hold", rendered)
        self.assertIn("Approval", rendered)
        self.assertIn("Budget", rendered)

    def test_interrupted_job_is_a_hold_even_without_pending_approval(self) -> None:
        inspection = SimpleNamespace(
            job_id="job-interrupted",
            job_status=None,
            audit_status=SimpleNamespace(value="INTERRUPTED"),
            company_work_mode="SOLO_JOB",
            coordination_policy="SOLO_FIRST",
            planning_reason="SAFE_SOLO",
            requested_effect="READ",
            graph_patch_count=0,
            reconstructed_tasks=(),
            runtime_runs=(),
            job_limits={},
            compiler_usage=Usage(),
            errors=(),
        )

        snapshot = build_operator_surface_snapshot(manager_report=None, inspection=inspection)

        self.assertEqual(snapshot.hold["status"], "HELD")
        self.assertIn("interrupted", snapshot.hold["reason"])
        self.assertIn("Inspect job-interrupted", snapshot.next_action)

    def test_company_attention_is_projected_without_creating_another_operator_state(self) -> None:
        attention = CompanyAttention(
            job_scan_limit=20,
            scanned_job_count=2,
            jobs_truncated=False,
            open_budget_incident_count=0,
            invalid_job_count=0,
            interrupted_job_count=1,
            blocking_effect_recovery_count=0,
            pending_approval_count=1,
            suppressed_pending_approval_count=1,
            items=(
                CompanyAttentionItem(
                    kind=CompanyAttentionKind.INTERRUPTED_JOB,
                    subject_id="job-old",
                    job_id="job-old",
                    run_id=None,
                    task_id=None,
                    employee_id=None,
                    state="INTERRUPTED_NEW_KERNEL_ATTEMPT_REQUIRED",
                    created_at="2026-01-01T00:00:00Z",
                    recommended_action="Inspect the ACTIVE JOB audit before choosing a new Kernel attempt.",
                ),
            ),
        )

        snapshot = build_operator_surface_snapshot(
            manager_report=None,
            inspection=None,
            attention=attention,
        )

        self.assertEqual(snapshot.attention["status"], "ACTION_REQUIRED")
        self.assertEqual(snapshot.attention["item_count"], 1)
        self.assertIn("Inspect the ACTIVE JOB audit", snapshot.next_action)
        self.assertIn("Attention", "\n".join(snapshot.lines()))
        self.assertNotIn("job-old", str(snapshot.attention))

    def test_assessment_projection_uses_observable_state_not_hidden_reasoning(self) -> None:
        snapshot = build_operator_surface_snapshot(manager_report=None, inspection=None)

        objective, observation, decision, next_action = assessment_projection(
            snapshot.as_dict(),
            current_objective="Review the release readiness.",
        )

        self.assertEqual(objective, "Review the release readiness.")
        self.assertIn("outcome evidence", observation)
        self.assertIn("next Work Order", decision)
        self.assertIn("Settings", next_action)
        self.assertNotIn("thought", " ".join((objective, observation, decision, next_action)).lower())


if __name__ == "__main__":
    unittest.main()
