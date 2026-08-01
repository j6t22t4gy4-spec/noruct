import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.frontdoor import (  # noqa: E402
    AuthoritySnapshotIdentity,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
)
from dynamic_firm.company.operating import RequestedEffect  # noqa: E402
from dynamic_firm.company.organization_fit import OrganizationFitLevel  # noqa: E402
from dynamic_firm.company.organization_fit_evidence import (  # noqa: E402
    extract_organization_fit,
)
from dynamic_firm.company.work_order_portfolio_models import (  # noqa: E402
    PortfolioSchedulingEnvelope,
)


class OrganizationFitEvidenceTests(unittest.TestCase):
    def _work_order(
        self,
        objective: str,
        *,
        acceptance_criteria: tuple[str, ...] = (),
        requested_effect: RequestedEffect = RequestedEffect.READ,
        requires_independent_review: bool = False,
    ):
        from dynamic_firm.company.operating import (  # noqa: PLC0415
            CompanyOperatingDecision,
            CompanyWorkMode,
            InitialCoordinationPolicy,
            OperatingReason,
        )

        return normalize_work_order(
            objective,
            work_order_id="wo-evidence",
            authority_snapshot=AuthoritySnapshotIdentity(
                company_id="company",
                company_revision=1,
                roster_revision=1,
                playbook_revision=1,
                action_policy_digest="policy",
            ),
            budget_snapshot=WorkOrderBudgetSnapshot(
                max_model_calls=4,
                max_tool_calls=4,
                max_cost_usd=1.0,
                max_wall_time_ms=1000,
            ),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            acceptance_criteria=acceptance_criteria,
            operating_decision=CompanyOperatingDecision(
                work_mode=CompanyWorkMode.SOLO_JOB,
                coordination_policy=InitialCoordinationPolicy.SOLO_FIRST,
                requested_effect=requested_effect,
                reason=OperatingReason.DIRECT_USER_MESSAGE,
                requires_independent_review=requires_independent_review,
            ),
        )

    def _envelope(self, **kwargs):
        return PortfolioSchedulingEnvelope(work_order_id="wo-evidence", **kwargs)

    def test_strong_solo_uses_typed_facts_and_leaves_unproven_axes_unknown(self) -> None:
        result = extract_organization_fit(
            self._work_order("팀으로 해줘: 읽기 작업", acceptance_criteria=("done",)),
            self._envelope(required_capabilities=("analysis",)),
        )

        self.assertFalse(result.team_candidate)
        self.assertEqual(result.profile.verifiability, OrganizationFitLevel.HIGH)
        self.assertEqual(result.profile.risk_irreversibility, OrganizationFitLevel.LOW)
        self.assertEqual(result.profile.dependency_coupling, OrganizationFitLevel.UNKNOWN)
        self.assertEqual(result.profile.information_dispersion, OrganizationFitLevel.UNKNOWN)
        self.assertEqual(result.profile.context_coupling, OrganizationFitLevel.UNKNOWN)
        self.assertEqual(result.profile.decomposability, OrganizationFitLevel.UNKNOWN)
        self.assertEqual(result.profile.error_correlation, OrganizationFitLevel.UNKNOWN)
        self.assertEqual(result.profile.latency_sensitivity, OrganizationFitLevel.UNKNOWN)
        self.assertEqual(
            {reference.input_path for reference in result.evidence_references},
            {"WorkOrder.acceptance_criteria", "WorkOrder.operating_decision.requested_effect"},
        )

    def test_independent_typed_evidence_marks_team_candidate(self) -> None:
        result = extract_organization_fit(
            self._work_order(
                "한 작업",
                acceptance_criteria=("reviewable",),
                requires_independent_review=True,
            ),
            self._envelope(required_capabilities=("analysis", "verification")),
        )

        self.assertTrue(result.team_candidate)
        self.assertEqual(result.profile.information_dispersion, OrganizationFitLevel.HIGH)
        self.assertEqual(result.profile.verifiability, OrganizationFitLevel.HIGH)
        self.assertEqual(
            {reference.input_path for reference in result.evidence_for("team_candidate")},
            {"PortfolioSchedulingEnvelope.required_capabilities"},
        )
        for field in (
            "information_dispersion",
            "verifiability",
            "team_candidate",
        ):
            self.assertTrue(result.evidence_for(field))

    def test_ambiguous_receipt_is_unknown_and_team_phrase_is_ignored(self) -> None:
        plain = extract_organization_fit(
            self._work_order("검토해줘"),
            self._envelope(),
        )
        with_team_phrase = extract_organization_fit(
            self._work_order("팀으로 해줘: 검토해줘"),
            self._envelope(),
        )

        self.assertEqual(plain, with_team_phrase)
        self.assertFalse(plain.team_candidate)
        for field in (
            "decomposability",
            "dependency_coupling",
            "context_coupling",
            "information_dispersion",
            "verifiability",
            "error_correlation",
            "latency_sensitivity",
        ):
            self.assertEqual(getattr(plain.profile, field), OrganizationFitLevel.UNKNOWN)
        self.assertEqual(plain.evidence_for("team_candidate"), ())


if __name__ == "__main__":
    unittest.main()
