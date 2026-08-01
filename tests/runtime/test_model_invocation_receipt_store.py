from __future__ import annotations

import json
import tempfile
import unittest
import argparse
import io
from dataclasses import replace
from pathlib import Path

from dynamic_firm.company.model_invocation_receipt import ModelInvocationReceipt
from dynamic_firm.application.job_cli import run_job_command
from dynamic_firm.runtime.models import (
    EmployeeRunResult,
    EventType,
    RunStatus,
    Usage,
    utc_now,
)
from dynamic_firm.runtime.store import RunStore
from tests.runtime.test_frozen_run_route_store import binding
from tests.runtime.helpers import make_request


def receipt(invocation_id: str = "call-1", **changes: object) -> ModelInvocationReceipt:
    values: dict[str, object] = {
        "invocation_id": invocation_id,
        "route_binding_digest": binding().digest,
        "context_projection_digest": "b" * 64,
        "attempt_id": "attempt-1",
        "fanout_parent_id": None,
        "terminal_status": "SUCCEEDED",
        "output_digest": "c" * 64,
        "usage_availability": "AVAILABLE",
        "usage_units": 0,
        "cost_availability": "UNAVAILABLE",
        "cost_usd": None,
        "latency_ms": 0,
    }
    values.update(changes)
    return ModelInvocationReceipt(**values)


