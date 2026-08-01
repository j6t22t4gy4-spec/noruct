from __future__ import annotations
import unittest
from dynamic_firm.company.fallback_admission import FallbackAttemptState,FallbackDecision,FallbackFailureKind,admit_fallback
class FallbackAdmissionTests(unittest.TestCase):
 def test_only_retryable_pre_effect_equivalent_transport_is_admitted(self): self.assertEqual(admit_fallback(FallbackAttemptState(True,True,False,False,"TRANSPORT")),FallbackDecision.ALLOWED)
 def test_partial_stream_started_effect_auth_policy_and_cancel_fail_closed(self):
  states=(FallbackAttemptState(True,True,True,False,"TRANSPORT"),FallbackAttemptState(True,True,False,True,"TRANSPORT"),FallbackAttemptState(True,True,False,False,"AUTH"),FallbackAttemptState(True,True,False,False,"POLICY"),FallbackAttemptState(True,True,False,False,"CANCEL"),FallbackAttemptState(False,True,False,False,"TRANSPORT"))
  self.assertTrue(all(admit_fallback(value) is FallbackDecision.DENIED for value in states))
