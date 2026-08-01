from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.company.user_routing_policy import (
    ApprovedRouteMetadata,
    ApprovedRouteRegistry,
    RouteReuseDisposition,
    UserRoutingPolicy,
    UserRoutingPolicyMode,
    decide_approved_route_reuse,
)
from dynamic_firm.product.local_routing_settings import (
    LocalRoutingSettings,
    first_run_local_routing_settings,
    load_local_routing_settings,
    write_local_routing_settings,
)


class LocalRoutingSettingsTests(unittest.TestCase):
    def _settings(self, mode: UserRoutingPolicyMode = UserRoutingPolicyMode.QUALITY_FIRST) -> LocalRoutingSettings:
        return LocalRoutingSettings(
            UserRoutingPolicy(mode),
            ApprovedRouteRegistry((ApprovedRouteMetadata(
                route_id="approved-route",
                execution_route_binding_digest="b" * 64,
                provider_config_digest="a" * 64,
                credential_reference="APPROVED_ROUTE_KEY",
            ),)),
        )

    def test_canonical_round_trip_is_deterministic_and_preserves_unrelated_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text("[unrelated]\nvalue = \"preserve-me\"\n", encoding="utf-8")
            settings = self._settings()
            write_local_routing_settings(path, settings)
            first = path.read_text(encoding="utf-8")
            restored = load_local_routing_settings(path)
            write_local_routing_settings(path, restored)
            second = path.read_text(encoding="utf-8")

        self.assertEqual(restored, settings)
        self.assertEqual(first, second)
        self.assertIn('[unrelated]\nvalue = "preserve-me"', first)

    def test_missing_file_or_table_is_explicit_first_run_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.toml"
            first_run = load_local_routing_settings(missing)
            present = Path(temporary) / "present.toml"
            present.write_text("[unrelated]\nvalue = 1\n", encoding="utf-8")
            table_missing = load_local_routing_settings(present)

        self.assertEqual(first_run, first_run_local_routing_settings())
        self.assertEqual(table_missing, first_run)
        self.assertEqual(first_run.approved_routes.routes, ())
        self.assertEqual(
            decide_approved_route_reuse(first_run.policy, first_run.approved_routes, "approved-route").disposition,
            RouteReuseDisposition.DENIED_FIRST_RUN_NO_APPROVED_ROUTES,
        )

    def test_policy_switch_and_caller_selected_prior_value_preserve_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            original = self._settings(UserRoutingPolicyMode.BALANCED)
            switched = LocalRoutingSettings(UserRoutingPolicy(UserRoutingPolicyMode.EFFICIENT), original.approved_routes)
            write_local_routing_settings(path, original)
            write_local_routing_settings(path, switched)
            self.assertEqual(load_local_routing_settings(path), switched)
            write_local_routing_settings(path, original)
            restored = load_local_routing_settings(path)

        self.assertEqual(restored, original)
        self.assertEqual(restored.approved_routes, switched.approved_routes)

    def test_rejects_unknown_noncanonical_duplicate_and_unsafe_route_metadata(self) -> None:
        bad_tables = (
            'policy = "{\\"mode\\":\\"NOPE\\"}"\napproved_routes = "{\\"routes\\":[]}"',
            'policy = "{\\"mode\\": \\"BALANCED\\"}"\napproved_routes = "{\\"routes\\":[]}"',
            'policy = "{\\"mode\\":\\"BALANCED\\",\\"mode\\":\\"EFFICIENT\\"}"\napproved_routes = "{\\"routes\\":[]}"',
            'policy = "{\\"mode\\":\\"BALANCED\\"}"\napproved_routes = "{\\"routes\\":[{\\"credential_reference\\":\\"sk-literal-secret\\",\\"provider_config_digest\\":\\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\",\\"route_id\\":\\"route\\"}]}"',
            'policy = "{\\"mode\\":\\"BALANCED\\"}"\napproved_routes = "{\\"routes\\":[],\\"extra\\":true}"',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            for table in bad_tables:
                path.write_text(f"[model_routing]\n{table}\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_local_routing_settings(path)


if __name__ == "__main__":
    unittest.main()
