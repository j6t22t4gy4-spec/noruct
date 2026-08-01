from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.model_intelligence_availability import AvailabilityState, resolve


class AvailabilityTests(unittest.TestCase):
    def test_retained_snapshot_wins_even_offline(self) -> None:
        outcome = resolve(retained_identity="retained", retained_valid=True, state=AvailabilityState.OFFLINE, bundled_default_identity="default", explicit_route="explicit")
        self.assertEqual((outcome.identity, outcome.source, outcome.explicit_route), ("retained", "LAST_KNOWN_GOOD", "explicit"))

    def test_explicit_route_precedes_bundled_default_after_invalid_retained_snapshot(self) -> None:
        outcome = resolve(retained_identity="retained", retained_valid=False, state=AvailabilityState.EXPIRED, bundled_default_identity="default", explicit_route="explicit")
        self.assertEqual((outcome.identity, outcome.rollback_identity, outcome.source), ("explicit", "retained", "EXPLICIT_ROUTE"))

    def test_fallback_states_select_bundled_default(self) -> None:
        for state in AvailabilityState:
            with self.subTest(state=state):
                outcome = resolve(retained_identity=None, retained_valid=False, state=state, bundled_default_identity="default")
                self.assertEqual((outcome.identity, outcome.source, outcome.state), ("default", "BUNDLED_CONSERVATIVE_DEFAULT", state))


if __name__ == "__main__":
    unittest.main()
