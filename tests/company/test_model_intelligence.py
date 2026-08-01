import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.model_intelligence import (
    MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA,
    ModelIdentityAssurance,
    ModelIntelligenceSnapshot,
    ObservationAvailability,
)


def golden_payload() -> dict[str, object]:
    return {
        "schema": MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA,
        "snapshot_id": "snapshot-20260801",
        "generated_at": "2026-08-01T00:00:00Z",
        "expires_at": "2026-08-08T00:00:00Z",
        "publisher_identity": "benchmark-publisher-v1",
        "signature_reference": "signature-reference-v1",
        "benchmark_harness_revision": "harness-r7",
        "dataset_revision": "dataset-r12",
        "evaluator_revision": "evaluator-r4",
        "provider_route_class": "general-purpose",
        "requested_model_id": "model-2026-08",
        "identity_assurance": "VERSIONED_MODEL_ID",
        "task_class_distributions": {
            "coding": {"sample_count": 24, "success_rate": 0.75, "lower_bound": 0.6, "upper_bound": 0.85},
            "verification": {"sample_count": 18, "success_rate": 0.7, "lower_bound": 0.5, "upper_bound": 0.82},
        },
        "error_correlation": [{"compared_model_id": "model-peer-2026", "correlation": 0.2, "sample_count": 15}],
        "cost_latency_source": {
            "region": "local-observation", "observed_at": "2026-08-01T00:00:00Z", "source_revision": "cost-r1",
            "latency_availability": "AVAILABLE", "latency_ms_p50": 320.0,
            "cost_availability": "AVAILABLE", "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
        },
        "limitations": ["synthetic-suite-only", "no-live-provider-claim"],
        "contamination_disclosure": "disclosure-none-known",
    }


