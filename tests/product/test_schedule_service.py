from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.product.schedule_service import ScheduleServiceStore


class ScheduleServiceStoreTests(unittest.TestCase):
    def test_three_unexpected_exits_open_the_persisted_restart_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.sqlite3"
            with ScheduleServiceStore(path) as store, patch("dynamic_firm.product.schedule_service._is_alive", return_value=False):
                for started_at in (0.0, 2.0, 4.0):
                    reservation = store.reserve_start(poll_seconds=30.0, limit=4, log_path=Path(directory) / "schedule.log", now=started_at)
                    store.mark_started(run_id=reservation.run_id or "", pid=12345)
                    store.status(now=started_at + 1.0)
                self.assertTrue(store.status(now=6.0).restart_blocked)
                with self.assertRaisesRegex(ValueError, "restart circuit"):
                    store.reserve_start(poll_seconds=30.0, limit=4, log_path=Path(directory) / "schedule.log", now=7.0)

    def test_explicit_stop_clears_restart_history_and_preserves_daemon_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.sqlite3"
            with ScheduleServiceStore(path) as store, patch("dynamic_firm.product.schedule_service._is_alive", return_value=False):
                reservation = store.reserve_start(poll_seconds=15.0, limit=8, log_path=Path(directory) / "schedule.log", now=0.0)
                self.assertEqual(reservation.poll_seconds, 15.0)
                self.assertEqual(reservation.limit, 8)
                store.mark_started(run_id=reservation.run_id or "", pid=12345)
                self.assertEqual(store.status(now=1.0).unexpected_starts, 1)
                self.assertEqual(store.stop().unexpected_starts, 0)


if __name__ == "__main__":
    unittest.main()
