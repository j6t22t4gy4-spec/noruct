from __future__ import annotations

import json
import unittest

from dynamic_firm.company.route_bound_continuation import RouteBoundContinuation


def continuation(**changes: object) -> RouteBoundContinuation:
    values: dict[str, object] = {
        "continuation_id": "continuation-1",
        "job_id": "job-1",
        "prior_session_id": "session-1",
        "session_id": "session-1",
        "prior_route_binding_digest": "a" * 64,
        "route_binding_digest": "a" * 64,
        "prior_frozen_route_admission_digest": "e" * 64,
        "frozen_route_admission_digest": "e" * 64,
        "context_projection_digest": "b" * 64,
        "intelligence_snapshot_digest": "c" * 64,
        "policy_digest": "d" * 64,
        "route_state": "STABLE",
    }
    values.update(changes)
    return RouteBoundContinuation(**values)


class RouteBoundContinuationTests(unittest.TestCase):
    def test_stable_continuation_round_trips_with_a_stable_digest(self) -> None:
        value = continuation()

        self.assertEqual(value.canonical_bytes(), value.canonical_json().encode("utf-8"))
        self.assertEqual(RouteBoundContinuation.from_canonical_json(value.canonical_json()), value)
        self.assertEqual(continuation().digest, value.digest)

    def test_stable_state_rejects_session_route_or_admission_drift(self) -> None:
        with self.assertRaises(ValueError):
            continuation(session_id="session-2")
        with self.assertRaises(ValueError):
            continuation(route_binding_digest="e" * 64)
        with self.assertRaises(ValueError):
            continuation(frozen_route_admission_digest="f" * 64)

    def test_typed_route_rebound_requires_a_new_session_and_route(self) -> None:
        rebound = continuation(
            session_id="session-2",
            route_binding_digest="e" * 64,
            frozen_route_admission_digest="f" * 64,
            route_state="ROUTE_REBOUND",
        )

        self.assertEqual(rebound.route_state, "ROUTE_REBOUND")
        with self.assertRaises(ValueError):
            continuation(
                route_binding_digest="e" * 64,
                frozen_route_admission_digest="f" * 64,
                route_state="ROUTE_REBOUND",
            )

    def test_fresh_session_stable_route_requires_new_session_without_route_drift(self) -> None:
        continued = continuation(
            session_id="session-2",
            route_state="FRESH_SESSION_STABLE_ROUTE",
        )

        self.assertEqual(continued.route_state, "FRESH_SESSION_STABLE_ROUTE")
        self.assertEqual(
            RouteBoundContinuation.from_canonical_json(continued.canonical_json()),
            continued,
        )
        with self.assertRaises(ValueError):
            continuation(route_state="FRESH_SESSION_STABLE_ROUTE")
        with self.assertRaises(ValueError):
            continuation(
                session_id="session-2",
                route_binding_digest="e" * 64,
                route_state="FRESH_SESSION_STABLE_ROUTE",
            )
        with self.assertRaises(ValueError):
            continuation(
                session_id="session-2",
                frozen_route_admission_digest="f" * 64,
                route_state="FRESH_SESSION_STABLE_ROUTE",
            )
        with self.assertRaises(ValueError):
            continuation(
                session_id="session-2",
                frozen_route_admission_digest="f" * 64,
                route_state="ROUTE_REBOUND",
            )
        with self.assertRaises(ValueError):
            continuation(
                session_id="session-2",
                route_binding_digest="e" * 64,
                route_state="ROUTE_REBOUND",
            )

    def test_parser_rejects_unknown_content_and_malformed_values(self) -> None:
        payload = continuation().canonical_payload()
        payload["provider_thread_id"] = "raw-thread"
        with self.assertRaises(ValueError):
            RouteBoundContinuation.from_canonical_json(json.dumps(payload))
        with self.assertRaises(ValueError):
            continuation(context_projection_digest="not-a-digest")
        with self.assertRaises(ValueError):
            RouteBoundContinuation.from_canonical_json('{"unknown":true}')
