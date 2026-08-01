import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.local_snapshot_publication import (
    LocalSnapshotPublicationSimulator,
    PublicationStatus,
)
from dynamic_firm.company.model_intelligence import MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA, ModelIntelligenceSnapshot


def payload(snapshot_id: str) -> dict[str, object]:
    return {
        "schema": MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA,
        "snapshot_id": snapshot_id,
        "generated_at": "2026-08-01T00:00:00Z",
        "expires_at": "2026-08-08T00:00:00Z",
        "publisher_identity": "synthetic-publisher",
        "signature_reference": "synthetic-signature",
        "benchmark_harness_revision": "harness-r1",
        "dataset_revision": "dataset-r1",
        "evaluator_revision": "evaluator-r1",
        "provider_route_class": "general",
        "requested_model_id": "synthetic-model",
        "identity_assurance": "VERSIONED_MODEL_ID",
        "task_class_distributions": {"coding": {"sample_count": 2, "success_rate": 0.5, "lower_bound": 0.1, "upper_bound": 0.9}},
        "error_correlation": [],
        "cost_latency_source": {"region": "synthetic", "observed_at": "2026-08-01T00:00:00Z", "source_revision": "source-r1", "latency_availability": "UNAVAILABLE", "latency_ms_p50": None, "cost_availability": "UNAVAILABLE", "input_cost_per_million": None, "output_cost_per_million": None},
        "limitations": ["synthetic fixture"],
        "contamination_disclosure": "synthetic-only",
    }


def snapshot_bytes(value: dict[str, object]) -> bytes:
    return ModelIntelligenceSnapshot.from_mapping(value).canonical_bytes()


def signature(value: bytes) -> str:
    return "synthetic:" + hashlib.sha256(value).hexdigest()


class LocalSnapshotPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.simulator = LocalSnapshotPublicationSimulator(
            synthetic_verifier=lambda content, detached: detached == signature(content),
            max_snapshot_bytes=4096,
        )

    def test_rejected_signature_noncanonical_and_oversize_do_not_change_active_identity(self) -> None:
        first = snapshot_bytes(payload("one"))
        accepted = self.simulator.publish(
            benchmark_run_reference="run-one", snapshot_canonical_bytes=first,
            detached_signature=signature(first), recorded_at="2026-08-01T00:00:00Z",
        )
        tampered = self.simulator.publish(
            benchmark_run_reference="run-tampered", snapshot_canonical_bytes=snapshot_bytes(payload("two")),
            detached_signature="synthetic:tampered", recorded_at="2026-08-01T00:00:01Z",
        )
        noncanonical_bytes = json.dumps(payload("noncanonical"), indent=2, sort_keys=True).encode("utf-8")
        noncanonical = self.simulator.publish(
            benchmark_run_reference="run-json", snapshot_canonical_bytes=noncanonical_bytes,
            detached_signature=signature(noncanonical_bytes), recorded_at="2026-08-01T00:00:02Z",
        )
        oversized = self.simulator.publish(
            benchmark_run_reference="run-oversize", snapshot_canonical_bytes=b"x" * 4097,
            detached_signature="synthetic:ignored", recorded_at="2026-08-01T00:00:03Z",
        )
        self.assertEqual(accepted.status, PublicationStatus.PUBLISHED)
        self.assertEqual([tampered.status, noncanonical.status, oversized.status], [PublicationStatus.REJECTED] * 3)
        self.assertEqual(self.simulator.active_digest, accepted.snapshot_digest)
        self.assertEqual([event.sequence for event in self.simulator.manifest], [1, 2, 3, 4])

    def test_publish_two_download_exact_canonical_bytes_and_explicit_rollback(self) -> None:
        first, second = snapshot_bytes(payload("first")), snapshot_bytes(payload("second"))
        one = self.simulator.publish(benchmark_run_reference="run-first", snapshot_canonical_bytes=first, detached_signature=signature(first), recorded_at="2026-08-01T00:00:00Z")
        two = self.simulator.publish(benchmark_run_reference="run-second", snapshot_canonical_bytes=second, detached_signature=signature(second), recorded_at="2026-08-01T00:01:00Z")
        self.assertEqual(self.simulator.download(two.snapshot_digest), second)
        self.assertEqual(self.simulator.active_digest, two.snapshot_digest)
        rollback = self.simulator.rollback(one.snapshot_digest, recorded_at="2026-08-01T00:02:00Z")
        self.assertEqual(rollback.status, PublicationStatus.ROLLED_BACK)
        self.assertEqual(self.simulator.active_digest, one.snapshot_digest)
        self.assertEqual(self.simulator.download(two.snapshot_digest), second)
        with self.assertRaises(KeyError):
            self.simulator.download("unknown")

    def test_manifest_and_receipts_are_content_free_and_benchmark_records_are_immutable(self) -> None:
        content = snapshot_bytes(payload("content-free"))
        receipt = self.simulator.publish(benchmark_run_reference="opaque-run-ref", snapshot_canonical_bytes=content, detached_signature=signature(content), recorded_at="2026-08-01T00:00:00Z")
        event = self.simulator.manifest[0]
        self.assertEqual(set(event.__dataclass_fields__), {"sequence", "status", "benchmark_run_reference", "snapshot_digest", "signature_digest", "recorded_at"})
        self.assertEqual(set(receipt.__dataclass_fields__), {"status", "snapshot_digest", "signature_digest", "benchmark_run_reference", "recorded_at"})
        self.assertEqual(self.simulator.benchmark_runs[0].benchmark_run_reference, "opaque-run-ref")
        with self.assertRaises(AttributeError):
            self.simulator.manifest += ()  # type: ignore[misc]

    def test_benchmark_reference_cannot_be_reused_for_another_snapshot(self) -> None:
        first, second = snapshot_bytes(payload("first-run")), snapshot_bytes(payload("second-run"))
        accepted = self.simulator.publish(benchmark_run_reference="single-run", snapshot_canonical_bytes=first, detached_signature=signature(first), recorded_at="2026-08-01T00:00:00Z")
        rejected = self.simulator.publish(benchmark_run_reference="single-run", snapshot_canonical_bytes=second, detached_signature=signature(second), recorded_at="2026-08-01T00:01:00Z")
        self.assertEqual(rejected.status, PublicationStatus.REJECTED)
        self.assertEqual(self.simulator.active_digest, accepted.snapshot_digest)

    def test_durable_publish_reopen_download_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "publication-state.json"
            first, second = snapshot_bytes(payload("durable-first")), snapshot_bytes(payload("durable-second"))
            persistent = LocalSnapshotPublicationSimulator(
                synthetic_verifier=lambda content, detached: detached == signature(content),
                max_snapshot_bytes=4096,
                local_state_path=state_path,
            )
            first_receipt = persistent.publish(benchmark_run_reference="durable-run-one", snapshot_canonical_bytes=first, detached_signature=signature(first), recorded_at="2026-08-01T00:00:00Z")
            second_receipt = persistent.publish(benchmark_run_reference="durable-run-two", snapshot_canonical_bytes=second, detached_signature=signature(second), recorded_at="2026-08-01T00:01:00Z")
            persistent.rollback(first_receipt.snapshot_digest, recorded_at="2026-08-01T00:02:00Z")
            reopened = LocalSnapshotPublicationSimulator(
                synthetic_verifier=lambda content, detached: False,
                max_snapshot_bytes=4096,
                local_state_path=state_path,
            )
            self.assertEqual(reopened.active_digest, first_receipt.snapshot_digest)
            self.assertEqual([event.sequence for event in reopened.manifest], [1, 2, 3])
            self.assertEqual(reopened.download(second_receipt.snapshot_digest), second)
            reopened.rollback(second_receipt.snapshot_digest, recorded_at="2026-08-01T00:03:00Z")
            self.assertEqual(reopened.active_digest, second_receipt.snapshot_digest)

    def test_durable_rejection_preserves_active_identity_and_never_persists_signature_literal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "publication-state.json"
            persistent = LocalSnapshotPublicationSimulator(
                synthetic_verifier=lambda content, detached: detached == "private-detached-signature",
                max_snapshot_bytes=4096,
                local_state_path=state_path,
            )
            first = snapshot_bytes(payload("durable-accepted"))
            accepted = persistent.publish(benchmark_run_reference="durable-run", snapshot_canonical_bytes=first, detached_signature="private-detached-signature", recorded_at="2026-08-01T00:00:00Z")
            rejected = persistent.publish(benchmark_run_reference="durable-rejected", snapshot_canonical_bytes=snapshot_bytes(payload("durable-rejected")), detached_signature="wrong-signature", recorded_at="2026-08-01T00:01:00Z")
            reopened = LocalSnapshotPublicationSimulator(
                synthetic_verifier=lambda content, detached: False,
                max_snapshot_bytes=4096,
                local_state_path=state_path,
            )
            persisted_text = state_path.read_text(encoding="utf-8")
            self.assertEqual([accepted.status, rejected.status], [PublicationStatus.PUBLISHED, PublicationStatus.REJECTED])
            self.assertEqual(reopened.active_digest, accepted.snapshot_digest)
            self.assertEqual([event.status for event in reopened.manifest], [PublicationStatus.PUBLISHED, PublicationStatus.REJECTED])
            self.assertNotIn("private-detached-signature", persisted_text)
            self.assertNotIn("detached_signature", persisted_text)
            self.assertNotIn("prompt", persisted_text)

    def test_durable_corrupt_or_tampered_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "publication-state.json"
            persistent = LocalSnapshotPublicationSimulator(
                synthetic_verifier=lambda content, detached: detached == signature(content),
                max_snapshot_bytes=4096,
                local_state_path=state_path,
            )
            content = snapshot_bytes(payload("tamper"))
            persistent.publish(benchmark_run_reference="tamper-run", snapshot_canonical_bytes=content, detached_signature=signature(content), recorded_at="2026-08-01T00:00:00Z")
            valid_state = state_path.read_bytes()
            state_path.write_bytes(b"not-json")
            with self.assertRaises(ValueError):
                LocalSnapshotPublicationSimulator(synthetic_verifier=lambda content, detached: False, max_snapshot_bytes=4096, local_state_path=state_path)
            tampered = json.loads(valid_state.decode("utf-8"))
            tampered["snapshots"][0]["canonical_bytes_base64"] = "e30="
            state_path.write_bytes(json.dumps(tampered, separators=(",", ":"), sort_keys=True).encode("utf-8"))
            with self.assertRaises(ValueError):
                LocalSnapshotPublicationSimulator(synthetic_verifier=lambda content, detached: False, max_snapshot_bytes=4096, local_state_path=state_path)


if __name__ == "__main__":
    unittest.main()
