from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.product.improvement_conclusion import (  # noqa: E402
    COMPARISON_RECORDED,
    COVERAGE_IDENTITY_MISMATCH,
    COVERAGE_MATCHED,
    OUTCOME_NOT_ESTABLISHED,
    ComparisonIdentity,
    conclude_improvement,
)


IDENTITY = {
    "task_revision": "task-r3",
    "source": "source-d7",
    "authority": "authority-a2",
    "budget": "budget-b4",
}
BASELINE = {
    "quality": 0.70,
    "complete_failure": 1,
    "safety_failure": 0,
    "cost": 2.5,
    "time": 100,
    "review": 30,
    "rework": 1,
}
CANDIDATE = {
    "quality": 0.80,
    "complete_failure": 0,
    "safety_failure": 0,
    "cost": 2.0,
    "time": 90,
    "review": 20,
    "rework": 0,
}


class ImprovementConclusionTests(unittest.TestCase):
    def test_matched_baseline_returns_only_fixed_deltas(self) -> None:
        result = conclude_improvement(IDENTITY, CANDIDATE, IDENTITY, BASELINE)

        self.assertEqual(result.conclusion, COMPARISON_RECORDED)
        self.assertEqual(result.coverage_status, COVERAGE_MATCHED)
        self.assertEqual(
            tuple(item.metric for item in result.metric_deltas),
            (
                "quality",
                "complete_failure",
                "safety_failure",
                "cost",
                "time",
                "review",
                "rework",
            ),
        )
        deltas = tuple(item.delta for item in result.metric_deltas)
        self.assertAlmostEqual(deltas[0], 0.10)
        self.assertEqual(deltas[1:], (-1, 0, -0.5, -10, -10, -1))
        self.assertEqual(result.outcome_claim, "NO_USER_OUTCOME_CLAIM")

    def test_mismatched_identity_closes_without_deltas(self) -> None:
        other = {**IDENTITY, "source": "source-other"}

        result = conclude_improvement(IDENTITY, CANDIDATE, other, BASELINE)

        self.assertEqual(result.conclusion, OUTCOME_NOT_ESTABLISHED)
        self.assertEqual(result.coverage_status, COVERAGE_IDENTITY_MISMATCH)
        self.assertEqual(result.metric_deltas, ())

    def test_missing_baseline_closes_without_deltas(self) -> None:
        result = conclude_improvement(IDENTITY, CANDIDATE)

        self.assertEqual(result.conclusion, OUTCOME_NOT_ESTABLISHED)
        self.assertEqual(result.metric_deltas, ())

    def test_outputs_are_immutable_and_identity_contains_fixed_metric_set(self) -> None:
        identity = ComparisonIdentity(**IDENTITY)
        result = conclude_improvement(identity, CANDIDATE, identity, BASELINE)

        with self.assertRaises(Exception):
            result.comparison_identity.task_revision = "changed"  # type: ignore[misc]
        self.assertEqual(identity.metric_set, tuple(item.metric for item in result.metric_deltas))


if __name__ == "__main__":
    unittest.main()
