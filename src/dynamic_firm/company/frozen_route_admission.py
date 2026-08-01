"""Durable admission evidence that couples a frozen route to its decision.

This module records a route already selected elsewhere.  It deliberately does
not resolve providers, inspect credentials, dispatch a model request, or grant
egress.  Its durable representation retains the complete existing binding for
replay; ``operator_safe_summary`` is the separately redacted display surface.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .execution_route_binding import ExecutionRouteBinding
from .route_selection_receipt import RouteSelectionReceipt


@dataclass(frozen=True, slots=True)
class FrozenRouteAdmission:
    """One immutable, selected route plus the receipt that selected it."""

    binding: ExecutionRouteBinding
    selection_receipt: RouteSelectionReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ExecutionRouteBinding):
            raise ValueError("binding must be an ExecutionRouteBinding")
        if not isinstance(self.selection_receipt, RouteSelectionReceipt):
            raise ValueError("selection_receipt must be a RouteSelectionReceipt")
        if self.selection_receipt.selected_route_id is None:
            raise ValueError("admission requires a selected route")
        if self.selection_receipt.selected_route_id != self.binding.route_id:
            raise ValueError("selection receipt must select the frozen binding route")
        if self.selection_receipt.policy_digest != self.binding.orchestration_policy_digest:
            raise ValueError("selection policy must match the frozen orchestration policy")

    def canonical_payload(self) -> dict[str, object]:
        """Durable payload, not an operator-safe projection.

        The complete binding is retained so future storage/replay can verify its
        identity.  Consumers that render for people must use
        :meth:`operator_safe_summary` instead.
        """
        return {
            "binding": self.binding.canonical_payload(),
            "binding_digest": self.binding.digest,
            "selection_receipt": self.selection_receipt.canonical_payload(),
            "selection_receipt_digest": self.selection_receipt.digest,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def operator_safe_summary(self) -> dict[str, object]:
        """Content-free display facts; never exposes credential/model details."""
        return {
            "route_id": self.binding.route_id,
            "binding_digest": self.binding.digest,
            "selection_receipt_digest": self.selection_receipt.digest,
            "selection_policy_digest": self.selection_receipt.policy_digest,
            "selection_reasons": list(self.selection_receipt.explanation()),
        }

    @classmethod
    def from_canonical_json(cls, raw: object) -> "FrozenRouteAdmission":
        if not isinstance(raw, str):
            raise ValueError("admission JSON must be canonical text")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("admission JSON is invalid") from exc
        fields = {
            "binding",
            "binding_digest",
            "selection_receipt",
            "selection_receipt_digest",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("admission JSON has unknown or missing fields")
        binding_raw = value["binding"]
        selection_raw = value["selection_receipt"]
        if not isinstance(binding_raw, dict) or not isinstance(selection_raw, dict):
            raise ValueError("admission components must be JSON objects")
        binding = ExecutionRouteBinding.from_canonical_json(
            json.dumps(binding_raw, sort_keys=True, separators=(",", ":"))
        )
        receipt = RouteSelectionReceipt.from_canonical_json(
            json.dumps(selection_raw, sort_keys=True, separators=(",", ":"))
        )
        if value["binding_digest"] != binding.digest:
            raise ValueError("admission binding digest drift")
        if value["selection_receipt_digest"] != receipt.digest:
            raise ValueError("admission selection receipt digest drift")
        admission = cls(binding=binding, selection_receipt=receipt)
        if raw != admission.canonical_json():
            raise ValueError("admission JSON is not canonical")
        return admission
