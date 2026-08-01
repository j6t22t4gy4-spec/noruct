from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.product.gateway_service import GatewayServiceStore


class GatewayServiceStoreTests(unittest.TestCase):
    def test_three_unexpected_exits_open_the_persisted_restart_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.sqlite3"
            with GatewayServiceStore(path) as store, patch("dynamic_firm.product.gateway_service._is_alive", return_value=False):
                for started_at in (0.0, 2.0, 4.0):
                    reservation = store.reserve_start(receivers=("telegram",), log_path=Path(directory) / "gateway.log", now=started_at)
                    store.mark_started(run_id=reservation.run_id or "", pid=12345)
                    store.status(now=started_at + 1.0)
                self.assertTrue(store.status(now=6.0).restart_blocked)
                with self.assertRaisesRegex(ValueError, "restart circuit"):
                    store.reserve_start(receivers=("telegram",), log_path=Path(directory) / "gateway.log", now=7.0)

    def test_explicit_stop_clears_restart_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.sqlite3"
            with GatewayServiceStore(path) as store, patch("dynamic_firm.product.gateway_service._is_alive", return_value=False):
                reservation = store.reserve_start(receivers=("ntfy",), log_path=Path(directory) / "gateway.log", now=0.0)
                store.mark_started(run_id=reservation.run_id or "", pid=12345)
                self.assertEqual(store.status(now=1.0).unexpected_starts, 1)
                self.assertEqual(store.stop().unexpected_starts, 0)

    def test_receiver_configuration_digest_is_preserved_across_lifecycle_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.sqlite3"
            digest = "a" * 64
            with GatewayServiceStore(path) as store:
                reservation = store.reserve_start(
                    receivers=("telegram",),
                    log_path=Path(directory) / "gateway.log",
                    receiver_config_digest=digest,
                )
                self.assertEqual(reservation.receiver_config_digest, digest)
                running = store.mark_started(run_id=reservation.run_id or "", pid=12345)
                self.assertEqual(running.receiver_config_digest, digest)

    def test_invalid_receiver_configuration_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with GatewayServiceStore(Path(directory) / "gateway.sqlite3") as store:
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    store.reserve_start(
                        receivers=("telegram",),
                        log_path=Path(directory) / "gateway.log",
                        receiver_config_digest="not-a-digest",
                    )

    def test_existing_service_state_migrates_without_losing_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """CREATE TABLE gateway_service (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1), state TEXT NOT NULL, pid INTEGER,
                    run_id TEXT, receivers_json TEXT NOT NULL, started_at REAL, log_path TEXT,
                    unexpected_starts_json TEXT NOT NULL)"""
                )
                connection.execute(
                    "INSERT INTO gateway_service VALUES (1, 'stopped', NULL, 'legacy-run', '[\"telegram\"]', 1.0, '/tmp/gateway.log', '[]')"
                )
            with GatewayServiceStore(path) as store:
                record = store.status(now=2.0)
            self.assertEqual(record.run_id, "legacy-run")
            self.assertEqual(record.receivers, ("telegram",))
            self.assertIsNone(record.receiver_config_digest)


if __name__ == "__main__":
    unittest.main()
