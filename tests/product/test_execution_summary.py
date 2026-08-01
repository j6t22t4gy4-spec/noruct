from __future__ import annotations

import unittest
from types import SimpleNamespace

from dynamic_firm.product.execution_summary import (
    EXECUTION_SUMMARY_SCHEMA,
    execution_summary,
)


class ExecutionSummaryTests(unittest.TestCase):
    def _inspection(self, *, audit: str = "TERMINAL", terminal: str | None = "SUCCEEDED") -> SimpleNamespace:
        return SimpleNamespace(
            job_id="job-summary-1",
            audit_status=SimpleNamespace(value=audit),
            job_status=terminal,
            replay_matches=audit == "TERMINAL",
            company_work_mode="SOLO_JOB",
            planning_mode="SOLO",
            operating_reason="VALID_SOLO",
            planning_reason="COMPILER_ACCEPTED",
            requested_effect="READ",
            tool_receipts=(),
            final_task_id="task-2",
            final_task_capabilities=(),
            validation_receipts=(),
            reconstructed_tasks=(
                {
                    "task_id": "task-1",
                    "assignee_id": "employee-1",
                    "status": "SUCCEEDED",
                    "hidden_reasoning": "must never reach the summary",
                },
                {
                    "task_id": "task-2",
                    "assignee_id": "employee-2",
                    "status": "SUCCEEDED",
                },
            ),
        )

    def test_summary_is_bounded_and_does_not_upgrade_missing_validation(self) -> None:
        order = SimpleNamespace(
            objective="Prepare a bounded execution report",
            requested_outcome="A reviewable summary",
        )
        summary = execution_summary(self._inspection(), work_order=order)
        rendered = repr(summary)
        self.assertEqual(summary["schema_version"], EXECUTION_SUMMARY_SCHEMA)
        self.assertEqual(summary["result"]["requested_purpose"], order.objective)
        self.assertEqual(summary["result"]["outcome_claim"], "NO_REAL_WORLD_OUTCOME_CLAIM")
        self.assertEqual(summary["verification"][0]["status"], "PASSED")
        self.assertEqual(summary["verification"][1]["status"], "NOT_RUN")
        self.assertLessEqual(len(summary["contribution"]), 3)
        self.assertLessEqual(len(summary["review_focus"]), 3)
        self.assertLessEqual(len(summary["verification"]), 5)
        self.assertEqual(summary["delivery"]["kind"], "NON_CODE")
        self.assertLessEqual(len(summary["limitations_next"]), 3)
        self.assertNotIn("hidden_reasoning", rendered)
        self.assertNotIn("must never reach", rendered)

    def test_summary_marks_absent_terminal_and_work_order_as_unknown(self) -> None:
        summary = execution_summary(self._inspection(audit="INTERRUPTED", terminal=None))
        self.assertEqual(summary["result"]["requested_purpose"], "UNKNOWN")
        self.assertEqual(summary["result"]["terminal_status"], "NOT_RECORDED")
        self.assertEqual(summary["verification"][0]["status"], "UNKNOWN")
        self.assertTrue(
            any(item["status"] == "UNKNOWN" for item in summary["limitations_next"])
        )

    def test_host_action_is_review_focus_not_effect_success_claim(self) -> None:
        inspection = self._inspection()
        inspection.requested_effect = "HOST_ACTION"
        summary = execution_summary(inspection)
        self.assertEqual(summary["review_focus"][0]["kind"], "EXTERNAL_EFFECT_BOUNDARY")
        self.assertEqual(summary["review_focus"][0]["status"], "REVIEW_REQUIRED")

    def test_effect_receipts_are_partial_or_unknown_never_real_world_success(self) -> None:
        inspection = self._inspection()
        inspection.tool_receipts = (
            {
                "action_id": "action-1",
                "task_id": "task-1",
                "tool_name": "workspace_write",
                "effect": "WRITE",
                "status": "SUCCEEDED",
            },
        )
        summary = execution_summary(inspection)
        receipt = summary["verification"][2]
        self.assertEqual(receipt["name"], "EXTERNAL_EFFECT_RECEIPTS")
        self.assertEqual(receipt["status"], "PARTIAL")
        inspection.tool_receipts = ({**inspection.tool_receipts[0], "status": "INDETERMINATE"},)
        self.assertEqual(execution_summary(inspection)["verification"][2]["status"], "UNKNOWN")

    def test_continuation_refusal_is_visible_without_configuration_detail(self) -> None:
        inspection = self._inspection()
        inspection.continuation_preflight_receipts = (
            {
                "receipt_id": "continuation-preflight:opaque",
                "continuation_kind": "READ_ONLY_PARTIAL",
                "code": "CAPABILITY_MANIFEST_MISMATCH",
                "created_at": "2026-07-31T00:00:00+00:00",
            },
        )
        receipt = execution_summary(inspection)["verification"][3]
        self.assertEqual(receipt["name"], "CONTINUATION_PREFLIGHT")
        self.assertEqual(receipt["status"], "FAILED")
        self.assertIn("CAPABILITY_MANIFEST_MISMATCH", receipt["evidence"])

    def test_code_delivery_keeps_named_validation_and_missing_facts_honest(self) -> None:
        inspection = self._inspection()
        inspection.final_task_capabilities = ("implementation",)
        inspection.validation_receipts = (
            {
                "task_id": "task-2",
                "employee_id": "employee-2",
                "name": "pytest",
                "status": "PASSED",
            },
        )
        delivery = execution_summary(inspection)["delivery"]
        self.assertEqual(delivery["schema_version"], "noruct.delivery-evidence.v1")
        self.assertEqual(delivery["kind"], "CODE")
        self.assertEqual(delivery["ai_responsibility"]["scope"], "IMPLEMENTATION_TASK")
        self.assertEqual(delivery["verification"][0]["name"], "pytest")
        self.assertEqual(delivery["verification"][0]["status"], "PASSED")
        inspection.validation_receipts = ()
        self.assertEqual(
            execution_summary(inspection)["delivery"]["verification"][0]["status"],
            "NOT_RUN",
        )

    def test_non_code_delivery_uses_same_schema_for_effect_receipt(self) -> None:
        inspection = self._inspection()
        inspection.requested_effect = "HOST_ACTION"
        inspection.tool_receipts = (
            {
                "action_id": "action-1",
                "task_id": "task-2",
                "tool_name": "workspace_write",
                "effect": "WRITE",
                "status": "SUCCEEDED",
            },
        )
        delivery = execution_summary(inspection)["delivery"]
        self.assertEqual(delivery["schema_version"], "noruct.delivery-evidence.v1")
        self.assertEqual(delivery["kind"], "NON_CODE")
        self.assertEqual(delivery["subject"]["kind"], "EXTERNAL_EFFECT")
        self.assertEqual(delivery["subject"]["status"], "PARTIAL")

    def test_direct_solo_and_team_keep_the_exact_same_honesty_contract(self) -> None:
        expected_fields = {
            "schema_version",
            "job_id",
            "result",
            "approach",
            "contribution",
            "review_focus",
            "verification",
            "delivery",
            "limitations_next",
        }
        for mode, planning in (
            ("DIRECT", "DIRECT"),
            ("SOLO_JOB", "SOLO"),
            ("TEAM_JOB", "DYNAMIC"),
        ):
            inspection = self._inspection()
            inspection.company_work_mode = mode
            inspection.planning_mode = planning
            summary = execution_summary(inspection)
            self.assertEqual(set(summary), expected_fields)
            self.assertEqual(summary["approach"]["company_work_mode"], mode)
            self.assertEqual(summary["result"]["outcome_claim"], "NO_REAL_WORLD_OUTCOME_CLAIM")
            self.assertEqual(summary["verification"][1]["status"], "NOT_RUN")
