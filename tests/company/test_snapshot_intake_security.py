import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.model_intelligence import MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA, ModelIntelligenceSnapshot
from dynamic_firm.company.snapshot_intake_security import (
    IntakeRejection,
    IntakeStatus,
    SnapshotIntakeEnvelope,
    SnapshotIntakeVerifier,
    TrustKeyPolicy,
    TrustKeyState,
)


def snapshot_bytes(*, snapshot_id: str, generated_at: str, expires_at: str, signature_reference: str = "sig-one") -> bytes:
    return ModelIntelligenceSnapshot.from_mapping(
        {
            "schema": MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA,
            "snapshot_id": snapshot_id,
            "generated_at": generated_at,
            "expires_at": expires_at,
            "publisher_identity": "publisher-one",
            "signature_reference": signature_reference,
            "benchmark_harness_revision": "harness-r1",
            "dataset_revision": "dataset-r1",
            "evaluator_revision": "evaluator-r1",
            "provider_route_class": "general",
            "requested_model_id": "model-r1",
            "identity_assurance": "VERSIONED_MODEL_ID",
            "task_class_distributions": {"coding": {"sample_count": 1, "success_rate": 1, "lower_bound": 1, "upper_bound": 1}},
            "error_correlation": [],
            "cost_latency_source": {"region": "local", "observed_at": generated_at, "source_revision": "r1", "latency_availability": "UNAVAILABLE", "latency_ms_p50": None, "cost_availability": "UNAVAILABLE", "input_cost_per_million": None, "output_cost_per_million": None},
            "limitations": ["synthetic-only"],
            "contamination_disclosure": "synthetic-only",
        }
    ).canonical_bytes()


def envelope(content: bytes, *, key_id: str = "key-three", generation: int = 3, sequence: int = 1, revision: int = 1, signature: str = "sig-one") -> SnapshotIntakeEnvelope:
    return SnapshotIntakeEnvelope(content, "publisher-one", key_id, generation, sequence, revision, signature)


class SnapshotIntakeSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.content = snapshot_bytes(snapshot_id="one", generated_at="2026-08-01T00:00:00Z", expires_at="2026-08-02T00:00:00Z")
        self.verifier = SnapshotIntakeVerifier(
            trust_keys=(
                TrustKeyPolicy("publisher-one", "key-old", 1, TrustKeyState.RETIRED),
                TrustKeyPolicy("publisher-one", "key-revoked", 2, TrustKeyState.REVOKED),
                TrustKeyPolicy("publisher-one", "key-three", 3, TrustKeyState.ACTIVE),
            ),
            synthetic_signature_checker=lambda content, key, signature: key == "key-three" and signature == "sig-one" and bool(content),
            max_snapshot_bytes=4096,
        )

    def test_active_key_accepts_canonical_snapshot_and_receipt_is_content_free(self) -> None:
        receipt = self.verifier.verify(envelope(self.content))
        self.assertEqual(receipt.status, IntakeStatus.ACCEPTED)
        self.assertIsNone(receipt.rejection)
        self.assertEqual(receipt.snapshot_digest, hashlib.sha256(self.content).hexdigest())
        self.assertEqual(set(receipt.__dataclass_fields__), {"status", "rejection", "publisher_identity", "key_id", "key_generation", "sequence", "revision", "snapshot_digest"})
        self.assertNotIn("sig-one", repr(receipt))
        self.assertEqual(self.verifier.accepted_cursors[0].sequence, 1)

    def test_retired_revoked_and_generation_mismatch_fail_closed_without_advancing(self) -> None:
        cases = (
            (envelope(self.content, key_id="key-old", generation=1), IntakeRejection.KEY_NOT_ACTIVE),
            (envelope(self.content, key_id="key-revoked", generation=2), IntakeRejection.KEY_NOT_ACTIVE),
            (envelope(self.content, generation=1), IntakeRejection.KEY_GENERATION_MISMATCH),
        )
        for submission, reason in cases:
            with self.subTest(reason=reason):
                receipt = self.verifier.verify(submission)
                self.assertEqual(receipt.rejection, reason)
                self.assertEqual(self.verifier.accepted_cursors, ())

    def test_old_active_generation_is_rejected_after_rotation(self) -> None:
        rotated = SnapshotIntakeVerifier(
            trust_keys=(
                TrustKeyPolicy("publisher-one", "key-old-active", 1, TrustKeyState.ACTIVE),
                TrustKeyPolicy("publisher-one", "key-three", 3, TrustKeyState.ACTIVE),
            ),
            synthetic_signature_checker=lambda *_: True,
        )
        receipt = rotated.verify(envelope(self.content, key_id="key-old-active", generation=1))
        self.assertEqual(receipt.rejection, IntakeRejection.KEY_GENERATION_ROLLBACK)
        self.assertEqual(rotated.accepted_cursors, ())

    def test_replay_revision_and_time_downgrade_do_not_advance_cursor(self) -> None:
        accepted = self.verifier.verify(envelope(self.content))
        self.assertEqual(accepted.status, IntakeStatus.ACCEPTED)
        replay = self.verifier.verify(envelope(self.content, sequence=1, revision=2))
        revision = self.verifier.verify(envelope(snapshot_bytes(snapshot_id="two", generated_at="2026-08-01T00:01:00Z", expires_at="2026-08-02T00:01:00Z"), sequence=2, revision=1))
        time = self.verifier.verify(envelope(snapshot_bytes(snapshot_id="three", generated_at="2026-08-01T00:00:00Z", expires_at="2026-08-03T00:00:00Z"), sequence=2, revision=2))
        self.assertEqual([replay.rejection, revision.rejection, time.rejection], [IntakeRejection.REPLAYED_SEQUENCE, IntakeRejection.REVISION_DOWNGRADE, IntakeRejection.GENERATED_TIME_ROLLBACK])
        self.assertEqual(self.verifier.accepted_cursors[0].sequence, 1)

    def test_noncanonical_malformed_and_oversized_inputs_fail_closed(self) -> None:
        noncanonical = json.dumps(json.loads(self.content), indent=2, sort_keys=True).encode("utf-8")
        for content in (noncanonical, b"{", b"x" * 4097):
            with self.subTest(size=len(content)):
                receipt = self.verifier.verify(envelope(content))
                self.assertEqual(receipt.rejection, IntakeRejection.MALFORMED_OR_OVERSIZED_SNAPSHOT)
                self.assertEqual(self.verifier.accepted_cursors, ())

    def test_signature_mismatch_and_checker_error_fail_closed(self) -> None:
        mismatch = self.verifier.verify(envelope(self.content, signature="sig-other"))
        broken = SnapshotIntakeVerifier(
            trust_keys=(TrustKeyPolicy("publisher-one", "key-three", 3, TrustKeyState.ACTIVE),),
            synthetic_signature_checker=lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic")),
        )
        error = broken.verify(envelope(self.content))
        self.assertEqual(mismatch.rejection, IntakeRejection.SIGNATURE_REFERENCE_MISMATCH)
        self.assertEqual(error.rejection, IntakeRejection.SYNTHETIC_SIGNATURE_REJECTED)
        self.assertEqual(broken.accepted_cursors, ())


if __name__ == "__main__":
    unittest.main()
