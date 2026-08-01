import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.assignment_rationale import (
    AssignmentAlternative,
    AssignmentExclusionReason,
    AssignmentRationale,
)
from dynamic_firm.product.assignment_rationale_summary import (
    AssignmentContribution,
    AssignmentMaterialDifference,
    AssignmentRationaleSummaryStatus,
    summarize_assignment_rationale,
)


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _alternative(identifier: str) -> AssignmentAlternative:
    return AssignmentAlternative.compared_candidate(
        alternative_id=identifier,
        material_profile_digest=_DIGEST_B,
        exclusion_reason=AssignmentExclusionReason.EVIDENCE_WEAK,
    )


class AssignmentRationaleSummaryGoldenTests(unittest.TestCase):
    def test_complete_golden_fixture(self) -> None:
        record = AssignmentRationale(
            required_capability="capability.review",
            selected_material_profile_digest=_DIGEST_A,
            exercised_capability="capability.review",
            alternatives=(_alternative("alternative-opaque-1"),),
        )

        summary = summarize_assignment_rationale((record,))

        self.assertEqual(summary.status, AssignmentRationaleSummaryStatus.RECORDED)
        self.assertEqual(len(summary.entries), 1)
        entry = summary.entries[0]
        self.assertEqual(entry.material_difference, AssignmentMaterialDifference.EXERCISED)
        self.assertEqual(entry.contribution, AssignmentContribution.EXERCISED)
        self.assertTrue(entry.rationale_drill_down_id.startswith("rr-"))
        self.assertEqual(entry.alternative_drill_down_ids, ("alternative-opaque-1",))
        self.assertEqual(entry.alternative_exclusion_reasons, ("EVIDENCE_WEAK",))
        self.assertEqual(summary.payload()["section"], "assignment-rationale-summary")

    def test_unknown_golden_fixture_does_not_infer_reason(self) -> None:
        record = AssignmentRationale(
            required_capability="capability.review",
            selected_material_profile_digest=_DIGEST_A,
            exercised_capability="capability.other",
        )

        summary = summarize_assignment_rationale((record,))

        entry = summary.entries[0]
        self.assertEqual(
            entry.material_difference,
            AssignmentMaterialDifference.NOT_EXERCISED,
        )
        self.assertEqual(entry.contribution, AssignmentContribution.NOT_EXERCISED)
        self.assertNotIn("reason", entry.payload())

    def test_no_records_and_three_entry_bound(self) -> None:
        self.assertEqual(
            summarize_assignment_rationale(()).status,
            AssignmentRationaleSummaryStatus.NO_RECORDED_ASSIGNMENT_RATIONALE,
        )
        records = tuple(
            AssignmentRationale(
                required_capability=f"capability.{index}",
                selected_material_profile_digest=_DIGEST_A,
                exercised_capability=f"capability.{index}",
            )
            for index in range(4)
        )
        self.assertEqual(len(summarize_assignment_rationale(records).entries), 3)


if __name__ == "__main__":
    unittest.main()
