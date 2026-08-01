from __future__ import annotations

import unittest

from dynamic_firm.product.settings_dashboard import (
    PAGES,
    control_id,
    controls_from_entries,
    page_controls,
    panel_options,
)
from dynamic_firm.product.settings_staging import SettingsCommandDraft


class SettingsDashboardTests(unittest.TestCase):

    def test_settings_command_draft_replaces_a_logical_change_and_starts_services_last(self) -> None:
        draft = SettingsCommandDraft()
        draft["connection"] = '/connection {"provider_kind":"openai_api"}'
        draft["automation:gateway"] = '/gateway-service {"action":"start","receivers":["slack"]}'
        draft["connection"] = '/connection {"provider_kind":"openrouter"}'

        self.assertEqual(
            draft.ordered(),
            (
                '/connection {"provider_kind":"openrouter"}',
                '/gateway-service {"action":"start","receivers":["slack"]}',
            ),
        )
        self.assertIn("openrouter", draft.summary())
        self.assertNotIn("openai_api", draft.summary())
        draft.clear()
        self.assertEqual(draft.ordered(), ())
        self.assertEqual(draft.summary(), "")

    def test_every_registry_entry_is_a_selectable_control_on_one_known_page(self) -> None:
        entries = (
            {"key": "provider.model", "category": "Connection", "title": "Model", "state": "configured", "value": "x", "scope": "GLOBAL", "effect": "connection", "summary": "", "agent_writable": True},
            {"key": "integration.browser", "category": "Integrations", "title": "Browser", "state": "not-configured", "value": "not configured", "scope": "GLOBAL", "effect": "external-state", "summary": "", "setup_hint": "noruct browser configure"},
            {"key": "channel.slack_channel", "category": "Messaging", "title": "Slack", "state": "needs-auth", "value": "enabled", "scope": "GLOBAL", "effect": "external-communication", "summary": ""},
        )
        controls = controls_from_entries(entries)

        self.assertEqual(len(controls), len(entries))
        self.assertEqual({item.page for item in controls}, {"Connection", "Environment", "Messaging"})
        self.assertTrue(all(item.page in PAGES and item.id.startswith("settings-entry-") for item in controls))
        self.assertEqual(page_controls(entries, "Environment")[0].key, "integration.browser")
        self.assertEqual(controls[-1].label, "Slack · needs-auth")

    def test_control_ids_are_stable_and_do_not_include_values(self) -> None:
        self.assertEqual(control_id("integration.mcp_action"), "settings-entry-integration-mcp-action")

    def test_editor_panel_catalog_is_unique_and_covers_customized_pages(self) -> None:
        for page in ("Integrations", "Environment", "Automation", "Company", "Data"):
            keys = [item.key for item in panel_options(page)]
            self.assertTrue(keys)
            self.assertEqual(len(keys), len(set(keys)))
