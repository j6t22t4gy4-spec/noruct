import unittest

from src.dynamic_firm.company.blueprint_reuse_value import (
    BLUEPRINT_REUSE_VALUE_SCHEMA,
    COMPARISON_RECORDED,
    COVERAGE_IDENTITY_MISMATCH,
    COVERAGE_INSUFFICIENT_EVIDENCE,
    COVERAGE_MATCHED,
    COVERAGE_NO_REUSE,
    COVERAGE_ZERO_SAMPLE,
    BlueprintReuseObservation,
    OrganizationReuseEligibility,
    OUTCOME_NOT_ESTABLISHED,
    adapt_blueprint_reuse_eligibility,
    compare_blueprint_reuse,
)


def observation(*, reused: bool, revision: str = "bp-r7", **metrics: object) -> BlueprintReuseObservation:
    return BlueprintReuseObservation(
        blueprint_revision=revision,
        context="release-workload-a",
        authority="company-authority-1",
        budget="budget-100",
        reused=reused,
        reuse_success=True if reused else None,
        adaptation_count=1 if reused else 0,
        structural_failure=False,
        planning_call_saving=2 if reused else 0,
        cost=metrics.get("cost", 8 if reused else 10),
        quality=metrics.get("quality", 0.88 if reused else 0.8),
        review=metrics.get("review", 3 if reused else 5),
    )


class BlueprintReuseValueTests(unittest.TestCase):
    def test_observation_is_exact_and_immutable(self) -> None:
        item = observation(reused=True)

        self.assertEqual(item.to_dict()["schema_version"], BLUEPRINT_REUSE_VALUE_SCHEMA)
        with self.assertRaises((AttributeError, TypeError)):
            item.cost = 0  # type: ignore[misc]

    def test_exact_matched_pair_records_only_cost_quality_review_deltas(self) -> None:
        result = compare_blueprint_reuse(observation(reused=True), observation(reused=False))

        self.assertEqual(result.conclusion, COMPARISON_RECORDED)
        self.assertEqual(result.coverage_status, COVERAGE_MATCHED)
        self.assertEqual(
            [(item.metric, item.delta) for item in result.metric_deltas],
            [("cost", -2), ("quality", 0.07999999999999996), ("review", -2)],
        )
        self.assertEqual(
            adapt_blueprint_reuse_eligibility(result),
            OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
        )
        self.assertFalse(result.automatic_reuse)
        self.assertEqual(result.outcome_claim, "NO_USER_OUTCOME_CLAIM")

    def test_zero_sample_insufficient_and_no_reuse_are_fail_closed(self) -> None:
        cases = (
            (compare_blueprint_reuse(None, None), COVERAGE_ZERO_SAMPLE),
            (compare_blueprint_reuse(observation(reused=True), None), COVERAGE_INSUFFICIENT_EVIDENCE),
            (compare_blueprint_reuse(observation(reused=False), observation(reused=False)), COVERAGE_NO_REUSE),
        )
        for result, coverage in cases:
            with self.subTest(coverage=coverage):
                self.assertEqual(result.conclusion, OUTCOME_NOT_ESTABLISHED)
                self.assertEqual(result.coverage_status, coverage)
                self.assertEqual(result.metric_deltas, ())
                self.assertEqual(
                    adapt_blueprint_reuse_eligibility(result),
                    OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
                )

    def test_revision_mismatch_does_not_calculate_deltas(self) -> None:
        result = compare_blueprint_reuse(
            observation(reused=True), observation(reused=False, revision="bp-r8")
        )

        self.assertEqual(result.conclusion, OUTCOME_NOT_ESTABLISHED)
        self.assertEqual(result.coverage_status, COVERAGE_IDENTITY_MISMATCH)
        self.assertEqual(result.metric_deltas, ())
        self.assertEqual(result.eligibility, OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE)

    def test_missing_metric_is_insufficient_and_structural_facts_are_not_outcomes(self) -> None:
        result = compare_blueprint_reuse(
            observation(reused=True, quality=None), observation(reused=False)
        )

        self.assertEqual(result.conclusion, OUTCOME_NOT_ESTABLISHED)
        self.assertEqual(result.coverage_status, COVERAGE_INSUFFICIENT_EVIDENCE)
        self.assertEqual(result.metric_deltas, ())


if __name__ == "__main__":
    unittest.main()
