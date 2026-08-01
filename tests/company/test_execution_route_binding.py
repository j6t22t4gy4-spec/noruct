from __future__ import annotations
import unittest
from dataclasses import FrozenInstanceError
from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding

def binding(**changes: object) -> ExecutionRouteBinding:
    value = {"attempt_id":"attempt-1","route_id":"route-1","execution_profile_id":"profile-1","provider_config_digest":"a"*64,"credential_reference":"NORUCT_PROVIDER_KEY","requested_model_id":"model-1","identity_assurance":"VERSIONED_MODEL_ID"}
    value.update({name:"b"*64 for name in ("required_capability_digest","inference_contract_digest","egress_policy_digest","intelligence_snapshot_digest","orchestration_policy_digest","compatibility_evidence_digest","fallback_policy_digest","fanout_policy_digest","continuation_policy_digest")}); value.update(changes)
    return ExecutionRouteBinding(**value)

class ExecutionRouteBindingTests(unittest.TestCase):
 def test_canonical_round_trip_and_immutability(self):
  value=binding(); self.assertEqual(ExecutionRouteBinding.from_canonical_json(value.canonical_json()),value)
  with self.assertRaises(FrozenInstanceError): value.route_id="other"
 def test_secret_and_malformed_fields_fail_closed(self):
  with self.assertRaises(ValueError): binding(credential_reference="sk-secret-value")
  with self.assertRaises(ValueError): binding(provider_config_digest="A"*64)
  with self.assertRaises(ValueError): ExecutionRouteBinding.from_canonical_json('{"unknown":true}')
