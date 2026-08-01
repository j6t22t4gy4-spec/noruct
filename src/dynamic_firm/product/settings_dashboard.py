"""Framework-free presentation model for the Settings Center.

The terminal screen must not invent its own partial feature list.  This small
adapter turns the Settings Registry snapshot into stable pages and selectable
capability controls, so every configured or available capability has the same
discoverable place in the GUI-like TUI and in future non-terminal surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


PAGES: tuple[str, ...] = (
    "Connection",
    "Execution",
    "Integrations",
    "Messaging",
    "Environment",
    "Automation",
    "Company",
    "Data",
    "Network",
)


@dataclass(frozen=True, slots=True)
class SettingsPanelOption:
    """One GUI-like editor choice within a Settings page."""

    key: str
    label: str


_PAGE_PANEL_OPTIONS: dict[str, tuple[SettingsPanelOption, ...]] = {
    "Integrations": tuple(
        SettingsPanelOption(key, label)
        for key, label in (
            ("web-search", "Web search"),
            ("media", "Media"),
            ("plugin", "Plugin"),
            ("mcp", "MCP read"),
            ("mcp-action", "MCP action"),
            ("skills", "Skills"),
            ("home-assistant", "Home Assistant"),
        )
    ),
    "Environment": tuple(
        SettingsPanelOption(key, label)
        for key, label in (
            ("browser", "Browser"),
            ("computer", "Computer use"),
            ("container", "Container"),
            ("remote", "Remote worker"),
        )
    ),
    "Automation": tuple(
        SettingsPanelOption(key, label)
        for key, label in (
            ("schedule", "Schedules"),
            ("schedule-service", "Schedule service"),
            ("gateway", "Gateway"),
        )
    ),
    "Company": (
        SettingsPanelOption("manager", "Manager profile"),
        SettingsPanelOption("employees", "Employee roster"),
        SettingsPanelOption("skills", "Employee skills"),
        SettingsPanelOption("delegation", "Delegation controls"),
        SettingsPanelOption("retention", "Learning review"),
        SettingsPanelOption("coordination", "Multi-device coordination"),
    ),
    "Data": (
        SettingsPanelOption("knowledge", "Knowledge vault"),
        SettingsPanelOption("evolution", "Artifact evolution"),
    ),
    "Network": (
        SettingsPanelOption("sources", "Trusted sources"),
        SettingsPanelOption("catalog", "Template catalog"),
        SettingsPanelOption("install", "Install lifecycle"),
        SettingsPanelOption("updates", "Version updates"),
        SettingsPanelOption("permissions", "Template permissions"),
        SettingsPanelOption("trust", "Trust boundary"),
    ),
}


def panel_options(page: str) -> tuple[SettingsPanelOption, ...]:
    """Return the stable editor catalog for one Settings page."""

    return _PAGE_PANEL_OPTIONS.get(page, ())


_ENVIRONMENT_KEYS = frozenset(
    {
        "integration.remote_worker",
        "integration.container",
        "integration.browser",
        "integration.computer_use",
    }
)


def page_for_entry(entry: Mapping[str, object]) -> str:
    """Return the user-facing dashboard page for one registry entry."""

    key = str(entry.get("key", ""))
    category = str(entry.get("category", ""))
    if key in _ENVIRONMENT_KEYS:
        return "Environment"
    return category if category in PAGES else "Data"


def control_id(key: str) -> str:
    """Build a stable, Textual-safe id without exposing config values."""

    return "settings-entry-" + "".join(
        character if character.isalnum() else "-" for character in key
    ).strip("-")


@dataclass(frozen=True, slots=True)
class SettingsControl:
    key: str
    page: str
    title: str
    state: str
    value: str
    scope: str
    effect: str
    summary: str
    setup_hint: str
    agent_writable: bool

    @property
    def id(self) -> str:
        return control_id(self.key)

    @property
    def configured(self) -> bool:
        return self.value == "enabled" or self.state in {
            "configured",
            "ready",
            "needs-auth",
            "withheld",
            "approval-required",
        }

    @property
    def label(self) -> str:
        # Lifecycle state is the actionable fact.  Showing only ``enabled``
        # hid needs-auth and policy-withheld profiles until the operator opened
        # the detail view.
        status = self.state
        if self.value not in {"", "enabled", "not configured"}:
            status = self.value if status == "configured" else f"{self.value} · {status}"
        return f"{self.title} · {status}"


def controls_from_entries(entries: Iterable[object]) -> tuple[SettingsControl, ...]:
    """Normalize one redacted registry snapshot into dashboard controls."""

    controls: list[SettingsControl] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        key = str(raw.get("key", "")).strip()
        if not key:
            continue
        controls.append(
            SettingsControl(
                key=key,
                page=page_for_entry(raw),
                title=str(raw.get("title", "Setting")),
                state=str(raw.get("state", "unknown")),
                value=str(raw.get("value", "")),
                scope=str(raw.get("scope", "GLOBAL")),
                effect=str(raw.get("effect", "")),
                summary=str(raw.get("summary", "")),
                setup_hint=str(raw.get("setup_hint", "")),
                agent_writable=bool(raw.get("agent_writable")),
            )
        )
    return tuple(controls)


def page_controls(entries: Iterable[object], page: str) -> tuple[SettingsControl, ...]:
    return tuple(item for item in controls_from_entries(entries) if item.page == page)
