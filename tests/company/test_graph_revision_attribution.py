from __future__ import annotations

import unittest

from dynamic_firm.company import (
    GraphMutationPolicy,
    GraphRevision,
    GraphRevisionImpactDisposition,
    GraphRevisionImpactEvidence,
    GraphRunRecord,
    assess_graph_revision_impact,
)
from dynamic_firm.kernel.models import (
    GraphPatchExpectedImpact,
    GraphPatchObservedOutcome,
)


def _baseline() -> GraphRunRecord:
    return GraphRunRecord(
        job_id="job-unchanged-baseline",
        work_order_digest="a" * 64,
        initial_graph_digest="b" * 64,
    )


def _candidate(*, extra_revision: bool = False) -> GraphRunRecord:
    record = GraphRunRecord(
        job_id="job-single-revision-candidate",
        work_order_digest="a" * 64,
        initial_graph_digest="b" * 64,
    ).append(
        GraphRevision(
            sequence=1,
            previous_graph_digest="b" * 64,
            next_graph_digest="c" * 64,
            operation="INSERT",
            proposer="kernel-replanner",
            trigger_evidence=("trigger-task:analysis",),
            budget_delta=0.1,
            approval_policy=GraphMutationPolicy.BOUNDED_AUTO,
            expected_impact=GraphPatchExpectedImpact.CAPABILITY_COVERAGE,
            observed_terminal_outcome=GraphPatchObservedOutcome.JOB_SUCCEEDED,
        )
    )
    if not extra_revision:
        return record
    return record.append(
        GraphRevision(
            sequence=2,
            previous_graph_digest="c" * 64,
            next_graph_digest="d" * 64,
            operation="RETRY",
            proposer="kernel-replanner",
            trigger_evidence=("trigger-task:final",),
            budget_delta=0.0,
            approval_policy=GraphMutationPolicy.BOUNDED_AUTO,
            observed_terminal_outcome=GraphPatchObservedOutcome.JOB_SUCCEEDED,
        )
    )


class GraphRevisionAttributionTests(unittest.TestCase):
    def test_matched_single_revision_pair_projects_actual_impact_without_authority(self) -> None:
        evidence = GraphRevisionImpactEvidence(
            context_fingerprint="d" * 64,
            evaluator_digest="e" * 64,
            baseline_run=_baseline(),
            candidate_run=_candidate(),
            baseline_terminal_outcome=GraphPatchObservedOutcome.JOB_FAILED,
            baseline_quality_score=0.4,
            candidate_quality_score=0.9,
            baseline_model_calls=6,
            candidate_model_calls=5,
        )

        assessment = assess_graph_revision_impact(evidence)

        self.assertEqual(assessment.disposition, GraphRevisionImpactDisposition.IMPROVED)
        self.assertEqual(assessment.quality_delta, 0.5)
        self.assertEqual(assessment.model_call_delta, 1)
        self.assertEqual(assessment.candidate_revision_sequence, 1)
        self.assertEqual(assessment.expected_impact, "CAPABILITY_COVERAGE")
        self.assertNotIn("objective", str(evidence.canonical_payload()))

    def test_rejects_non_matched_or_confounded_revision_pairs(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            GraphRevisionImpactEvidence(
                context_fingerprint="d" * 64,
                evaluator_digest="e" * 64,
                baseline_run=_baseline(),
                candidate_run=_candidate(extra_revision=True),
                baseline_terminal_outcome=GraphPatchObservedOutcome.JOB_SUCCEEDED,
                baseline_quality_score=0.7,
                candidate_quality_score=0.8,
                baseline_model_calls=4,
                candidate_model_calls=4,
            )
        mismatched = GraphRunRecord(
            job_id="job-other-work-order",
            work_order_digest="f" * 64,
            initial_graph_digest="b" * 64,
        )
        with self.assertRaisesRegex(ValueError, "same Work Order"):
            GraphRevisionImpactEvidence(
                context_fingerprint="d" * 64,
                evaluator_digest="e" * 64,
                baseline_run=mismatched,
                candidate_run=_candidate(),
                baseline_terminal_outcome=GraphPatchObservedOutcome.JOB_SUCCEEDED,
                baseline_quality_score=0.7,
                candidate_quality_score=0.8,
                baseline_model_calls=4,
                candidate_model_calls=4,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
