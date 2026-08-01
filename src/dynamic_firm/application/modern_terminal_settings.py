from __future__ import annotations

"""Bounded global runtime settings commands for the Modern product surface.

The adapter changes only future-job configuration.  Company Roster/Skill
changes continue through their separate proposal lifecycle, and no credential
value is read, displayed, or stored here.
"""

from typing import Any

from dataclasses import fields
import json
from dynamic_firm.providers.profiles import PROVIDER_KINDS, provider_profile
from dynamic_firm.product.company_coordination_settings import (
    CompanyCoordinationSettings,
    write_company_coordination_settings,
)
from dynamic_firm.product.local_routing_settings import (
    LocalRoutingSettings,
    load_local_routing_settings,
    write_local_routing_settings,
)
from dynamic_firm.product.settings_registry import SettingsRegistry
from dynamic_firm.product.modern_tui import ModernTerminalCommandResult
from dynamic_firm.company.user_routing_policy import (
    ApprovedRouteMetadata,
    ApprovedRouteRegistry,
    UserRoutingPolicy,
    UserRoutingPolicyMode,
)


_SYNTHETIC_ROUTE_ONBOARDING_FIELDS = frozenset(
    {
        "fixture",
        "route_id",
        "execution_route_binding_digest",
        "provider_config_digest",
        "credential_reference",
    }
)


