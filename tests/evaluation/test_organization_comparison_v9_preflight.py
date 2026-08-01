import dataclasses
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from dynamic_firm.evaluation.organization_comparison_v9 import (  # noqa: E402
    EvaluatorIndependence,
    create_v9_manifest,
)
from dynamic_firm.evaluation.organization_comparison_v9_preflight import (  # noqa: E402
    EXECUTION_STATUS,
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_NEGATIVE,
    OUTCOME_SUCCESS,
    PREFLIGHT_SCHEMA,
    RESULT_SCHEMA,
    OrganizationComparisonV9PreflightError,
    OrganizationComparisonV9SlotReuseError,
    prepare_v9_slot_preflight,
)


def manifest():
    return create_v9_manifest(
        task_revision="task-r17",
        source_revision="source-r23",
        authority_revision="authority-r4",
        budget_model_calls=2,
        budget_wall_time_ms=30_000,
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


def preflight(**overrides):
    value = {
        "manifest": manifest(),
        "slot_id": "v9:strong-solo:1",
        "quota_identity": "quota-r1",
        "quota_available_model_calls": 2,
        "quota_available_wall_time_ms": 30_000,
        "provider_kind": "approved-provider",
        "model_pin": "approved-model@rev-1",
        "consent_id": "consent-r1",
        "consent_scope": "one-v9-slot",
        "consent_confirmed": True,
        "stop_threshold_id": "stop-r1",
        "stop_threshold": "stop-on-safety-or-budget",
        "stop_threshold_confirmed": True,
        "evaluator_identity": "evaluator-contract-1",
        "evaluator_risk_id": "risk-r1",
        "evaluator_risk_summary": "isolated evaluator, no inherited credentials",
        "evaluator_risk_accepted": True,
        "one_slot_confirmation_id": "confirm-r1",
        "confirmed_slot_id": "v9:strong-solo:1",
        "confirmed_slot_count": 1,
        "one_slot_confirmed": True,
    }
    value.update(overrides)
    return prepare_v9_slot_preflight(**value)


class OrganizationComparisonV9PreflightTests(unittest.TestCase):
    def test_h4_preflight_is_sealed_and_has_no_effects(self):
        result = preflight()

        self.assertEqual(result.schema_version, PREFLIGHT_SCHEMA)
        self.assertTrue(result.ready)
        self.assertEqual(result.execution_status, EXECUTION_STATUS)
        self.assertFalse(result.execution_started)
        self.assertEqual(result.provider_calls, 0)
        self.assertFalse(result.slot_reserved)
        self.assertEqual(result.result_template.schema_version, RESULT_SCHEMA)
        self.assertEqual(result.slot_id, "v9:strong-solo:1")
        self.assertEqual(result.preflight_hash, preflight().preflight_hash)
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            result.model_pin = "changed"  # type: ignore[misc]

    def test_missing_h4_fields_fail_closed(self):
        required = (
            "quota_identity",
            "provider_kind",
            "model_pin",
            "consent_id",
            "consent_scope",
            "stop_threshold_id",
            "stop_threshold",
            "evaluator_risk_id",
            "evaluator_risk_summary",
            "one_slot_confirmation_id",
        )
        for field in required:
            with self.subTest(field=field):
                with self.assertRaises(OrganizationComparisonV9PreflightError):
                    preflight(**{field: ""})
        for field in (
            "consent_confirmed",
            "stop_threshold_confirmed",
            "evaluator_risk_accepted",
            "one_slot_confirmed",
        ):
            with self.subTest(field=field):
                with self.assertRaises(OrganizationComparisonV9PreflightError):
                    preflight(**{field: False})

    def test_quota_and_exactly_one_slot_are_checked(self):
        with self.assertRaises(OrganizationComparisonV9PreflightError):
            preflight(quota_available_model_calls=1)
        with self.assertRaises(OrganizationComparisonV9PreflightError):
            preflight(quota_available_wall_time_ms=29_999)
        with self.assertRaises(OrganizationComparisonV9PreflightError):
            preflight(confirmed_slot_count=2)
        with self.assertRaises(OrganizationComparisonV9PreflightError):
            preflight(confirmed_slot_id="v9:strong-solo:2")

    def test_known_used_slot_cannot_be_reused(self):
        with self.assertRaises(OrganizationComparisonV9SlotReuseError):
            preflight(prior_result_slot_ids=("v9:strong-solo:1",))

    def test_result_template_is_append_only_and_preserves_negative_outcome(self):
        template = preflight().result_template
        for outcome in (
            OUTCOME_SUCCESS,
            OUTCOME_FAILED,
            OUTCOME_INTERRUPTED,
            OUTCOME_NEGATIVE,
        ):
            record = template.append(
                record_id=f"record-{outcome}",
                outcome=outcome,
                outcome_reason="explicit terminal observation",
                evidence_digest="evidence-r1",
            )
            self.assertEqual(record.outcome, outcome)
            self.assertTrue(record.append_only)
            with self.assertRaises(OrganizationComparisonV9SlotReuseError):
                record.append_result()


if __name__ == "__main__":
    unittest.main()
