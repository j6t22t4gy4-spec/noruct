import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.assignment_rationale import (
    AssignmentAlternative,
    AssignmentExclusionReason,
    AssignmentRationale,
)
from dynamic_firm.runtime.models import EmployeeCapabilityProfile


class AssignmentRationaleTests(unittest.TestCase):
    def profile(self) -> EmployeeCapabilityProfile:
        return EmployeeCapabilityProfile.create(
            employee_id="opaque-employee-id",
            roster_revision=1,
            model_profile="bounded-model",
            capability_ids=("required-capability", "exercised-capability"),
            skill_revision_refs=(),
            tool_names=(),
            tool_grant_digest="a" * 64,
            permission_effects=(),
            permission_digest="b" * 64,
            knowledge_scopes=(),
            memory_namespace="opaque-memory",
            memory_revision_refs=(),
            session_policy="bounded",
            validator_ids=(),
            evaluation_revision="evaluation-1",
        )

    def test_exercised_capability_and_selected_material_digest_are_recorded(self) -> None:
        rationale = AssignmentRationale.from_profile(
            required_capability="required-capability",
            selected_profile=self.profile(),
            exercised_capability="exercised-capability",
        )

        self.assertEqual(rationale.selected_material_profile_digest, self.profile().material_digest)
        self.assertEqual(rationale.exercised_capability, "exercised-capability")
        self.assertEqual(rationale.alternatives, ())
        self.assertNotIn("employee_id", rationale.payload())

    def test_unexercised_selected_difference_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AssignmentRationale.from_profile(
                required_capability="required-capability",
                selected_profile=self.profile(),
                exercised_capability="unexercised-capability",
            )

    def test_only_explicitly_compared_alternatives_are_bounded(self) -> None:
        alternative = AssignmentAlternative.compared_candidate(
            alternative_id="opaque-alternative-id",
            material_profile_digest="c" * 64,
            exclusion_reason=AssignmentExclusionReason.CAPABILITY_MISMATCH,
        )
        rationale = AssignmentRationale.from_profile(
            required_capability="required-capability",
            selected_profile=self.profile(),
            exercised_capability="exercised-capability",
            alternatives=(alternative,),
        )
        self.assertTrue(rationale.alternatives[0].compared)

        with self.assertRaises(ValueError):
            AssignmentAlternative(
                alternative_id="opaque-unexamined-id",
                material_profile_digest="d" * 64,
                exclusion_reason=AssignmentExclusionReason.UNKNOWN,
                compared=False,
            )

    def test_role_and_name_fields_are_not_part_of_the_record(self) -> None:
        with self.assertRaises(TypeError):
            AssignmentRationale(
                required_capability="required-capability",
                selected_material_profile_digest="e" * 64,
                exercised_capability="exercised-capability",
                role="reviewer",  # type: ignore[call-arg]
            )


if __name__ == "__main__":
    unittest.main()
