from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import main
from dynamic_firm.foundation.migration_preview import (
    MigrationApplyError,
    apply_employee_runtime_migration,
    preview_employee_runtime_migration,
)
from dynamic_firm.runtime.models import EventType
from dynamic_firm.runtime.store import EmployeeSessionUpdate, RunStore, employee_session_namespace
from tests.runtime.helpers import make_request
from tests.runtime.test_store import RunStoreTests


class EmployeeRuntimeMigrationPreviewTests(unittest.TestCase):
    _READY_CUTOVER = {"technical_default_ready": True}

    def test_absent_state_is_not_created_and_preview_stays_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "missing.db"

            payload = preview_employee_runtime_migration(state)

            self.assertFalse(state.exists())
            self.assertEqual(payload["execution"], "READ_ONLY_PREVIEW")
            self.assertEqual(payload["inventory"]["state"], "ABSENT")
            self.assertEqual(payload["inventory"]["schema_compatibility"]["state"], "ABSENT")
            self.assertFalse(payload["transition"]["runtime_changed"])
            self.assertFalse(payload["transition"]["apply_available"])
            self.assertIn("NO_STATE_TO_MIGRATE", payload["transition"]["blockers"])
            self.assertNotIn("COMMERCIAL_DEFAULT_GATE_OPEN", payload["transition"]["blockers"])
            self.assertTrue(payload["privacy"]["raw_session_keys_exposed"] is False)

    def test_read_only_preview_reports_aggregate_state_without_history_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "runtime state.db"
            store = RunStore(state)
            request = make_request(request_id="migration-preview-run")
            handle, _ = store.create_run(request)
            store.begin_run(handle.run_id)
            namespace = employee_session_namespace(request.employee.employee_id, "private-session")
            store.terminalize(
                RunStoreTests._success_result(request, handle),
                EventType.RUN_SUCCEEDED,
                {},
                employee_session=EmployeeSessionUpdate(
                    namespace_hash=namespace,
                    employee_id=request.employee.employee_id,
                    expected_revision=0,
                    messages=(
                        {"role": "user", "content": "do not disclose this private history"},
                        {"role": "assistant", "content": "api_key=sk-preview-secret-1234567890"},
                    ),
                    max_messages=16,
                    max_chars=10_000,
                ),
            )
            waiting, _ = store.create_run(make_request(request_id="migration-preview-waiting"))
            store.begin_run(waiting.run_id)
            store.close()
            before = state.read_bytes()

            payload = preview_employee_runtime_migration(state)

            self.assertEqual(state.read_bytes(), before)
            self.assertEqual(payload["inventory"]["state"], "READ_ONLY_INVENTORIED")
            self.assertEqual(payload["inventory"]["employee_session_records"], 1)
            self.assertEqual(payload["inventory"]["employee_session_employees"], 1)
            self.assertEqual(payload["inventory"]["employee_session_message_count"], 2)
            self.assertEqual(payload["inventory"]["active_employee_runs"], 1)
            self.assertEqual(
                payload["inventory"]["schema_compatibility"]["state"],
                "SUPPORTED_PENDING_AUTHORIZATION",
            )
            self.assertIn("ACTIVE_EMPLOYEE_RUNS_PRESENT", payload["transition"]["blockers"])
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn("private-session", serialized)
            self.assertNotIn("private history", serialized)
            self.assertNotIn("sk-preview-secret", serialized)
            self.assertFalse(payload["transition"]["runtime_changed"])
            self.assertTrue(payload["transition"]["state_preserved"])

    def test_cli_is_read_only_and_reports_blocked_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "runtime.db"
            store = RunStore(state)
            store.close()
            before = state.read_bytes()
            output = io.StringIO()

            with patch(
                "dynamic_firm.foundation.migration_preview.foundation_cutover_status",
                return_value=self._READY_CUTOVER,
            ):
                exit_code = main(
                    ["foundation", "migration-preview", "--state", str(state), "--json"],
                    stdout=output,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(state.read_bytes(), before)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["schema_version"], "noruct.employee-runtime-state-compatibility-preview.v2")
            self.assertEqual(payload["transition"]["historical_state_label"], "historical_employee_state")
            self.assertEqual(payload["transition"]["runtime"], "noruct")
            self.assertEqual(payload["transition"]["apply_status"], "READY")
            self.assertTrue(payload["transition"]["apply_available"])
            self.assertFalse(payload["runtime_rollback"]["available"])

    def test_apply_creates_verified_backup_and_no_transform_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "runtime.db"
            store = RunStore(state)
            request = make_request(request_id="migration-apply-run")
            handle, _ = store.create_run(request)
            store.begin_run(handle.run_id)
            store.terminalize(RunStoreTests._success_result(request, handle), EventType.RUN_SUCCEEDED, {})
            store.close()
            before = state.read_bytes()

            with patch(
                "dynamic_firm.foundation.migration_preview.foundation_cutover_status",
                return_value=self._READY_CUTOVER,
            ):
                payload = apply_employee_runtime_migration(
                    state,
                    backup_directory=root / "backups",
                )

            backup = Path(payload["backup_path"])
            receipt = Path(payload["receipt_path"])
            self.assertEqual(state.read_bytes(), before)
            self.assertTrue(backup.is_file())
            self.assertTrue(receipt.is_file())
            self.assertEqual(payload["status"], "APPLIED_NO_DATA_TRANSFORM")
            self.assertEqual(payload["transition"]["data_transform"], "NONE")
            self.assertFalse(payload["transition"]["config_changed"])
            self.assertFalse(payload["transition"]["runtime_rollback_available"])
            self.assertEqual(payload["backup_rehearsal"], "read_only_inventory_match")
            self.assertNotIn("migration-apply-run", receipt.read_text(encoding="utf-8"))

    def test_apply_refuses_active_runs_without_creating_a_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "runtime.db"
            store = RunStore(state)
            handle, _ = store.create_run(make_request(request_id="migration-apply-active"))
            store.begin_run(handle.run_id)
            store.close()
            backups = root / "backups"

            with patch(
                "dynamic_firm.foundation.migration_preview.foundation_cutover_status",
                return_value=self._READY_CUTOVER,
            ):
                with self.assertRaisesRegex(MigrationApplyError, "ACTIVE_EMPLOYEE_RUNS_PRESENT"):
                    apply_employee_runtime_migration(state, backup_directory=backups)

            self.assertFalse(backups.exists())

    def test_cli_apply_requires_confirmation_then_returns_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "runtime.db"
            store = RunStore(state)
            store.close()
            output = io.StringIO()
            error = io.StringIO()
            with patch(
                "dynamic_firm.foundation.migration_preview.foundation_cutover_status",
                return_value=self._READY_CUTOVER,
            ):
                self.assertNotEqual(
                    main(
                        ["foundation", "migration-apply", "--state", str(state)],
                        stdout=output,
                        stderr=error,
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "foundation", "migration-apply", "--state", str(state),
                            "--backup-dir", str(root / "backups"), "--confirm", "--json",
                        ],
                        stdout=output,
                        stderr=error,
                    ),
                    0,
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "APPLIED_NO_DATA_TRANSFORM")

    def test_future_schema_is_read_only_and_an_explicit_migration_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "future-schema.db"
            connection = sqlite3.connect(state)
            connection.execute("CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO runtime_meta(key, value) VALUES('schema_version', '999')"
            )
            connection.commit()
            connection.close()
            before = state.read_bytes()

            payload = preview_employee_runtime_migration(state)

            self.assertEqual(state.read_bytes(), before)
            compatibility = payload["inventory"]["schema_compatibility"]
            self.assertEqual(compatibility["state"], "UNSUPPORTED_FUTURE_SCHEMA")
            self.assertFalse(compatibility["migration_readable"])
            self.assertIn(
                "RUNTIME_SCHEMA_UNSUPPORTED_FUTURE",
                payload["transition"]["blockers"],
            )
            self.assertFalse(payload["transition"]["apply_available"])
            output = io.StringIO()
            self.assertEqual(
                main(
                    ["foundation", "migration-preview", "--state", str(state)],
                    stdout=output,
                ),
                0,
            )
            rendered = output.getvalue()
            self.assertIn("unsupported future schema", rendered)
            self.assertIn("RUNTIME_SCHEMA_UNSUPPORTED_FUTURE", rendered)


if __name__ == "__main__":
    unittest.main()
