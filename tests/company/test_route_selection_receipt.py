from __future__ import annotations
import unittest
from dynamic_firm.company.route_selection_receipt import HardConstraintRejection, RouteCandidateReceipt, RouteSelectionReceipt, SelectionReason

class RouteSelectionReceiptTests(unittest.TestCase):
 def test_simple_tie_golden_explanation_and_round_trip(self):
  value=RouteSelectionReceipt((RouteCandidateReceipt("simple"),RouteCandidateReceipt("complex")),"simple",(SelectionReason.HARD_CONSTRAINTS_SATISFIED,SelectionReason.SIMPLE_ROUTE_TIE_PREFERENCE),"a"*64)
  self.assertEqual(value.explanation(),("HARD_CONSTRAINTS_SATISFIED","SIMPLE_ROUTE_TIE_PREFERENCE")); self.assertEqual(RouteSelectionReceipt.from_canonical_json(value.canonical_json()),value)
 def test_rejection_is_not_inferiority_and_invalid_selection_fails(self):
  rejected=RouteCandidateReceipt("denied",(HardConstraintRejection.AUTHORITY,))
  value=RouteSelectionReceipt((rejected,RouteCandidateReceipt("ok")),"ok",(SelectionReason.POLICY_ORDER,),"a"*64)
  self.assertNotIn("inferior"," ".join(value.explanation()).lower())
  with self.assertRaises(ValueError): RouteSelectionReceipt((rejected,),"denied",(SelectionReason.POLICY_ORDER,),"a"*64)
  with self.assertRaises(ValueError): RouteSelectionReceipt.from_canonical_json('{"unknown":true}')
