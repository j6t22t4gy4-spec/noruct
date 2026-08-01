from __future__ import annotations
import unittest
from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.multi_route_job_plan import MultiRouteJobPlan, TaskRouteAssignment
from dynamic_firm.company.multi_route_runtime_policy import MultiRouteRuntimePolicy
from dynamic_firm.company.route_provider_registry import FrozenRouteProviderRegistry, RouteProviderDefinition
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.providers.fallback import FallbackModelProvider

def binding(**changes: object) -> ExecutionRouteBinding:
    values={"attempt_id":"a","route_id":"route-a","execution_profile_id":"p","provider_config_digest":"a"*64,"credential_reference":"NORUCT_PROVIDER_KEY","requested_model_id":"model","identity_assurance":"VERSIONED_MODEL_ID"}
    values.update({name:"b"*64 for name in ("required_capability_digest","inference_contract_digest","egress_policy_digest","intelligence_snapshot_digest","orchestration_policy_digest","compatibility_evidence_digest","fallback_policy_digest","fanout_policy_digest","continuation_policy_digest")}); values.update(changes)
    return ExecutionRouteBinding(**values)

class RouteProviderRegistryTests(unittest.TestCase):
 def _policy(self, *bindings: ExecutionRouteBinding) -> MultiRouteRuntimePolicy:
  return MultiRouteRuntimePolicy(
   MultiRouteJobPlan(
    "c"*64,
    tuple(TaskRouteAssignment(f"task-{index}", "employee", value.digest, final=index == len(bindings) - 1) for index, value in enumerate(bindings)),
    (),
    "employee",
   ),
   bindings,
  )
 def test_constructs_only_exact_frozen_route_without_resolving_credential(self):
  received=[]
  registry=FrozenRouteProviderRegistry((RouteProviderDefinition("route-a","a"*64,"NORUCT_PROVIDER_KEY",lambda value: received.append(value.credential_reference) or object()),))
  registry.construct(binding()); self.assertEqual(received,["NORUCT_PROVIDER_KEY"])
 def test_missing_route_or_drift_fails_before_factory(self):
  called=[]; registry=FrozenRouteProviderRegistry((RouteProviderDefinition("route-a","a"*64,"NORUCT_PROVIDER_KEY",lambda value: called.append(value) or object()),))
  with self.assertRaises(ValueError): registry.construct(binding(route_id="route-b"))
  with self.assertRaises(ValueError): registry.construct(binding(provider_config_digest="c"*64))
  with self.assertRaises(ValueError): registry.construct(binding(credential_reference="OTHER_PROVIDER_KEY"))
  self.assertEqual(called,[])
 def test_frozen_route_rejects_legacy_mutable_fallback_adapter(self):
  registry=FrozenRouteProviderRegistry((
   RouteProviderDefinition(
    "route-a", "a"*64, "NORUCT_PROVIDER_KEY",
    lambda _value: FallbackModelProvider((("primary", ScriptedModelProvider([])),)),
   ),
  ))
  with self.assertRaisesRegex(ValueError, "legacy mutable fallback"):
   registry.construct(binding())
 def test_validate_frozen_bindings_returns_exact_metadata_without_factory_calls(self):
  called=[]
  first=binding()
  second=binding(route_id="route-b", provider_config_digest="c"*64, credential_reference="OTHER_PROVIDER_KEY")
  registry=FrozenRouteProviderRegistry((
   RouteProviderDefinition(first.route_id,first.provider_config_digest,first.credential_reference,lambda value: called.append(value) or object()),
   RouteProviderDefinition(second.route_id,second.provider_config_digest,second.credential_reference,lambda value: called.append(value) or object()),
  ))
  self.assertEqual(
   registry.validate_frozen_bindings(self._policy(first, second)),
   (("route-a", "a"*64, "NORUCT_PROVIDER_KEY"), ("route-b", "c"*64, "OTHER_PROVIDER_KEY")),
  )
  self.assertEqual(called,[])
 def test_validate_frozen_bindings_rejects_missing_or_metadata_drift_without_factory_calls(self):
  called=[]
  registry=FrozenRouteProviderRegistry((RouteProviderDefinition("route-a","a"*64,"NORUCT_PROVIDER_KEY",lambda value: called.append(value) or object()),))
  for invalid in (
   binding(route_id="route-b"),
   binding(provider_config_digest="c"*64),
   binding(credential_reference="OTHER_PROVIDER_KEY"),
  ):
   with self.assertRaises(ValueError): registry.validate_frozen_bindings(self._policy(invalid))
  with self.assertRaises(TypeError): registry.validate_frozen_bindings(object())
  self.assertEqual(called,[])