class ModelInvocationReceiptStoreTests(unittest.TestCase):
    @staticmethod
    def _succeeded(request, handle) -> EmployeeRunResult:
        return EmployeeRunResult(
            run_id=handle.run_id,
            request_id=handle.request_id,
            job_id=request.task.job_id,
            task_id=request.task.task_id,
            employee_id=request.employee.employee_id,
            status=RunStatus.SUCCEEDED,
            summary="done",
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

    def test_persists_canonically_reopens_and_orders_stably(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            try:
                handle, _ = store.create_run(
                    make_request(request_id="receipt-persist"),
                    frozen_route_binding=binding(),
                )
                later = receipt("call-2")
                earlier = receipt("call-1", output_digest="d" * 64)
                self.assertTrue(store.store_model_invocation_receipt(handle.run_id, later))
                self.assertTrue(store.store_model_invocation_receipt(handle.run_id, earlier))
                self.assertEqual(
                    store.list_model_invocation_receipts(handle.run_id), [earlier, later]
                )
            finally:
                store.close()
            reopened = RunStore(path)
            try:
                self.assertEqual(
                    reopened.list_model_invocation_receipts(handle.run_id), [earlier, later]
                )
            finally:
                reopened.close()

    def test_exact_retry_is_idempotent_and_different_receipt_conflicts(self) -> None:
        store = RunStore()
        try:
            handle, _ = store.create_run(
                make_request(request_id="receipt-retry"), frozen_route_binding=binding()
            )
            expected = receipt()
            self.assertTrue(store.store_model_invocation_receipt(handle.run_id, expected))
            self.assertFalse(store.store_model_invocation_receipt(handle.run_id, expected))
            with self.assertRaises(ValueError):
                store.store_model_invocation_receipt(
                    handle.run_id, replace(expected, output_digest="d" * 64)
                )
        finally:
            store.close()

    def test_reopen_refuses_unresolved_prior_epoch_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            try:
                handle, _ = store.create_run(
                    make_request(request_id="receipt-reservation-reopen"),
                    frozen_route_binding=binding(),
                )
                self.assertTrue(
                    store.acquire_model_invocation_dispatch_lease(
                        handle.run_id, dispatch_epoch="epoch-first"
                    )
                )
                first = store.reserve_model_invocation_dispatch(
                    handle.run_id,
                    dispatch_epoch="epoch-first",
                    route_binding_digest=binding().digest,
                    context_projection_digest="b" * 64,
                    attempt_id="attempt-1",
                )
                self.assertEqual(store.list_model_invocation_dispatch_reservations(handle.run_id), [first])
            finally:
                store.close()
            reopened = RunStore(path)
            try:
                with self.assertRaisesRegex(ValueError, "different-epoch"):
                    reopened.reserve_model_invocation_dispatch(
                        handle.run_id,
                        dispatch_epoch="epoch-second",
                        route_binding_digest=binding().digest,
                        context_projection_digest="d" * 64,
                        attempt_id="attempt-1",
                    )
                self.assertEqual(reopened.list_model_invocation_receipts(handle.run_id), [])
                self.assertEqual(
                    reopened.list_model_invocation_dispatch_reservations(handle.run_id), [first]
                )
            finally:
                reopened.close()

    def test_live_different_epoch_refuses_without_receipt_or_reservation_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            first_store = RunStore(path)
            try:
                handle, _ = first_store.create_run(
                    make_request(request_id="receipt-live-epoch-refusal"),
                    frozen_route_binding=binding(),
                )
                self.assertTrue(
                    first_store.acquire_model_invocation_dispatch_lease(
                        handle.run_id, dispatch_epoch="epoch-live-one"
                    )
                )
                first = first_store.reserve_model_invocation_dispatch(
                    handle.run_id,
                    dispatch_epoch="epoch-live-one",
                    route_binding_digest=binding().digest,
                    context_projection_digest="b" * 64,
                    attempt_id="attempt-1",
                )
                second_store = RunStore(path)
                try:
                    with self.assertRaisesRegex(ValueError, "different-epoch"):
                        second_store.reserve_model_invocation_dispatch(
                            handle.run_id,
                            dispatch_epoch="epoch-live-two",
                            route_binding_digest=binding().digest,
                            context_projection_digest="d" * 64,
                            attempt_id="attempt-1",
                        )
                    self.assertEqual(second_store.list_model_invocation_receipts(handle.run_id), [])
                    self.assertEqual(
                        second_store.list_model_invocation_dispatch_reservations(handle.run_id),
                        [first],
                    )
                finally:
                    second_store.close()
            finally:
                first_store.close()

    def test_same_epoch_reservations_coexist_and_finalize_independently(self) -> None:
        store = RunStore()
        try:
            handle, _ = store.create_run(
                make_request(request_id="receipt-same-epoch-fanout"),
                frozen_route_binding=binding(),
            )
            self.assertTrue(
                store.acquire_model_invocation_dispatch_lease(
                    handle.run_id, dispatch_epoch="epoch-fanout"
                )
            )
            first = store.reserve_model_invocation_dispatch(
                handle.run_id,
                dispatch_epoch="epoch-fanout",
                route_binding_digest=binding().digest,
                context_projection_digest="b" * 64,
                attempt_id="attempt-1",
            )
            second = store.reserve_model_invocation_dispatch(
                handle.run_id,
                dispatch_epoch="epoch-fanout",
                route_binding_digest=binding().digest,
                context_projection_digest="d" * 64,
                attempt_id="attempt-1",
            )
            self.assertEqual(
                store.list_model_invocation_dispatch_reservations(handle.run_id),
                sorted([first, second]),
            )
            self.assertTrue(store.finalize_model_invocation_receipt(
                handle.run_id, receipt(first, context_projection_digest="b" * 64)
            ))
            self.assertTrue(store.finalize_model_invocation_receipt(
                handle.run_id, receipt(second, context_projection_digest="d" * 64)
            ))
            self.assertEqual(store.list_model_invocation_dispatch_reservations(handle.run_id), [])
            self.assertEqual(len(store.list_model_invocation_receipts(handle.run_id)), 2)
        finally:
            store.close()

    def test_dispatcher_lease_refuses_foreign_epoch_and_releases_only_after_clean_terminal_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            owner = RunStore(path)
            try:
                request = make_request(request_id="receipt-dispatcher-lease")
                handle, _ = owner.create_run(request, frozen_route_binding=binding())
                self.assertTrue(
                    owner.acquire_model_invocation_dispatch_lease(
                        handle.run_id, dispatch_epoch="epoch-owner"
                    )
                )
                self.assertFalse(
                    owner.acquire_model_invocation_dispatch_lease(
                        handle.run_id, dispatch_epoch="epoch-owner"
                    )
                )
                reservation = owner.reserve_model_invocation_dispatch(
                    handle.run_id,
                    dispatch_epoch="epoch-owner",
                    route_binding_digest=binding().digest,
                    context_projection_digest="b" * 64,
                    attempt_id="attempt-1",
                )
                self.assertFalse(
                    owner.release_model_invocation_dispatch_lease(
                        handle.run_id, dispatch_epoch="epoch-owner"
                    )
                )
                contender = RunStore(path)
                try:
                    with self.assertRaisesRegex(ValueError, "different-epoch dispatcher lease"):
                        contender.acquire_model_invocation_dispatch_lease(
                            handle.run_id, dispatch_epoch="epoch-contender"
                        )
                    with self.assertRaisesRegex(ValueError, "different-epoch dispatcher lease"):
                        contender.reserve_model_invocation_dispatch(
                            handle.run_id,
                            dispatch_epoch="epoch-contender",
                            route_binding_digest=binding().digest,
                            context_projection_digest="d" * 64,
                            attempt_id="attempt-1",
                        )
                    self.assertEqual(contender.list_model_invocation_receipts(handle.run_id), [])
                    self.assertEqual(
                        contender.list_model_invocation_dispatch_reservations(handle.run_id),
                        [reservation],
                    )
                finally:
                    contender.close()
                owner.finalize_model_invocation_receipt(handle.run_id, receipt(reservation))
                owner.begin_run(handle.run_id)
                owner.terminalize(
                    self._succeeded(request, handle), EventType.RUN_SUCCEEDED, {}
                )
                self.assertTrue(
                    owner.release_model_invocation_dispatch_lease(
                        handle.run_id, dispatch_epoch="epoch-owner"
                    )
                )
                self.assertFalse(
                    owner.has_model_invocation_dispatch_lease(handle.run_id)
                )
            finally:
                owner.close()

    def test_explicit_frozen_recovery_seals_abandoned_reservation_without_replay(self) -> None:
        store = RunStore()
        try:
            request = make_request(request_id="receipt-explicit-frozen-recovery")
            handle, _ = store.create_run(request, frozen_route_binding=binding())
            store.begin_frozen_run_with_dispatch_lease(handle.run_id, dispatch_epoch="epoch-lost")
            invocation_id = store.reserve_model_invocation_dispatch(
                handle.run_id,
                dispatch_epoch="epoch-lost",
                route_binding_digest=binding().digest,
                context_projection_digest="b" * 64,
                attempt_id="attempt-1",
            )
            inspection = store.inspect_frozen_run_recovery(handle.run_id)
            self.assertTrue(inspection.dispatch_epoch_present)
            self.assertEqual(inspection.outstanding_invocation_ids, (invocation_id,))
            with self.assertRaisesRegex(ValueError, "explicit operator confirmation"):
                store.claim_and_terminalize_frozen_run(
                    handle.run_id,
                    expected_binding_digest=binding().digest,
                    recovery_id="recovery-fixture",
                    operator_confirmed_abandoned=False,
                )
            result = store.claim_and_terminalize_frozen_run(
                handle.run_id,
                expected_binding_digest=binding().digest,
                recovery_id="recovery-fixture",
                operator_confirmed_abandoned=True,
            )
            self.assertEqual(result.failure.code, "FROZEN_DISPATCHER_ABANDONED")
            self.assertFalse(store.has_model_invocation_dispatch_lease(handle.run_id))
            receipts = store.list_model_invocation_receipts(handle.run_id)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].terminal_status.value, "INDETERMINATE")
            self.assertEqual(receipts[0].safe_error_code, "DISPATCHER_ABANDONED")
            with self.assertRaisesRegex(ValueError, "sealed by frozen run recovery"):
                store.finalize_model_invocation_receipt(
                    handle.run_id,
                    receipt(invocation_id),
                )
            with self.assertRaisesRegex(ValueError, "reservations require a nonterminal run"):
                store.reserve_model_invocation_dispatch(
                    handle.run_id,
                    dispatch_epoch="epoch-new",
                    route_binding_digest=binding().digest,
                    context_projection_digest="c" * 64,
                    attempt_id="attempt-1",
                )
        finally:
            store.close()

    def test_cli_frozen_run_seal_requires_confirmation_and_exact_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            try:
                request = make_request(request_id="receipt-cli-frozen-recovery")
                handle, _ = store.create_run(request, frozen_route_binding=binding())
                store.begin_frozen_run_with_dispatch_lease(handle.run_id, dispatch_epoch="epoch-lost")
                store.reserve_model_invocation_dispatch(
                    handle.run_id, dispatch_epoch="epoch-lost",
                    route_binding_digest=binding().digest,
                    context_projection_digest="b" * 64, attempt_id="attempt-1",
                )
            finally:
                store.close()
            args = argparse.Namespace(
                job_command="frozen-run-seal", job_id=request.task.job_id,
                run_id=handle.run_id, binding_digest=binding().digest,
                recovery_id="cli-recovery", confirm=False, json=True,
            )
            with self.assertRaisesRegex(ValueError, "requires --confirm"):
                run_job_command(args, state_path=path, settings={}, output=io.StringIO())
            args.confirm = True
            output = io.StringIO()
            self.assertEqual(
                run_job_command(args, state_path=path, settings={}, output=output), 0
            )
            self.assertEqual(json.loads(output.getvalue())["replay"], "PROHIBITED")

    def test_unknown_unbound_and_foreign_binding_fail_closed(self) -> None:
        store = RunStore()
        try:
            with self.assertRaises(KeyError):
                store.store_model_invocation_receipt("unknown-run", receipt())
            legacy, _ = store.create_run(make_request(request_id="receipt-legacy"))
            with self.assertRaises(ValueError):
                store.store_model_invocation_receipt(legacy.run_id, receipt())

            bound, _ = store.create_run(
                make_request(request_id="receipt-foreign"), frozen_route_binding=binding()
            )
            foreign = receipt(route_binding_digest=binding("route-b").digest)
            with self.assertRaises(ValueError):
                store.store_model_invocation_receipt(bound.run_id, foreign)
        finally:
            store.close()

    def test_noncanonical_digest_tamper_and_foreign_persisted_binding_fail_closed(self) -> None:
        store = RunStore()
        try:
            handle, _ = store.create_run(
                make_request(request_id="receipt-tamper"), frozen_route_binding=binding()
            )
            expected = receipt()
            store.store_model_invocation_receipt(handle.run_id, expected)
            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                "UPDATE employee_run_model_invocation_receipts SET receipt_json = ? WHERE run_id = ?",
                (json.dumps(expected.canonical_payload()), handle.run_id),
            )
            with self.assertRaises(ValueError):
                store.list_model_invocation_receipts(handle.run_id)

            second, _ = store.create_run(
                make_request(request_id="receipt-digest"), frozen_route_binding=binding()
            )
            store.store_model_invocation_receipt(second.run_id, expected)
            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                "UPDATE employee_run_model_invocation_receipts SET receipt_digest = ? WHERE run_id = ?",
                ("e" * 64, second.run_id),
            )
            with self.assertRaises(ValueError):
                store.list_model_invocation_receipts(second.run_id)

            third, _ = store.create_run(
                make_request(request_id="receipt-foreign-persisted"),
                frozen_route_binding=binding(),
            )
            store.store_model_invocation_receipt(third.run_id, expected)
            foreign = receipt(route_binding_digest=binding("route-b").digest)
            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                """
                UPDATE employee_run_model_invocation_receipts
                SET receipt_json = ?, receipt_digest = ?
                WHERE run_id = ?
                """,
                (foreign.canonical_json(), foreign.digest, third.run_id),
            )
            with self.assertRaises(ValueError):
                store.list_model_invocation_receipts(third.run_id)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
