import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.company.organization_fit import (
    OrganizationFitLevel,
    OrganizationFitProfile,
)
from dynamic_firm.company.verification_budget_policy import (
    VerificationBudgetPolicy,
    VerificationRequirement,
    VerificationTier,
    policy_for,
)


class VerificationBudgetPolicyTests(unittest.TestCase):
    def test_low_risk_highly_verifiable_work_is_deterministic_only(self) -> None:
        profile = OrganizationFitProfile(
            risk_irreversibility=OrganizationFitLevel.LOW,
            verifiability=OrganizationFitLevel.HIGH,
            error_correlation=OrganizationFitLevel.LOW,
        )

        policy = policy_for(profile)

        self.assertEqual(policy.tier, VerificationTier.DETERMINISTIC)
        self.assertEqual(policy.requirement, VerificationRequirement.DETERMINISTIC_ONLY)
        self.assertEqual(policy.ceiling.max_verification_rounds, 1)
        self.assertEqual(policy.ceiling.max_additional_independent_requirements, 0)

    def test_high_risk_fails_safe_to_independent_requirement(self) -> None:
        profile = OrganizationFitProfile(
            risk_irreversibility=OrganizationFitLevel.HIGH,
            verifiability=OrganizationFitLevel.HIGH,
            error_correlation=OrganizationFitLevel.LOW,
        )

        policy = VerificationBudgetPolicy.from_profile(profile)

        self.assertEqual(policy.tier, VerificationTier.INDEPENDENT)
        self.assertEqual(
            policy.requirement,
            VerificationRequirement.ADDITIONAL_INDEPENDENT_EVIDENCE_OR_REVIEWER,
        )
        self.assertEqual(policy.ceiling.max_additional_independent_requirements, 1)
        self.assertEqual(policy.ceiling.authority_scope, "ADVISORY_ONLY")
        self.assertEqual(policy.ceiling.work_order_cap_effect, "UNCHANGED")

    def test_unknown_relevant_fact_fails_safe(self) -> None:
        profile = OrganizationFitProfile(
            risk_irreversibility=OrganizationFitLevel.UNKNOWN,
            verifiability=OrganizationFitLevel.HIGH,
            error_correlation=OrganizationFitLevel.LOW,
        )

        policy = policy_for(profile)

        self.assertEqual(policy.tier, VerificationTier.INDEPENDENT)
        self.assertEqual(
            policy.requirement,
            VerificationRequirement.ADDITIONAL_INDEPENDENT_EVIDENCE_OR_REVIEWER,
        )

    def test_high_correlation_and_low_verifiability_fail_safe(self) -> None:
        for dimension, value in (
            ("verifiability", OrganizationFitLevel.LOW),
            ("error_correlation", OrganizationFitLevel.HIGH),
        ):
            values = {
                "risk_irreversibility": OrganizationFitLevel.LOW,
                "verifiability": OrganizationFitLevel.HIGH,
                "error_correlation": OrganizationFitLevel.LOW,
            }
            values[dimension] = value
            policy = policy_for(OrganizationFitProfile(**values))
            self.assertEqual(policy.tier, VerificationTier.INDEPENDENT)

    def test_policy_and_ceiling_are_immutable(self) -> None:
        policy = policy_for(OrganizationFitProfile())

        with self.assertRaises(FrozenInstanceError):
            policy.tier = VerificationTier.DETERMINISTIC  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            policy.ceiling.max_verification_rounds = 99  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
