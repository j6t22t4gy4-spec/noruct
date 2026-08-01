from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.runtime.store import SCHEMA_VERSION, RunStore


class RunStoreSchemaBootstrapTests(unittest.TestCase):
    def test_fresh_initialization_bootstraps_run_event_and_session_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runtime.db")
            try:
                self.assertEqual(store.schema_version(), SCHEMA_VERSION)
                tables = {
                    str(row[0])
                    for row in store._conn.execute(  # noqa: SLF001 - schema contract
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        "runtime_meta",
                        "employee_runs",
                        "run_events",
                        "run_messages",
                        "employee_session_state",
                        "employee_session_leases",
                    }.issubset(tables)
                )
            finally:
                store.close()

    def test_existing_version_reopens_and_sanitizes_legacy_run_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            store.close()

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE runtime_meta SET value = '25' WHERE key = 'schema_version'"
                )
                connection.execute(
                    """
                    INSERT INTO employee_runs(
                        run_id, request_id, job_id, task_id, employee_id, status,
                        request_json, usage_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-run",
                        "legacy-request",
                        "legacy-job",
                        "legacy-task",
                        "legacy-employee",
                        "RUNNING",
                        json.dumps(
                            {
                                "session_key": "private-session-secret",
                                "context": {
                                    "task_evidence": {
                                        "pack_id": "pack-1",
                                        "items": [{"content": "private evidence"}],
                                    }
                                },
                            }
                        ),
                        "{}",
                        "2026-08-01T00:00:00+00:00",
                        "2026-08-01T00:00:00+00:00",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = RunStore(path)
            try:
                self.assertEqual(reopened.schema_version(), SCHEMA_VERSION)
                row = reopened._conn.execute(  # noqa: SLF001 - schema contract
                    "SELECT request_json FROM employee_runs WHERE run_id = ?",
                    ("legacy-run",),
                ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                request = json.loads(str(row["request_json"]))
                self.assertNotIn("private-session-secret", str(request))
                self.assertNotIn("private evidence", str(request))
                self.assertFalse(request["context"]["task_evidence"]["content_retained"])
                self.assertIsNotNone(reopened.get_run("legacy-run"))
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
