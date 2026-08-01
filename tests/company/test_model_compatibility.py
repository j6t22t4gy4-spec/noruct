from __future__ import annotations

import unittest

from dynamic_firm.company.model_compatibility import (
    CapabilityState,
    CompatibilityCache,
    CompatibilityEvidence,
    SafeErrorClassification,
)


class ModelCompatibilityTests(unittest.TestCase):
    @staticmethod
    def result(state: str = "SUPPORTED") -> dict[str, str]:
        return {
            "auth": state,
            "endpoint": state,
            "model": state,
            "structured_output": state,
            "tool_round_trip": state,
            "stream_cancel": state,
        }

    def test_supported_evidence_round_trips_and_cache_invalidates_on_drift(self) -> None:
        evidence = CompatibilityEvidence.from_result("route-a", "adapter-1", "a" * 64, self.result())
        restored = CompatibilityEvidence.from_canonical_json(evidence.canonical_json())
        self.assertTrue(evidence.is_compatible)
        self.assertEqual(restored, evidence)
        self.assertEqual(restored.digest, evidence.digest)

        cache = CompatibilityCache()
        cache.put(evidence)
        self.assertIs(cache.get("route-a", "adapter-1", "a" * 64), evidence)
        self.assertIsNone(cache.get("route-a", "adapter-2", "a" * 64))
        self.assertIsNone(cache.get("route-a", "adapter-1", "b" * 64))

    def test_unavailable_unsafe_and_unknown_results_fail_closed(self) -> None:
        unavailable = CompatibilityEvidence.from_result("route-a", "adapter-1", "a" * 64, self.result("UNAVAILABLE"))
        unsafe = CompatibilityEvidence.from_result("route-a", "adapter-1", "a" * 64, self.result("UNSAFE"))
        unknown_error = CompatibilityEvidence.from_result(
            "route-a", "adapter-1", "a" * 64, self.result(), SafeErrorClassification.UNKNOWN
        )
        self.assertFalse(unavailable.is_compatible)
        self.assertFalse(unsafe.is_compatible)
        self.assertFalse(unknown_error.is_compatible)
        self.assertIs(unavailable.states[0][1], CapabilityState.UNAVAILABLE)

    def test_malformed_states_digests_and_canonical_payload_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            CompatibilityEvidence.from_result("route-a", "adapter-1", "not-a-digest", self.result())
        with self.assertRaises(ValueError):
            CompatibilityEvidence.from_result("route-a", "adapter-1", "a" * 64, {**self.result(), "extra": "SUPPORTED"})
        with self.assertRaises(ValueError):
            CompatibilityEvidence.from_result("route-a", "adapter-1", "a" * 64, self.result("NOT_A_STATE"))
        with self.assertRaises(ValueError):
            CompatibilityEvidence.from_result("route-a", "adapter-1", "a" * 64, self.result(), "NOT_A_SAFE_ERROR")
        with self.assertRaises(ValueError):
            CompatibilityEvidence.from_canonical_json("{\\\"schema\\\":\\\"wrong\\\"}")
