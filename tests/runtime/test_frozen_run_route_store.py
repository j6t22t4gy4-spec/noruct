from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.frozen_route_admission import FrozenRouteAdmission
from dynamic_firm.company.route_selection_receipt import (
    RouteCandidateReceipt,
    RouteSelectionReceipt,
    SelectionReason,
)
from dynamic_firm.runtime.store import RunStore
from tests.runtime.helpers import make_request


def binding(route_id: str = "route-a") -> ExecutionRouteBinding:
    values: dict[str, object] = {
        "attempt_id": "attempt-1",
        "route_id": route_id,
        "execution_profile_id": "profile-a",
        "provider_config_digest": "a" * 64,
        "credential_reference": "NORUCT_PROVIDER_KEY",
        "requested_model_id": "model-a",
        "identity_assurance": "VERSIONED_MODEL_ID",
    }
    values.update(
        {
            name: "b" * 64
            for name in (
                "required_capability_digest",
                "inference_contract_digest",
                "egress_policy_digest",
                "intelligence_snapshot_digest",
                "orchestration_policy_digest",
                "compatibility_evidence_digest",
                "fallback_policy_digest",
                "fanout_policy_digest",
                "continuation_policy_digest",
            )
        }
    )
    return ExecutionRouteBinding(**values)


def admission(route_id: str = "route-a") -> FrozenRouteAdmission:
    expected_binding = binding(route_id)
    return FrozenRouteAdmission(
        binding=expected_binding,
        selection_receipt=RouteSelectionReceipt(
            candidates=(RouteCandidateReceipt(route_id),),
            selected_route_id=route_id,
            selection_reasons=(SelectionReason.POLICY_ORDER,),
            policy_digest=expected_binding.orchestration_policy_digest,
        ),
    )


