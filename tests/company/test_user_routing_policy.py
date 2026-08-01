import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.user_routing_policy import (
    ApprovedRouteMetadata,
    ApprovedRouteRegistry,
    ApprovedRouteReuseDecision,
    RouteReuseDisposition,
    UserRoutingPolicy,
    UserRoutingPolicyMode,
    decide_approved_route_reuse,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def registry() -> ApprovedRouteRegistry:
    return ApprovedRouteRegistry((
        ApprovedRouteMetadata("route-b", DIGEST_B, DIGEST_B, "SECONDARY_PROVIDER_KEY"),
        ApprovedRouteMetadata("route-a", DIGEST_A, DIGEST_A, "PRIMARY_PROVIDER_KEY"),
    ))


class UserRoutingPolicyTests(unittest.TestCase):
    def test_all_four_explicit_modes_are_closed_and_immutable(self) -> None:
        self.assertEqual(
            set(UserRoutingPolicyMode),
            {
                UserRoutingPolicyMode.QUALITY_FIRST,
                UserRoutingPolicyMode.BALANCED,
                UserRoutingPolicyMode.EFFICIENT,
                UserRoutingPolicyMode.PRIVATE_LOCAL_FIRST,
            },
        )
        with self.assertRaises(AttributeError):
            UserRoutingPolicy(UserRoutingPolicyMode.QUALITY_FIRST).mode = UserRoutingPolicyMode.BALANCED  # type: ignore[misc]

    def test_existing_approved_route_reuses_without_activation_and_is_content_free(self) -> None:
        decision = decide_approved_route_reuse(
            UserRoutingPolicy(UserRoutingPolicyMode.QUALITY_FIRST), registry(), "route-a"
        )
        self.assertEqual(decision.disposition, RouteReuseDisposition.REUSED_QUALITY_FIRST)
        self.assertEqual(decision.selected_route_id, "route-a")
        self.assertEqual(set(decision.__dataclass_fields__), {
            "requested_route_id", "selected_route_id", "disposition", "policy_digest", "registry_digest",
        })
        self.assertNotIn("credential", decision.canonical_json())
        self.assertNotIn("activate", decision.canonical_json())

    def test_missing_route_fails_closed_without_a_fifth_user_profile(self) -> None:
        policy = UserRoutingPolicy(UserRoutingPolicyMode.BALANCED)
        denied = decide_approved_route_reuse(policy, registry(), "not-approved")
        self.assertEqual(denied.disposition, RouteReuseDisposition.DENIED_MISSING_APPROVAL)
        self.assertIsNone(denied.selected_route_id)

    def test_empty_registry_is_a_distinct_fail_closed_first_run_journey(self) -> None:
        empty = ApprovedRouteRegistry(())
        denied = decide_approved_route_reuse(
            UserRoutingPolicy(UserRoutingPolicyMode.PRIVATE_LOCAL_FIRST), empty, "first-route"
        )
        self.assertEqual(denied.disposition, RouteReuseDisposition.DENIED_FIRST_RUN_NO_APPROVED_ROUTES)
        self.assertIsNone(denied.selected_route_id)
        self.assertEqual(ApprovedRouteRegistry.from_canonical_json(empty.canonical_json()), empty)
        self.assertEqual(ApprovedRouteReuseDecision.from_canonical_json(denied.canonical_json()), denied)
        self.assertNotIn("credential", denied.canonical_json())

    def test_policy_switch_changes_decision_not_registry_and_rollback_is_deterministic(self) -> None:
        approved = registry()
        prior = UserRoutingPolicy(UserRoutingPolicyMode.QUALITY_FIRST)
        switched = UserRoutingPolicy(UserRoutingPolicyMode.BALANCED)
        first = decide_approved_route_reuse(prior, approved, "route-a")
        changed = decide_approved_route_reuse(switched, approved, "route-a")
        restored = decide_approved_route_reuse(prior, approved, "route-a")
        self.assertEqual(approved.digest, registry().digest)
        self.assertNotEqual(first.policy_digest, changed.policy_digest)
        self.assertNotEqual(first.digest, changed.digest)
        self.assertEqual(first, restored)

    def test_canonical_round_trips_and_rejects_unknown_missing_noncanonical_and_unsafe_values(self) -> None:
        approved = registry()
        policy = UserRoutingPolicy(UserRoutingPolicyMode.PRIVATE_LOCAL_FIRST)
        decision = decide_approved_route_reuse(policy, approved, "route-a")
        self.assertEqual(ApprovedRouteRegistry.from_canonical_json(approved.canonical_json()), approved)
        self.assertEqual(UserRoutingPolicy.from_canonical_json(policy.canonical_json()), policy)
        self.assertEqual(ApprovedRouteReuseDecision.from_canonical_json(decision.canonical_json()), decision)
        with self.assertRaises(ValueError):
            ApprovedRouteRegistry.from_canonical_json(json.dumps(approved.canonical_payload()))
        with self.assertRaises(ValueError):
            UserRoutingPolicy.from_canonical_json('{"unknown":"value"}')
        with self.assertRaises(ValueError):
            ApprovedRouteReuseDecision.from_canonical_json('{"requested_route_id":"route-a"}')
        with self.assertRaises(ValueError):
            ApprovedRouteMetadata("unsafe route", DIGEST_A, DIGEST_A, "PRIMARY_PROVIDER_KEY")
        with self.assertRaises(ValueError):
            ApprovedRouteMetadata("route-a", DIGEST_A, "A" * 64, "PRIMARY_PROVIDER_KEY")
        with self.assertRaises(ValueError):
            ApprovedRouteMetadata("route-a", DIGEST_A, DIGEST_A, "literal-secret-value")


if __name__ == "__main__":
    unittest.main()
