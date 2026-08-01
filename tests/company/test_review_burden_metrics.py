import unittest
from dataclasses import asdict
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.models import EvidenceSource, organization_episode_from_dict
from dynamic_firm.company.review_burden_metrics import (
    COMPREHENDED,
    DISCOVERED,
    NOT_RECORDED,
    NOT_RUN,
    ReviewBurdenMetrics,
    aggregate_review_burden,
)
from dynamic_firm.company.workflow_models import OrganizationEpisode


def episode(**metrics: object) -> OrganizationEpisode:
    return OrganizationEpisode.create(
        job_id="review-burden-job",
        source=EvidenceSource.OFFLINE_FIXTURE,
        task_family="review-burden",
        context_fingerprint="context",
        execution_profile="READ_ONLY",
        planning_mode="SOLO",
        plan_template=(),
        success=True,
        quality_score=0.9,
        baseline_quality_score=None,
        model_calls=1,
        baseline_model_calls=None,
        employee_count=1,
        maximum_parallelism=1,
        writer_count=1,
        approvals_requested=0,
        approvals_granted=0,
        preapproval_mutations=0,
        validation_attempts=(True,),
        ledger_digest="a" * 64,
        **metrics,
    )


class ReviewBurdenMetricsTest(unittest.TestCase):
    def test_missing_defaults_are_explicit_and_not_zero(self) -> None:
        decoded = organization_episode_from_dict(asdict(episode()))

        self.assertEqual(decoded.review_wait_ms, NOT_RECORDED)
        self.assertEqual(decoded.reopened_evidence_count, NOT_RECORDED)
        self.assertEqual(decoded.unverified_item_discovery, NOT_RUN)
        report = aggregate_review_burden((decoded,))
        self.assertEqual(report.recorded_coverage["review_wait_ms"], 0)
        self.assertIsNone(report.numeric_metrics["review_wait_ms"].mean)
        self.assertEqual(report.quality.recorded_count, 1)
        self.assertEqual(report.quality_mean, 0.9)

    def test_zero_and_nonzero_numeric_facts_round_trip(self) -> None:
        original = episode(
            review_wait_ms=0,
            reopened_evidence_count=2,
            unused_subartifact_rate=0.0,
            rework_count=0,
            approval_friction_count=3,
            unverified_item_discovery=DISCOVERED,
            summary_comprehension_status=COMPREHENDED,
        )
        decoded = organization_episode_from_dict(asdict(original))

        self.assertEqual(decoded.review_wait_ms, 0)
        self.assertEqual(decoded.reopened_evidence_count, 2)
        self.assertEqual(decoded.unused_subartifact_rate, 0.0)
        self.assertEqual(decoded.rework_count, 0)
        self.assertEqual(decoded.approval_friction_count, 3)
        self.assertEqual(decoded.review_burden_metrics, original.review_burden_metrics)

    def test_aggregate_keeps_coverage_states_and_quality_separate(self) -> None:
        first = episode(
            review_wait_ms=100,
            reopened_evidence_count=0,
            unused_subartifact_rate=0.25,
            rework_count=1,
            approval_friction_count=0,
            unverified_item_discovery=DISCOVERED,
            summary_comprehension_status=COMPREHENDED,
        )
        second = episode()
        report = aggregate_review_burden((first, second))

        self.assertEqual(report.episode_count, 2)
        self.assertEqual(report.recorded_coverage["review_wait_ms"], 1)
        self.assertEqual(report.numeric_metrics["review_wait_ms"].mean, 100.0)
        self.assertEqual(report.numeric_metrics["rework_count"].total, 1)
        self.assertEqual(report.state_counts["unverified_item_discovery"][DISCOVERED], 1)
        self.assertEqual(report.state_counts["unverified_item_discovery"][NOT_RUN], 1)
        self.assertEqual(report.quality.recorded_count, 2)
        self.assertEqual(report.quality.mean_quality_score, 0.9)

    def test_metric_value_object_is_immutable(self) -> None:
        metrics = ReviewBurdenMetrics(review_wait_ms=0)
        with self.assertRaises(AttributeError):
            metrics.review_wait_ms = 1  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
