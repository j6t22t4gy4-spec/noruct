import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.product.execution_conclusion_renderer import (
    ExecutionConclusionViewModel,
    build_execution_conclusion_view_model,
    render_execution_conclusion_cli,
    render_execution_conclusion_json,
    render_execution_conclusion_modern_tui,
)
from dynamic_firm.product.execution_summary import EXECUTION_SUMMARY_SCHEMA
from dynamic_firm.product.execution_summary_v2 import EXECUTION_SUMMARY_V2_SCHEMA
from dynamic_firm.product.terminal import display_width


def _v1() -> dict:
    return {
        "schema_version": EXECUTION_SUMMARY_SCHEMA,
        "job_id": "job-1",
        "result": {
            "requested_purpose": "Ship a bounded change",
            "requested_outcome": "A reviewable result",
            "terminal_status": "SUCCEEDED",
            "outcome_claim": "NO_REAL_WORLD_OUTCOME_CLAIM",
        },
        "approach": {
            "company_work_mode": "SOLO",
            "planning_mode": "DIRECT",
            "recorded_reasons": ("bounded scope", "local evidence"),
        },
        "contribution": ({
            "employee_id": "employee-1",
            "task_id": "task-1",
            "task_status": "COMPLETED",
            "responsibility": "TASK_EXECUTION",
        },),
        "review_focus": ({
            "kind": "BOUNDARY",
            "status": "REVIEW_REQUIRED",
            "reason": "Check the contract",
        },),
        "verification": ({
            "name": "FOCUSED_TEST",
            "status": "PASSED",
            "evidence": "opaque-receipt",
        },),
        "delivery": {"unsupported": "must not appear"},
        "limitations_next": ({
            "status": "UNKNOWN",
            "issue": "Outcome is not independently verified",
            "next_action": "Review the named evidence",
        },),
        "raw_prompt": "must not appear",
    }


class ExecutionConclusionRendererTests(unittest.TestCase):
    def test_v1_build_is_immutable_and_bounded(self) -> None:
        model = build_execution_conclusion_view_model(_v1())

        self.assertIsInstance(model, ExecutionConclusionViewModel)
        with self.assertRaises((AttributeError, TypeError)):
            model.job_id = "changed"
        self.assertEqual(len(model.verification), 1)
        self.assertNotIn("unsupported", render_execution_conclusion_json(model))
        self.assertNotIn("raw_prompt", render_execution_conclusion_json(model))

    def test_v2_extensions_are_projected_without_raw_fields(self) -> None:
        summary = {
            "schema_version": EXECUTION_SUMMARY_V2_SCHEMA,
            "v1": _v1(),
            "extensions": {
                "assignment_rationale": {
                    "status": "RECORDED",
                    "items": ({"rationale_id": "r-1", "summary": "Recorded fit"},),
                },
                "ai_contribution": {
                    "status": "RECORDED",
                    "items": ({"employee_id": "ai-1", "summary": "Bounded contribution", "raw": "omit"},),
                },
                "review_focus": {
                    "status": "RECORDED",
                    "items": ({"kind": "ASSUMPTION", "status": "REVIEW_REQUIRED", "reason": "Check"},),
                },
                "material_alternatives": {
                    "status": "RECORDED",
                    "items": ({"alternative_id": "alt-1", "exclusion_reason": "Not selected"},),
                },
                "improvement_status": "OUTCOME_NOT_ESTABLISHED",
                "evidence_level": "MECHANISM_ONLY",
            },
        }

        model = build_execution_conclusion_view_model(summary)
        output = json.loads(render_execution_conclusion_json(model))
        self.assertEqual(model.source_schema, EXECUTION_SUMMARY_V2_SCHEMA)
        self.assertEqual(output["improvement"]["status"], "OUTCOME_NOT_ESTABLISHED")
        self.assertEqual(output["contribution"], [{"employee_id": "ai-1", "summary": "Bounded contribution"}])
        self.assertNotIn("raw", output["contribution"][0])

    def test_all_adapters_use_same_model_and_width_snapshots_are_bounded(self) -> None:
        model = build_execution_conclusion_view_model(_v1())
        cli = render_execution_conclusion_cli(model)
        plain_json = render_execution_conclusion_json(model)

        self.assertIn("Request", cli)
        self.assertEqual(
            json.loads(plain_json),
            {
                "source_schema": EXECUTION_SUMMARY_SCHEMA,
                "job_id": "job-1",
                "request": {"purpose": "Ship a bounded change", "outcome": "A reviewable result"},
                "completion": {"terminal_status": "SUCCEEDED", "outcome_claim": "NO_REAL_WORLD_OUTCOME_CLAIM"},
                "approach": {
                    "company_work_mode": "SOLO",
                    "planning_mode": "DIRECT",
                    "recorded_reasons": ["bounded scope", "local evidence"],
                    "assignment_rationale": [],
                },
                "contribution": [{"employee_id": "employee-1", "task_id": "task-1", "task_status": "COMPLETED", "responsibility": "TASK_EXECUTION"}],
                "review": [{"kind": "BOUNDARY", "status": "REVIEW_REQUIRED", "reason": "Check the contract"}],
                "verification": [{"name": "FOCUSED_TEST", "status": "PASSED", "evidence": "opaque-receipt"}],
                "alternatives": [],
                "improvement": {"status": "NOT_RECORDED", "evidence_level": "UNKNOWN"},
                "limitations_next": [{"status": "UNKNOWN", "issue": "Outcome is not independently verified", "next_action": "Review the named evidence"}],
            },
        )
        snapshots = {
            width: render_execution_conclusion_modern_tui(model, width=width)
            for width in (40, 80, 120)
        }
        self.assertEqual(set(snapshots), {40, 80, 120})
        for width, snapshot in snapshots.items():
            self.assertIn("Verification", snapshot)
            self.assertTrue(all(display_width(line) <= width for line in snapshot.splitlines()))

    def test_only_v1_and_v2_are_accepted(self) -> None:
        with self.assertRaises(ValueError):
            build_execution_conclusion_view_model({"schema_version": "other"})

    def test_malformed_optional_fields_close_to_empty_bounded_sections(self) -> None:
        summary = _v1()
        summary["approach"]["recorded_reasons"] = None
        model = build_execution_conclusion_view_model(summary)
        self.assertEqual(model.approach.recorded_reasons, ())


if __name__ == "__main__":
    unittest.main()
