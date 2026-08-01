"""One secret-free Settings Center contract for people and Company agents.

The CLI has accumulated useful configuration commands, but a terminal-only
command list is not a settings system.  This registry gives the TUI and an
employee the same redacted inventory and the same narrow write boundary.
Credential *names* may be shown; credential values never enter this module.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.product.global_settings import (
    GlobalRuntimeSettings,
    remove_optional_settings_table,
    write_global_runtime_settings,
)
from dynamic_firm.product.local_routing_settings import load_local_routing_settings
from dynamic_firm.providers.profiles import PROVIDER_KINDS
from dynamic_firm.runtime.models import IdempotencyMode, ToolEffect, ToolRisk
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolDefinition, ToolValidationError


_INTEGRATION_TABLES: tuple[tuple[str, str, str], ...] = (
    ("mcp", "MCP read capabilities", "integration"),
    ("mcp_action", "MCP actions", "external-state"),
    ("browser", "Browser", "external-state"),
    ("computer_use", "Computer use", "external-state"),
    ("web_search", "Web search", "integration"),
    ("home_assistant", "Home Assistant", "external-state"),
    ("openai_media", "Media tools", "external-state"),
    ("plugin", "Plugins", "external-state"),
    ("skills", "External skills", "integration"),
    ("remote_worker", "Remote worker", "external-state"),
    ("container", "Container workspace", "external-state"),
)
_CHANNEL_TABLES: tuple[tuple[str, str], ...] = (
    ("channel", "Generic outbound"),
    ("inbound_channel", "Generic inbound"),
    # These keys must be the actual persisted TOML table names.  The prior
    # short display aliases (``telegram``, ``slack``, ``discord`` …) made a
    # configured channel look permanently "not configured" in Settings and
    # prevented its lifecycle action from being rendered.
    ("telegram_channel", "Telegram"),
    ("slack_channel", "Slack"),
    ("slack_inbound", "Slack inbound"),
    ("discord_channel", "Discord"),
    ("discord_inbound", "Discord inbound"),
    ("ntfy_channel", "ntfy"),
    ("ntfy_inbound", "ntfy inbound"),
    ("email_channel", "Email"),
    ("email_inbound", "Email inbound"),
    ("mattermost_channel", "Mattermost"),
    ("mattermost_inbound", "Mattermost inbound"),
    ("matrix_channel", "Matrix"),
    ("matrix_inbound", "Matrix inbound"),
    ("teams_channel", "Teams"),
    ("dingtalk_channel", "DingTalk"),
)

# These are first-party CLI entrypoints, not opaque shell snippets.  They are
# shown in the Settings Center when a capability is absent so "not configured"
# always has a concrete, discoverable next step.
_INTEGRATION_SETUP: dict[str, str] = {
    "mcp": "noruct mcp configure",
    "mcp_action": "noruct mcp action-configure",
    "browser": "noruct browser configure",
    "computer_use": "noruct computer-use configure",
    "web_search": "noruct web-search configure --base-url HTTPS_OR_LOOPBACK_URL",
    "home_assistant": "noruct home-assistant configure --base-url HTTPS_OR_LOOPBACK_URL --allow-entity ENTITY_ID",
    "openai_media": "noruct media configure --enable image",
    "plugin": "noruct plugin install LOCAL_PLUGIN_DIRECTORY --confirm --enable",
    "skills": "noruct chat --skills-dir LOCAL_SKILL_ROOT",
    "remote_worker": "noruct environment worker-configure",
    "container": "noruct environment container-configure",
}

# A configured profile is not necessarily exposed to the next Employee Job.
# Keep this policy projection in the Settings authority so the UI never calls
# a globally withheld integration "ready".  Mixed read/action integrations
# (browser and Home Assistant) stay configured: their individual action
# controls are shown separately by their capability page.
_EXTERNAL_READ_ONLY_TABLES = frozenset({"mcp", "web_search"})
_EXTERNAL_STATE_ONLY_TABLES = frozenset({
    "mcp_action", "computer_use", "openai_media", "plugin",
    "remote_worker", "container",
})
_CHANNEL_SETUP: dict[str, str] = {
    "channel": "noruct channel configure",
    "inbound_channel": "noruct channel inbox-configure",
    "telegram_channel": "noruct channel telegram-configure",
    "slack_channel": "noruct channel slack-configure",
    "slack_inbound": "noruct channel slack-inbox-configure",
    "discord_channel": "noruct channel discord-configure",
    "discord_inbound": "noruct channel discord-inbox-configure",
    "ntfy_channel": "noruct channel ntfy-configure",
    "ntfy_inbound": "noruct channel ntfy-inbox-configure",
    "email_channel": "noruct channel email-configure",
    "email_inbound": "noruct channel email-inbox-configure",
    "mattermost_channel": "noruct channel mattermost-configure",
    "mattermost_inbound": "noruct channel mattermost-inbox-configure",
    "matrix_channel": "noruct channel matrix-configure",
    "matrix_inbound": "noruct channel matrix-inbox-configure",
    "teams_channel": "noruct channel teams-configure",
    "dingtalk_channel": "noruct channel dingtalk-configure",
}


@dataclass(frozen=True, slots=True)
class SettingsEntry:
    key: str
    category: str
    title: str
    scope: str
    state: str
    effect: str
    summary: str
    value: str = ""
    agent_writable: bool = False
    setup_hint: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "category": self.category,
            "title": self.title,
            "scope": self.scope,
            "state": self.state,
            "effect": self.effect,
            "summary": self.summary,
            "value": self.value,
            "agent_writable": self.agent_writable,
            "setup_hint": self.setup_hint,
        }


def _read(path: Path) -> dict[str, object]:
    target = path.expanduser()
    if not target.is_file():
        return {}
    value = tomllib.loads(target.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _table(settings: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = settings.get(name, {})
    return value if isinstance(value, Mapping) else {}


def _secret_state(table: Mapping[str, object]) -> str:
    names = [
        str(value).strip()
        for key, value in table.items()
        if key.endswith("_env") and isinstance(value, str) and str(value).strip()
    ]
    if not names:
        return "configured"
    return "ready" if all(os.environ.get(name) for name in names) else "needs-auth"


def _company_coordination_state(table: Mapping[str, object]) -> str:
    """Render a coordination lifecycle without loading a bearer token."""

    if not table:
        return "not-configured"
    if table.get("enabled") is not True:
        return "disabled"
    required = ("endpoint", "company_scope_digest", "device_id", "token_env")
    if any(not isinstance(table.get(key), str) or not str(table[key]).strip() for key in required):
        return "invalid"
    token_env = str(table["token_env"]).strip()
    return "ready" if os.environ.get(token_env) else "needs-auth"


def _company_coordination_value(table: Mapping[str, object]) -> str:
    if not table:
        return "not configured"
    if table.get("enabled") is not True:
        return "disabled"
    device_id = table.get("device_id")
    return str(device_id).strip() if isinstance(device_id, str) and device_id.strip() else "incomplete"


def _company_coordination_field(table: Mapping[str, object], key: str) -> str:
    value = table.get(key)
    return str(value).strip() if isinstance(value, str) and str(value).strip() else ""


def _integration_state(
    key: str,
    table: Mapping[str, object],
    runtime: GlobalRuntimeSettings,
) -> tuple[str, str]:
    """Return the visible lifecycle state and an authority explanation."""

    if not table:
        return "not-configured", "No local profile is configured."
    if key in _EXTERNAL_READ_ONLY_TABLES and runtime.external_read_mode == "blocked":
        return "withheld", "Configured locally, but global External reads is blocked."
    if key in _EXTERNAL_STATE_ONLY_TABLES and runtime.external_state_mode == "blocked":
        return "withheld", "Configured locally, but global External state changes is blocked."
    return _secret_state(table), "Configured capability is evaluated again for every future Job."


def _local_routing_entry(path: Path) -> SettingsEntry:
    """Project approved-route reuse without exposing route metadata.

    The registry is deliberately observational: a value can show only the
    user's policy and the number of already approved routes.  It cannot make
    a route available, resolve a provider, or disclose an identifier,
    credential reference, or configuration digest.
    """

    try:
        local_routing = load_local_routing_settings(path)
    except (OSError, TypeError, ValueError):
        return SettingsEntry(
            "routing.local_policy",
            "Model routing",
            "Approved route reuse",
            "LOCAL",
            "configuration-error",
            "future-job-route-reuse",
            "Local route reuse is fail-closed until the bounded routing settings are repaired.",
            "configuration error",
        )

    approved_count = len(local_routing.approved_routes.routes)
    value = f"{local_routing.policy.mode.value} · {approved_count} approved routes"
    if approved_count == 0:
        return SettingsEntry(
            "routing.local_policy",
            "Model routing",
            "Approved route reuse",
            "LOCAL",
            "first-run-no-approved-routes",
            "future-job-route-reuse",
            "First-run local policy has no approved routes; it cannot authorize route reuse.",
            value,
        )
    return SettingsEntry(
        "routing.local_policy",
        "Model routing",
        "Approved route reuse",
        "LOCAL",
        "configured",
        "future-job-route-reuse",
        "Shows only a local policy and approval count; provider activation, credentials, and egress remain separate.",
        value,
    )


class SettingsRegistry:
    """Read redacted global configuration and apply bounded policy changes."""

    def __init__(self, config_path: str | Path) -> None:
        self.path = Path(config_path).expanduser().resolve()

    def entries(self) -> tuple[SettingsEntry, ...]:
        settings = _read(self.path)
        runtime = GlobalRuntimeSettings.from_mapping(settings)
        provider_state = "ready"
        if runtime.provider_kind not in {"openai_codex", "external_exec"} and not runtime.no_auth:
            provider_state = "ready" if os.environ.get(runtime.api_key_env) else "needs-auth"
        rows: list[SettingsEntry] = [
            SettingsEntry("provider.kind", "Connection", "Model provider", "GLOBAL", provider_state, "connection", "Provider identity is global; secrets stay in the environment.", runtime.provider_kind, True),
            SettingsEntry("provider.model", "Connection", "Default model", "GLOBAL", "configured", "connection", "Used by future Company jobs.", runtime.model or "not configured", True),
            SettingsEntry("provider.base_url", "Connection", "Provider endpoint", "GLOBAL", "configured" if runtime.base_url else "not-configured", "connection", "Non-secret API endpoint for compatible providers. Codex and external executable providers do not use this value.", runtime.base_url or "not used", False),
            SettingsEntry("provider.api_key_env", "Connection", "Credential environment variable", "GLOBAL", "not-required" if runtime.no_auth or not runtime.api_key_env else _secret_state({"api_key_env": runtime.api_key_env}), "authentication", "Only the environment-variable name is stored. Its credential value remains outside Noruct.", "not required" if runtime.no_auth else (runtime.api_key_env or "not required"), False),
            SettingsEntry("provider.no_auth", "Connection", "Authentication mode", "GLOBAL", "configured", "authentication", "Compatible local endpoints may explicitly disable authentication; hosted providers retain their required environment-backed credential.", "no-auth" if runtime.no_auth else "environment", False),
            SettingsEntry("provider.codex_command", "Connection", "Codex executable", "GLOBAL", "configured" if runtime.codex_command else "not-configured", "external-executable", "Used only for the user-managed Codex provider; Noruct never reads its login credentials.", runtime.codex_command or "not configured", False),
            SettingsEntry("provider.external_command", "Connection", "External bridge executable", "GLOBAL", "configured" if runtime.external_command else "not-configured", "external-executable", "Used only for the external-exec provider bridge.", runtime.external_command or "not configured", False),
            SettingsEntry("provider.request_timeout", "Connection", "Provider hard guard", "GLOBAL", "configured", "connection", "Leak guard for one provider request. It does not determine whether an actively streaming Codex turn remains healthy.", f"{runtime.request_timeout:g}s", False),
            SettingsEntry("provider.stale_timeout", "Connection", "No-progress deadline", "GLOBAL", "configured", "connection", "Codex is retried when it emits no JSON progress event for this duration; each progress event resets the deadline.", f"{runtime.stale_timeout:g}s", False),
            SettingsEntry("run.permission_mode", "Execution", "Workspace authority", "GLOBAL", "configured", "workspace-write", "Ask mode exposes local mutation tools; read-only exposes no mutation tools. Capability trust controls dialog frequency for the exposed tools.", runtime.permission_mode, True),
            SettingsEntry("run.capability_trust_mode", "Execution", "Capability trust profile", "GLOBAL", "configured", "approval-policy", "Strict prompts for each effect. Trusted auto-runs ordinary workspace work and explicitly installed tools. Autonomous auto-runs every already enabled capability. Every ToolIntent and result is still audited.", runtime.capability_trust_mode, True),
            SettingsEntry("workspace.command_execution", "Execution", "Terminal command execution", "GLOBAL", "blocked" if runtime.permission_mode != "ask" else ("approval-required" if runtime.capability_trust_mode == "strict" else "trusted"), "execute", "Workspace authority decides whether commands exist; the Capability trust profile decides whether an already-granted command asks again. Command intent and result are always recorded.", "blocked" if runtime.permission_mode != "ask" else ("ask + per-command approval" if runtime.capability_trust_mode == "strict" else f"ask + {runtime.capability_trust_mode} execution")),
            SettingsEntry("run.external_read_mode", "Execution", "External reads", "GLOBAL", "configured", "network-read", "Blocked withholds MCP, web-read, and web-search tools. Ask requires an approval for every read; allow exposes configured read capabilities.", runtime.external_read_mode, True),
            SettingsEntry("run.external_state_mode", "Execution", "External state changes", "GLOBAL", "configured", "external-state", "Blocked withholds external/device actions. Ask keeps a dialog; user-authorized-auto lets the selected Capability trust profile auto-run the already configured action set.", runtime.external_state_mode, True),
            SettingsEntry("run.agent_settings_mode", "Execution", "Agent setting proposals", "GLOBAL", "configured", "settings-write", "Ask lets a Company employee propose bounded setting changes behind approval. Blocked leaves settings user-only.", runtime.agent_settings_mode, True),
            SettingsEntry("run.cost_mode", "Execution", "Cost mode", "GLOBAL", "configured", "model-cost", "Controls default context and model-cost posture for future jobs.", runtime.cost_mode, True),
            SettingsEntry("run.limits", "Execution", "Execution envelope", "GLOBAL", "configured", "budget", "Long-running defaults apply to future jobs. Narrow these only when you want a deliberate per-job stop condition.", f"${runtime.max_cost_usd:g} · {runtime.max_model_calls} model / {runtime.max_tool_calls} tool", True),
            SettingsEntry("run.max_cost_usd", "Execution", "Cost envelope", "GLOBAL", "configured", "budget", "Future-job accounting ceiling; the default is intentionally non-restrictive.", f"{runtime.max_cost_usd:g}", True),
            SettingsEntry("run.max_model_calls", "Execution", "Model-call envelope", "GLOBAL", "configured", "budget", "Future-job call ceiling; the default is intentionally non-restrictive.", str(runtime.max_model_calls), True),
            SettingsEntry("run.max_tool_calls", "Execution", "Tool-call envelope", "GLOBAL", "configured", "budget", "Future-job tool ceiling; the default is intentionally non-restrictive.", str(runtime.max_tool_calls), True),
            SettingsEntry("run.max_wall_time", "Execution", "Wall-time envelope", "GLOBAL", "configured", "budget", "Future-job wall-time ceiling in seconds; the default is 24 hours.", f"{runtime.max_wall_time:g}", True),
            _local_routing_entry(self.path),
        ]
        for key, title, effect in _INTEGRATION_TABLES:
            table = _table(settings, key)
            state, authority = _integration_state(key, table, runtime)
            rows.append(SettingsEntry(
                f"integration.{key}", "Integrations", title, "GLOBAL",
                state, effect,
                authority,
                "enabled" if table else "not configured",
                setup_hint=_INTEGRATION_SETUP.get(key, "noruct capabilities"),
            ))
        for key, title in _CHANNEL_TABLES:
            table = _table(settings, key)
            rows.append(SettingsEntry(
                f"channel.{key}", "Messaging", title, "GLOBAL",
                _secret_state(table) if table else "not-configured", "external-communication",
                "Credentials are environment references only; connecting a channel does not send a message.",
                "enabled" if table else "not configured",
                setup_hint=_CHANNEL_SETUP.get(key, "noruct capabilities"),
            ))
        rows.extend((
            SettingsEntry("automation.schedule", "Automation", "Scheduled Company jobs", "LOCAL", "available", "scheduled-job", "Schedules are stored locally. Creating one does not run it; foreground or service start is an explicit operator action.", "local lifecycle", setup_hint="noruct schedule create GOAL --every-minutes N --confirm"),
            SettingsEntry("company.retention", "Company", "Learning review", "COMPANY", "configured", "company-state", "Patch and roster promotion authority remains separate from global connection settings.", "review-controlled"),
            SettingsEntry(
                "company.coordination",
                "Company",
                "Multi-device coordination",
                "GLOBAL",
                _company_coordination_state(_table(settings, "company_coordination")),
                "cross-device-effect-lease",
                "An opt-in HTTPS control plane coordinates only opaque resource leases and receipt-bound same-Job claims. The token value remains in its named environment variable; it is never displayed or stored.",
                _company_coordination_value(_table(settings, "company_coordination")),
                True,
                setup_hint="Open Settings → Company → Multi-device coordination",
            ),
            SettingsEntry("company.coordination.endpoint", "Company", "Coordination endpoint", "GLOBAL", _company_coordination_state(_table(settings, "company_coordination")), "cross-device-effect-lease", "HTTPS origin for the optional opaque coordination control plane.", _company_coordination_field(_table(settings, "company_coordination"), "endpoint")),
            SettingsEntry("company.coordination.scope", "Company", "Company scope digest", "GLOBAL", _company_coordination_state(_table(settings, "company_coordination")), "cross-device-effect-lease", "Opaque SHA-256 namespace shared only by this Company's authorized devices.", _company_coordination_field(_table(settings, "company_coordination"), "company_scope_digest")),
            SettingsEntry("company.coordination.device", "Company", "Device identity", "GLOBAL", _company_coordination_state(_table(settings, "company_coordination")), "cross-device-effect-lease", "Non-secret device identifier used in lease receipts.", _company_coordination_field(_table(settings, "company_coordination"), "device_id")),
            SettingsEntry("company.coordination.token_env", "Company", "Coordination token environment", "GLOBAL", _company_coordination_state(_table(settings, "company_coordination")), "authentication", "Environment-variable name only. Its token value is never inspected or stored by Settings.", _company_coordination_field(_table(settings, "company_coordination"), "token_env")),
            SettingsEntry("company.coordination.loopback", "Company", "Loopback development", "GLOBAL", _company_coordination_state(_table(settings, "company_coordination")), "cross-device-effect-lease", "Permits HTTP only for localhost development; production coordination requires HTTPS.", "yes" if _table(settings, "company_coordination").get("allow_insecure_loopback") is True else "no"),
            SettingsEntry("data.knowledge", "Data", "Knowledge vault", "LOCAL", "configured", "local-data", "Assets, evidence, and decisions remain local unless separately opted into a network action.", "local"),
            SettingsEntry("data.evolution", "Data", "Local artifact evolution", "LOCAL", "configured", "artifact-policy", "This is the local activation posture. Shared-network contribution consent is a separate boundary.", "local policy"),
            SettingsEntry("network.sources", "Network", "Trusted Network sources", "LOCAL", "available", "signed-distribution", "A source is a signed template origin. Adding one cannot grant credentials, capabilities, or execution authority.", "manage in catalog", setup_hint="noruct network source list"),
            SettingsEntry("network.private_team", "Network", "Private team Registry access", "LOCAL", "available", "credential-reference", "A private Registry is locally named and never remotely enumerated. Noruct stores only a token environment-variable name; the token value is neither displayed nor saved.", "operator-configured", setup_hint="noruct network source add SOURCE_ID --publisher-class PRIVATE_TEAM --origin HTTPS_ORIGIN --registry-id REGISTRY_ID --credential-env TOKEN_ENV --allowed-signers FILE --principal PRINCIPAL --ssh-keygen ABSOLUTE_PATH --operator-id OPERATOR --confirm"),
            SettingsEntry("network.catalog", "Network", "Agent, Tool, Skill, Workflow, and Benchmark catalog", "LOCAL", "available", "versioned-template", "Discovery is pointer-only; every remote bundle is signature-verified, staged, reviewed, installed, and explicitly activated locally.", "local-first", setup_hint="noruct network search"),
            SettingsEntry("network.installation", "Network", "Install and rollback lifecycle", "LOCAL", "available", "reviewed-install", "Network templates must be staged and reviewed before local install. Activation is separate, affects future Jobs only, and has an explicit rollback path.", "inactive until activated", setup_hint="noruct network install SNAPSHOT_ID ARTIFACT_ID VERSION --confirm"),
            SettingsEntry("network.updates", "Network", "Template update policy", "LOCAL", "configured", "future-job-update", "Pinned is default. First-party automatic updates require an enabled first-party source and affect future Jobs only.", "pinned by default", setup_hint="noruct network updates company_default"),
            SettingsEntry("network.permissions", "Network", "Template authority boundary", "LOCAL", "available", "capability-bound", "A template cannot add credentials, arbitrary code, or new authority. Tool templates bind only registered local adapters and each activation remains capability-bounded.", "local policy required", setup_hint="noruct network activate SCOPE ARTIFACT VERSION --allowed-capability CAPABILITY --confirm"),
            SettingsEntry("network.shared_evolution", "Network", "Shared Evolution publisher", "LOCAL", "available", "first-party-publisher", "Shared Evolution publishes typed, privacy-bounded first-party releases into the same Network lifecycle; it is not the Network's authority.", "optional", setup_hint="noruct network source list"),
        ))
        return tuple(rows)

    def summary(self) -> dict[str, object]:
        entries = self.entries()
        return {
            "config_path": str(self.path),
            "entry_count": len(entries),
            "categories": tuple(dict.fromkeys(item.category for item in entries)),
            "entries": tuple(item.as_dict() for item in entries),
        }

    def apply_global_change(self, change: Mapping[str, object]) -> dict[str, object]:
        """Apply a small typed change. It intentionally cannot carry secrets."""

        key = str(change.get("key", "")).strip()
        value = change.get("value")
        if key not in {
            "provider.kind", "provider.model", "run.permission_mode", "run.capability_trust_mode",
            "run.external_read_mode", "run.external_state_mode", "run.agent_settings_mode", "run.cost_mode", "run.max_cost_usd",
            "run.max_model_calls", "run.max_tool_calls", "run.max_wall_time",
        }:
            raise ValueError("This setting is read-only to agents; use the user Settings Center or a typed CLI configure command")
        if isinstance(value, (dict, list, tuple)) or len(str(value).encode("utf-8")) > 256:
            raise ValueError("Setting value must be one short scalar")
        if any(token in key.lower() for token in ("token", "secret", "password", "api_key")):
            raise ValueError("Secrets cannot be written through an agent setting tool")
        current = GlobalRuntimeSettings.from_mapping(_read(self.path))
        field = {
            "provider.kind": "provider_kind", "provider.model": "model",
            "run.permission_mode": "permission_mode", "run.capability_trust_mode": "capability_trust_mode", "run.external_read_mode": "external_read_mode",
            "run.external_state_mode": "external_state_mode", "run.agent_settings_mode": "agent_settings_mode",
            "run.cost_mode": "cost_mode", "run.max_cost_usd": "max_cost_usd",
            "run.max_model_calls": "max_model_calls", "run.max_tool_calls": "max_tool_calls",
            "run.max_wall_time": "max_wall_time",
        }[key]
        if key == "provider.kind":
            normalized = str(value).strip().replace("-", "_")
            if normalized not in PROVIDER_KINDS:
                raise ValueError("Unsupported provider kind")
            value = normalized
        elif key in {"run.max_model_calls", "run.max_tool_calls"}:
            value = int(value)
        elif key in {"run.max_cost_usd", "run.max_wall_time"}:
            value = float(value)
        else:
            value = str(value).strip()
        updated = replace(current, **{field: value})
        # A provider switch needs its complete non-secret connection details.
        # Refuse a partial invalid profile rather than inventing a URL/model.
        updated.validate()
        target = write_global_runtime_settings(self.path, updated)
        return {"changed": key, "value": str(value), "config_path": str(target), "restart_scope": "future-jobs"}

    def disable_configured_entry(self, entry_key: str) -> dict[str, object]:
        """Disable one configured integration or message channel.

        Removing the named TOML table is safer than keeping a dormant
        credential-bearing configuration around.  This boundary never reads
        secret values and intentionally cannot enable a new capability.
        """

        normalized = entry_key.strip()
        known = {
            **{f"integration.{name}": name for name, _title, _effect in _INTEGRATION_TABLES},
            **{f"channel.{name}": name for name, _title in _CHANNEL_TABLES},
        }
        table = known.get(normalized)
        if table is None:
            raise ValueError("Only configured integrations and messaging channels can be disabled here")
        settings = _read(self.path)
        if not _table(settings, table):
            raise ValueError("That capability is not configured")
        target = remove_optional_settings_table(self.path, table)
        return {"disabled": normalized, "config_path": str(target), "restart_scope": "future-jobs"}

    def tool_definitions(self) -> tuple[ToolDefinition, ToolDefinition]:
        def validate_inspect(arguments: Mapping[str, object]) -> Mapping[str, object]:
            if set(arguments) - {"category"}:
                raise ToolValidationError("Only optional category is accepted")
            category = str(arguments.get("category", "")).strip()
            if len(category) > 64:
                raise ToolValidationError("Category is too long")
            return {"category": category}

        async def inspect(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            payload = self.summary()
            category = str(arguments["category"])
            if category:
                payload["entries"] = tuple(item for item in payload["entries"] if item["category"].lower() == category.lower())
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)

        def validate_change(arguments: Mapping[str, object]) -> Mapping[str, object]:
            if set(arguments) != {"key", "value"}:
                raise ToolValidationError("settings change requires exactly key and value")
            return {"key": str(arguments["key"]), "value": arguments["value"]}

        async def apply(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            result = self.apply_global_change(arguments)
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        return (
            ToolDefinition(
                name="inspect_global_settings", description="Inspect Noruct's redacted global Settings Center. Secret values are never available.",
                input_schema={"type": "object", "properties": {"category": {"type": "string"}}, "additionalProperties": False},
                effect=ToolEffect.READ, risk=ToolRisk.LOW, idempotency_mode=IdempotencyMode.NATURAL_KEY,
                validator=validate_inspect, resource_key=lambda _: "noruct:settings:global", handler=inspect,
                parallel_safe=True,
            ),
            ToolDefinition(
                name="apply_global_setting", description="Propose and, after user approval, apply one bounded secret-free global setting for future Noruct jobs.",
                input_schema={"type": "object", "properties": {"key": {"type": "string"}, "value": {}}, "required": ["key", "value"], "additionalProperties": False},
                effect=ToolEffect.WRITE, risk=ToolRisk.MEDIUM, idempotency_mode=IdempotencyMode.NATURAL_KEY,
                validator=validate_change, resource_key=lambda item: f"noruct:settings:global:{item['key']}", handler=apply,
                requires_approval=True, approval_preview=lambda item: f"Change global Noruct setting {item['key']} to {item['value']!r}. This affects future jobs; no secret is written.",
            ),
        )
