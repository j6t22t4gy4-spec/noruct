from __future__ import annotations

import unittest

from dynamic_firm.runtime.models import ActionPolicy, IdempotencyMode, ToolEffect, ToolGrant, ToolRisk
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolDefinition, ToolRegistry


def _definition(name: str, effect: ToolEffect) -> ToolDefinition:
    async def handler(arguments, cancellation: CancellationToken) -> str:
        cancellation.raise_if_cancelled()
        return "ok"

    return ToolDefinition(
        name=name,
        description=name,
        input_schema={"type": "object"},
        effect=effect,
        risk=ToolRisk.LOW,
        idempotency_mode=IdempotencyMode.NATURAL_KEY,
        validator=lambda value: value,
        resource_key=lambda _: "fixture:*",
        handler=handler,
    )


class CapabilityProjectionAuditTests(unittest.TestCase):
    def test_audit_reports_exposed_withheld_and_invalid_grants(self) -> None:
        registry = ToolRegistry()
        registry.register(_definition("read_fixture", ToolEffect.READ))
        registry.register(_definition("write_fixture", ToolEffect.WRITE))
        audit = registry.audit_projection(
            ActionPolicy(
                tool_grants=(
                    ToolGrant("read_fixture", (ToolEffect.READ,)),
                    ToolGrant("missing_fixture", (ToolEffect.READ,)),
                    ToolGrant("write_fixture", (ToolEffect.READ,)),
                )
            )
        )

        self.assertFalse(audit.valid)
        self.assertEqual(audit.exposed_tool_names, ("read_fixture",))
        self.assertEqual(audit.withheld_tool_names, ("write_fixture",))
        self.assertEqual(audit.dangling_grant_names, ("missing_fixture",))
        self.assertEqual(audit.effect_mismatch_names, ("write_fixture",))

    def test_audit_accepts_a_deliberately_withheld_registered_tool(self) -> None:
        registry = ToolRegistry()
        registry.register(_definition("read_fixture", ToolEffect.READ))
        registry.register(_definition("write_fixture", ToolEffect.WRITE))

        audit = registry.audit_projection(
            ActionPolicy(tool_grants=(ToolGrant("read_fixture", (ToolEffect.READ,)),))
        )

        self.assertTrue(audit.valid)
        self.assertEqual(audit.withheld_tool_names, ("write_fixture",))
