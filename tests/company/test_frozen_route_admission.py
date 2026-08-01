from __future__ import annotations

import json
import unittest

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.frozen_route_admission import FrozenRouteAdmission
from dynamic_firm.company.route_selection_receipt import (
    RouteCandidateReceipt,
    RouteSelectionReceipt,
    SelectionReason,
)


def binding(**changes: object) -> ExecutionRouteBinding:
    values = {
        "attempt_id": "attempt-1",
        "route_id": "route-1",
        "execution_profile_id": "profile-1",
        "provider_config_digest": "a" * 64,
        "credential_reference": "NORUCT_PROVIDER_KEY",
        "requested_model_id": "model-1",
        "identity_assurance": "VERSIONED_MODEL_ID",
    }
    values.update({name: "b" * 64 for name in (
        "required_capability_digest", "inference_contract_digest", "egress_policy_digest",
        "intelligence_snapshot_digest", "orchestration_policy_digest",
        "compatibility_evidence_digest", "fallback_policy_digest", "fanout_policy_digest",
        "continuation_policy_digest",
    )})
    values.update(changes)
    return ExecutionRouteBinding(**values)


def receipt(**changes: object) -> RouteSelectionReceipt:
    values = {
        "candidates": (RouteCandidateReceipt("route-1"),),
        "selected_route_id": "route-1",
        "selection_reasons": (SelectionReason.POLICY_ORDER,),
        "policy_digest": "b" * 64,
    }
    values.update(changes)
    return RouteSelectionReceipt(**values)


class FrozenRouteAdmissionTests(unittest.TestCase):
    def test_golden_canonical_round_trip_and_safe_summary_is_redacted(self) -> None:
        value = FrozenRouteAdmission(binding(), receipt())
        self.assertEqual(FrozenRouteAdmission.from_canonical_json(value.canonical_json()), value)
        self.assertEqual(value.digest, FrozenRouteAdmission.from_canonical_json(value.canonical_json()).digest)
        rendered = json.dumps(value.operator_safe_summary(), sort_keys=True)
        self.assertNotIn(value.binding.credential_reference, rendered)
        self.assertNotIn(value.binding.requested_model_id, rendered)
        self.assertNotIn("credential_reference", rendered)
        self.assertNotIn("requested_model_id", rendered)

    def test_no_selection_foreign_route_and_policy_drift_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            FrozenRouteAdmission(binding(), receipt(selected_route_id=None, selection_reasons=()))
        with self.assertRaises(ValueError):
            FrozenRouteAdmission(binding(), receipt(selected_route_id="route-2", candidates=(RouteCandidateReceipt("route-2"),)))
        with self.assertRaises(ValueError):
            FrozenRouteAdmission(binding(), receipt(policy_digest="c" * 64))

    def test_component_digest_drift_and_noncanonical_payload_fail_closed(self) -> None:
        value = FrozenRouteAdmission(binding(), receipt())
        raw = json.loads(value.canonical_json())
        raw["binding_digest"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "binding digest drift"):
            FrozenRouteAdmission.from_canonical_json(json.dumps(raw, sort_keys=True, separators=(",", ":")))
        raw = json.loads(value.canonical_json())
        raw["selection_receipt_digest"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "selection receipt digest drift"):
            FrozenRouteAdmission.from_canonical_json(json.dumps(raw, sort_keys=True, separators=(",", ":")))
        with self.assertRaisesRegex(ValueError, "not canonical"):
            FrozenRouteAdmission.from_canonical_json(json.dumps(value.canonical_payload(), indent=2))
        with self.assertRaises(ValueError):
            FrozenRouteAdmission.from_canonical_json('{"unknown":true}')
