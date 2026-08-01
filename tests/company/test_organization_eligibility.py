import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.company.organization_eligibility import (
    LEGACY_MIGRATION_TABLE,
    OrganizationReuseEligibility,
    explain_eligibility_transition,
    migrate_legacy_eligibility,
    negative_transfer_eligibility,
    validate_eligibility_transition,
)


class OrganizationEligibilityTests(unittest.TestCase):
    def test_enum_contains_exactly_the_four_contract_states(self):
        self.assertEqual(
            set(OrganizationReuseEligibility),
            {
                OrganizationReuseEligibility.SOLO_REQUIRED,
                OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
                OrganizationReuseEligibility.OBSERVE_ONLY,
                OrganizationReuseEligibility.AUTO_REUSE_ELIGIBLE,
            },
        )

    def test_legacy_migration_is_explicit_and_conservative(self):
        self.assertEqual(
            dict(LEGACY_MIGRATION_TABLE),
            {
                "INSUFFICIENT_EVIDENCE": OrganizationReuseEligibility.SOLO_REQUIRED,
                "SOLO_REQUIRED": OrganizationReuseEligibility.SOLO_REQUIRED,
                "TEAM_ELIGIBLE": OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
                "REPLICA_ELIGIBLE": OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
            },
        )
        for legacy_value in LEGACY_MIGRATION_TABLE:
            self.assertNotEqual(
                migrate_legacy_eligibility(legacy_value),
                OrganizationReuseEligibility.AUTO_REUSE_ELIGIBLE,
            )

    def test_negative_transfer_has_only_conservative_destinations(self):
        self.assertEqual(
            negative_transfer_eligibility(),
            OrganizationReuseEligibility.SOLO_REQUIRED,
        )
        self.assertEqual(
            negative_transfer_eligibility(observe_only=True),
            OrganizationReuseEligibility.OBSERVE_ONLY,
        )
        self.assertFalse(
            validate_eligibility_transition(
                OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
                OrganizationReuseEligibility.AUTO_REUSE_ELIGIBLE,
                matched_cohort_evidence=True,
                negative_transfer=True,
            )
        )

    def test_auto_reuse_requires_explicit_matched_cohort_evidence(self):
        self.assertFalse(
            validate_eligibility_transition(
                OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
                OrganizationReuseEligibility.AUTO_REUSE_ELIGIBLE,
            )
        )
        self.assertTrue(
            validate_eligibility_transition(
                OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
                OrganizationReuseEligibility.AUTO_REUSE_ELIGIBLE,
                matched_cohort_evidence=True,
            )
        )

    def test_operator_explanation_is_content_free(self):
        self.assertEqual(
            explain_eligibility_transition(
                OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
                OrganizationReuseEligibility.AUTO_REUSE_ELIGIBLE,
            ),
            "TRANSITION_REJECTED",
        )
        self.assertEqual(
            explain_eligibility_transition(
                OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
                OrganizationReuseEligibility.AUTO_REUSE_ELIGIBLE,
                matched_cohort_evidence=True,
            ),
            "MATCHED_COHORT_EVIDENCE",
        )


if __name__ == "__main__":
    unittest.main()
