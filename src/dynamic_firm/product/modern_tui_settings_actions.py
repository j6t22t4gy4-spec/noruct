"""Settings Center staged-command event handling.

The handler only records commands in the owner's SettingsCommandDraft. The
caller retains the modal dismissal and controller apply boundary.
"""

from __future__ import annotations

import json
from typing import Any

from dynamic_firm.providers.profiles import provider_profile

from .modern_tui_settings_network_actions import handle_network_settings_action


async def handle_settings_button(
    owner: Any,
    event: Any,
    *,
    NoMatches: Any,
    Static: Any,
    Input: Any,
    Button: Any,
) -> None:
    button_id = event.button.id or ""
    if button_id.startswith("settings-page-"):
        owner._page = button_id.removeprefix("settings-page-").title()
        owner._focused_key = ""
        await owner.recompose()
        return
    if button_id == "settings-auth-account":
        if owner._provider_kind != "openai_codex":
            owner._values["provider.model"] = "codex-default"
        owner._provider_kind = "openai_codex"
        owner._values["provider.kind"] = "openai_codex"
        await owner.recompose()
        return
    if button_id == "settings-auth-api":
        owner._provider_kind = "openai_api"
        owner._values["provider.kind"] = "openai_api"
        profile = provider_profile("openai_api")
        owner._values["provider.base_url"] = profile.base_url
        owner._values["provider.api_key_env"] = profile.api_key_env or ""
        owner._provider_no_auth = False
        await owner.recompose()
        return
    if button_id.startswith("settings-provider-"):
        if button_id == "settings-provider-auth-environment":
            owner._provider_no_auth = False
            await owner.recompose()
            return
        if button_id == "settings-provider-auth-none":
            owner._provider_no_auth = True
            await owner.recompose()
            return
        if button_id == "settings-provider-login":
            codex_command = owner.query_one("#settings-provider-codex-command", Input).value.strip() or "codex"
            model = owner.query_one("#settings-model-input", Input).value.strip()
            payload: dict[str, object] = {
                "provider_kind": "openai_codex",
                "codex_command": codex_command,
            }
            if model:
                payload["model"] = model
            owner._pending["connection"] = "/connection " + json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )
            owner._pending["provider-login"] = "/provider-login"
            event.button.add_class("settings-selected")
            rendered = " · ".join(owner._pending.values())
            owner.query_one("#settings-pending", Static).update(
                f"Pending ({len(owner._pending)}): {rendered}\nDone saves the connection, then hands the terminal to the provider sign-in."
            )
            return
        selected_provider = button_id.removeprefix("settings-provider-")
        if selected_provider == "openai_codex" and owner._provider_kind != "openai_codex":
            owner._values["provider.model"] = "codex-default"
        if selected_provider != owner._provider_kind and selected_provider not in {"openai_codex", "external_exec"}:
            profile = provider_profile(selected_provider)
            owner._values["provider.base_url"] = profile.base_url
            owner._values["provider.api_key_env"] = profile.api_key_env or ""
            owner._provider_no_auth = profile.api_key_env is None
        owner._provider_kind = selected_provider
        await owner.recompose()
        return
    if button_id.startswith("settings-channel-direction-"):
        owner._channel_direction = button_id.removeprefix("settings-channel-direction-")
        owner._channel_kind = "telegram" if owner._channel_direction == "inbound" else "slack"
        await owner.recompose()
        return
    if button_id.startswith("settings-channel-") and button_id != "settings-channel-stage":
        owner._channel_kind = button_id.removeprefix("settings-channel-")
        await owner.recompose()
        return
    if button_id.startswith("settings-integration-"):
        owner._integration_kind = button_id.removeprefix("settings-integration-")
        await owner.recompose()
        return
    if button_id.startswith("settings-environment-"):
        owner._environment_kind = button_id.removeprefix("settings-environment-")
        await owner.recompose()
        return
    if button_id.startswith("settings-automation-"):
        owner._automation_kind = button_id.removeprefix("settings-automation-")
        await owner.recompose()
        return
    if button_id.startswith("settings-company-"):
        owner._company_kind = button_id.removeprefix("settings-company-")
        await owner.recompose()
        return
    if button_id.startswith("settings-data-"):
        owner._data_kind = button_id.removeprefix("settings-data-")
        await owner.recompose()
        return
    if await handle_network_settings_action(owner, event, Input=Input, Static=Static):
        return
    selected = next(
        (item for item in owner._page_controls() if item.id == button_id),
        None,
    )
    if selected is not None:
        owner._focused_key = selected.key
        await owner.recompose()
        return
    if button_id == "settings-close":
        owner.dismiss(None)
        return
    if button_id == "settings-reset":
        owner._reset_staged_changes()
        await owner.recompose()
        return
    if button_id == "settings-done":
        owner.dismiss(owner._ordered_pending_commands())
        return
    if button_id == "settings-model-picker":
        owner._pending["model-picker"] = "/model"
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to apply, or Cancel to discard."
        )
        return
    commands = {
        "settings-permission-ask": ("workspace", "/permission ask"),
        "settings-permission-read-only": ("workspace", "/permission read-only"),
        "settings-trust-strict": ("trust", "/trust strict"),
        "settings-trust-trusted": ("trust", "/trust trusted"),
        "settings-trust-autonomous": ("trust", "/trust autonomous"),
        "settings-read-allow": ("external-read", "/external-read allow"),
        "settings-read-ask": ("external-read", "/external-read ask"),
        "settings-read-blocked": ("external-read", "/external-read blocked"),
        "settings-mode-standard": ("cost", "/mode standard"),
        "settings-mode-economy": ("cost", "/mode economy"),
        "settings-review-approval": ("review", "/review approval"),
        "settings-review-auto-review": ("review", "/review auto-review"),
        "settings-review-always-approve": ("review", "/review always-approve"),
        "settings-evolution-never": ("evolution", "/evolution never"),
        "settings-evolution-propose": ("evolution", "/evolution propose"),
        "settings-evolution-always-approve": ("evolution", "/evolution always-approve"),
        "settings-external-blocked": ("external-state", "/external-state blocked"),
        "settings-external-ask": ("external-state", "/external-state ask"),
        "settings-external-auto": ("external-state", "/external-state user-authorized-auto"),
        "settings-agent-ask": ("agent-settings", "/agent-settings ask"),
        "settings-agent-blocked": ("agent-settings", "/agent-settings blocked"),
    }
    disable = owner._disable_actions.get(button_id)
    if disable is not None:
        key, command = disable
        owner._pending[f"disable:{key}"] = command
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to apply, or Cancel to discard."
        )
        return
    if button_id == "settings-connection-stage":
        provider_value = owner.query_one("#settings-provider-kind", Input).value.strip().lower().replace("-", "_")
        connection_fields = (
            ("provider_kind", "#settings-provider-kind", "provider.kind"),
            ("base_url", "#settings-provider-endpoint", "provider.base_url"),
            ("api_key_env", "#settings-provider-auth-env", "provider.api_key_env"),
            ("codex_command", "#settings-provider-codex-command", "provider.codex_command"),
            ("external_command", "#settings-provider-external-command", "provider.external_command"),
            ("model", "#settings-model-input", "provider.model"),
            ("stale_timeout", "#settings-provider-stale-timeout", "provider.stale_timeout"),
            ("request_timeout", "#settings-provider-timeout", "provider.request_timeout"),
        )
        changes: dict[str, object] = {}
        for key, selector, _settings_key in connection_fields:
            try:
                field = owner.query_one(selector, Input)
            except NoMatches:
                continue
            value = field.value.strip()
            if key == "provider_kind":
                value = provider_value
            # Send the complete visible profile.  A model-only edit on
            # a custom compatible endpoint must not silently reset the
            # endpoint to the provider catalog default.
            if value:
                changes[key] = value
        if provider_value not in {"openai_codex", "external_exec"}:
            changes["no_auth"] = owner._provider_no_auth
        if not changes:
            owner.query_one("#settings-pending", Static).update(
                "Change at least one non-secret connection value before staging it."
            )
            return
        # One transaction prevents a provider switch from being
        # rejected midway because its model/endpoint/bridge command
        # has not been persisted yet.
        command = "/connection " + json.dumps(changes, ensure_ascii=False, separators=(",", ":"))
        owner._pending["connection"] = command
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to apply, or Cancel to discard."
        )
        return
    if button_id == "settings-limits-stage":
        fields = (
            ("run.max_cost_usd", "#settings-limit-cost", float, 0.0),
            ("run.max_model_calls", "#settings-limit-model-calls", int, 1),
            ("run.max_tool_calls", "#settings-limit-tool-calls", int, 1),
            ("run.max_wall_time", "#settings-limit-wall-time", float, 0.001),
        )
        staged: list[str] = []
        try:
            for key, selector, cast, minimum in fields:
                raw = owner.query_one(selector, Input).value.strip()
                if not raw:
                    continue
                value = cast(raw)
                if value < minimum:
                    raise ValueError
                command = f"/setting {key} {value}"
                owner._pending[key] = command
                staged.append(command)
        except ValueError:
            owner.query_one("#settings-pending", Static).update(
                "Run limits must be numeric and positive (cost may be zero)."
            )
            return
        if not staged:
            owner.query_one("#settings-pending", Static).update(
                "Enter at least one run limit before staging it."
            )
            return
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to apply, or Cancel to discard."
        )
        return
    if button_id == "settings-manager-stage":
        model_profile = owner.query_one("#settings-manager-model-profile", Input).value.strip()
        role = owner.query_one("#settings-manager-role", Input).value.strip()
        rationale = owner.query_one("#settings-manager-rationale", Input).value.strip()
        if not model_profile or not role or not rationale:
            owner.query_one("#settings-pending", Static).update(
                "Manager model profile, role, and a rationale are required."
            )
            return
        owner._pending["company:manager"] = "/company-manager-revise " + json.dumps(
            {"model_profile": model_profile, "role": role, "rationale": rationale},
            ensure_ascii=False, separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        owner.query_one("#settings-pending", Static).update(
            "Pending Manager ROSTER proposal. Done creates the proposal only; review and apply stay explicit."
        )
        return
    if button_id == "settings-employee-stage":
        employee_id = owner.query_one("#settings-employee-id", Input).value.strip()
        role = owner.query_one("#settings-employee-role", Input).value.strip()
        capabilities = tuple(
            item.strip()
            for item in owner.query_one("#settings-employee-capabilities", Input).value.split(",")
            if item.strip()
        )
        model_profile = owner.query_one("#settings-employee-model-profile", Input).value.strip()
        rationale = owner.query_one("#settings-employee-rationale", Input).value.strip()
        if not employee_id or not role or not capabilities or not model_profile or not rationale:
            owner.query_one("#settings-pending", Static).update(
                "Employee ID, role, at least one capability, model profile, and a rationale are required."
            )
            return
        owner._pending["company:employee"] = "/company-employee-revise " + json.dumps(
            {
                "employee_id": employee_id,
                "role": role,
                "capabilities": capabilities,
                "model_profile": model_profile,
                "rationale": rationale,
            },
            ensure_ascii=False, separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        owner.query_one("#settings-pending", Static).update(
            "Pending Employee ROSTER proposal. Done creates the proposal only; review and apply stay explicit."
        )
        return
    if button_id == "settings-skill-stage":
        def parts(selector: str) -> tuple[str, ...]:
            return tuple(
                item.strip()
                for item in owner.query_one(selector, Input).value.split("|")
                if item.strip()
            )

        payload = {
            "employee_id": owner.query_one("#settings-skill-employee-id", Input).value.strip(),
            "skill_key": owner.query_one("#settings-skill-key", Input).value.strip(),
            "context_key": owner.query_one("#settings-skill-context", Input).value.strip(),
            "purpose": owner.query_one("#settings-skill-purpose", Input).value.strip(),
            "steps": parts("#settings-skill-steps"),
            "verification_steps": parts("#settings-skill-verification"),
            "prohibitions": parts("#settings-skill-prohibitions"),
            "correction_id": owner.query_one("#settings-skill-correction", Input).value.strip(),
            "rationale": owner.query_one("#settings-skill-rationale", Input).value.strip(),
        }
        if (
            not all(payload[key] for key in ("employee_id", "skill_key", "context_key", "purpose", "correction_id", "rationale"))
            or not payload["steps"]
            or not payload["verification_steps"]
        ):
            owner.query_one("#settings-pending", Static).update(
                "Employee, skill/context keys, purpose, procedure and verification steps, confirmed correction ID, and rationale are required."
            )
            return
        owner._pending["company:skill"] = "/company-skill-propose " + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )
        event.button.add_class("settings-selected")
        owner.query_one("#settings-pending", Static).update(
            "Pending Employee Skill Patch proposal. Done creates the proposal only; Skill approval, apply, and rollback remain explicit."
        )
        return
    if button_id == "settings-delegation-open-graph":
        owner._pending["company:delegation"] = "/graph"
        event.button.add_class("settings-selected")
        owner.query_one("#settings-pending", Static).update(
            "Done opens Future Job Graph controls; nothing has been changed yet."
        )
        return
    if button_id == "settings-coordination-stage":
        endpoint = owner.query_one("#settings-coordination-endpoint", Input).value.strip()
        scope = owner.query_one("#settings-coordination-scope", Input).value.strip()
        device = owner.query_one("#settings-coordination-device", Input).value.strip()
        token_env = owner.query_one("#settings-coordination-token-env", Input).value.strip()
        raw_loopback = owner.query_one("#settings-coordination-loopback", Input).value.strip().lower()
        if raw_loopback not in {"yes", "no", "true", "false"}:
            owner.query_one("#settings-pending", Static).update(
                "Loopback development must be yes or no."
            )
            return
        if not endpoint or not scope or not device or not token_env:
            owner.query_one("#settings-pending", Static).update(
                "Endpoint, scope digest, device ID, and token environment name are required. The token value is never entered here."
            )
            return
        owner._pending["company:coordination"] = "/company-coordination " + json.dumps(
            {
                "enabled": True,
                "endpoint": endpoint,
                "company_scope_digest": scope,
                "device_id": device,
                "token_env": token_env,
                "allow_insecure_loopback": raw_loopback in {"yes", "true"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\\nDone verifies the named environment token and saves only non-secret coordination metadata. No network request is made."
        )
        return
    if button_id == "settings-coordination-disable":
        owner._pending["company:coordination"] = "/company-coordination {\"enabled\":false}"
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\\nDone disables remote coordination for future Jobs; no token is read."
        )
        return
    if button_id == "settings-web-search-stage":
        value = owner.query_one("#settings-web-search-url", Input).value.strip()
        if not value:
            owner.query_one("#settings-pending", Static).update(
                "Enter one HTTPS SearXNG URL, or an explicit loopback HTTP URL."
            )
            return
        owner._pending["integration:web-search"] = "/quick-web-search " + value
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to apply, or Cancel to discard."
        )
        return
    if button_id == "settings-browser-stage":
        node_command = owner.query_one("#settings-browser-node", Input).value.strip()
        endpoint = owner.query_one("#settings-browser-endpoint", Input).value.strip()
        raw_control = owner.query_one("#settings-browser-control", Input).value.strip().lower()
        capture_directory = owner.query_one("#settings-browser-capture-directory", Input).value.strip()
        if raw_control not in {"yes", "no"}:
            owner.query_one("#settings-pending", Static).update("Browser control must be yes or no.")
            return
        if capture_directory and raw_control != "yes":
            owner.query_one("#settings-pending", Static).update("A Browser capture directory requires approved control to be enabled.")
            return
        if not node_command or not endpoint:
            owner.query_one("#settings-pending", Static).update(
                "Enter an absolute Node executable and one loopback CDP endpoint."
            )
            return
        owner._pending["integration:browser"] = "/quick-browser " + json.dumps(
            {
                "node_command": node_command,
                "cdp_endpoint": endpoint,
                "allow_control": raw_control == "yes",
                **({"capture_directory": capture_directory} if capture_directory else {}),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to apply, or Cancel to discard."
        )
        return
    if button_id == "settings-computer-stage":
        driver_command = owner.query_one("#settings-computer-driver", Input).value.strip()
        raw_apps = owner.query_one("#settings-computer-apps", Input).value.strip()
        raw_control = owner.query_one("#settings-computer-control", Input).value.strip().lower()
        apps = [value.strip() for value in raw_apps.split(",") if value.strip()]
        if raw_control not in {"yes", "no"}:
            owner.query_one("#settings-pending", Static).update("Computer control must be yes or no.")
            return
        if not driver_command or not apps:
            owner.query_one("#settings-pending", Static).update(
                "Enter an absolute desktop-driver executable and at least one allowed application."
            )
            return
        owner._pending["integration:computer"] = "/quick-computer " + json.dumps(
            {"driver_command": driver_command, "allowed_apps": apps, "allow_control": raw_control == "yes"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save the local allowlist; every capture or control action remains approved."
        )
        return
    if button_id == "settings-media-stage":
        api_key_env = owner.query_one("#settings-media-env", Input).value.strip()
        capabilities = owner.query_one("#settings-media-capabilities", Input).value.strip()
        if not api_key_env or not capabilities:
            owner.query_one("#settings-pending", Static).update(
                "Enter a credential environment-variable name and at least one media capability."
            )
            return
        owner._pending["integration:media"] = "/quick-media " + json.dumps(
            {"api_key_env": api_key_env, "capabilities": capabilities},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to apply, or Cancel to discard."
        )
        return
    if button_id == "settings-plugin-stage":
        source = owner.query_one("#settings-plugin-path", Input).value.strip()
        if not source:
            owner.query_one("#settings-pending", Static).update(
                "Enter a local plugin directory. Done installs only an inactive exact version."
            )
            return
        owner._pending["integration:plugin"] = "/quick-plugin " + source
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to install inactive; exact-version activation remains a separate confirmed action."
        )
        return
    if button_id == "settings-mcp-stage":
        python_command = owner.query_one("#settings-mcp-python", Input).value.strip()
        server_command = owner.query_one("#settings-mcp-server", Input).value.strip()
        tool_name = owner.query_one("#settings-mcp-tool", Input).value.strip()
        if not python_command or not server_command or not tool_name:
            owner.query_one("#settings-pending", Static).update(
                "Enter Python, a server executable/script, and one read-only MCP tool name."
            )
            return
        owner._pending["integration:mcp"] = "/quick-mcp " + json.dumps(
            {"python_command": python_command, "server_command": server_command, "tool_name": tool_name},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save the bounded read-only MCP profile."
        )
        return
    if button_id == "settings-mcp-action-stage":
        python_command = owner.query_one("#settings-mcp-action-python", Input).value.strip()
        server_command = owner.query_one("#settings-mcp-action-server", Input).value.strip()
        tool_name = owner.query_one("#settings-mcp-action-tool", Input).value.strip()
        if not python_command or not server_command or not tool_name:
            owner.query_one("#settings-pending", Static).update(
                "Enter Python, a server executable/script, and one MCP action tool name."
            )
            return
        owner._pending["integration:mcp-action"] = "/quick-mcp-action " + json.dumps(
            {"python_command": python_command, "server_command": server_command, "tool_name": tool_name},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save one approval-gated MCP action profile."
        )
        return
    if button_id == "settings-skills-stage":
        raw_roots = owner.query_one("#settings-skills-roots", Input).value.strip()
        roots = [value.strip() for value in raw_roots.split(",") if value.strip()]
        if not roots:
            owner.query_one("#settings-pending", Static).update(
                "Enter at least one existing local directory containing compatible SKILL.md files."
            )
            return
        owner._pending["integration:skills"] = "/quick-skills " + json.dumps(
            {"roots": roots}, ensure_ascii=False, separators=(",", ":")
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save external skill roots. Discovery remains read-only."
        )
        return
    if button_id == "settings-home-assistant-stage":
        base_url = owner.query_one("#settings-home-assistant-url", Input).value.strip()
        token_env = owner.query_one("#settings-home-assistant-token-env", Input).value.strip()
        entities = [
            value.strip()
            for value in owner.query_one("#settings-home-assistant-entities", Input).value.split(",")
            if value.strip()
        ]
        services = [
            value.strip()
            for value in owner.query_one("#settings-home-assistant-services", Input).value.split(",")
            if value.strip()
        ]
        if not base_url or not token_env or not entities:
            owner.query_one("#settings-pending", Static).update(
                "Enter a Home Assistant URL, token environment name, and at least one allowed entity pattern."
            )
            return
        owner._pending["integration:home-assistant"] = "/quick-home-assistant " + json.dumps(
            {
                "base_url": base_url,
                "token_env": token_env,
                "allowed_entities": entities,
                "allowed_services": services,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save the allowlist; service calls retain individual approval."
        )
        return
    if button_id == "settings-channel-stage":
        values: dict[str, str] = {}
        for name in ("one", "two", "three", "four", "five"):
            try:
                values[name] = owner.query_one(f"#settings-channel-field-{name}", Input).value.strip()
            except NoMatches:
                continue
        inbound_required = {
            "telegram": ("one", "two", "three"),
            "slack": ("one", "two", "three", "four"),
            "discord": ("one", "two", "three", "four"),
            "ntfy": ("one", "two", "three"),
            "email": ("one", "two", "three", "four", "five"),
            "matrix": ("one", "two", "three", "four", "five"),
            "mattermost": ("one", "two", "three", "four", "five"),
            "custom": ("one", "two", "three", "four"),
        }
        outbound_required = {
            "slack": ("one", "two"),
            "discord": ("one",), "teams": ("one",), "dingtalk": ("one",),
            "ntfy": ("one", "two"), "email": ("one", "two", "three", "four"),
            "matrix": ("one", "two", "three"), "mattermost": ("one", "two", "three"),
            "custom": ("one",),
        }
        required = (
            inbound_required if owner._channel_direction == "inbound" else outbound_required
        )[owner._channel_kind]
        if any(not values.get(name, "") for name in required):
            owner.query_one("#settings-pending", Static).update(
                "Fill every required field for the selected messaging app before staging it."
            )
            return
        owner._pending[f"messaging:{owner._channel_direction}:{owner._channel_kind}"] = "/quick-channel " + json.dumps(
            {"direction": owner._channel_direction, "kind": owner._channel_kind, "fields": values},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save {owner._channel_direction} {owner._channel_kind.title()}; no message is sent and no receiver starts."
        )
        return
    if button_id == "settings-telegram-stage":
        workspace = owner.query_one("#settings-telegram-workspace", Input).value.strip()
        raw_senders = owner.query_one("#settings-telegram-senders", Input).value.strip()
        token_env = owner.query_one("#settings-telegram-token-env", Input).value.strip()
        senders = [value.strip() for value in raw_senders.split(",") if value.strip()]
        if not workspace or not senders or not token_env:
            owner.query_one("#settings-pending", Static).update(
                "Enter an existing workspace, at least one allowlisted sender ID, and a token environment name."
            )
            return
        owner._pending["messaging:telegram"] = "/quick-telegram " + json.dumps(
            {"workspace": workspace, "allowed_senders": senders, "token_env": token_env},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save the Telegram allowlist; it will not start a receiver."
        )
        return
    if button_id == "settings-slack-stage":
        channel_id = owner.query_one("#settings-slack-channel-id", Input).value.strip()
        token_env = owner.query_one("#settings-slack-token-env", Input).value.strip()
        if not channel_id or not token_env:
            owner.query_one("#settings-pending", Static).update(
                "Enter a Slack channel ID and a token environment-variable name."
            )
            return
        owner._pending["messaging:slack"] = "/quick-slack " + json.dumps(
            {"channel_id": channel_id, "token_env": token_env},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save Slack; sending remains an explicit action."
        )
        return
    if button_id == "settings-schedule-stage":
        goal = owner.query_one("#settings-schedule-goal", Input).value.strip()
        every_minutes = owner.query_one("#settings-schedule-every", Input).value.strip()
        name = owner.query_one("#settings-schedule-name", Input).value.strip()
        workspace = owner.query_one("#settings-schedule-workspace", Input).value.strip()
        try:
            interval = int(every_minutes)
        except ValueError:
            interval = 0
        if not goal or not workspace or not 1 <= interval <= 43_200:
            owner.query_one("#settings-pending", Static).update(
                "Enter a goal, existing workspace, and interval from 1 through 43200 minutes."
            )
            return
        owner._pending["automation:schedule"] = "/quick-schedule " + json.dumps(
            {"goal": goal, "every_minutes": interval, "name": name, "workspace": workspace},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to create it locally. It will not run until you explicitly start a schedule service or tick."
        )
        return
    if button_id in {
        "settings-schedule-service-status",
        "settings-schedule-service-start",
        "settings-schedule-service-stop",
    }:
        action = button_id.removeprefix("settings-schedule-service-")
        owner._pending["automation:schedule-service"] = "/schedule-service " + json.dumps(
            {"action": action}, ensure_ascii=False, separators=(",", ":")
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        detail = (
            "Starting creates one local dispatcher after Done; each due Job retains the ordinary provider budget."
            if action == "start"
            else "Choose Done to apply this explicit schedule-service operation."
        )
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\n{detail}"
        )
        return
    if button_id in {"settings-gateway-status", "settings-gateway-start", "settings-gateway-stop"}:
        action = button_id.removeprefix("settings-gateway-")
        raw_receivers = owner.query_one("#settings-gateway-receivers", Input).value.strip()
        receivers = [value.strip().lower() for value in raw_receivers.split(",") if value.strip()]
        if action == "start" and not receivers:
            owner.query_one("#settings-pending", Static).update(
                "Enter at least one configured inbound receiver before starting the gateway service."
            )
            return
        owner._pending["automation:gateway"] = "/gateway-service " + json.dumps(
            {"action": action, "receivers": receivers},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        detail = (
            "Starting will create one local supervisor after Done; selected receivers must be configured and credential-ready."
            if action == "start"
            else "Choose Done to apply this explicit local gateway-service operation."
        )
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\n{detail}"
        )
        return
    if button_id == "settings-container-stage":
        image = owner.query_one("#settings-container-image", Input).value.strip()
        docker_command = owner.query_one("#settings-container-docker", Input).value.strip()
        raw_programs = owner.query_one("#settings-container-programs", Input).value.strip()
        programs: dict[str, list[str]] = {}
        for item in raw_programs.split(","):
            identifier, separator, command = item.strip().partition("=")
            if separator and identifier.strip() and command.strip():
                programs[identifier.strip()] = [command.strip()]
        if not image or not docker_command or not programs:
            owner.query_one("#settings-pending", Static).update(
                "Enter an image, Docker command, and at least one id=/absolute/program allowlist entry."
            )
            return
        owner._pending["environment:container"] = "/quick-container " + json.dumps(
            {"image": image, "docker_command": docker_command, "programs": programs},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save the container allowlist; no image is pulled or container started."
        )
        return
    if button_id == "settings-remote-stage":
        target_id = owner.query_one("#settings-remote-target-id", Input).value.strip()
        receipt = owner.query_one("#settings-remote-receipt", Input).value.strip()
        raw_programs = owner.query_one("#settings-remote-programs", Input).value.strip()
        identity_file = owner.query_one("#settings-remote-identity-file", Input).value.strip()
        programs: dict[str, str] = {}
        for item in raw_programs.split(","):
            identifier, separator, command = item.strip().partition("=")
            if separator and identifier.strip() and command.strip():
                programs[identifier.strip()] = command.strip()
        if not target_id or not receipt or not programs:
            owner.query_one("#settings-pending", Static).update(
                "Enter a target ID, verified receipt path, and at least one id=/absolute/program allowlist entry."
            )
            return
        owner._pending["environment:remote-worker"] = "/quick-remote-worker " + json.dumps(
            {
                "target_id": target_id,
                "receipt": receipt,
                "programs": programs,
                "identity_file": identity_file or None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        event.button.add_class("settings-selected")
        rendered = " · ".join(owner._pending.values())
        owner.query_one("#settings-pending", Static).update(
            f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to save the receipt-bound worker; no SSH connection or remote program is started."
        )
        return
    selection = commands.get(button_id)
    if selection is None:
        return
    group, command = selection
    owner._pending[group] = command
    owner._selected[group] = command
    for button in owner.query(f".setting-{group}"):
        button.remove_class("settings-selected")
    event.button.add_class("settings-selected")
    rendered = " · ".join(owner._pending.values())
    owner.query_one("#settings-pending", Static).update(
        f"Pending ({len(owner._pending)}): {rendered}\nChoose Done to apply, or Cancel to discard."
    )
