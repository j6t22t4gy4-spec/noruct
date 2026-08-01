from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import (
    EmployeeRunResult,
    EventType,
    RunStatus,
    RunSignal,
    SemanticReplanDirective,
    SemanticReplanOperation,
    SignalCode,
    Usage,
    utc_now,
)
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import (
    SCHEMA_VERSION,
    EmployeeSessionConflict,
    EmployeeSessionUpdate,
    RunStore,
    employee_session_namespace,
)
from dynamic_firm.runtime.tools import ToolRegistry
from dynamic_firm.runtime.store_model_invocation_receipt import (
    FrozenDispatcherLeaseConflict,
)
from tests.runtime.test_frozen_run_route_store import binding
from tests.runtime.helpers import make_request


class RunStoreTests(unittest.TestCase):
    @staticmethod
    def _success_result(request, handle, summary="done") -> EmployeeRunResult:
        return EmployeeRunResult(
            run_id=handle.run_id,
            request_id=handle.request_id,
            job_id=request.task.job_id,
            task_id=request.task.task_id,
            employee_id=request.employee.employee_id,
            status=RunStatus.SUCCEEDED,
            summary=summary,
            output_artifact_refs=(),
            acceptance_evidence=(),
            unresolved_issues=(),
            observations=(),
            suggested_followups=(),
            signals=(),
            partial_result=False,
            usage=Usage(),
            last_event_seq=0,
            started_at=utc_now(),
            finished_at=utc_now(),
        )

    def test_request_id_is_idempotent_and_event_sequence_is_unique(self) -> None:
        store = RunStore()
        request = make_request()
        first, created_first = store.create_run(request)
        second, created_second = store.create_run(request)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first, second)
        self.assertEqual([event.seq for event in store.list_events(first.run_id)], [1])
        self.assertEqual(store.list_events(first.run_id)[0].type, EventType.RUN_CREATED)
        store.close()

    def test_terminal_result_preserves_valid_semantic_replan_directive(self) -> None:
        """A stored Employee signal must remain actionable during Kernel reconcile."""

        store = RunStore()
        request = make_request(request_id="semantic-replan-round-trip")
        handle, _ = store.create_run(request)
        store.begin_run(handle.run_id)
        result = replace(
            self._success_result(request, handle),
            signals=(
                RunSignal(
                    code=SignalCode.ASSUMPTION_INVALIDATED,
                    semantic_replan=SemanticReplanDirective(
                        operation=SemanticReplanOperation.SPLIT,
                        capability_ids=("research", "verification"),
                        assumption_refs=("evidence:assumption-1",),
                    ),
                ),
            ),
        )

        stored = store.terminalize(result, EventType.RUN_SUCCEEDED, {})
        restored = store.get_result(handle.run_id)

        self.assertIsNotNone(stored.signals[0].semantic_replan)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(
            restored.signals[0].semantic_replan,
            result.signals[0].semantic_replan,
        )
        store.close()

    def test_terminal_result_drops_malformed_semantic_replan_directive(self) -> None:
        """Corrupt receipt data must retain the signal without gaining mutation authority."""

        store = RunStore()
        request = make_request(request_id="semantic-replan-malformed")
        handle, _ = store.create_run(request)
        store.begin_run(handle.run_id)
        result = self._success_result(request, handle)
        # Persist through the store, then alter only the opaque JSON payload
        # to emulate a corrupt older receipt without a permissive parser path.
        store.terminalize(result, EventType.RUN_SUCCEEDED, {})
        with store._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT result_json FROM employee_runs WHERE run_id = ?", (handle.run_id,)
            ).fetchone()
            assert row is not None
            body = json.loads(row["result_json"])
            body["signals"] = [{
                "code": SignalCode.ASSUMPTION_INVALIDATED.value,
                "semantic_replan": {"operation": "SPLIT", "capability_ids": "not-a-list"},
            }]
            conn.execute(
                "UPDATE employee_runs SET result_json = ? WHERE run_id = ?",
                (json.dumps(body), handle.run_id),
            )
        restored = store.get_result(handle.run_id)

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.signals[0].code, SignalCode.ASSUMPTION_INVALIDATED)
        self.assertIsNone(restored.signals[0].semantic_replan)
        store.close()

    def test_non_terminal_run_is_closed_as_process_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            handle, _ = store.create_run(make_request())
            store.begin_run(handle.run_id)
            store.close()

            reopened = RunStore(path)
            service = NativeEmployeeRuntimeService(
                store=reopened,
                provider=ScriptedModelProvider([]),
                registry=ToolRegistry(),
            )
            result = reopened.get_result(handle.run_id)

            self.assertEqual(len(service.recovered_results), 1)
            self.assertIsNotNone(result)
            self.assertEqual(result.status, RunStatus.FAILED)
            self.assertEqual(result.failure.code, "PROCESS_INTERRUPTED")
            self.assertEqual(reopened.list_events(handle.run_id)[-1].type, EventType.RUN_FAILED)
            reopened.close()

    def test_generic_recovery_defers_frozen_pre_dispatch_and_leased_runs(self) -> None:
        """Generic recovery cannot claim either side of the frozen start boundary."""

        store = RunStore()
        try:
            handle, _ = store.create_run(
                make_request(request_id="frozen-recovery-boundary"),
                frozen_route_binding=binding(),
            )
            self.assertEqual(store.recover_interrupted_runs(), [])
            self.assertEqual(store.get_status(handle.run_id), RunStatus.CREATED)

            self.assertEqual(
                store.begin_frozen_run_with_dispatch_lease(
                    handle.run_id, dispatch_epoch="epoch-boundary"
                ),
                RunStatus.RUNNING,
            )
            self.assertTrue(store.has_model_invocation_dispatch_lease(handle.run_id))
            self.assertEqual(store.recover_interrupted_runs(), [])
            self.assertEqual(store.get_status(handle.run_id), RunStatus.RUNNING)
        finally:
            store.close()

    def test_frozen_dispatcher_lease_first_creation_requires_created_status(self) -> None:
        """A missing lease never turns a later lifecycle state into a takeover."""

        store = RunStore()
        try:
            for status in (
                RunStatus.RUNNING,
                RunStatus.WAITING_APPROVAL,
                RunStatus.CANCELLING,
            ):
                handle, _ = store.create_run(
                    make_request(request_id=f"frozen-lease-{status.value.lower()}"),
                    frozen_route_binding=binding(),
                )
                with store._transaction() as conn:  # noqa: SLF001 - lifecycle corruption guard
                    conn.execute(
                        "UPDATE employee_runs SET status = ? WHERE run_id = ?",
                        (status.value, handle.run_id),
                    )
                event_seq = store.get_last_seq(handle.run_id)
                with self.assertRaises(FrozenDispatcherLeaseConflict):
                    store.begin_frozen_run_with_dispatch_lease(
                        handle.run_id, dispatch_epoch="contender-epoch"
                    )
                self.assertEqual(store.get_status(handle.run_id), status)
                self.assertEqual(store.get_last_seq(handle.run_id), event_seq)
                self.assertFalse(store.has_model_invocation_dispatch_lease(handle.run_id))
        finally:
            store.close()

    def test_local_resume_envelope_is_content_free_verified_and_non_dispatchable(self) -> None:
        store = RunStore()
        saved = store.save_local_resume_envelope(
            job_id="job-resume-envelope",
            work_order_digest="a" * 64,
            graph_digest="b" * 64,
            references={"authority_digest": "c" * 64},
        )

        candidate = store.recovery_candidate("job-resume-envelope")

        self.assertEqual(saved["status"], "PENDING")
        self.assertEqual(
            store.save_local_resume_envelope(
                job_id="job-resume-envelope",
                work_order_digest="a" * 64,
                graph_digest="b" * 64,
                references={"authority_digest": "c" * 64},
            ),
            saved,
        )
        with self.assertRaisesRegex(ValueError, "cannot be replaced"):
            store.save_local_resume_envelope(
                job_id="job-resume-envelope",
                work_order_digest="a" * 64,
                graph_digest="d" * 64,
                references={"authority_digest": "c" * 64},
            )
        self.assertEqual(candidate["references"], {"authority_digest": "c" * 64})
        self.assertFalse(candidate["dispatch_allowed"])
        self.assertEqual(
            candidate["required_checks"],
            ("source_hashes", "approval_receipts", "budget_lease", "active_job_audit"),
        )
        store.finalize_local_resume_envelope("job-resume-envelope")
        with self.assertRaisesRegex(ValueError, "non-terminal"):
            store.recovery_candidate("job-resume-envelope")
        store.close()

    def test_corrupted_local_resume_envelope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            store.save_local_resume_envelope(
                job_id="job-resume-corrupt",
                work_order_digest="a" * 64,
                graph_digest="b" * 64,
                references={"authority_digest": "c" * 64},
            )
            store.close()

            with sqlite3.connect(path) as conn:
                conn.execute(
                    "UPDATE local_resume_envelopes SET graph_digest = ? WHERE job_id = ?",
                    ("d" * 64, "job-resume-corrupt"),
                )

            reopened = RunStore(path)
            with self.assertRaisesRegex(RuntimeError, "integrity mismatch"):
                reopened.recovery_candidate("job-resume-corrupt")
            reopened.close()

    def test_employee_session_projection_is_bounded_redacted_and_revisioned(self) -> None:
        store = RunStore()
        request = make_request(request_id="session-turn-1")
        handle, _ = store.create_run(request)
        store.begin_run(handle.run_id)
        namespace = employee_session_namespace(
            request.employee.employee_id,
            "conversation-1",
        )

        store.terminalize(
            self._success_result(request, handle),
            EventType.RUN_SUCCEEDED,
            {},
            employee_session=EmployeeSessionUpdate(
                namespace_hash=namespace,
                employee_id=request.employee.employee_id,
                expected_revision=0,
                messages=(
                    {"role": "user", "content": "old turn"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "new turn"},
                    {
                        "role": "assistant",
                        "content": "api_key=sk-session-secret-1234567890",
                        "_db_persisted": True,
                    },
                ),
                max_messages=2,
                max_chars=10_000,
            ),
        )

        snapshot = store.load_employee_session(namespace, request.employee.employee_id)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(snapshot.message_count, 2)
        self.assertEqual(snapshot.last_run_id, handle.run_id)
        self.assertEqual(snapshot.messages[0]["content"], "new turn")
        self.assertNotIn("_db_persisted", snapshot.messages[1])
        self.assertNotIn("sk-session-secret", str(snapshot.messages))
        self.assertNotEqual(
            namespace,
            employee_session_namespace("employee-reviewer", "conversation-1"),
        )
        store.close()

    def test_employee_session_character_bound_keeps_a_complete_recent_turn(self) -> None:
        store = RunStore()
        request = make_request(request_id="session-char-bound")
        handle, _ = store.create_run(request)
        store.begin_run(handle.run_id)
        namespace = employee_session_namespace(request.employee.employee_id, "bounded")

        store.terminalize(
            self._success_result(request, handle),
            EventType.RUN_SUCCEEDED,
            {},
            employee_session=EmployeeSessionUpdate(
                namespace_hash=namespace,
                employee_id=request.employee.employee_id,
                expected_revision=0,
                messages=(
                    {"role": "user", "content": "x" * 1_000},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "recent"},
                    {"role": "assistant", "content": "recent answer"},
                ),
                max_messages=32,
                max_chars=120,
            ),
        )

        snapshot = store.load_employee_session(namespace, request.employee.employee_id)

        self.assertEqual(snapshot.message_count, 2)
        self.assertEqual(
            [message["content"] for message in snapshot.messages],
            ["recent", "recent answer"],
        )
        store.close()

    def test_employee_session_conflict_rolls_back_terminal_success(self) -> None:
        store = RunStore()
        first = make_request(request_id="session-cas-1")
        first_handle, _ = store.create_run(first)
        store.begin_run(first_handle.run_id)
        namespace = employee_session_namespace(first.employee.employee_id, "shared")
        update = EmployeeSessionUpdate(
            namespace_hash=namespace,
            employee_id=first.employee.employee_id,
            expected_revision=0,
            messages=(
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"},
            ),
            max_messages=32,
            max_chars=10_000,
        )
        store.terminalize(
            self._success_result(first, first_handle),
            EventType.RUN_SUCCEEDED,
            {},
            employee_session=update,
        )
        second = replace(
            first,
            request_id="session-cas-2",
            task=replace(first.task, task_id="session-cas-task-2"),
        )
        second_handle, _ = store.create_run(second)
        store.begin_run(second_handle.run_id)

        with self.assertRaises(EmployeeSessionConflict):
            store.terminalize(
                self._success_result(second, second_handle),
                EventType.RUN_SUCCEEDED,
                {},
                employee_session=replace(update, messages=tuple(update.messages)),
            )

        self.assertEqual(store.get_status(second_handle.run_id), RunStatus.RUNNING)
        self.assertIsNone(store.get_result(second_handle.run_id))
        self.assertEqual(
            store.load_employee_session(namespace, first.employee.employee_id).revision,
            1,
        )
        store.close()

    def test_runtime_schema_v2_adds_employee_sessions_without_rewriting_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            request = make_request(request_id="pre-session-schema-run")
            store = RunStore(path)
            handle, _ = store.create_run(request)
            store.close()
            with sqlite3.connect(path) as connection:
                request_json = json.loads(
                    connection.execute(
                        "SELECT request_json FROM employee_runs WHERE run_id = ?",
                        (handle.run_id,),
                    ).fetchone()[0]
                )
                request_json["session_key"] = "legacy-session-key"
                connection.execute(
                    "UPDATE employee_runs SET request_json = ? WHERE run_id = ?",
                    (json.dumps(request_json), handle.run_id),
                )
                connection.execute("DROP TABLE employee_session_state")
                connection.execute(
                    "UPDATE runtime_meta SET value = '2' WHERE key = 'schema_version'"
                )

            migrated = RunStore(path)

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(migrated.get_run(handle.run_id))
            with sqlite3.connect(path) as connection:
                table = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name = 'employee_session_state'
                    """
                ).fetchone()
                migrated_request = connection.execute(
                    "SELECT request_json FROM employee_runs WHERE run_id = ?",
                    (handle.run_id,),
                ).fetchone()[0]
            self.assertEqual(table[0], "employee_session_state")
            self.assertNotIn("legacy-session-key", migrated_request)
            self.assertIn("namespace_hash", migrated_request)
            migrated.close()

    def test_employee_run_idempotency_snapshot_does_not_store_raw_session_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            request = replace(
                make_request(request_id="private-session-request"),
                session_key="private-product-session",
            )
            store = RunStore(path)

            first, first_created = store.create_run(request)
            duplicate, duplicate_created = store.create_run(request)
            stored = store.get_run(first.run_id)["request_json"]

            self.assertTrue(first_created)
            self.assertFalse(duplicate_created)
            self.assertEqual(first, duplicate)
            self.assertNotIn("private-product-session", stored)
            self.assertIn(
                employee_session_namespace(
                    request.employee.employee_id,
                    request.session_key,
                ),
                stored,
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
