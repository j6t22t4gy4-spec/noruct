from __future__ import annotations

import unittest
from types import SimpleNamespace

from dynamic_firm.runtime.models import CostEfficiencyMode, RunLimits
from dynamic_firm.product.session_bindings import (
    session_cost_mode_binding,
    session_mcp_binding,
    session_provider_binding,
)


class SessionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SimpleNamespace(
            provider_kind="openai_api",
            base_url="https://example.invalid/v1",
            api_key_env="NORUCT_TEST_KEY",
            mcp_action=None,
            mcp_read_only=None,
            run_limits=RunLimits(cost_efficiency_mode=CostEfficiencyMode.ECONOMY),
        )

    def test_provider_and_cost_binding_are_secret_free_identity_projections(self) -> None:
        self.assertEqual(
            session_provider_binding(self.config),
            {
                "provider_kind": "openai_api",
                "provider_base_url": "https://example.invalid/v1",
                "provider_api_key_env": "NORUCT_TEST_KEY",
            },
        )
        self.assertEqual(
            session_cost_mode_binding(self.config),
            {"cost_efficiency_mode": "economy"},
        )

    def test_mcp_binding_is_stable_and_opaque_without_an_mcp_profile(self) -> None:
        first = session_mcp_binding(self.config)
        second = session_mcp_binding(self.config)

        self.assertEqual(first, second)
        self.assertRegex(first["mcp_binding_digest"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
