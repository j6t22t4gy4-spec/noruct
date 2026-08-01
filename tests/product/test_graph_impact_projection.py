from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.graph_revision_attribution import (
    GraphRevisionImpactAssessment,
    GraphRevisionImpactDisposition,
)
from dynamic_firm.kernel.models import GraphPatchObservedOutcome
from dynamic_firm.product.graph_impact_projection import (
    GraphImpactOutcomeStatus,
    GraphImpactTruthState,
    project_graph_impact,
)


_INITIAL = "a" * 64
_FINAL = "b" * 64
_OUTCOME = "c" * 64


class GraphImpactProjectionTests(unittest.TestCase):
    def test_absent_outcome_is_structural_only_and_content_free(self) -> None:
        projection = project_graph_impact(
            initial_graph_digest=_INITIAL,
            final_graph_digest=_FINAL,
            accepted_revision_sequence=1,
            accepted_operation="ADD_REPLICA",
            accepted_lease_delta=2,
            organization_selection_evidence_id="org-evidence-1",
            alternative_evidence_id="alternative-evidence-1",
        )

        self.assertEqual(projection.truth_state, GraphImpactTruthState.STRUCTURAL_ONLY)
        self.assertEqual(projection.outcome_status, GraphImpactOutcomeStatus.OUTCOME_NOT_ESTABLISHED)
        self.assertIsNone(projection.outcome_evidence_id)
        self.assertIsNone(projection.quality_delta)
        self.assertIsNone(projection.disposition)
        self.assertNotIn("prompt", projection.canonical_payload())
        self.assertNotIn("transcript", projection.canonical_payload())

    def test_exact_assessment_populates_matched_outcome_fields(self) -> None:
        assessment = GraphRevisionImpactAssessment(
            evidence_digest=_OUTCOME,
            context_fingerprint="d" * 64,
            candidate_revision_sequence=1,
            expected_impact="LOWER_COST",
            baseline_terminal_outcome=GraphPatchObservedOutcome.JOB_SUCCEEDED,
            candidate_terminal_outcome=GraphPatchObservedOutcome.JOB_SUCCEEDED,
            quality_delta=0.25,
            model_call_delta=-1,
            disposition=GraphRevisionImpactDisposition.IMPROVED,
        )

        projection = project_graph_impact(
            initial_graph_digest=_INITIAL,
            final_graph_digest=_FINAL,
            accepted_revision_sequence=1,
            accepted_operation="ADD_REPLICA",
            accepted_lease_delta=2,
            organization_selection_evidence_id="org-evidence-1",
            alternative_evidence_id="alternative-evidence-1",
            impact_assessment=assessment,
        )

        self.assertEqual(projection.truth_state, GraphImpactTruthState.MATCHED_OUTCOME)
        self.assertEqual(projection.outcome_status, GraphImpactOutcomeStatus.MATCHED_OUTCOME)
        self.assertEqual(projection.outcome_evidence_id, _OUTCOME)
        self.assertEqual(projection.quality_delta, 0.25)
        self.assertEqual(projection.disposition, GraphRevisionImpactDisposition.IMPROVED)


if __name__ == "__main__":
    unittest.main()
