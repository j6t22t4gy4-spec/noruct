import hashlib
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dynamic_firm.company.model_identity import ObservedModelIdentity, SnapshotIdentityBinding, classify_observed_metadata
from dynamic_firm.company.model_intelligence import ModelIdentityAssurance, ModelIntelligenceSnapshot

def snapshot() -> ModelIntelligenceSnapshot:
    return ModelIntelligenceSnapshot.from_mapping({"schema":"noruct.model-intelligence-snapshot.v1","snapshot_id":"s1","generated_at":"2026-08-01T00:00:00Z","expires_at":"2026-08-02T00:00:00Z","publisher_identity":"p","signature_reference":"s","benchmark_harness_revision":"h","dataset_revision":"d","evaluator_revision":"e","provider_route_class":"general","requested_model_id":"local-model","identity_assurance":"LOCAL_CONTENT_DIGEST","task_class_distributions":{"coding":{"sample_count":1,"success_rate":1,"lower_bound":1,"upper_bound":1}},"error_correlation":[],"cost_latency_source":{"region":"r","observed_at":"2026-08-01T00:00:00Z","source_revision":"r1","latency_availability":"UNAVAILABLE","latency_ms_p50":None,"cost_availability":"UNAVAILABLE","input_cost_per_million":None,"output_cost_per_million":None},"limitations":["l"],"contamination_disclosure":"none"})

class ModelIdentityTests(unittest.TestCase):
 def test_all_states_and_canonical_binding(self):
  digest="a"*64
  for state in ModelIdentityAssurance:
   kwargs={"local_content_digest":digest} if state is ModelIdentityAssurance.LOCAL_CONTENT_DIGEST else ({"provider_revision":"rev1"} if state is ModelIdentityAssurance.IMMUTABLE_PROVIDER_REVISION else {})
   self.assertEqual(ObservedModelIdentity("m","general",state,**kwargs).assurance,state)
  obs=ObservedModelIdentity("local-model","general",ModelIdentityAssurance.LOCAL_CONTENT_DIGEST,digest)
  binding=SnapshotIdentityBinding.bind(snapshot(),obs)
  self.assertIn(obs.digest,binding.canonical_json())
  with self.assertRaises(FrozenInstanceError): obs.requested_model_id="x"
 def test_remote_never_becomes_content_digest_and_mismatch_fails_closed(self):
  with self.assertRaises(ValueError): ObservedModelIdentity("remote","general",ModelIdentityAssurance.FLOATING_ALIAS,"a"*64)
  with self.assertRaises(ValueError): ObservedModelIdentity("remote","general",ModelIdentityAssurance.IMMUTABLE_PROVIDER_REVISION,"a"*64,"rev1")
  with self.assertRaises(ValueError): SnapshotIdentityBinding.bind(snapshot(),ObservedModelIdentity("other","general",ModelIdentityAssurance.LOCAL_CONTENT_DIGEST,"a"*64))
  with self.assertRaises(ValueError): SnapshotIdentityBinding.bind(snapshot(),ObservedModelIdentity("local-model","general",ModelIdentityAssurance.VERSIONED_MODEL_ID))
  with self.assertRaises(ValueError): SnapshotIdentityBinding.bind(snapshot(),ObservedModelIdentity("local-model","other",ModelIdentityAssurance.LOCAL_CONTENT_DIGEST,"a"*64))
 def test_raw_metadata_classifier_is_closed_and_conservative(self):
  digest="b"*64
  cases=((None,ModelIdentityAssurance.IDENTITY_UNKNOWN), ({},ModelIdentityAssurance.IDENTITY_UNKNOWN),
   ({"local_artifact_bytes":b"fixture"},ModelIdentityAssurance.LOCAL_CONTENT_DIGEST),
   ({"immutable_provider_revision":"rev-1"},ModelIdentityAssurance.IMMUTABLE_PROVIDER_REVISION),
   ({"versioned_model_id":"remote-2026-08"},ModelIdentityAssurance.VERSIONED_MODEL_ID),
   ({"floating_alias":"remote-2026-08"},ModelIdentityAssurance.FLOATING_ALIAS),
   ({"immutable_provider_revision":"rev-1","floating_alias":"remote-2026-08"},ModelIdentityAssurance.IDENTITY_UNKNOWN),
   ({"local_content_digest":"remote-id"},ModelIdentityAssurance.IDENTITY_UNKNOWN),
   ({"unknown":"x"},ModelIdentityAssurance.IDENTITY_UNKNOWN))
  for metadata, expected in cases:
   with self.subTest(metadata=metadata): self.assertEqual(classify_observed_metadata("remote-2026-08","general",metadata).assurance,expected)
  local=classify_observed_metadata("local","general",{"local_artifact_bytes":b"fixture"})
  self.assertEqual(local.local_content_digest,hashlib.sha256(b"fixture").hexdigest())
 def test_malformed_local_artifact_observation_fails_closed(self):
  observed=classify_observed_metadata("local-model","specialist",{"local_artifact_bytes":"not-bytes"})
  self.assertEqual(observed.assurance,ModelIdentityAssurance.IDENTITY_UNKNOWN)
  self.assertEqual(observed.requested_model_id,"local-model")
  self.assertEqual(observed.provider_route_class,"specialist")
if __name__=="__main__": unittest.main()
