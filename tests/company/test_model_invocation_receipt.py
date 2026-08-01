from __future__ import annotations
import unittest
from dynamic_firm.company.model_invocation_receipt import ModelInvocationReceipt

def receipt(invocation_id="call-1", **changes):
 value={"invocation_id":invocation_id,"route_binding_digest":"a"*64,"context_projection_digest":"b"*64,"attempt_id":"attempt-1","fanout_parent_id":None,"terminal_status":"SUCCEEDED","output_digest":"c"*64,"usage_availability":"AVAILABLE","usage_units":0,"cost_availability":"UNAVAILABLE","cost_usd":None,"latency_ms":0}
 value.update(changes); return ModelInvocationReceipt(**value)
class ModelInvocationReceiptTests(unittest.TestCase):
 def test_concurrent_lineage_receipts_do_not_share_attribution(self):
  first=receipt("call-1",fanout_parent_id="fanout-1"); second=receipt("call-2",fanout_parent_id="fanout-1",output_digest="d"*64)
  self.assertNotEqual(first.digest,second.digest); self.assertEqual(ModelInvocationReceipt.from_canonical_json(first.canonical_json()),first)
 def test_terminal_availability_and_safe_error_fail_closed(self):
  failed=receipt(terminal_status="FAILED",output_digest=None,safe_error_code="TRANSPORT",usage_availability="UNAVAILABLE",usage_units=None)
  self.assertEqual(failed.safe_error_code,"TRANSPORT")
  with self.assertRaises(ValueError): receipt(cost_availability="UNAVAILABLE",cost_usd=0)
  with self.assertRaises(ValueError): receipt(terminal_status="FAILED",safe_error_code="FAIL",output_digest="c"*64)
  with self.assertRaises(ValueError): ModelInvocationReceipt.from_canonical_json('{"unknown":true}')
