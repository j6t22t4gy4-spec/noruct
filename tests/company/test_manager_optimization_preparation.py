import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.company.manager_optimization_preparation import (
    FIXED_FAILURE_STAGES,
    FailureStageAttribution,
    ManagerFailureStage,
    ManagerOptimizationDecision,
    ManagerOptimizationPreparationError,
    RoleTierRoute,
    attribute_failure_stage,
    build_bounded_candidate,
    build_one_stage_ablation_plan,
    guard_matched_reports,
    prepare_manager_optimization,
    strong_solo_fallback,
)
from dynamic_firm.evaluation.organization_comparison_v9 import (
    V9_REPORT_SCHEMA,
    CostTime,
    CompleteSafetyFailure,
    LowerDecileQuality,
    NegativeTransfer,
    OrganizationComparisonV9ArmReport,
    OrganizationComparisonV9Report,
    ReviewRework,
)


def _arm(name, *, quality, complete, rework, slots=4, cost=None):
    return OrganizationComparisonV9ArmReport(
        arm=name,
        lower_decile_quality=LowerDecileQuality(slots, quality),
        complete_safety_failure=CompleteSafetyFailure(slots, complete, 0),
        cost_time=cost or CostTime(10, 2, 12, 100, 20, 120),
        review_rework=ReviewRework(rework, 2, 0.2, rework, 1, "NONE", "PASS"),
        negative_transfer=NegativeTransfer(slots, 0, 0),
    )


def _report(manager_quality, manager_complete, manager_rework, *, cost=None):
    arms = tuple(
        _arm(
            name,
            quality=0.5 if name == "strong-solo" else manager_quality,
            complete=0 if name == "strong-solo" else manager_complete,
            rework=manager_rework if name == "manager-led-graph" else 4,
            cost=cost,
        )
        for name in (
            "strong-solo",
            "homogeneous-replica",
            "heterogeneous-graph",
            "manager-led-graph",
        )
    )
    return OrganizationComparisonV9Report(
        V9_REPORT_SCHEMA, "release-workload", "matched-manifest", arms
    )


class ManagerOptimizationPreparationTests(unittest.TestCase):
    def test_provider_free_preparation_is_fixed_and_terminal(self):
        attribution = attribute_failure_stage(
            workload_id="w1",
            stage="handoff",
            evidence_id="e1",
            reason_code="handoff_loss",
            complete_failure=True,
        )
        prepared = prepare_manager_optimization(attribution)

        self.assertIsInstance(attribution, FailureStageAttribution)
        self.assertEqual(tuple(item.stage for item in prepared.ablation_plan.ablations), FIXED_FAILURE_STAGES)
        self.assertEqual(prepared.candidate.additional_model_calls, 0)
        self.assertTrue(prepared.solo_fallback.terminal)
        self.assertFalse(prepared.solo_fallback.retry_allowed)
        self.assertFalse(prepared.solo_fallback.loop_allowed)

    def test_candidate_rejects_forbidden_changes(self):
        candidate = build_bounded_candidate(ManagerFailureStage.PLANNING)
        with self.assertRaises(ManagerOptimizationPreparationError):
            type(candidate)(
                candidate_id=candidate.candidate_id,
                stage=candidate.stage,
                ablation=candidate.ablation,
                routing=candidate.routing,
                prompt_changed=True,
            )
        with self.assertRaises(ManagerOptimizationPreparationError):
            RoleTierRoute("manager", "")

    def test_one_stage_plan_and_solo_fallback(self):
        plan = build_one_stage_ablation_plan()
        self.assertEqual(len(plan.ablations), 6)
        self.assertEqual(str(strong_solo_fallback().decision), "STRONG_SOLO")

    def test_guard_requires_two_matches_and_then_allows_independent_review(self):
        before = _report(0.5, 0, 4)
        after = _report(0.6, 0, 3)
        one = guard_matched_reports(before, after, matching_release_workloads=1)
        self.assertEqual(one.decision, ManagerOptimizationDecision.SOLO_REQUIRED)

        two = guard_matched_reports(before, after, matching_release_workloads=2)
        self.assertEqual(two.decision, ManagerOptimizationDecision.REVIEW_REQUIRED)
        self.assertTrue(two.lower_tail_improved)
        self.assertFalse(two.promotion_allowed)

    def test_guard_keeps_observe_only_on_failure_or_unmatched_cost(self):
        before = _report(0.5, 0, 4)
        after = _report(0.4, 1, 5, cost=CostTime(11, 2, 13, 100, 20, 120))
        result = guard_matched_reports(before, after, matching_release_workloads=2)
        self.assertEqual(result.decision, ManagerOptimizationDecision.OBSERVE_ONLY)
        self.assertIn("complete_failure_rate_worsened", result.reasons)
        self.assertIn("matched_model_review_cost_missing", result.reasons)


if __name__ == "__main__":
    unittest.main()
