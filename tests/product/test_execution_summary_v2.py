from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.product.execution_summary import EXECUTION_SUMMARY_SCHEMA
from dynamic_firm.product.execution_summary_v2 import (
    EXECUTION_SUMMARY_V2_SCHEMA,
    migrate_execution_summary,
    negotiate_execution_summary_version,
    execution_summary_v2,
    v1_payload_from_execution_summary_v2,
)


class ExecutionSummaryV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.v1 = {
            "schema_version": EXECUTION_SUMMARY_SCHEMA,
            "job_id": "job-golden-1",
            "result": {
                "requested_purpose": "Review a bounded change",
                "terminal_status": "SUCCEEDED",
            },
            "verification": ({"name": "focused-test", "status": "PASSED"},),
        }

    def test_v1_compatibility_golden_is_unchanged_and_extractable(self) -> None:
        envelope = execution_summary_v2(self.v1)

        self.assertEqual(
            envelope,
            {
                "schema_version": EXECUTION_SUMMARY_V2_SCHEMA,
                "v1": self.v1,
                "extensions": {
                    "assignment_rationale": {"status": "NOT_RECORDED", "items": ()},
                    "ai_contribution": {"status": "NOT_RECORDED", "items": ()},
                    "review_focus": {"status": "NOT_RECORDED", "items": ()},
                    "material_alternatives": {"status": "NOT_RECORDED", "items": ()},
                    "improvement_status": "NOT_RECORDED",
                    "evidence_level": "UNKNOWN",
                },
            },
        )
        self.assertEqual(v1_payload_from_execution_summary_v2(envelope), self.v1)
        self.assertEqual(migrate_execution_summary(envelope, EXECUTION_SUMMARY_SCHEMA), self.v1)

    def test_complete_v2_golden_keeps_only_explicit_facts_and_three_items(self) -> None:
        facts = {
            "assignment_rationale": [{"task_id": "task-1", "reason": "required capability"}],
            "ai_contribution": [
                {"kind": "PROPOSED", "subject": "change-set-1"},
                {"kind": "INTEGRATED", "subject": "change-set-1"},
                {"kind": "REVIEWED", "subject": "validation-1"},
            ],
            "review_focus": [{"kind": "BOUNDARY", "evidence_id": "review-1"}],
            "material_alternatives": [
                {"choice": "SOLO", "status": "REJECTED"},
                {"choice": "TEAM", "status": "SELECTED"},
            ],
            "improvement_status": "OUTCOME_NOT_ESTABLISHED",
            "evidence_level": "PARTIAL",
        }
        envelope = execution_summary_v2(self.v1, extension_facts=facts)

        self.assertEqual(envelope["v1"], self.v1)
        self.assertEqual(envelope["extensions"]["assignment_rationale"]["status"], "RECORDED")
        self.assertEqual(
            envelope["extensions"]["ai_contribution"]["items"],
            tuple(facts["ai_contribution"]),
        )
        self.assertEqual(envelope["extensions"]["improvement_status"], "OUTCOME_NOT_ESTABLISHED")
        self.assertEqual(envelope["extensions"]["evidence_level"], "PARTIAL")
        for name in ("assignment_rationale", "ai_contribution", "review_focus", "material_alternatives"):
            self.assertLessEqual(len(envelope["extensions"][name]["items"]), 3)

    def test_missing_evidence_golden_uses_fixed_conservative_states(self) -> None:
        envelope = execution_summary_v2(self.v1, extension_facts={})
        extensions = envelope["extensions"]

        for name in ("assignment_rationale", "ai_contribution", "review_focus", "material_alternatives"):
            self.assertEqual(extensions[name], {"status": "NOT_RECORDED", "items": ()})
        self.assertEqual(extensions["improvement_status"], "NOT_RECORDED")
        self.assertEqual(extensions["evidence_level"], "UNKNOWN")

    def test_bounds_and_unknown_extensions_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            execution_summary_v2(
                self.v1,
                extension_facts={"review_focus": [{"kind": str(i)} for i in range(4)]},
            )
        with self.assertRaises(ValueError):
            execution_summary_v2(self.v1, extension_facts={"inferred_fact": "never"})

    def test_version_negotiation_and_explicit_migration(self) -> None:
        self.assertEqual(
            negotiate_execution_summary_version(None, (EXECUTION_SUMMARY_SCHEMA, EXECUTION_SUMMARY_V2_SCHEMA)),
            EXECUTION_SUMMARY_V2_SCHEMA,
        )
        self.assertEqual(
            negotiate_execution_summary_version(EXECUTION_SUMMARY_SCHEMA, (EXECUTION_SUMMARY_V2_SCHEMA,)),
            EXECUTION_SUMMARY_V2_SCHEMA,
        )
        self.assertEqual(migrate_execution_summary(self.v1, EXECUTION_SUMMARY_V2_SCHEMA)["schema_version"], EXECUTION_SUMMARY_V2_SCHEMA)


if __name__ == "__main__":
    unittest.main()
