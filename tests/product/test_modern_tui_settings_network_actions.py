from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from dynamic_firm.product.modern_tui_settings_network_actions import (
    handle_network_settings_action,
)


class _Button:
    def __init__(self, button_id: str) -> None:
        self.id = button_id
        self.classes: list[str] = []

    def add_class(self, value: str) -> None:
        self.classes.append(value)


class _Pending:
    def __init__(self) -> None:
        self.text = ""

    def update(self, value: str) -> None:
        self.text = value


class _Owner:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._network_kind = "catalog"
        self._pending: dict[str, str] = {}
        self._values = values or {}
        self.pending = _Pending()
        self.recomposed = 0
        self.mutations: list[dict[str, object]] = []

    async def recompose(self) -> None:
        self.recomposed += 1

    def query_one(self, selector: str, _kind: object) -> object:
        if selector == "#settings-pending":
            return self.pending
        return SimpleNamespace(value=self._values[selector])

    def _stage_network_mutation(self, **kwargs: object) -> None:
        self.mutations.append(kwargs)


class NetworkSettingsActionTests(unittest.TestCase):
    def test_open_stages_only_a_local_inspection_command(self) -> None:
        owner = _Owner()
        event = SimpleNamespace(button=_Button("settings-network-open"))

        consumed = asyncio.run(
            handle_network_settings_action(owner, event, Input=object, Static=object)
        )

        self.assertTrue(consumed)
        self.assertEqual(owner._pending, {"network:inspect": "/network search"})
        self.assertEqual(owner.mutations, [])
        self.assertIn("confirm=true", owner.pending.text)

    def test_activation_stages_exact_future_job_artifact_only(self) -> None:
        owner = _Owner(
            {
                "#settings-network-action-source": "source-a",
                "#settings-network-action-registry": "registry-a",
                "#settings-network-action-snapshot": "snapshot-a",
                "#settings-network-action-operator": "operator-a",
                "#settings-network-action-reason": "reviewed",
                "#settings-network-action-artifact": "tool-a",
                "#settings-network-action-version": "1.2.3",
                "#settings-network-action-scope": "company_default",
                "#settings-network-action-capabilities": "read, search",
                "#settings-network-action-update-mode": "PINNED",
            }
        )
        event = SimpleNamespace(button=_Button("settings-network-activate-artifact"))

        consumed = asyncio.run(
            handle_network_settings_action(owner, event, Input=object, Static=object)
        )

        self.assertTrue(consumed)
        self.assertEqual(len(owner.mutations), 1)
        mutation = owner.mutations[0]
        self.assertEqual(mutation["action"], "activate")
        self.assertEqual(
            mutation["payload"],
            {
                "scope_key": "company_default",
                "artifact_id": "tool-a",
                "version": "1.2.3",
                "allowed_capabilities": ["read", "search"],
            },
        )
        self.assertIn("future-Job", str(mutation["label"]))

    def test_non_network_button_is_not_consumed(self) -> None:
        owner = _Owner()
        event = SimpleNamespace(button=_Button("settings-model-picker"))

        self.assertFalse(
            asyncio.run(handle_network_settings_action(owner, event, Input=object, Static=object))
        )


if __name__ == "__main__":
    unittest.main()
