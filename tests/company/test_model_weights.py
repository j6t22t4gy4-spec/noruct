from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.model_weights import (
    MODEL_WEIGHT_PROFILE_SCHEMA,
    ModelWeightProfile,
    RouteSignals,
    VersionedWeightProfile,
    select_route,
)


class WeightProfileTests(unittest.TestCase):
    def route(self, **changes: object) -> RouteSignals:
        values: dict[str, object] = dict(route_id="route", simpler_rank=1, eligible_authority=True, eligible_capability=True, eligible_egress=True, quality=0.8, reliability=0.8, latency=0.5, cost=0.2, uncertainty=0.0)
        values.update(changes)
        return RouteSignals(**values)  # type: ignore[arg-type]

    def test_profiles_have_stable_versioned_canonical_digests(self) -> None:
        self.assertEqual(len(ModelWeightProfile), 4)
        for profile in ModelWeightProfile:
            value = VersionedWeightProfile(profile)
            self.assertEqual(value.canonical_payload()["schema"], MODEL_WEIGHT_PROFILE_SCHEMA)
            self.assertEqual(len(value.digest), 64)
            self.assertEqual(value.canonical_bytes(), VersionedWeightProfile(profile).canonical_bytes())
            self.assertAlmostEqual(sum(value.canonical_payload()[name] for name in ("quality_weight", "reliability_weight", "latency_weight", "cost_weight")), 1.0)

    def test_hard_gates_and_missing_quality_prevent_selection(self) -> None:
        denied = self.route(route_id="denied", eligible_authority=False, quality=1.0, reliability=1.0, latency=1.0, cost=0.0)
        self.assertEqual(select_route((denied, self.route()), VersionedWeightProfile(ModelWeightProfile.BALANCED)).route_id, "route")
        self.assertIsNone(select_route((self.route(quality=None, cost=0.0),), VersionedWeightProfile(ModelWeightProfile.EFFICIENT)))

    def test_missing_latency_or_cost_is_not_numeric_zero(self) -> None:
        zero = self.route(route_id="zero", latency=0.0, cost=0.0)
        missing = self.route(route_id="missing", latency=None, cost=None)
        chosen = select_route((missing, zero), VersionedWeightProfile(ModelWeightProfile.BALANCED))
        self.assertEqual(chosen.route_id, "zero")

    def test_symmetric_uncertainty_tie_prefers_simpler_route(self) -> None:
        leader = self.route(route_id="leader", simpler_rank=3, quality=0.8, uncertainty=0.1)
        simpler = self.route(route_id="simpler", simpler_rank=0, quality=0.75, uncertainty=0.0)
        self.assertEqual(select_route((leader, simpler), VersionedWeightProfile(ModelWeightProfile.BALANCED)).route_id, "simpler")

    def test_invalid_numeric_values_are_rejected(self) -> None:
        for value in (True, math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.route(cost=value)


if __name__ == "__main__":
    unittest.main()