class ModelIntelligenceSnapshotTests(unittest.TestCase):
    def test_golden_round_trip_is_canonical_and_immutable(self) -> None:
        snapshot = ModelIntelligenceSnapshot.from_mapping(golden_payload())
        restored = ModelIntelligenceSnapshot.from_mapping(json.loads(snapshot.canonical_json()))

        self.assertEqual(restored, snapshot)
        self.assertEqual(restored.digest, snapshot.digest)
        self.assertEqual(snapshot.content_digest, snapshot.digest)
        self.assertEqual(snapshot.identity_assurance, ModelIdentityAssurance.VERSIONED_MODEL_ID)
        self.assertEqual(snapshot.cost_latency_source.cost_availability, ObservationAvailability.AVAILABLE)
        self.assertEqual(snapshot.canonical_payload()["schema"], MODEL_INTELLIGENCE_SNAPSHOT_SCHEMA)
        with self.assertRaises(FrozenInstanceError):
            snapshot.requested_model_id = "different-model"  # type: ignore[misc]

    def test_unknown_raw_or_executable_and_single_rank_inputs_are_rejected(self) -> None:
        for field, value in (
            ("raw_prompt", "do not retain"),
            ("dataset_row", {"content": "do not retain"}),
            ("aggregate_rank", 1),
            ("executable_payload", lambda: None),
        ):
            with self.subTest(field=field):
                payload = golden_payload()
                payload[field] = value
                with self.assertRaises(ValueError):
                    ModelIntelligenceSnapshot.from_mapping(payload)

    def test_malformed_values_and_unknown_nested_fields_are_rejected(self) -> None:
        payload = golden_payload()
        payload["identity_assurance"] = "WEIGHT_DIGEST_ASSUMED"
        with self.assertRaises(ValueError):
            ModelIntelligenceSnapshot.from_mapping(payload)

    def test_error_correlation_accepts_signed_pearson_boundaries_and_round_trips(self) -> None:
        for correlation in (-1.0, 1.0):
            with self.subTest(correlation=correlation):
                payload = golden_payload()
                payload["error_correlation"][0]["correlation"] = correlation  # type: ignore[index]
                snapshot = ModelIntelligenceSnapshot.from_mapping(payload)
                self.assertEqual(snapshot.error_correlation[0].correlation, correlation)
                self.assertEqual(
                    ModelIntelligenceSnapshot.from_mapping(json.loads(snapshot.canonical_json())), snapshot
                )

    def test_error_correlation_rejects_out_of_range_and_nonfinite_coefficients(self) -> None:
        for invalid in (-1.0001, 1.0001, float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                payload = golden_payload()
                payload["error_correlation"][0]["correlation"] = invalid  # type: ignore[index]
                with self.assertRaises(ValueError):
                    ModelIntelligenceSnapshot.from_mapping(payload)

    def test_unavailable_observations_are_unknown_and_available_zero_is_valid(self) -> None:
        payload = golden_payload()
        source = payload["cost_latency_source"]  # type: ignore[assignment]
        source["cost_availability"] = "UNAVAILABLE"  # type: ignore[index]
        source["input_cost_per_million"] = None  # type: ignore[index]
        source["output_cost_per_million"] = None  # type: ignore[index]
        snapshot = ModelIntelligenceSnapshot.from_mapping(payload)
        self.assertEqual(snapshot.cost_latency_source.cost_availability, ObservationAvailability.UNAVAILABLE)
        self.assertIsNone(snapshot.cost_latency_source.input_cost_per_million)
        self.assertEqual(json.loads(snapshot.canonical_json())["cost_latency_source"]["input_cost_per_million"], None)

        payload = golden_payload()
        payload["cost_latency_source"]["latency_availability"] = "UNAVAILABLE"  # type: ignore[index]
        payload["cost_latency_source"]["latency_ms_p50"] = None  # type: ignore[index]
        snapshot = ModelIntelligenceSnapshot.from_mapping(payload)
        self.assertEqual(snapshot.cost_latency_source.latency_availability, ObservationAvailability.UNAVAILABLE)
        self.assertIsNone(snapshot.cost_latency_source.latency_ms_p50)

        payload = golden_payload()
        payload["cost_latency_source"]["input_cost_per_million"] = 0  # type: ignore[index]
        payload["cost_latency_source"]["output_cost_per_million"] = 0  # type: ignore[index]
        snapshot = ModelIntelligenceSnapshot.from_mapping(payload)
        self.assertEqual(snapshot.cost_latency_source.input_cost_per_million, 0.0)
        self.assertEqual(ModelIntelligenceSnapshot.from_mapping(json.loads(snapshot.canonical_json())), snapshot)

        payload = golden_payload()
        payload["cost_latency_source"]["cost_availability"] = "UNAVAILABLE"  # type: ignore[index]
        with self.assertRaises(ValueError):
            ModelIntelligenceSnapshot.from_mapping(payload)

    def test_observations_reject_nonfinite_values(self) -> None:
        for field in (
            "latency_ms_p50",
            "input_cost_per_million",
            "output_cost_per_million",
        ):
            for invalid in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, invalid=invalid):
                    payload = golden_payload()
                    payload["cost_latency_source"][field] = invalid  # type: ignore[index]
                    with self.assertRaises(ValueError):
                        ModelIntelligenceSnapshot.from_mapping(payload)

        payload = golden_payload()
        payload["cost_latency_source"]["latency_availability"] = "UNAVAILABLE"  # type: ignore[index]
        with self.assertRaises(ValueError):
            ModelIntelligenceSnapshot.from_mapping(payload)

        payload = golden_payload()
        payload["task_class_distributions"] = {"unknown_class": payload["task_class_distributions"]["coding"]}  # type: ignore[index]
        with self.assertRaises(ValueError):
            ModelIntelligenceSnapshot.from_mapping(payload)

        payload = golden_payload()
        payload["task_class_distributions"]["coding"]["rank"] = 1  # type: ignore[index]
        with self.assertRaises(ValueError):
            ModelIntelligenceSnapshot.from_mapping(payload)


if __name__ == "__main__":
    unittest.main()
