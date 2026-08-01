import unittest
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.product.execution_contribution import (
    ApprovalDecision,
    ApprovalFact,
    ArtifactIdentity,
    ContributionState,
    DeliveryFact,
    EffectFact,
    EffectIntentStatus,
    EffectOutcomeStatus,
    FinalOwnerFact,
    GraphProposalFact,
    IntegrationStatus,
    ProjectionIssueCode,
    ProjectionStatus,
    ProposalFact,
    TaskOwnershipFact,
    project_execution_contribution,
)


class ExecutionContributionContractTests(unittest.TestCase):
    def setUp(self):
        self.code_identity = ArtifactIdentity("CODE", "change-17", "digest-code")
        self.non_code_identity = ArtifactIdentity("DOCUMENT", "artifact-9", "digest-document")
        self.effect_identity = ArtifactIdentity("EXTERNAL_EFFECT", "effect-4", "digest-effect")

    def test_code_fixture_preserves_authored_and_executed_boundaries(self):
        result = project_execution_contribution(
            delivery=DeliveryFact(self.code_identity, "CODE"),
            task=TaskOwnershipFact("task-code", self.code_identity, owner_kind="AI"),
            effect=EffectFact(
                "receipt-code",
                self.code_identity,
                EffectIntentStatus.RECORDED,
                EffectOutcomeStatus.SUCCEEDED,
            ),
        )

        self.assertEqual(result.status, ProjectionStatus.COMPLETE)
        self.assertEqual(
            [entry.state for entry in result.entries],
            [ContributionState.AUTHORED, ContributionState.EXECUTED],
        )
        effect_entry = result.entries[-1]
        self.assertEqual(effect_entry.effect_intent_status, EffectIntentStatus.RECORDED)
        self.assertEqual(effect_entry.effect_outcome_status, EffectOutcomeStatus.SUCCEEDED)
        self.assertNotIn("line", result.to_dict())

    def test_non_code_fixture_separates_proposal_decision_and_integration(self):
        result = project_execution_contribution(
            delivery=DeliveryFact(self.non_code_identity, "NON_CODE"),
            task=TaskOwnershipFact("task-document", self.non_code_identity),
            proposal=ProposalFact("proposal-2", self.non_code_identity),
            approval=ApprovalFact(
                "approval-2",
                "proposal-2",
                self.non_code_identity,
                ApprovalDecision.APPROVED,
            ),
            final_owner=FinalOwnerFact(
                "owner-final",
                self.non_code_identity,
                IntegrationStatus.INTEGRATED,
                owner_kind="HUMAN",
            ),
        )

        self.assertEqual(
            [entry.state for entry in result.entries],
            [
                ContributionState.AUTHORED,
                ContributionState.PROPOSED,
                ContributionState.SELECTED,
                ContributionState.INTEGRATED,
            ],
        )
        self.assertNotEqual(result.entries[1].state, result.entries[2].state)
        self.assertEqual(result.entries[2].evidence_kind, "APPROVAL_DECISION")

    def test_external_effect_fixture_keeps_intent_distinct_from_success(self):
        result = project_execution_contribution(
            delivery=DeliveryFact(self.effect_identity, "EXTERNAL_EFFECT"),
            task=TaskOwnershipFact("task-effect", self.effect_identity),
            effect=EffectFact(
                "receipt-effect",
                self.effect_identity,
                EffectIntentStatus.STARTED,
                EffectOutcomeStatus.INDETERMINATE,
            ),
        )

        effect_entry = result.entries[-1]
        self.assertEqual(effect_entry.state, ContributionState.EXECUTED)
        self.assertEqual(effect_entry.effect_intent_status, EffectIntentStatus.STARTED)
        self.assertEqual(effect_entry.effect_outcome_status, EffectOutcomeStatus.INDETERMINATE)
        self.assertNotEqual(effect_entry.effect_intent_status, effect_entry.effect_outcome_status)

    def test_graph_proposal_is_not_selected_without_approval(self):
        result = project_execution_contribution(
            delivery=DeliveryFact(self.non_code_identity, "NON_CODE"),
            proposal=GraphProposalFact("proposal-pending", self.non_code_identity),
        )

        self.assertEqual(result.status, ProjectionStatus.COMPLETE)
        self.assertEqual([entry.state for entry in result.entries], [ContributionState.PROPOSED])

    def test_identity_conflict_returns_typed_empty_result(self):
        result = project_execution_contribution(
            delivery=DeliveryFact(self.code_identity, "CODE"),
            task=TaskOwnershipFact("task-conflict", self.non_code_identity),
        )

        self.assertEqual(result.status, ProjectionStatus.IDENTITY_CONFLICT)
        self.assertTrue(result.is_conflict)
        self.assertEqual(result.entries, ())
        self.assertEqual(
            result.issue.code,
            ProjectionIssueCode.ARTIFACT_IDENTITY_CONFLICT,
        )

    def test_missing_identity_evidence_returns_typed_unknown_result(self):
        result = project_execution_contribution()

        self.assertEqual(result.status, ProjectionStatus.UNKNOWN)
        self.assertTrue(result.is_unknown)
        self.assertEqual(result.entries, ())
        self.assertEqual(result.issue.code, ProjectionIssueCode.NO_ARTIFACT_IDENTITY)


if __name__ == "__main__":
    unittest.main()
