import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.company.organization_plan import (
    FrozenOrganizationPlan,
    OrganizationPlanBindingError,
    OrganizationPlanRoute,
    SourceAuthorityBinding,
)


def make_plan() -> FrozenOrganizationPlan:
    return FrozenOrganizationPlan.from_routes(
        {
            OrganizationPlanRoute.TASK_DEPENDENCY: SourceAuthorityBinding("work-order-1", "wo-v1"),
            OrganizationPlanRoute.ASSIGNMENT: SourceAuthorityBinding("roster-1", "roster-v1"),
            OrganizationPlanRoute.INFORMATION_EVIDENCE: SourceAuthorityBinding("evidence-1", "evidence-v1"),
            OrganizationPlanRoute.ARTIFACT_COMMUNICATION: SourceAuthorityBinding("artifact-route-1", "artifact-v1"),
            OrganizationPlanRoute.DECISION_EFFECT: SourceAuthorityBinding("policy-1", "policy-v1"),
            OrganizationPlanRoute.VERIFICATION: SourceAuthorityBinding("validator-1", "validator-v1"),
            OrganizationPlanRoute.LEARNING_CANDIDATE: SourceAuthorityBinding("episode-1", "episode-v1"),
        }
    )


class FrozenOrganizationPlanTests(unittest.TestCase):
    def test_valid_projection_has_exactly_seven_routes_and_validates(self) -> None:
        plan = make_plan()
        observed = {
            binding.authority_id: binding.authority_digest
            for binding in plan.source_bindings
        }

        self.assertEqual(len(plan.routes), 7)
        self.assertEqual(tuple(route for route, _ in plan.routes), tuple(OrganizationPlanRoute))
        self.assertTrue(plan.validate_source_bindings(observed))

    def test_stale_digest_fails_closed(self) -> None:
        plan = make_plan()
        observed = {
            binding.authority_id: binding.authority_digest
            for binding in plan.source_bindings
        }
        observed["roster-1"] = "roster-v2"

        with self.assertRaises(OrganizationPlanBindingError):
            plan.validate(observed)

    def test_missing_authority_reference_fails_closed(self) -> None:
        plan = make_plan()
        observed = {
            binding.authority_id: binding.authority_digest
            for binding in plan.source_bindings
            if binding.authority_id != "validator-1"
        }

        with self.assertRaises(OrganizationPlanBindingError):
            plan.validate(observed)

    def test_plan_and_bindings_are_immutable(self) -> None:
        plan = make_plan()

        with self.assertRaises(FrozenInstanceError):
            plan.assignment = SourceAuthorityBinding("other", "digest")
        with self.assertRaises(FrozenInstanceError):
            plan.assignment.authority_id = "other"


if __name__ == "__main__":
    unittest.main()
