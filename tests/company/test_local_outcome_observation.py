from __future__ import annotations

import unittest

from dynamic_firm.company.local_outcome_observation import LocalOutcomeAggregate, LocalOutcomeObservation


def payload(observation_id: str = "observation-1", **changes: object) -> dict[str, object]:
    value: dict[str, object] = {"observation_id": observation_id, "task_class": "coding", "terminal_status": "SUCCEEDED",
        "validation_failed": False, "rework_required": False, "tool_failure": False, "structured_failure": False,
        "latency_availability": "AVAILABLE", "latency_ms": 0, "usage_availability": "UNAVAILABLE", "usage_units": None}
    value.update(changes)
    return value


class LocalOutcomeObservationTests(unittest.TestCase):
    def test_unknown_and_zero_measurements_are_distinct(self) -> None:
        zero = LocalOutcomeObservation.from_mapping(payload())
        unknown = LocalOutcomeObservation.from_mapping(payload("observation-2", latency_availability="UNAVAILABLE", latency_ms=None))
        self.assertEqual(zero.latency_ms, 0)
        self.assertIsNone(unknown.latency_ms)

    def test_privacy_canary_and_malformed_measurements_fail_closed(self) -> None:
        with self.assertRaises(ValueError): LocalOutcomeObservation.from_mapping({**payload(), "prompt": "forbidden"})
        with self.assertRaises(ValueError): LocalOutcomeObservation.from_mapping(payload(latency_availability="UNAVAILABLE", latency_ms=0))
        with self.assertRaises(ValueError): LocalOutcomeObservation.from_mapping(payload(usage_availability="AVAILABLE", usage_units=None))

    def test_aggregate_is_idempotent_and_marks_small_samples_insufficient(self) -> None:
        aggregate = LocalOutcomeAggregate()
        first = LocalOutcomeObservation.from_mapping(payload())
        self.assertTrue(aggregate.record(first))
        self.assertFalse(aggregate.record(first))
        with self.assertRaises(ValueError): aggregate.record(LocalOutcomeObservation.from_mapping(payload(terminal_status="FAILED")))
        summary = aggregate.summary("coding", minimum_sample=2)
        self.assertEqual(summary.sample_count, 1)
        self.assertFalse(summary.minimum_sample_met)
        self.assertEqual(summary.observed_latency_count, 1)
        self.assertEqual(summary.observed_usage_count, 0)
