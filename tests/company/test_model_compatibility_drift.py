from __future__ import annotations

import unittest

from dynamic_firm.company.model_compatibility import CompatibilityEvidence
from dynamic_firm.company.model_compatibility_drift import (
    CompatibilityEligibilityRegistry,
    FutureEligibilityStatus,
    MaterialDriftReason,
)


def compatible_evidence(*, adapter: str = "adapter-1", identity: str = "a" * 64) -> CompatibilityEvidence:
    return CompatibilityEvidence.from_result(
        "route-a",
        adapter,
        identity,
        {
            "auth": "SUPPORTED",
            "endpoint": "SUPPORTED",
            "model": "SUPPORTED",
            "structured_output": "SUPPORTED",
            "tool_round_trip": "SUPPORTED",
            "stream_cancel": "SUPPORTED",
        },
    )


class ModelCompatibilityDriftTests(unittest.TestCase):
    def test_invalidation_preserves_active_pin_and_blocks_future_job_for_each_reason(self) -> None:
        for reason in MaterialDriftReason:
            with self.subTest(reason=reason):
                registry = CompatibilityEligibilityRegistry()
                evidence = compatible_evidence()
                registry.record_compatible(evidence)
                pin = registry.pin_active_job("job-1", evidence)

                invalidation = registry.invalidate(evidence, reason)

                self.assertEqual(invalidation.reason, reason)
                self.assertEqual(pin.evidence_digest, evidence.digest)
                self.assertEqual(pin.key.material_identity_digest, "a" * 64)
                decision = registry.future_eligibility(evidence)
                self.assertEqual(decision.status, FutureEligibilityStatus.REQUIRES_COMPATIBILITY_REFRESH)
                self.assertEqual(decision.invalidation_reason, reason)

    def test_replacement_evidence_restores_only_future_eligibility(self) -> None:
        registry = CompatibilityEligibilityRegistry()
        prior = compatible_evidence()
        registry.record_compatible(prior)
        active_pin = registry.pin_active_job("job-1", prior)
        registry.invalidate(prior, MaterialDriftReason.MODEL_IDENTITY)

        replacement = compatible_evidence(identity="b" * 64)
        registry.record_compatible(replacement)

        self.assertEqual(registry.future_eligibility(prior).status, FutureEligibilityStatus.REQUIRES_COMPATIBILITY_REFRESH)
        self.assertTrue(registry.future_eligibility(replacement).eligible)
        self.assertEqual(active_pin.evidence_digest, prior.digest)
        self.assertEqual(active_pin.key.material_identity_digest, "a" * 64)

    def test_unregistered_or_malformed_inputs_fail_closed(self) -> None:
        registry = CompatibilityEligibilityRegistry()
        evidence = compatible_evidence()
        self.assertEqual(registry.future_eligibility(evidence).status, FutureEligibilityStatus.REQUIRES_COMPATIBILITY_REFRESH)
        with self.assertRaises(ValueError):
            registry.pin_active_job("job-1", evidence)
        with self.assertRaises(ValueError):
            registry.invalidate(evidence, "UNRECOGNIZED")
        with self.assertRaises(ValueError):
            compatible_evidence(identity="not-a-digest")