class FrozenRunRouteStoreTests(unittest.TestCase):
    def test_binding_persists_across_reopen_and_idempotent_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            store = RunStore(path)
            try:
                request = make_request(request_id="frozen-route")
                expected = binding()
                handle, created = store.create_run(request, frozen_route_binding=expected)
                retried, retry_created = store.create_run(
                    request, frozen_route_binding=expected
                )
                self.assertTrue(created)
                self.assertFalse(retry_created)
                self.assertEqual(retried, handle)
            finally:
                store.close()
            reopened = RunStore(path)
            try:
                self.assertEqual(reopened.get_frozen_route_binding(handle.run_id), expected)
            finally:
                reopened.close()

    def test_binding_mismatch_and_bound_unbound_retries_fail_closed(self) -> None:
        store = RunStore()
        try:
            request = make_request(request_id="binding-mismatch")
            store.create_run(request, frozen_route_binding=binding())
            with self.assertRaises(ValueError):
                store.create_run(request, frozen_route_binding=binding("route-b"))
            with self.assertRaises(ValueError):
                store.create_run(request)

            unbound_request = make_request(request_id="unbound")
            handle, created = store.create_run(unbound_request)
            self.assertTrue(created)
            self.assertIsNone(store.get_frozen_route_binding(handle.run_id))
            with self.assertRaises(ValueError):
                store.create_run(unbound_request, frozen_route_binding=binding())
        finally:
            store.close()

    def test_tampered_binding_or_digest_and_unknown_run_fail_closed(self) -> None:
        store = RunStore()
        try:
            handle, _ = store.create_run(
                make_request(request_id="tamper"), frozen_route_binding=binding()
            )
            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                "UPDATE employee_run_frozen_routes SET binding_digest = ? WHERE run_id = ?",
                ("c" * 64, handle.run_id),
            )
            with self.assertRaises(ValueError):
                store.get_frozen_route_binding(handle.run_id)
            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                "UPDATE employee_run_frozen_routes SET binding_json = ? WHERE run_id = ?",
                ("{malformed", handle.run_id),
            )
            with self.assertRaises(ValueError):
                store.get_frozen_route_binding(handle.run_id)
            with self.assertRaises(KeyError):
                store.get_frozen_route_binding("unknown-run")
        finally:
            store.close()

    def test_admission_persists_canonically_across_reopen_and_exact_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            expected = admission()
            store = RunStore(path)
            try:
                request = make_request(request_id="frozen-admission")
                handle, created = store.create_run(
                    request, frozen_route_admission=expected
                )
                retried, retry_created = store.create_run(
                    request,
                    frozen_route_binding=expected.binding,
                    frozen_route_admission=expected,
                )
                self.assertTrue(created)
                self.assertFalse(retry_created)
                self.assertEqual(retried, handle)
                self.assertEqual(store.get_frozen_route_binding(handle.run_id), expected.binding)
            finally:
                store.close()
            reopened = RunStore(path)
            try:
                self.assertEqual(
                    reopened.get_frozen_route_admission(handle.run_id), expected
                )
            finally:
                reopened.close()

    def test_admission_binding_and_legacy_transitions_fail_closed(self) -> None:
        store = RunStore()
        try:
            expected = admission()
            request = make_request(request_id="admission-transition")
            store.create_run(request, frozen_route_admission=expected)
            with self.assertRaises(ValueError):
                store.create_run(request, frozen_route_binding=expected.binding)
            with self.assertRaises(ValueError):
                store.create_run(request)
            with self.assertRaises(ValueError):
                store.create_run(
                    make_request(request_id="admission-binding-mismatch"),
                    frozen_route_binding=binding("route-b"),
                    frozen_route_admission=expected,
                )

            legacy_request = make_request(request_id="legacy-bound")
            legacy_handle, _ = store.create_run(
                legacy_request, frozen_route_binding=expected.binding
            )
            self.assertIsNone(store.get_frozen_route_admission(legacy_handle.run_id))
            with self.assertRaises(ValueError):
                store.create_run(legacy_request, frozen_route_admission=expected)

            unbound_request = make_request(request_id="legacy-unbound")
            unbound_handle, _ = store.create_run(unbound_request)
            self.assertIsNone(store.get_frozen_route_admission(unbound_handle.run_id))
            with self.assertRaises(ValueError):
                store.create_run(unbound_request, frozen_route_admission=expected)
        finally:
            store.close()

    def test_tampered_admission_json_digest_or_binding_pair_fails_closed(self) -> None:
        store = RunStore()
        try:
            expected = admission()
            handle, _ = store.create_run(
                make_request(request_id="tamper-admission"),
                frozen_route_admission=expected,
            )
            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                "UPDATE employee_run_frozen_route_admissions SET admission_digest = ? WHERE run_id = ?",
                ("c" * 64, handle.run_id),
            )
            with self.assertRaises(ValueError):
                store.get_frozen_route_admission(handle.run_id)
            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                "UPDATE employee_run_frozen_route_admissions SET admission_digest = ? WHERE run_id = ?",
                (expected.digest, handle.run_id),
            )
            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                "UPDATE employee_run_frozen_route_admissions SET admission_json = ? WHERE run_id = ?",
                ("{malformed", handle.run_id),
            )
            with self.assertRaises(ValueError):
                store.get_frozen_route_admission(handle.run_id)

            second, _ = store.create_run(
                make_request(request_id="tamper-admission-pair"),
                frozen_route_admission=expected,
            )
            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                "UPDATE employee_run_frozen_routes SET binding_json = ?, binding_digest = ? WHERE run_id = ?",
                (binding("route-b").canonical_json(), binding("route-b").digest, second.run_id),
            )
            with self.assertRaises(ValueError):
                store.get_frozen_route_admission(second.run_id)
        finally:
            store.close()

    def test_physical_admission_resolution_requires_exact_durable_pair(self) -> None:
        store = RunStore()
        try:
            expected = admission()
            request = make_request(request_id="physical-admission")
            handle, _ = store.create_run(request, frozen_route_admission=expected)
            self.assertEqual(store.resolve_frozen_route_admission(handle.run_id), expected)
            self.assertEqual(
                store.resolve_frozen_route_admission(request.request_id), expected
            )

            legacy, _ = store.create_run(
                make_request(request_id="physical-binding-only"),
                frozen_route_binding=expected.binding,
            )
            with self.assertRaises(ValueError):
                store.resolve_frozen_route_admission(legacy.run_id)

            store._conn.execute(  # noqa: SLF001 - persistence tamper contract
                "UPDATE employee_run_frozen_routes SET binding_json = ?, binding_digest = ? WHERE run_id = ?",
                (binding("route-b").canonical_json(), binding("route-b").digest, handle.run_id),
            )
            with self.assertRaises(ValueError):
                store.resolve_frozen_route_admission(request.request_id)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
