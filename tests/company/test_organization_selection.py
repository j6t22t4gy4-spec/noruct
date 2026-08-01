import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.company.organization_fit import OrganizationFitLevel, OrganizationFitProfile
from dynamic_firm.company.organization_fit_evidence import (
    OrganizationFitEvidence,
    OrganizationFitEvidenceReference,
)
from dynamic_firm.company.organization_selection import (
    OrganizationSelection,
    OrganizationSelectionReason,
    select_organization,
)


_DIMENSIONS = (
    "decomposability",
    "dependency_coupling",
    "context_coupling",
    "information_dispersion",
    "verifiability",
    "risk_irreversibility",
    "error_correlation",
    "latency_sensitivity",
)


def _profile(**overrides: OrganizationFitLevel) -> OrganizationFitProfile:
    values = {dimension: OrganizationFitLevel.LOW for dimension in _DIMENSIONS}
    values.update(overrides)
    return OrganizationFitProfile(**values)


def _reference(output_field: str, source_type: str, source_field: str):
    return OrganizationFitEvidenceReference(
        output_field=output_field,
        source_type=source_type,
        source_field=source_field,
    )


class OrganizationSelectionTests(unittest.TestCase):
    def test_selection_table(self):
        exact_evidence = {
            "independent_work": (
                _profile(dependency_coupling=OrganizationFitLevel.HIGH),
                (
                    _reference(
                        "dependency_coupling",
                        "PortfolioSchedulingEnvelope",
                        "dependency_work_order_ids",
                    ),
                    _reference(
                        "team_candidate",
                        "PortfolioSchedulingEnvelope",
                        "dependency_work_order_ids",
                    ),
                ),
                OrganizationSelectionReason.INDEPENDENT_WORK_EVIDENCE,
            ),
            "information_dispersion": (
                _profile(information_dispersion=OrganizationFitLevel.HIGH),
                (
                    _reference(
                        "information_dispersion",
                        "PortfolioSchedulingEnvelope",
                        "required_capabilities",
                    ),
                    _reference(
                        "team_candidate",
                        "PortfolioSchedulingEnvelope",
                        "required_capabilities",
                    ),
                ),
                OrganizationSelectionReason.INFORMATION_DISPERSION_EVIDENCE,
            ),
            "independent_verification": (
                _profile(verifiability=OrganizationFitLevel.HIGH),
                (
                    _reference(
                        "verifiability",
                        "WorkOrder.operating_decision",
                        "requires_independent_review",
                    ),
                    _reference(
                        "team_candidate",
                        "WorkOrder.operating_decision",
                        "requires_independent_review",
                    ),
                ),
                OrganizationSelectionReason.INDEPENDENT_VERIFICATION_EVIDENCE,
            ),
        }
        cases = [
            (
                "missing core profile evidence",
                OrganizationFitProfile(),
                (),
                OrganizationSelection.DIRECT_OR_STRONG_SOLO,
                OrganizationSelectionReason.CORE_EVIDENCE_MISSING,
            ),
            (
                "complete profile without independent value evidence",
                _profile(),
                (),
                OrganizationSelection.DIRECT_OR_STRONG_SOLO,
                OrganizationSelectionReason.NO_INDEPENDENT_VALUE_EVIDENCE,
            ),
            *(
                (
                    name,
                    profile,
                    references,
                    OrganizationSelection.TEAM_EXPERIMENT_CANDIDATE,
                    reason,
                )
                for name, (profile, references, reason) in exact_evidence.items()
            ),
            (
                "role label, headcount, and preference are not typed evidence",
                _profile(),
                (
                    _reference("headcount", "Roster", "people"),
                    _reference("role", "Roster", "role_name"),
                    _reference("preference", "Manager", "requested_team"),
                    _reference("team_candidate", "WorkOrder", "prose"),
                ),
                OrganizationSelection.DIRECT_OR_STRONG_SOLO,
                OrganizationSelectionReason.NO_INDEPENDENT_VALUE_EVIDENCE,
            ),
        ]

        for name, profile, references, expected_selection, expected_reason in cases:
            with self.subTest(name=name):
                result = select_organization(
                    OrganizationFitEvidence(
                        profile=profile,
                        evidence_references=references,
                        team_candidate=True,
                    )
                )
                self.assertEqual(result.selection, expected_selection)
                self.assertEqual(result.reason_codes, (expected_reason,))


if __name__ == "__main__":
    unittest.main()
