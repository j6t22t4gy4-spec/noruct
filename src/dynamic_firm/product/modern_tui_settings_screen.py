from __future__ import annotations

"""Lazy Textual Settings Center for the Modern terminal surface.

The screen stages bounded local commands and returns them to the controller on
Done.  It never owns Company state, applies Roster/Skill patches, reads a
credential value, or starts an external capability on its own.
"""

import json
from typing import Any, Mapping

from dynamic_firm.product.settings_dashboard import (
    PAGES,
    SettingsControl,
    page_controls,
    panel_options,
)
from dynamic_firm.product.settings_staging import SettingsCommandDraft
from dynamic_firm.providers.profiles import PROVIDER_SETUP_OPTIONS, provider_profile

from .modern_tui_settings_actions import handle_settings_button
from .modern_tui_settings_compose import compose_settings_screen


def create_settings_screen(
    *,
    ComposeResult: Any,
    NoMatches: Any,
    Container: Any,
    Grid: Any,
    Horizontal: Any,
    ModalScreen: Any,
    Button: Any,
    Input: Any,
    Static: Any,
) -> type[Any]:
    """Create SettingsScreen only after the optional Textual framework is present."""

    class SettingsScreen(ModalScreen[tuple[str, ...] | None]):
        """A redacted, global Settings Center rather than a tiny mode picker."""

        CSS = """
        SettingsScreen { align: center middle; }
        #settings-card { width: 112; max-width: 98%; height: 90%; border: heavy $accent;
          background: $surface; padding: 1 2; overflow-y: auto; }
        .settings-actions { height: auto; margin-top: 1; }
        #settings-page-grid { grid-size: 5 2; grid-columns: 1fr 1fr 1fr 1fr 1fr;
          grid-rows: 3 3; height: 6; margin-top: 1; }
        #settings-provider-grid { grid-size: 4 8; grid-columns: 1fr 1fr 1fr 1fr;
          grid-rows: 3 3 3 3 3 3 3 3; height: 24; margin-top: 1; }
        #settings-channel-grid { grid-size: 3 3; grid-columns: 1fr 1fr 1fr;
          grid-rows: 3 3 3; height: 9; margin-top: 1; }
        #settings-integration-grid { grid-size: 4 2; grid-columns: 1fr 1fr 1fr 1fr;
          grid-rows: 3 3; height: 6; margin-top: 1; }
        #settings-environment-grid { grid-size: 2 2; grid-columns: 1fr 1fr;
          grid-rows: 3 3; height: 6; margin-top: 1; }
        #settings-automation-grid { grid-size: 3 1; grid-columns: 1fr 1fr 1fr;
          grid-rows: 3; height: 3; margin-top: 1; }
        #settings-data-grid, #settings-channel-direction-grid {
          grid-size: 2 1; grid-columns: 1fr 1fr; grid-rows: 3; height: 3; margin-top: 1; }
        #settings-network-grid { grid-size: 2 2; grid-columns: 1fr 1fr;
          grid-rows: 3 3; height: 6; margin-top: 1; }
        #settings-company-grid { grid-size: 3 2; grid-columns: 1fr 1fr 1fr;
          grid-rows: 3 3; height: 6; margin-top: 1; }
        #settings-commit { dock: bottom; height: 3; background: $surface; padding: 0 1; }
        .settings-row { height: auto; margin-top: 1; }
        .settings-section { height: auto; margin-top: 1; color: $accent; text-style: bold; }
        .settings-entry { height: auto; color: $text-muted; }
        .settings-capability { width: 1fr; height: auto; text-align: left; margin-top: 1;
          background: $panel; border: round #39435a; color: $text; }
        .settings-capability.settings-focused { border: heavy $accent; background: #1a2d4a; }
        #settings-detail { height: auto; margin-top: 1; padding: 1; border: round $accent;
          background: #111b2d; }
        .settings-choice { background: $panel; color: $text; }
        .settings-choice.settings-selected, .settings-page.settings-selected { background: #2f9e63; color: #ffffff; text-style: bold; border: tall #62d995; }
        .settings-page { background: $panel; color: $text-muted; }
        .settings-provider { background: $panel; color: $text; }
        .settings-provider.settings-selected { background: #315fbe; color: #ffffff;
          text-style: bold; border: tall #83b4ff; }
        """

        def __init__(self, snapshot: object) -> None:
            super().__init__()
            self._snapshot = snapshot
            self._pending = SettingsCommandDraft()
            # Connection is the first-run decision and was previously hidden
            # behind an Execution-first default, making the Settings Center
            # look like it had no provider/auth controls at all.
            self._page = "Connection"
            self._focused_key = ""
            values = {
                str(item.get("key")): str(item.get("value"))
                for item in getattr(snapshot, "settings_entries", ())
                if isinstance(item, dict)
            }
            self._initial_values = dict(values)
            self._values = values
            self._provider_kind = values.get("provider.kind", "").strip().lower().replace("-", "_") or "openai_api"
            self._initial_provider_kind = self._provider_kind
            self._provider_no_auth = values.get("provider.no_auth", "environment") == "no-auth"
            self._initial_provider_no_auth = self._provider_no_auth
            self._channel_direction = "inbound"
            self._channel_kind = "telegram"
            self._integration_kind = "web-search"
            self._environment_kind = "browser"
            self._automation_kind = "schedule"
            self._company_kind = "manager"
            self._data_kind = "knowledge"
            self._network_kind = "sources"
            self._selected: dict[str, str] = {
                "workspace": f"/permission {values.get('run.permission_mode', 'ask')}",
                "trust": f"/trust {values.get('run.capability_trust_mode', 'trusted')}",
                "external-read": f"/external-read {values.get('run.external_read_mode', 'allow')}",
                "cost": f"/mode {values.get('run.cost_mode', 'standard')}",
                "external-state": f"/external-state {values.get('run.external_state_mode', 'ask')}",
                "agent-settings": f"/agent-settings {values.get('run.agent_settings_mode', 'ask')}",
                "review": f"/review {getattr(snapshot, 'review_mode', 'approval')}",
                "evolution": f"/evolution {getattr(snapshot, 'evolution_mode', 'never')}",
            }
            self._initial_selected = dict(self._selected)
            self._disable_actions: dict[str, tuple[str, str]] = {}
            for item in getattr(snapshot, "settings_entries", ()):
                if not isinstance(item, dict):
                    continue
                category = str(item.get("category", ""))
                key = str(item.get("key", ""))
                if category not in {"Integrations", "Messaging"} or item.get("value") != "enabled":
                    continue
                button_id = "settings-disable-" + key.replace(".", "-").replace("_", "-")
                self._disable_actions[button_id] = (key, f"/settings-disable {key}")

        def _choice_classes(self, group: str, command: str) -> str:
            classes = f"settings-choice setting-{group}"
            if self._selected.get(group) == command:
                classes += " settings-selected"
            return classes

        def _page_controls(self) -> tuple[SettingsControl, ...]:
            return page_controls(getattr(self._snapshot, "settings_entries", ()), self._page)

        def _focused_control(self) -> SettingsControl | None:
            return next(
                (item for item in self._page_controls() if item.key == self._focused_key),
                None,
            )

        def _provider_choice_classes(self, provider_kind: str) -> str:
            classes = "settings-provider"
            if self._provider_kind == provider_kind:
                classes += " settings-selected"
            return classes

        def _channel_choice_classes(self, channel_kind: str) -> str:
            classes = "settings-provider"
            if self._channel_kind == channel_kind:
                classes += " settings-selected"
            return classes

        def _app_choice_classes(self, selected: str, candidate: str) -> str:
            return "settings-provider settings-selected" if selected == candidate else "settings-provider"

        def _ordered_pending_commands(self) -> tuple[str, ...]:
            """Apply configuration before explicit service lifecycle actions."""

            return self._pending.ordered()

        def _reset_staged_changes(self) -> None:
            """Restore the snapshot-backed editor state without applying a command."""

            self._pending.clear()
            self._values = dict(self._initial_values)
            self._provider_kind = self._initial_provider_kind
            self._provider_no_auth = self._initial_provider_no_auth
            self._channel_direction = "inbound"
            self._channel_kind = "telegram"
            self._integration_kind = "web-search"
            self._environment_kind = "browser"
            self._automation_kind = "schedule"
            self._company_kind = "manager"
            self._data_kind = "knowledge"
            self._network_kind = "sources"
            self._selected = dict(self._initial_selected)
            self._focused_key = ""

        def _stage_network_mutation(
            self,
            *,
            key: str,
            action: str,
            payload: Mapping[str, object],
            label: str,
            button: Any,
        ) -> None:
            """Stage one explicit local Network lifecycle transition.

            The modal never calls the Network service itself.  Returning the
            same typed `/network` request used by the terminal keeps Settings
            and a future GUI behind one product boundary, while Done remains
            the only apply boundary.
            """

            request = {"confirm": True, **payload}
            self._pending[f"network:{key}"] = "/network " + action + " " + json.dumps(
                request, ensure_ascii=False, separators=(",", ":")
            )
            button.add_class("settings-selected")
            rendered = " · ".join(self._pending.values())
            self.query_one("#settings-pending", Static).update(
                f"Pending ({len(self._pending)}): {rendered}\n"
                f"Done applies {label} to the local future-Job catalog; running Jobs are unchanged."
            )

        def compose(self) -> ComposeResult:
            return compose_settings_screen(
                self,
                ComposeResult=ComposeResult,
                Container=Container,
                Grid=Grid,
                Horizontal=Horizontal,
                Button=Button,
                Input=Input,
                Static=Static,
            )

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            await handle_settings_button(
                self,
                event,
                NoMatches=NoMatches,
                Static=Static,
                Input=Input,
                Button=Button,
            )

    return SettingsScreen
