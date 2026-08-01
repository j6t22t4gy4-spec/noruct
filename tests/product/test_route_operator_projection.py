from __future__ import annotations

import json
import unittest

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.model_intelligence import ModelIdentityAssurance
from dynamic_firm.company.model_invocation_receipt import (
    InvocationTerminalStatus,
    ModelInvocationReceipt,
    ReceiptAvailability,
)
from dynamic_firm.company.route_selection_receipt import (
    RouteCandidateReceipt,
    RouteSelectionReceipt,
    SelectionReason,
)
from dynamic_firm.product.route_operator_projection import (
    CompatibilityPoint,
    CompatibilityStatus,
    EgressOperatorState,
    EgressPolicyState,
    FallbackOperatorState,
    OperatorTaskIdentity,
    RouteOperatorProjection,
    build_route_operator_projection,
)


def _digest(seed: str) -> str:
    return (seed * 64)[:64]


def _binding() -> ExecutionRouteBinding:
    return ExecutionRouteBinding(
        attempt_id="attempt-1", route_id="route-korean-safe", execution_profile_id="profile-1",
        provider_config_digest=_digest("a"), credential_reference="TEST_ROUTE_TOKEN",
        requested_model_id="must-not-project", identity_assurance=ModelIdentityAssurance.VERSIONED_MODEL_ID,
        required_capability_digest=_digest("b"), inference_contract_digest=_digest("c"),
        egress_policy_digest=_digest("d"), intelligence_snapshot_digest=_digest("e"),
        orchestration_policy_digest=_digest("f"), compatibility_evidence_digest=_digest("1"),
        fallback_policy_digest=_digest("2"), fanout_policy_digest=_digest("3"), continuation_policy_digest=_digest("4"),
    )


def _selection(route_id: str) -> RouteSelectionReceipt:
    return RouteSelectionReceipt(
        candidates=(RouteCandidateReceipt(route_id, uncertainty=0.25),),
        selected_route_id=route_id,
        selection_reasons=(SelectionReason.HARD_CONSTRAINTS_SATISFIED,),
        policy_digest=_digest("5"),
    )


def _projection(receipt: ModelInvocationReceipt | None = None) -> RouteOperatorProjection:
    binding = _binding()
    if receipt is not None:
        receipt = ModelInvocationReceipt(
            invocation_id=receipt.invocation_id, route_binding_digest=binding.digest,
            context_projection_digest=receipt.context_projection_digest, attempt_id=receipt.attempt_id,
            fanout_parent_id=receipt.fanout_parent_id, terminal_status=receipt.terminal_status,
            output_digest=receipt.output_digest, usage_availability=receipt.usage_availability,
            usage_units=receipt.usage_units, cost_availability=receipt.cost_availability,
            cost_usd=receipt.cost_usd, latency_ms=receipt.latency_ms,
            safe_error_code=receipt.safe_error_code,
        )
    return build_route_operator_projection(
        OperatorTaskIdentity("employee-7", "task-9"), binding, _selection(binding.route_id),
        CompatibilityPoint("compat-point-1", CompatibilityStatus.COMPATIBLE),
        EgressOperatorState(EgressPolicyState.OFFLINE), receipt,
        FallbackOperatorState.NOT_USED if receipt is not None else FallbackOperatorState.NOT_OBSERVED,
    )


