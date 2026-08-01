import hashlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.model_intelligence import MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA, ModelIntelligenceSnapshot
from dynamic_firm.company.model_intelligence_catalog import SQLiteModelIntelligenceCatalog


def payload(snapshot_id: str, *, expires_at: str = "2026-08-08T00:00:00Z") -> dict[str, object]:
    return {
        "schema": MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA, "snapshot_id": snapshot_id,
        "generated_at": "2026-08-01T00:00:00Z", "expires_at": expires_at,
        "publisher_identity": "fixture-publisher", "signature_reference": "fixture-signature",
        "benchmark_harness_revision": "harness-r1", "dataset_revision": "dataset-r1", "evaluator_revision": "evaluator-r1",
        "provider_route_class": "general", "requested_model_id": "model-fixture", "identity_assurance": "VERSIONED_MODEL_ID",
        "task_class_distributions": {"coding": {"sample_count": 2, "success_rate": 0.5, "lower_bound": 0.1, "upper_bound": 0.9}},
        "error_correlation": [],
        "cost_latency_source": {"region": "fixture", "observed_at": "2026-08-01T00:00:00Z", "source_revision": "fixture-r1", "latency_availability": "UNAVAILABLE", "latency_ms_p50": None, "cost_availability": "UNAVAILABLE", "input_cost_per_million": None, "output_cost_per_million": None},
        "limitations": ["synthetic-only"], "contamination_disclosure": "none-known",
    }


def signature(value: dict[str, object]) -> str:
    return "synthetic:" + hashlib.sha256(ModelIntelligenceSnapshot.from_mapping(value).canonical_bytes()).hexdigest()


class ModelIntelligenceCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "catalog.sqlite3"
        self.clock = datetime(2026, 8, 1, tzinfo=UTC)
        self.now = lambda: self.clock
        self.verifier = lambda bytes_, detached: detached == "synthetic:" + hashlib.sha256(bytes_).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def catalog(self) -> SQLiteModelIntelligenceCatalog:
        return SQLiteModelIntelligenceCatalog(self.path, synthetic_verifier=self.verifier, now=self.now)

    def test_verified_candidate_requires_explicit_activation_and_survives_restart(self) -> None:
        candidate = payload("snapshot-one")
        with self.catalog() as catalog:
            record = catalog.ingest(candidate, detached_signature=signature(candidate), signature_reference="fixture-v1")
            self.assertEqual(record.state, "VERIFIED_CANDIDATE")
            self.assertIsNone(catalog.active())
            active = catalog.activate(record.digest)
            self.assertEqual(active.state, "ACTIVE")
        with self.catalog() as restarted:
            self.assertEqual(restarted.active().digest, active.digest)  # type: ignore[union-attr]
            self.assertEqual(restarted.snapshot(active.digest).snapshot_id, "snapshot-one")

    def test_signature_schema_and_payload_tamper_cannot_replace_last_known_good(self) -> None:
        good = payload("snapshot-good")
        bad = payload("snapshot-bad")
        with self.catalog() as catalog:
            active = catalog.activate(catalog.ingest(good, detached_signature=signature(good), signature_reference="fixture-v1").digest)
            rejected = catalog.ingest(bad, detached_signature="synthetic:wrong", signature_reference="fixture-v1")
            self.assertEqual(rejected.state, "REJECTED")
            self.assertEqual(catalog.active().digest, active.digest)  # type: ignore[union-attr]
            malformed = payload("snapshot-malformed")
            malformed["schema"] = "unknown.schema"
            with self.assertRaises(ValueError):
                catalog.ingest(malformed, detached_signature="synthetic:bad", signature_reference="fixture-v1")
            self.assertEqual(catalog.active().digest, active.digest)  # type: ignore[union-attr]

    def test_expiry_boundary_rejects_and_rollback_is_explicit_and_restart_safe(self) -> None:
        first, second = payload("snapshot-first"), payload("snapshot-second")
        expired = payload("snapshot-expired", expires_at="2026-08-01T00:00:00Z")
        expired["generated_at"] = "2026-07-31T00:00:00Z"
        with self.catalog() as catalog:
            first_active = catalog.activate(catalog.ingest(first, detached_signature=signature(first), signature_reference="fixture-v1").digest)
            self.assertEqual(catalog.ingest(expired, detached_signature=signature(expired), signature_reference="fixture-v1").state, "REJECTED")
            second_active = catalog.activate(catalog.ingest(second, detached_signature=signature(second), signature_reference="fixture-v1").digest)
            self.assertNotEqual(first_active.digest, second_active.digest)
            self.assertEqual(catalog.rollback(first_active.digest).digest, first_active.digest)
        with self.catalog() as restarted:
            self.assertEqual(restarted.active().digest, first_active.digest)  # type: ignore[union-attr]

    def test_candidate_that_expires_before_activation_is_rejected(self) -> None:
        candidate = payload("snapshot-between", expires_at="2026-08-01T00:00:01Z")
        with self.catalog() as catalog:
            record = catalog.ingest(
                candidate,
                detached_signature=signature(candidate),
                signature_reference="fixture-v1",
            )
            self.assertEqual(record.state, "VERIFIED_CANDIDATE")
            self.clock = datetime(2026, 8, 1, 0, 0, 1, tzinfo=UTC)
            with self.assertRaises(ValueError):
                catalog.activate(record.digest)
            self.assertIsNone(catalog.active())
            self.assertEqual(catalog._record(record.digest).state, "REJECTED")


if __name__ == "__main__":
    unittest.main()
