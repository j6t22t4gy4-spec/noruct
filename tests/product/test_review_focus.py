from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.product.review_focus import (
    NONE_RECORDED,
    REVIEW_FOCUS_SCHEMA,
    project_review_focus,
)


def _candidate(
    evidence_id: str,
    *,
    severity: str,
    evidence_status: str,
) -> dict[str, str]:
    return {
        "subject": f"subject-{evidence_id}",
        "reason": f"reason-{evidence_id}",
        "evidence_id": evidence_id,
        "failure_impact": f"impact-{evidence_id}",
        "severity": severity,
        "evidence_status": evidence_status,
    }


class ReviewFocusTests(unittest.TestCase):
    def test_empty_golden_is_exact_none_recorded(self) -> None:
        self.assertEqual(project_review_focus(()), NONE_RECORDED)

    def test_one_candidate_golden_keeps_only_focus_item_schema(self) -> None:
        result = project_review_focus(
            (_candidate("evidence-1", severity="HIGH", evidence_status="NOT_RUN"),)
        )
        self.assertEqual(
            result,
            (
                {
                    "subject": "subject-evidence-1",
                    "reason": "reason-evidence-1",
                    "evidence_id": "evidence-1",
                    "failure_impact": "impact-evidence-1",
                },
            ),
        )
        self.assertNotIn("severity", result[0])
        self.assertEqual(REVIEW_FOCUS_SCHEMA, "noruct.review-focus.v1")

    def test_four_candidate_golden_orders_and_caps_at_three(self) -> None:
        result = project_review_focus(
            (
                _candidate("evidence-z", severity="LOW", evidence_status="PASSED"),
                _candidate("evidence-c", severity="HIGH", evidence_status="PARTIAL"),
                _candidate("evidence-b", severity="HIGH", evidence_status="FAILED"),
                _candidate("evidence-a", severity="HIGH", evidence_status="FAILED"),
            )
        )
        self.assertEqual(
            tuple(item["evidence_id"] for item in result),
            ("evidence-a", "evidence-b", "evidence-c"),
        )


if __name__ == "__main__":
    unittest.main()
