from __future__ import annotations
import unittest
from dynamic_firm.company.independent_verification_plan import IndependentCallShape,IndependentVerificationPlan
def shape(seed,**changes):
 value={"provider_route_digest":seed*64,"model_identity_digest":"b"*64,"context_projection_digest":"c"*64,"source_projection_digest":"d"*64,"tools_enabled":False,"read_only":False}; value.update(changes); return IndependentCallShape(**value)
class IndependentVerificationPlanTests(unittest.TestCase):
 def test_heterogeneous_no_tools_candidate_and_read_only_verifier_are_independent(self):
  value=IndependentVerificationPlan(shape("a"),shape("e",model_identity_digest="f"*64,context_projection_digest="1"*64,source_projection_digest="2"*64,read_only=True),-0.2); self.assertTrue(value.effectively_independent)
 def test_clone_fallback_and_high_error_correlation_are_not_independent(self):
  clone=IndependentVerificationPlan(shape("a"),shape("a",read_only=True),0); self.assertFalse(clone.effectively_independent)
  fallback=IndependentVerificationPlan(shape("a"),shape("e",model_identity_digest="f"*64,context_projection_digest="1"*64,source_projection_digest="2"*64,read_only=True,availability_fallback=True),0); self.assertFalse(fallback.effectively_independent)
  correlated=IndependentVerificationPlan(shape("a"),shape("e",model_identity_digest="f"*64,context_projection_digest="1"*64,source_projection_digest="2"*64,read_only=True),0.8); self.assertFalse(correlated.effectively_independent)
 def test_tool_enabled_candidate_or_writer_verifier_fails_closed(self):
  with self.assertRaises(ValueError): IndependentVerificationPlan(shape("a",tools_enabled=True),shape("e",read_only=True),0)
  with self.assertRaises(ValueError): IndependentVerificationPlan(shape("a"),shape("e"),0)