def _synthetic_route_onboarding_metadata(argument: str) -> ApprovedRouteMetadata:
    """Parse a provider-free route fixture without activating any route.

    The marker makes this narrow local RC command incapable of presenting an
    arbitrary provider configuration as a real approval flow.  The stored
    value remains the existing secret-free ``ApprovedRouteMetadata`` only.
    """

    try:
        payload = json.loads(argument)
    except json.JSONDecodeError as exc:
        raise ValueError("Synthetic route onboarding payload was malformed.") from exc
    if not isinstance(payload, dict) or set(payload) != _SYNTHETIC_ROUTE_ONBOARDING_FIELDS:
        raise ValueError("Synthetic route onboarding payload has unknown or missing fields.")
    if payload.get("fixture") != "SYNTHETIC_PROVIDER_FREE":
        raise ValueError("Route onboarding accepts only the provider-free synthetic fixture.")
    try:
        return ApprovedRouteMetadata(
            route_id=payload["route_id"],
            execution_route_binding_digest=payload["execution_route_binding_digest"],
            provider_config_digest=payload["provider_config_digest"],
            credential_reference=payload["credential_reference"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Synthetic route onboarding metadata is invalid.") from exc


def _onboard_synthetic_route(
    owner: Any,
    argument: str,
) -> ModernTerminalCommandResult:
    """Preview or explicitly retain one synthetic approved-route fixture."""

    action, separator, payload = argument.partition(" ")
    if action not in {"preview", "confirm"} or not separator or not payload.strip():
        return ModernTerminalCommandResult(messages=(
            "Usage: /routing-onboard [preview|confirm] SYNTHETIC_PROVIDER_FREE_JSON",
            "Preview never writes. Confirm records only a local synthetic route fixture; it does not activate a provider, credential, or egress.",
        ))
    try:
        route = _synthetic_route_onboarding_metadata(payload.strip())
        current = load_local_routing_settings(owner.config.config_path)
    except (OSError, ValueError) as exc:
        return ModernTerminalCommandResult(messages=(f"Synthetic route onboarding was refused · {exc}",))
    existing = next(
        (item for item in current.approved_routes.routes if item.route_id == route.route_id),
        None,
    )
    if action == "preview":
        status = "already retained" if existing == route else (
            "conflicts with retained route" if existing is not None else "ready for explicit local confirmation"
        )
        return ModernTerminalCommandResult(messages=(
            f"Synthetic route preview · {route.route_id} · {status}",
            "No registry, credential reference, egress state, provider activation, or external request changed.",
        ))
    if existing == route:
        return ModernTerminalCommandResult(messages=(
            f"Synthetic route already retained · {route.route_id}",
            "Approved-route reuse needs no repeated confirmation and does not activate a provider, credential, or egress.",
        ))
    if existing is not None:
        return ModernTerminalCommandResult(messages=(
            "Synthetic route confirmation was refused because the retained route identifier has different frozen metadata.",
        ))
    try:
        write_local_routing_settings(
            owner.config.config_path,
            LocalRoutingSettings(
                policy=current.policy,
                approved_routes=ApprovedRouteRegistry((*current.approved_routes.routes, route)),
            ),
        )
    except (OSError, ValueError) as exc:
        return ModernTerminalCommandResult(messages=(f"Synthetic route confirmation was not saved · {exc}",))
    return ModernTerminalCommandResult(messages=(
        f"Synthetic route retained locally · {route.route_id}",
        "This records no credential value and activates no provider or egress; it applies only to future local route admission.",
    ))

def execute_runtime_settings_command(owner: Any, command: str, argument: str) -> ModernTerminalCommandResult | None:
    """Apply connection, global runtime, and authority posture settings commands."""

    if command == "/routing-onboard":
        return _onboard_synthetic_route(owner, argument)
    if command == "/routing-policy":
        target = owner.config.config_path
        options = "QUALITY_FIRST|BALANCED|EFFICIENT|PRIVATE_LOCAL_FIRST"
        tokens = argument.split()
        if not tokens:
            try:
                current = load_local_routing_settings(target)
            except (OSError, ValueError) as exc:
                return ModernTerminalCommandResult(
                    messages=(f"Local routing policy could not be read · {exc}",)
                )
            return ModernTerminalCommandResult(messages=(
                f"Local routing policy · {current.policy.mode.value}",
                f"Usage: /routing-policy [{options}]",
                "This preference affects future Jobs only and does not activate routes, credentials, or egress.",
            ))
        if len(tokens) != 1:
            return ModernTerminalCommandResult(messages=(
                f"Usage: /routing-policy [{options}]",
            ))
        try:
            mode = UserRoutingPolicyMode(tokens[0])
        except ValueError:
            return ModernTerminalCommandResult(messages=(
                f"Routing policy must be one of {options}.",
            ))
        try:
            current = load_local_routing_settings(target)
            updated = LocalRoutingSettings(
                policy=UserRoutingPolicy(mode),
                approved_routes=current.approved_routes,
            )
            write_local_routing_settings(target, updated)
        except (OSError, ValueError) as exc:
            return ModernTerminalCommandResult(
                messages=(f"Local routing policy was not saved · {exc}",)
            )
        return ModernTerminalCommandResult(messages=(
            f"Local routing policy · {current.policy.mode.value} → {mode.value}",
            "Applied to future Jobs only; this does not activate routes, credentials, or egress.",
        ))
    if command == "/company-coordination":
        try:
            payload = json.loads(argument)
        except json.JSONDecodeError:
            return ModernTerminalCommandResult(messages=("Company coordination settings payload was malformed.",))
        if not isinstance(payload, dict) or set(payload) - {
            "enabled", "endpoint", "company_scope_digest", "device_id",
            "token_env", "allow_insecure_loopback",
        }:
            return ModernTerminalCommandResult(messages=("Company coordination settings contain an unsupported field.",))
        if payload.get("enabled") is not False and payload.get("enabled") is not True:
            return ModernTerminalCommandResult(messages=("Company coordination requires an explicit enabled boolean.",))
        enabled = payload["enabled"]
        if not enabled and set(payload) != {"enabled"}:
            return ModernTerminalCommandResult(messages=("Disabled Company coordination accepts only enabled=false.",))
        if enabled:
            required = {"enabled", "endpoint", "company_scope_digest", "device_id", "token_env", "allow_insecure_loopback"}
            if set(payload) != required or any(not isinstance(payload[key], str) for key in required - {"enabled", "allow_insecure_loopback"}) or not isinstance(payload["allow_insecure_loopback"], bool):
                return ModernTerminalCommandResult(messages=("Company coordination needs endpoint, scope digest, device ID, token environment name, and a loopback boolean.",))
        try:
            settings = CompanyCoordinationSettings(
                enabled=enabled,
                endpoint=str(payload.get("endpoint", "")).strip(),
                company_scope_digest=str(payload.get("company_scope_digest", "")).strip(),
                device_id=str(payload.get("device_id", "")).strip(),
                token_env=str(payload.get("token_env", "NORUCT_COMPANY_COORDINATION_TOKEN")).strip(),
                allow_insecure_loopback=payload.get("allow_insecure_loopback", False) is True,
            )
            target = write_company_coordination_settings(owner.config.config_path, settings)
            owner.settings = owner.ports.load_config(target)
        except (OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Company coordination was not saved · {exc}",))
        if not enabled:
            return ModernTerminalCommandResult(messages=("Multi-device Company coordination disabled for future Jobs. No token was read.",))
        return ModernTerminalCommandResult(messages=(
            f"Multi-device Company coordination saved for {settings.device_id}. It applies to future Jobs; no network request was made.",
            "Only opaque resource leases and receipt-bound continuation claims use this endpoint. Work Orders, prompts, results, Memory, and token values remain local.",
        ))
    if command == "/provider":
        if not argument:
            return ModernTerminalCommandResult(
                messages=(
                    f"Provider · {owner.config.provider_kind}",
                    "Set with /provider <kind>. The Settings Center can stage provider, endpoint, executable, credential environment name, model, and timeout together.",
                )
            )
        selected = argument.strip().lower().replace("-", "_")
        if selected not in PROVIDER_KINDS:
            return ModernTerminalCommandResult(messages=("Unsupported provider kind. Use /settings to choose a configured provider profile.",))
        changes: dict[str, object] = {"provider_kind": selected}
        if selected not in {"openai_codex", "external_exec"}:
            profile = provider_profile(selected)
            changes.update(
                base_url=profile.base_url,
                api_key_env=profile.api_key_env or "",
                no_auth=profile.api_key_env is None,
            )
        try:
            previous = owner.config.provider_kind
            owner._persist_global_runtime_defaults(**changes)
        except ValueError as exc:
            return ModernTerminalCommandResult(messages=(f"Provider change was not saved · {exc}",))
        return ModernTerminalCommandResult(messages=(f"Provider · {previous} → {owner.config.provider_kind}",))
    if command == "/connection":
        # Reserved for the Settings Center.  Connection values are saved
        # together so provider validation never observes a half-switched
        # API/executable profile.  The payload contains only non-secret
        # metadata; credential values remain outside Noruct.
        try:
            payload = json.loads(argument)
        except json.JSONDecodeError:
            return ModernTerminalCommandResult(messages=("Settings Center connection payload was malformed.",))
        if not isinstance(payload, dict) or not payload:
            return ModernTerminalCommandResult(messages=("Settings Center connection payload must be a non-empty object.",))
        allowed = {
            "provider_kind", "base_url", "model", "api_key_env",
            "codex_command", "external_command", "request_timeout", "stale_timeout", "no_auth",
        }
        if set(payload) - allowed or any(not isinstance(value, (str, int, float, bool)) for value in payload.values()):
            return ModernTerminalCommandResult(messages=("Settings Center connection payload contains an unsupported value.",))
        changes: dict[str, object] = {}
        selected = str(payload.get("provider_kind", owner.config.provider_kind)).strip().lower().replace("-", "_")
        if selected not in PROVIDER_KINDS:
            return ModernTerminalCommandResult(messages=("Unsupported provider kind.",))
        changes["provider_kind"] = selected
        if selected not in {"openai_codex", "external_exec"} and selected != owner.config.provider_kind:
            profile = provider_profile(selected)
            changes.update(
                base_url=profile.base_url,
                api_key_env=profile.api_key_env or "",
                no_auth=profile.api_key_env is None,
            )
        for field in allowed - {"provider_kind"}:
            if field not in payload:
                continue
            value = payload[field]
            if isinstance(value, str):
                value = value.strip()
                if not value or len(value.encode("utf-8")) > 512 or "\n" in value or "\r" in value:
                    return ModernTerminalCommandResult(messages=("Connection values must be bounded, non-empty single lines.",))
            changes[field] = value
        endpoint = changes.get("base_url")
        if isinstance(endpoint, str) and endpoint and not (endpoint.startswith("https://") or endpoint.startswith("http://127.0.0.1") or endpoint.startswith("http://localhost")):
            return ModernTerminalCommandResult(messages=("Provider endpoint must use HTTPS or an explicit loopback HTTP endpoint.",))
        auth_env = changes.get("api_key_env")
        if isinstance(auth_env, str) and auth_env and not (auth_env[0].isalpha() or auth_env[0] == "_"):
            return ModernTerminalCommandResult(messages=("Credential environment variable name is invalid.",))
        if isinstance(auth_env, str) and auth_env and not all(character.isalnum() or character == "_" for character in auth_env):
            return ModernTerminalCommandResult(messages=("Credential environment variable name is invalid.",))
        if "no_auth" in changes and not isinstance(changes["no_auth"], bool):
            return ModernTerminalCommandResult(messages=("No-auth selection must be a boolean.",))
        timeout = changes.get("request_timeout")
        if timeout is not None:
            try:
                timeout = float(timeout)
                if not 0 < timeout <= 3_600:
                    raise ValueError
            except (TypeError, ValueError):
                return ModernTerminalCommandResult(messages=("Provider hard guard must be between 0 and 3600 seconds.",))
            changes["request_timeout"] = timeout
        stale_timeout = changes.get("stale_timeout")
        if stale_timeout is not None:
            try:
                stale_timeout = float(stale_timeout)
                if not 0 < stale_timeout <= 600:
                    raise ValueError
            except (TypeError, ValueError):
                return ModernTerminalCommandResult(messages=("No-progress deadline must be between 0 and 600 seconds.",))
            changes["stale_timeout"] = stale_timeout
        try:
            previous = owner.config.provider_kind
            owner._persist_global_runtime_defaults(**changes)
        except ValueError as exc:
            return ModernTerminalCommandResult(messages=(f"Connection changes were not saved · {exc}",))
        return ModernTerminalCommandResult(messages=(f"Connection saved · {previous} → {owner.config.provider_kind}. It applies to future Jobs.",))
    if command in {"/endpoint", "/auth-env", "/codex-command", "/external-command", "/request-timeout", "/stale-timeout"}:
        if not argument:
            return ModernTerminalCommandResult(messages=(f"{command} requires one value. Open /settings for field guidance.",))
        value = argument.strip()
        if len(value.encode("utf-8")) > 512 or "\n" in value or "\r" in value:
            return ModernTerminalCommandResult(messages=("Connection setting must be one bounded line.",))
        field = {
            "/endpoint": "base_url",
            "/auth-env": "api_key_env",
            "/codex-command": "codex_command",
            "/external-command": "external_command",
            "/request-timeout": "request_timeout",
            "/stale-timeout": "stale_timeout",
        }[command]
        if field == "api_key_env":
            if not (value and (value[0].isalpha() or value[0] == "_") and all(character.isalnum() or character == "_" for character in value)):
                return ModernTerminalCommandResult(messages=("Credential environment variable name is invalid.",))
        if field == "request_timeout":
            try:
                numeric = float(value)
                if not 0 < numeric <= 3_600:
                    raise ValueError
                value = numeric
            except ValueError:
                return ModernTerminalCommandResult(messages=("Provider hard guard must be between 0 and 3600 seconds.",))
        if field == "stale_timeout":
            try:
                numeric = float(value)
                if not 0 < numeric <= 600:
                    raise ValueError
                value = numeric
            except ValueError:
                return ModernTerminalCommandResult(messages=("No-progress deadline must be between 0 and 600 seconds.",))
        if field == "base_url" and not (value.startswith("https://") or value.startswith("http://127.0.0.1") or value.startswith("http://localhost")):
            return ModernTerminalCommandResult(messages=("Provider endpoint must use HTTPS or an explicit loopback HTTP endpoint.",))
        try:
            owner._persist_global_runtime_defaults(**{field: value})
        except ValueError as exc:
            return ModernTerminalCommandResult(messages=(f"Connection setting was not saved · {exc}",))
        return ModernTerminalCommandResult(messages=(f"Connection setting · {field} updated for future Jobs.",))
    if command == "/setting":
        key, separator, raw_value = argument.partition(" ")
        if not separator or not raw_value.strip():
            return ModernTerminalCommandResult(messages=("Use /setting <key> <value> from the Settings Center.",))
        fields: dict[str, tuple[str, type[float] | type[int]]] = {
            "run.max_cost_usd": ("max_cost_usd", float),
            "run.max_model_calls": ("max_model_calls", int),
            "run.max_tool_calls": ("max_tool_calls", int),
            "run.max_wall_time": ("max_wall_time", float),
        }
        target = fields.get(key)
        if target is None:
            return ModernTerminalCommandResult(messages=("That Settings Center value is not writable.",))
        field, caster = target
        try:
            value = caster(raw_value.strip())
            invalid = value < 0 if field == "max_cost_usd" else value <= 0
            if invalid:
                raise ValueError
        except ValueError:
            return ModernTerminalCommandResult(messages=("Setting value must be within its positive bound.",))
        owner._persist_global_runtime_defaults(**{field: value})
        return ModernTerminalCommandResult(messages=(f"Global setting · {key} → {value}",))
    if command == "/settings-disable":
        if not argument:
            return ModernTerminalCommandResult(messages=("Use /settings-disable integration.<name> or channel.<name> from the Settings Center.",))
        try:
            result = SettingsRegistry(owner.config.config_path).disable_configured_entry(argument)
            owner.settings = owner.ports.load_config(owner.config.config_path)
            owner._persist_global_runtime_defaults()
        except ValueError as exc:
            return ModernTerminalCommandResult(messages=(f"Could not disable setting · {exc}",))
        return ModernTerminalCommandResult(
            messages=(f"Disabled {result['disabled']} for future Jobs. Its configuration table was removed; credentials remain only in your environment.",)
        )
    if command == "/external-state":
        if not argument:
            return ModernTerminalCommandResult(
                messages=(
                    f"External state changes · {owner.config.external_state_mode}",
                    "Set with /external-state blocked, /external-state ask, or /external-state user-authorized-auto. The Capability trust profile decides whether a granted action needs another dialog; credentials are never exposed to the agent.",
                )
            )
        normalized = argument.lower().replace("_", "-")
        if normalized not in {"blocked", "ask", "user-authorized-auto"}:
            return ModernTerminalCommandResult(messages=("External state mode must be blocked, ask, or user-authorized-auto.",))
        previous = owner.config.external_state_mode
        owner._persist_global_runtime_defaults(external_state_mode=normalized)
        return ModernTerminalCommandResult(
            messages=(f"External state changes · {previous} → {normalized}", "Applied to future Jobs. user-authorized-auto works with /trust trusted or /trust autonomous; configuration itself never exposes or stores credentials."),
        )
    if command == "/trust":
        if not argument:
            return ModernTerminalCommandResult(
                messages=(
                    f"Capability trust profile · {owner.config.capability_trust_mode}",
                    "strict prompts for every effect; trusted auto-runs ordinary workspace work and explicitly installed plugins; autonomous auto-runs every already enabled capability. Tool intent and result audit remains on in every profile.",
                )
            )
        normalized = argument.lower().strip().replace("_", "-")
        if normalized not in {"strict", "trusted", "autonomous"}:
            return ModernTerminalCommandResult(messages=("Trust profile must be strict, trusted, or autonomous.",))
        previous = owner.config.capability_trust_mode
        owner._persist_global_runtime_defaults(capability_trust_mode=normalized)
        return ModernTerminalCommandResult(
            messages=(
                f"Capability trust profile · {previous} → {normalized}",
                "Applied to future Jobs. This does not create a new capability or expose credentials; it only changes approval friction for already granted tools.",
            )
        )
    if command == "/external-read":
        if not argument:
            return ModernTerminalCommandResult(
                messages=(
                    f"External reads · {owner.config.external_read_mode}",
                    "Blocked hides configured MCP/web read/search capabilities. Ask requests approval for each read. Allow restores configured read capabilities without an additional read-specific prompt.",
                )
            )
        normalized = argument.lower()
        if normalized not in {"blocked", "ask", "allow"}:
            return ModernTerminalCommandResult(messages=("External read mode must be blocked, ask, or allow.",))
        previous = owner.config.external_read_mode
        owner._persist_global_runtime_defaults(external_read_mode=normalized)
        return ModernTerminalCommandResult(messages=(f"External reads · {previous} → {normalized}",))
    if command == "/agent-settings":
        if not argument:
            return ModernTerminalCommandResult(
                messages=(
                    f"Agent setting proposals · {owner.config.agent_settings_mode}",
                    "Ask exposes only the bounded, approval-gated setting proposal tool in explicit Company Jobs. Blocked keeps settings user-only.",
                )
            )
        normalized = argument.lower()
        if normalized not in {"blocked", "ask"}:
            return ModernTerminalCommandResult(messages=("Agent setting mode must be blocked or ask.",))
        previous = owner.config.agent_settings_mode
        owner._persist_global_runtime_defaults(agent_settings_mode=normalized)
        return ModernTerminalCommandResult(messages=(f"Agent setting proposals · {previous} → {normalized}",))
    if command == "/permission":
        if not argument:
            return ModernTerminalCommandResult(
                messages=(
                    f"Workspace authority · {owner.config.permission_mode}",
                    "Set with /permission ask or /permission read-only. Ask exposes workspace tools; /trust decides whether already-granted work asks again.",
                )
            )
        normalized = argument.lower().replace("_", "-")
        if normalized not in {"ask", "read-only"}:
            return ModernTerminalCommandResult(
                messages=("Permission mode must be ask or read-only.",)
            )
        previous = owner.config.permission_mode
        owner._persist_global_runtime_defaults(permission_mode=normalized)
        return ModernTerminalCommandResult(
            messages=(
                f"Workspace authority · {previous} → {normalized}",
                (
                    "Coding goals can now use workspace tools; /trust strict keeps per-action dialogs while trusted/autonomous follows the selected trust profile."
                    if normalized == "ask"
                    else "Future Jobs are read-only; no workspace mutation tool is exposed."
                ),
            )
        )
    if command == "/tools":
        facts = owner.ports.tui_company_facts(owner.config, owner.roster_snapshot)
        rendered = ", ".join(facts["tools"]) or "none"
        guidance = (
            "Writes are enabled through a disposable shadow workspace; approve the displayed change set to apply it."
            if owner.config.permission_mode == "ask"
            else "Writes are disabled. Use /permission ask to make approval-gated workspace tools available."
        )
        return ModernTerminalCommandResult(
            messages=(
                f"Effective tools · {rendered}",
                guidance,
            )
        )

    return None