class RouteOperatorProjectionTests(unittest.TestCase):
    def test_canonical_round_trip_and_shared_render_agreement(self) -> None:
        projection = _projection()
        raw = projection.canonical_json()
        self.assertEqual(RouteOperatorProjection.from_canonical_json(raw), projection)
        rows = dict(projection.render_tui_rows())
        lines = projection.render_cli_lines()
        self.assertEqual(rows["route_id"], projection.route_id)
        self.assertEqual(rows["terminal_status"], "OFFLINE")
        self.assertEqual(rows["fallback_state"], "NOT_OBSERVED")
        self.assertIn("경로=route-korean-safe", lines[0])
        self.assertIn("상태=OFFLINE", lines[0])
        self.assertIn("대체=NOT_OBSERVED", lines)
        self.assertNotIn("must-not-project", raw)
        self.assertNotIn("TEST_ROUTE_TOKEN", raw)
        self.assertNotIn("provider_config_digest", raw)
        self.assertNotIn("credential_reference", raw)
        self.assertNotIn("requested_model_id", raw)

    def test_actual_receipt_summary_preserves_unknown_and_zero_cost(self) -> None:
        seed = ModelInvocationReceipt(
            invocation_id="invoke-1", route_binding_digest=_digest("9"), context_projection_digest=_digest("8"),
            attempt_id="attempt-1", fanout_parent_id=None, terminal_status=InvocationTerminalStatus.SUCCEEDED,
            output_digest=_digest("7"), usage_availability=ReceiptAvailability.UNAVAILABLE, usage_units=None,
            cost_availability=ReceiptAvailability.AVAILABLE, cost_usd=0, latency_ms=12,
        )
        projection = _projection(seed)
        self.assertEqual(projection.actual_receipt, {
            "terminal_status": "SUCCEEDED", "usage_availability": "UNAVAILABLE",
            "cost_availability": "AVAILABLE", "cost_usd": 0.0, "latency_ms": 12.0,
        })
        self.assertEqual(dict(projection.render_tui_rows())["terminal_status"], "SUCCEEDED")
        self.assertEqual(dict(projection.render_tui_rows())["fallback_state"], "NOT_USED")

    def test_unverified_egress_and_unclassified_fanout_are_explicit(self) -> None:
        projection = build_route_operator_projection(
            OperatorTaskIdentity("employee-7", "task-9"), _binding(), _selection("route-korean-safe"),
            CompatibilityPoint("compat-point-1", CompatibilityStatus.UNKNOWN),
            EgressOperatorState(EgressPolicyState.UNVERIFIED),
            fallback_state=FallbackOperatorState.FANOUT_UNCLASSIFIED,
        )
        rows = dict(projection.render_tui_rows())
        self.assertEqual(rows["egress_policy_state"], "UNVERIFIED")
        self.assertEqual(rows["fallback_state"], "FANOUT_UNCLASSIFIED")

    def test_foreign_or_malformed_receipt_and_noncanonical_payload_are_rejected(self) -> None:
        binding = _binding()
        foreign = ModelInvocationReceipt(
            invocation_id="invoke-foreign", route_binding_digest=_digest("0"), context_projection_digest=_digest("8"),
            attempt_id="attempt-1", fanout_parent_id=None, terminal_status=InvocationTerminalStatus.FAILED,
            output_digest=None, usage_availability=ReceiptAvailability.UNAVAILABLE, usage_units=None,
            cost_availability=ReceiptAvailability.UNAVAILABLE, cost_usd=None, latency_ms=2,
        )
        with self.assertRaisesRegex(ValueError, "different frozen route"):
            build_route_operator_projection(
                OperatorTaskIdentity("employee-7", "task-9"), binding, _selection(binding.route_id),
                CompatibilityPoint("compat-point-1", CompatibilityStatus.UNKNOWN),
                EgressOperatorState(EgressPolicyState.NOT_AUTHORIZED), foreign,
            )
        raw = _projection().canonical_json()
        self.assertRaises(ValueError, RouteOperatorProjection.from_canonical_json, json.dumps(json.loads(raw)))

    def test_narrow_cjk_render_preserves_priority_labels_and_restart_golden(self) -> None:
        first = _projection()
        restarted = _projection()
        self.assertEqual(first.canonical_json(), restarted.canonical_json())
        narrow = first.render_cli_lines(width=16)
        self.assertTrue(narrow[0].startswith("경로="))
        self.assertIn("상태", narrow[0])
        self.assertTrue(narrow[0].endswith("…") or "OFFLINE" in narrow[0])
        self.assertTrue(all(len(line) <= 16 for line in narrow))


if __name__ == "__main__":
    unittest.main()
