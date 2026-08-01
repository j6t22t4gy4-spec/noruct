import dataclasses
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from dynamic_firm.evaluation.organization_comparison_v9 import (
    LEGACY_V7_V8_SCHEMAS,
    ORGANIZATION_ARMS,
    PROVIDER_FREE_REHEARSAL,
    EvaluatorIndependence,
    LegacyCampaignRejected,
    V9IntegrityError,
    V9ManifestError,
    create_v9_manifest,
    rehearse_provider_free,
    validate_v9_manifest,
)


def _manifest():
    return create_v9_manifest(
        task_revision="task-r17",
        source_revision="source-r23",
        authority_revision="authority-r4",
        budget_model_calls=12,
        budget_wall_time_ms=180_000,
        acceptance_revision="acceptance-r9",
        evaluator=EvaluatorIndependence(
            identity="evaluator-contract-1",
            profile="content-free-v1",
            network_isolated=True,
            credential_inheritance=False,
            independent_of_arm=True,
            independent_of_results=True,
        ),
    )


class OrganizationComparisonV9Tests(unittest.TestCase):
    def test_provider_free_4x4_rehearsal_and_exact_matching(self):
        manifest = _manifest()
        rehearsal = rehearse_provider_free(manifest)
        self.assertTrue(rehearsal.passed)
        self.assertEqual(rehearsal.arms_checked, 4)
        self.assertEqual(rehearsal.slots_checked, 16)
        self.assertEqual(rehearsal.provider_calls, 0)
        self.assertEqual(tuple(name for name, _ in manifest.arms), ORGANIZATION_ARMS)
        self.assertTrue(
            all(
                slot.provider_kind == PROVIDER_FREE_REHEARSAL
                for slot in manifest.slots()
            )
        )

    def test_manifest_and_slots_are_sealed(self):
        manifest = _manifest()
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            manifest.task_revision = "changed"  # type: ignore[misc]
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            manifest.slots()[0].source_revision = "changed"  # type: ignore[misc]

    def test_post_seal_mutation_fails_closed(self):
        manifest = _manifest()
        object.__setattr__(manifest, "task_revision", "changed")
        with self.assertRaises(V9IntegrityError):
            rehearse_provider_free(manifest)

    def test_unmatched_shared_field_fails_closed(self):
        manifest = _manifest()
        slot = manifest.slots()[0]
        changed_slot = dataclasses.replace(slot, source_revision="other-source")
        changed_arms = ((manifest.arms[0][0], (changed_slot,) + manifest.arms[0][1][1:]),) + manifest.arms[1:]
        changed = dataclasses.replace(manifest, arms=changed_arms)
        with self.assertRaises(V9IntegrityError):
            validate_v9_manifest(changed)

    def test_evaluator_non_independence_fails_closed(self):
        with self.assertRaises(V9ManifestError):
            EvaluatorIndependence(
                identity="evaluator-contract-1",
                profile="content-free-v1",
                network_isolated=False,
                credential_inheritance=False,
                independent_of_arm=True,
                independent_of_results=True,
            )

    def test_duplicate_slot_fails_closed(self):
        manifest = _manifest()
        duplicate = manifest.arms[1][1][0]
        changed_arms = (manifest.arms[0], (duplicate,) + manifest.arms[1][1][1:]) + manifest.arms[2:]
        changed = dataclasses.replace(manifest, arms=changed_arms)
        with self.assertRaises(V9IntegrityError):
            validate_v9_manifest(changed)

    def test_legacy_v7_v8_input_is_rejected(self):
        self.assertEqual(len(LEGACY_V7_V8_SCHEMAS), 4)
        for schema in LEGACY_V7_V8_SCHEMAS:
            with self.assertRaises(LegacyCampaignRejected):
                create_v9_manifest(
                    task_revision="task-r17",
                    source_revision="source-r23",
                    authority_revision="authority-r4",
                    budget_model_calls=12,
                    budget_wall_time_ms=180_000,
                    acceptance_revision="acceptance-r9",
                    evaluator=_manifest().evaluator,
                    legacy_input={"schema_version": schema},
                )

    def test_report_schema_keeps_metric_axes_separate(self):
        from dynamic_firm.evaluation.organization_comparison_v9 import (
            CompleteSafetyFailure,
            CostTime,
            LowerDecileQuality,
            NegativeTransfer,
            OrganizationComparisonV9ArmReport,
            OrganizationComparisonV9Report,
            ReviewRework,
            V9_REPORT_SCHEMA,
        )

        report = OrganizationComparisonV9Report(
            schema_version=V9_REPORT_SCHEMA,
            benchmark_id="organization-comparison-v9",
            manifest_content_hash=_manifest().content_hash,
            arms=(
                OrganizationComparisonV9ArmReport(
                    arm=ORGANIZATION_ARMS[0],
                    lower_decile_quality=LowerDecileQuality(4, None),
                    complete_safety_failure=CompleteSafetyFailure(4, 0, 0),
                    cost_time=CostTime(0, 0, 0, 0, 0, 0),
                    review_rework=ReviewRework(
                        "NOT_RECORDED", "NOT_RECORDED", "NOT_RECORDED", "NOT_RECORDED", "NOT_RECORDED", "NOT_RUN", "NOT_RUN"
                    ),
                    negative_transfer=NegativeTransfer(4, 0, 0),
                ),
            ),
        )
        self.assertIsNone(report.arms[0].lower_decile_quality.lower_decile_quality)
        self.assertEqual(report.arms[0].review_rework.rework_count, "NOT_RECORDED")


if __name__ == "__main__":
    unittest.main()
