import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.product.release_monitoring import (
    H4_ESCALATION_NOTICE,
    H4EscalationNotice,
    InMemoryH4AlertDelivery,
    ObservationState,
    ReleaseObservation,
    observe_release,
)


def complete_observation(**changes: int) -> ReleaseObservation:
    values = {
        "install_attempts": 1,
        "install_failures": 0,
        "crash_safe_receipts": 1,
        "approval_effect_incidents": 0,
        "migration_failures": 0,
        "review_burden_signals": 0,
        "observation_state": ObservationState.COMPLETE,
    }
    values.update(changes)
    return ReleaseObservation(**values)


class ReleaseMonitoringTests(unittest.TestCase):
    def test_clean_complete_window_is_sufficient_without_alert(self) -> None:
        delivery = InMemoryH4AlertDelivery()

        result = observe_release(complete_observation(), delivery)

        self.assertTrue(result.observation_sufficient)
        self.assertFalse(result.threshold_breached)
        self.assertFalse(result.h4_escalation_required)
        self.assertEqual(delivery.notices, ())

    def test_each_bounded_signal_breach_delivers_only_one_opaque_h4_notice(self) -> None:
        for field in (
            "install_failures",
            "approval_effect_incidents",
            "migration_failures",
            "review_burden_signals",
        ):
            with self.subTest(field=field):
                delivery = InMemoryH4AlertDelivery()
                result = observe_release(complete_observation(**{field: 1}), delivery)

                self.assertTrue(result.threshold_breached)
                self.assertTrue(result.h4_escalation_required)
                self.assertEqual(delivery.notices, (H4_ESCALATION_NOTICE,))
                self.assertEqual(delivery.notices[0], H4EscalationNotice())

    def test_not_run_and_insufficient_are_not_healthy_or_alerting(self) -> None:
        for state in (ObservationState.NOT_RUN, ObservationState.INSUFFICIENT):
            with self.subTest(state=state):
                delivery = InMemoryH4AlertDelivery()
                result = observe_release(
                    complete_observation(observation_state=state), delivery
                )

                self.assertFalse(result.observation_sufficient)
                self.assertFalse(result.threshold_breached)
                self.assertEqual(delivery.notices, ())

    def test_no_data_is_non_decisive(self) -> None:
        delivery = InMemoryH4AlertDelivery()
        result = observe_release(
            ReleaseObservation(
                install_attempts=0,
                install_failures=0,
                crash_safe_receipts=0,
                approval_effect_incidents=0,
                migration_failures=0,
                review_burden_signals=0,
                observation_state=ObservationState.COMPLETE,
            ),
            delivery,
        )

        self.assertFalse(result.observation_sufficient)
        self.assertFalse(result.h4_escalation_required)
        self.assertEqual(delivery.notices, ())

    def test_negative_and_malformed_inputs_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            complete_observation(migration_failures=-1)
        with self.assertRaises(ValueError):
            complete_observation(install_attempts=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            complete_observation(observation_state="COMPLETE")  # type: ignore[arg-type]

        delivery = InMemoryH4AlertDelivery()
        result = observe_release(object(), delivery)  # type: ignore[arg-type]
        self.assertFalse(result.observation_sufficient)
        self.assertFalse(result.threshold_breached)
        self.assertEqual(delivery.notices, ())

    def test_observation_and_notice_are_immutable(self) -> None:
        observation = complete_observation()
        with self.assertRaises(AttributeError):
            observation.install_attempts = 2  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            H4_ESCALATION_NOTICE.code = "other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
