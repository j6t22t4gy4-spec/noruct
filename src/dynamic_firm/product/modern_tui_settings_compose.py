"""Settings Center Textual widget composition.

The owner carries only staged UI state; this component never applies settings or
owns Company state.
"""

from __future__ import annotations

from typing import Any

from dynamic_firm.product.settings_dashboard import PAGES, panel_options
from dynamic_firm.providers.profiles import PROVIDER_SETUP_OPTIONS


def compose_settings_screen(
    owner: Any,
    *,
    ComposeResult: Any,
    Container: Any,
    Grid: Any,
    Horizontal: Any,
    Button: Any,
    Input: Any,
    Static: Any,
) -> Any:
        snapshot = owner._snapshot
        entries = getattr(snapshot, "settings_entries", ())
        with Container(id="settings-card"):
            yield Static("NORUCT SETTINGS CENTER", classes="modal-title")
            with Grid(id="settings-page-grid"):
                for page in PAGES:
                    classes = "settings-page" + (" settings-selected" if page == owner._page else "")
                    yield Button(page, id=f"settings-page-{page.lower()}", classes=classes)
            yield Static(
                "Global defaults · Model: {model} · Authority: {authority} · Workspace: {workspace}".format(
                    model=getattr(snapshot, "model", "unavailable"),
                    authority=getattr(snapshot, "authority", "unavailable"),
                    workspace=getattr(snapshot, "workspace", "unavailable"),
                ),
                markup=False,
            )
            yield Static(
                "Stage policies below; Done applies them. Nothing changes while Settings is open.",
                id="settings-pending",
                classes="settings-entry",
                markup=False,
            )
            # This bar is docked to the modal viewport. Long capability
            # pages scroll behind it without hiding the only apply/cancel
            # boundary.
            with Horizontal(id="settings-commit", classes="settings-actions"):
                yield Button("Cancel", id="settings-close")
                yield Button("Reset staged", id="settings-reset")
                yield Button("Done", id="settings-done", variant="success")
            controls = owner._page_controls()
            if owner._page == "Connection":
                yield Static("AUTHENTICATION", classes="settings-section")
                yield Static(
                    "Choose account sign-in or an API/local connection. Noruct never reads or stores provider credentials.",
                    classes="settings-entry",
                )
                with Horizontal(classes="settings-actions"):
                    yield Button(
                        "OpenAI account sign-in",
                        id="settings-auth-account",
                        classes=owner._app_choice_classes(owner._provider_kind, "openai_codex"),
                    )
                    yield Button(
                        "API key / local server",
                        id="settings-auth-api",
                        classes=owner._app_choice_classes(
                            "api" if owner._provider_kind not in {"openai_codex", "external_exec"} else owner._provider_kind,
                            "api",
                        ),
                    )
                if owner._provider_kind == "openai_codex":
                    with Horizontal(classes="settings-actions"):
                        yield Button(
                            "Start OpenAI sign-in after Done",
                            id="settings-provider-login",
                            classes="settings-choice",
                        )
                yield Static("SELECTED CONNECTION", classes="settings-section")
                yield Input(value=owner._provider_kind, placeholder="Provider kind", id="settings-provider-kind")
                yield Static("MODEL", classes="settings-section")
                yield Input(value=owner._values.get("provider.model", str(getattr(snapshot, "model", ""))), placeholder="Model identifier", id="settings-model-input")
                with Horizontal(classes="settings-actions"):
                    yield Button("Choose model", id="settings-model-picker", classes="settings-choice")
                if owner._provider_kind == "openai_codex":
                    yield Input(value=owner._values.get("provider.codex_command", "").replace("not configured", "") or "codex", placeholder="Codex executable", id="settings-provider-codex-command")
                elif owner._provider_kind == "external_exec":
                    yield Input(value=owner._values.get("provider.external_command", "").replace("not configured", ""), placeholder="External bridge executable", id="settings-provider-external-command")
                else:
                    yield Input(value=owner._values.get("provider.base_url", "").replace("not used", ""), placeholder="Provider endpoint", id="settings-provider-endpoint")
                    yield Input(value=owner._values.get("provider.api_key_env", "").replace("not required", ""), placeholder="API key environment variable name", id="settings-provider-auth-env")
                    yield Static("Authentication", classes="settings-row")
                    with Horizontal(classes="settings-actions"):
                        yield Button(
                            "Environment credential",
                            id="settings-provider-auth-environment",
                            classes=owner._app_choice_classes("none" if owner._provider_no_auth else "environment", "environment"),
                        )
                        yield Button(
                            "No auth (local/compatible)",
                            id="settings-provider-auth-none",
                            classes=owner._app_choice_classes("none" if owner._provider_no_auth else "environment", "none"),
                        )
                yield Input(value=owner._values.get("provider.stale_timeout", "").removesuffix("s"), placeholder="No-progress deadline seconds (Codex)", id="settings-provider-stale-timeout")
                yield Input(value=owner._values.get("provider.request_timeout", "").removesuffix("s"), placeholder="Hard provider guard seconds", id="settings-provider-timeout")
                with Horizontal(classes="settings-actions"):
                    yield Button("Save connection after Done", id="settings-connection-stage", classes="settings-choice setting-model")
                yield Static("AVAILABLE PROVIDERS", classes="settings-section")
                yield Static("Selecting a provider updates the non-secret form above; it does not save until Done.", classes="settings-entry")
                with Grid(id="settings-provider-grid"):
                    for provider_kind, label, _description in PROVIDER_SETUP_OPTIONS:
                        yield Button(
                            label,
                            id=f"settings-provider-{provider_kind}",
                            classes=owner._provider_choice_classes(provider_kind),
                        )
            if owner._page == "Execution":
                yield Static("Workspace changes and terminal commands", classes="settings-row")
                with Horizontal(classes="settings-actions"):
                    yield Button("Ask before edits & commands", id="settings-permission-ask", classes=owner._choice_classes("workspace", "/permission ask"))
                    yield Button("Read-only", id="settings-permission-read-only", classes=owner._choice_classes("workspace", "/permission read-only"))
                yield Static("Capability trust profile", classes="settings-row")
                with Horizontal(classes="settings-actions"):
                    yield Button("Strict · ask each effect", id="settings-trust-strict", classes=owner._choice_classes("trust", "/trust strict"))
                    yield Button("Trusted · ordinary work", id="settings-trust-trusted", classes=owner._choice_classes("trust", "/trust trusted"))
                    yield Button("Autonomous · enabled tools", id="settings-trust-autonomous", classes=owner._choice_classes("trust", "/trust autonomous"))
                yield Static("Trust changes only dialog frequency for already enabled, bounded capabilities. Every tool intent and result remains in the Job audit.", classes="settings-entry", markup=False)
                yield Static("External read authority", classes="settings-row")
                with Horizontal(classes="settings-actions"):
                    yield Button("Block", id="settings-read-blocked", classes=owner._choice_classes("external-read", "/external-read blocked"))
                    yield Button("Ask for each read", id="settings-read-ask", classes=owner._choice_classes("external-read", "/external-read ask"))
                    yield Button("Allow configured reads", id="settings-read-allow", classes=owner._choice_classes("external-read", "/external-read allow"))
                yield Static("Context mode", classes="settings-row")
                with Horizontal(classes="settings-actions"):
                    yield Button("Standard", id="settings-mode-standard", classes=owner._choice_classes("cost", "/mode standard"))
                    yield Button("Economy", id="settings-mode-economy", classes=owner._choice_classes("cost", "/mode economy"))
                yield Static("External state changes", classes="settings-row")
                with Horizontal(classes="settings-actions"):
                    yield Button("Blocked", id="settings-external-blocked", classes=owner._choice_classes("external-state", "/external-state blocked"))
                    yield Button("Ask", id="settings-external-ask", classes=owner._choice_classes("external-state", "/external-state ask"))
                    yield Button("Authorized auto", id="settings-external-auto", classes=owner._choice_classes("external-state", "/external-state user-authorized-auto"))
                yield Static("Agent setting proposals", classes="settings-row")
                with Horizontal(classes="settings-actions"):
                    yield Button("Ask before agent change", id="settings-agent-ask", classes=owner._choice_classes("agent-settings", "/agent-settings ask"))
                    yield Button("User-only settings", id="settings-agent-blocked", classes=owner._choice_classes("agent-settings", "/agent-settings blocked"))
                yield Static("Future-job execution envelope (defaults are deliberately long-running)", classes="settings-row")
                yield Input(value=owner._values.get("run.max_cost_usd", ""), placeholder="Cost envelope USD", id="settings-limit-cost")
                yield Input(value=owner._values.get("run.max_model_calls", ""), placeholder="Model-call envelope", id="settings-limit-model-calls")
                yield Input(value=owner._values.get("run.max_tool_calls", ""), placeholder="Tool-call envelope", id="settings-limit-tool-calls")
                yield Input(value=owner._values.get("run.max_wall_time", ""), placeholder="Wall-time envelope seconds (default: 86400)", id="settings-limit-wall-time")
                with Horizontal(classes="settings-actions"):
                    yield Button("Stage run limits", id="settings-limits-stage", classes="settings-choice setting-limits")
            if owner._page == "Company":
                with Grid(id="settings-company-grid"):
                    for option in panel_options("Company"):
                        yield Button(option.label, id=f"settings-company-{option.key}", classes=owner._app_choice_classes(owner._company_kind, option.key))
                if owner._company_kind == "manager":
                    yield Static(
                        "A Manager profile revision is a proposed ROSTER Patch. It never changes a running Job and still needs explicit approval then apply.",
                        classes="settings-entry", markup=False,
                    )
                    yield Input(
                        value=owner._values.get("company.manager.model_profile", ""),
                        placeholder="Manager model profile", id="settings-manager-model-profile",
                    )
                    yield Input(
                        value=owner._values.get("company.manager.role", ""),
                        placeholder="Manager role", id="settings-manager-role",
                    )
                    yield Input(
                        placeholder="Why should this Manager profile change?", id="settings-manager-rationale",
                    )
                    with Horizontal(classes="settings-actions"):
                        yield Button("Stage Manager revision", id="settings-manager-stage", classes="settings-choice")
                elif owner._company_kind == "employees":
                    yield Static(
                        "Employee changes create an approval-only ROSTER Patch. Capabilities affect only future Job staffing; they do not grant new tool or external authority.",
                        classes="settings-entry", markup=False,
                    )
                    yield Input(placeholder="Existing employee ID", id="settings-employee-id")
                    yield Input(placeholder="Role", id="settings-employee-role")
                    yield Input(placeholder="Capabilities, comma separated", id="settings-employee-capabilities")
                    yield Input(placeholder="Model profile", id="settings-employee-model-profile")
                    yield Input(placeholder="Why should this employee change?", id="settings-employee-rationale")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Stage Employee revision", id="settings-employee-stage", classes="settings-choice")
                elif owner._company_kind == "skills":
                    yield Static(
                        "Skill procedures are versioned employee instructions, not a toggle. Staging creates an approval-only Skill Patch from one confirmed operator correction; it never changes a running Employee instance.",
                        classes="settings-entry", markup=False,
                    )
                    yield Input(placeholder="Existing employee ID", id="settings-skill-employee-id")
                    yield Input(placeholder="Skill key", id="settings-skill-key")
                    yield Input(placeholder="Applicability context key", id="settings-skill-context")
                    yield Input(placeholder="Purpose", id="settings-skill-purpose")
                    yield Input(placeholder="Procedure steps, separated by |", id="settings-skill-steps")
                    yield Input(placeholder="Verification steps, separated by |", id="settings-skill-verification")
                    yield Input(placeholder="Optional prohibitions, separated by |", id="settings-skill-prohibitions")
                    yield Input(placeholder="Confirmed correction ID", id="settings-skill-correction")
                    yield Input(placeholder="Why should this procedure be proposed?", id="settings-skill-rationale")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Stage Skill Patch", id="settings-skill-stage", classes="settings-choice")
                elif owner._company_kind == "delegation":
                    yield Static(
                        "Delegation preferences are local defaults for a future Work Order. They choose a Blueprint, Employee constraints, concurrency, cost/time ceilings, review, and bounded mutation posture; no current Job changes.",
                        classes="settings-entry", markup=False,
                    )
                    with Horizontal(classes="settings-actions"):
                        yield Button("Open Graph & delegation controls", id="settings-delegation-open-graph", classes="settings-choice")
                elif owner._company_kind == "retention":
                    yield Static("Reversible Company learning review", classes="settings-row")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Approval", id="settings-review-approval", classes=owner._choice_classes("review", "/review approval"))
                        yield Button("Auto review", id="settings-review-auto-review", classes=owner._choice_classes("review", "/review auto-review"))
                        yield Button("Always approve", id="settings-review-always-approve", classes=owner._choice_classes("review", "/review always-approve"))
                elif owner._company_kind == "coordination":
                    yield Static(
                        "Opt-in coordination prevents two authorized devices from claiming the same external effect and permits only receipt-bound same-Job continuation. It never uploads Work Orders, prompts, results, Memory, or credential values.",
                        classes="settings-entry",
                        markup=False,
                    )
                    yield Input(
                        value=owner._values.get("company.coordination.endpoint", ""),
                        placeholder="HTTPS coordination origin (or loopback development origin)",
                        id="settings-coordination-endpoint",
                    )
                    yield Input(
                        value=owner._values.get("company.coordination.scope", ""),
                        placeholder="Company scope SHA-256 digest", id="settings-coordination-scope",
                    )
                    yield Input(
                        value=owner._values.get("company.coordination.device", ""),
                        placeholder="Device id, e.g. device-laptop", id="settings-coordination-device",
                    )
                    yield Input(
                        value=owner._values.get("company.coordination.token_env", "NORUCT_COMPANY_COORDINATION_TOKEN"),
                        placeholder="Token environment variable name", id="settings-coordination-token-env",
                    )
                    yield Input(
                        value=owner._values.get("company.coordination.loopback", "no"),
                        placeholder="Allow insecure loopback development only? yes or no",
                        id="settings-coordination-loopback",
                    )
                    with Horizontal(classes="settings-actions"):
                        yield Button("Stage coordination", id="settings-coordination-stage", classes="settings-choice")
                        yield Button("Disable coordination", id="settings-coordination-disable", classes="settings-choice")
                else:
                    yield Static(
                        "Employee Skill patches remain approval-only. Retention auto-review and always-approve never grant automatic Skill mutation or rollback authority.",
                        classes="settings-entry",
                        markup=False,
                    )
            if owner._page == "Data":
                with Grid(id="settings-data-grid"):
                    for option in panel_options("Data"):
                        yield Button(option.label, id=f"settings-data-{option.key}", classes=owner._app_choice_classes(owner._data_kind, option.key))
                if owner._data_kind == "knowledge":
                    yield Static(
                        "Knowledge assets, evidence, intent, and decisions stay in the local vault. Use /knowledge, /intent, /decision, /question, and /research for data operations; opening Settings never uploads or rewrites them.",
                        classes="settings-entry",
                        markup=False,
                    )
                else:
                    yield Static("Local artifact evolution policy", classes="settings-row")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Never", id="settings-evolution-never", classes=owner._choice_classes("evolution", "/evolution never"))
                        yield Button("Propose", id="settings-evolution-propose", classes=owner._choice_classes("evolution", "/evolution propose"))
                        yield Button("Always approve", id="settings-evolution-always-approve", classes=owner._choice_classes("evolution", "/evolution always-approve"))
                    yield Static(
                        "This controls local compatible artifact activation only. Shared-network contribution remains a separate explicit opt-in and is not changed here.",
                        classes="settings-entry",
                        markup=False,
                    )
            if owner._page == "Network":
                with Grid(id="settings-network-grid"):
                    for option in panel_options("Network"):
                        yield Button(option.label, id=f"settings-network-{option.key}", classes=owner._app_choice_classes(owner._network_kind, option.key))
                network_copy = {
                    "sources": (
                        "Trusted sources are signed origins, not executable plugins. "
                        "Registering a source only records its public key policy locally. A private-team source also records one private Registry id and a token environment-variable name; it is never remotely enumerated and no token value enters Settings.",
                        "Open source status",
                        "/network",
                    ),
                    "catalog": (
                        "Search sees only locally installed or signature-verified staged templates. "
                        "Remote catalog metadata never becomes a recommendation before verification.",
                        "Search local catalog",
                        "/network search",
                    ),
                    "install": (
                        "Installation is deliberately two-stage: a reviewed snapshot installs an inactive exact version, then an explicit activation pins it for future Jobs. "
                        "Rollback restores the preceding installed version without changing a running Job.",
                        "Show install and rollback guidance",
                        "/network install",
                    ),
                    "updates": (
                        "Imported capabilities stay exact and pinned until you explicitly activate one reviewed version for a future Job.",
                        "Open update status",
                        "/network updates",
                    ),
                    "permissions": (
                        "A template is configuration, not authority. Skills compose bounded snapshots, Tools bind registered local adapters, and every activation declares the capabilities it may use.",
                        "Show activation boundary",
                        "/network permissions",
                    ),
                    "trust": (
                        "The lifecycle is discover → verify signature → stage → review → install → activate → pin → rollback. "
                        "A Network artifact cannot introduce credentials, new authority, or arbitrary downloaded code.",
                        "Show Network boundary",
                        "/network trust",
                    ),
                }
                copy, label, command = network_copy[owner._network_kind]
                yield Static(copy, classes="settings-entry", markup=False)
                with Horizontal(classes="settings-actions"):
                    yield Button(label, id="settings-network-open", classes="settings-choice")
                if owner._network_kind == "sources":
                    yield Static("ADD TRUSTED SOURCE", classes="settings-section")
                    yield Static(
                        "This stores a signer policy only. Use FIRST_PARTY, COMMUNITY, or PRIVATE_TEAM; a private source needs a Registry id and an environment-variable name, never a token value.",
                        classes="settings-entry", markup=False,
                    )
                    yield Input(placeholder="Stable source ID", id="settings-network-source-id")
                    yield Input(value="COMMUNITY", placeholder="FIRST_PARTY, COMMUNITY, or PRIVATE_TEAM", id="settings-network-publisher-class")
                    yield Input(placeholder="HTTPS origin (or explicit loopback development origin)", id="settings-network-origin")
                    yield Input(placeholder="Allowed OpenSSH signers file", id="settings-network-allowed-signers")
                    yield Input(placeholder="Signer principal", id="settings-network-principal")
                    yield Input(value="ssh-keygen", placeholder="OpenSSH ssh-keygen executable", id="settings-network-ssh-keygen")
                    yield Input(value="local-owner", placeholder="Local operator ID", id="settings-network-operator-id")
                    yield Input(placeholder="Private credential environment-variable name (PRIVATE_TEAM only)", id="settings-network-credential-env")
                    yield Input(placeholder="Private Registry ID (PRIVATE_TEAM only)", id="settings-network-private-registry")
                    yield Input(value="no", placeholder="Allow insecure loopback development? yes or no", id="settings-network-loopback")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Stage source registration", id="settings-network-source-stage", classes="settings-choice")
                elif owner._network_kind == "catalog":
                    yield Input(placeholder="Search installed or verified staged templates", id="settings-network-search-query")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Stage catalog search", id="settings-network-search-stage", classes="settings-choice")
                elif owner._network_kind == "install":
                    yield Static("LIFECYCLE ACTIONS", classes="settings-section")
                    yield Static(
                        "Enter only the fields for the action you choose. Review/installation never activates an Artifact; activation affects future Jobs only.",
                        classes="settings-entry", markup=False,
                    )
                    yield Input(placeholder="Trusted source ID (stage only)", id="settings-network-action-source")
                    yield Input(placeholder="Registry ID (stage only)", id="settings-network-action-registry")
                    yield Input(placeholder="Snapshot ID", id="settings-network-action-snapshot")
                    yield Input(value="local-owner", placeholder="Review operator ID", id="settings-network-action-operator")
                    yield Input(placeholder="Review reason", id="settings-network-action-reason")
                    yield Input(placeholder="Artifact ID", id="settings-network-action-artifact")
                    yield Input(placeholder="Exact Artifact version", id="settings-network-action-version")
                    yield Input(value="company_default", placeholder="Activation scope", id="settings-network-action-scope")
                    yield Input(placeholder="Permitted capabilities, comma separated", id="settings-network-action-capabilities")
                    yield Input(value="PINNED", placeholder="PINNED or PROPOSE; activation is always explicit", id="settings-network-action-update-mode")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Stage Registry", id="settings-network-stage-registry", classes="settings-choice")
                        yield Button("Approve", id="settings-network-review-approve", classes="settings-choice")
                        yield Button("Reject", id="settings-network-review-reject", classes="settings-choice")
                        yield Button("Install", id="settings-network-install-artifact", classes="settings-choice")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Activate", id="settings-network-activate-artifact", classes="settings-choice")
                        yield Button("Rollback", id="settings-network-rollback-artifact", classes="settings-choice")
                        yield Button("Save update policy", id="settings-network-update-policy", classes="settings-choice")
            if owner._page in {"Integrations", "Messaging", "Environment"}:
                configured = [
                    (button_id, key, command)
                    for button_id, (key, command) in owner._disable_actions.items()
                    if any(item.key == key for item in controls)
                ]
                if configured:
                    yield Static("Configured capabilities", classes="settings-row")
                    for button_id, key, _command in configured:
                        with Horizontal(classes="settings-actions"):
                            yield Button(f"Disable {key.split('.', 1)[-1].replace('_', ' ').title()}", id=button_id, classes="settings-choice")
                else:
                    yield Static("No configured capability to disable on this page. Use the typed configure command to add credentials or endpoints; secret values never appear here.", classes="settings-entry", markup=False)
            if owner._page == "Integrations":
                yield Static("Choose one integration to configure.", classes="settings-row")
                with Grid(id="settings-integration-grid"):
                    for option in panel_options("Integrations"):
                        yield Button(option.label, id=f"settings-integration-{option.key}", classes=owner._app_choice_classes(owner._integration_kind, option.key))
                if owner._integration_kind == "web-search":
                    yield Input(placeholder="HTTPS or loopback SearXNG URL", id="settings-web-search-url")
                    with Horizontal(classes="settings-actions"): yield Button("Stage web search", id="settings-web-search-stage", classes="settings-choice")
                elif owner._integration_kind == "media":
                    yield Input(value="OPENAI_API_KEY", placeholder="Credential environment variable name", id="settings-media-env")
                    yield Input(value="image", placeholder="image, speech, transcription, video", id="settings-media-capabilities")
                    with Horizontal(classes="settings-actions"): yield Button("Stage media", id="settings-media-stage", classes="settings-choice")
                elif owner._integration_kind == "plugin":
                    yield Input(placeholder="Local directory containing noruct-plugin.json", id="settings-plugin-path")
                    with Horizontal(classes="settings-actions"): yield Button("Stage install & enable", id="settings-plugin-stage", classes="settings-choice")
                elif owner._integration_kind == "mcp":
                    yield Input(placeholder="Absolute Python executable", id="settings-mcp-python")
                    yield Input(placeholder="Absolute MCP server executable/script", id="settings-mcp-server")
                    yield Input(value="read_context", placeholder="Read-only MCP tool name", id="settings-mcp-tool")
                    with Horizontal(classes="settings-actions"): yield Button("Stage MCP", id="settings-mcp-stage", classes="settings-choice")
                elif owner._integration_kind == "mcp-action":
                    yield Input(placeholder="Absolute Python executable", id="settings-mcp-action-python")
                    yield Input(placeholder="Absolute MCP action server executable/script", id="settings-mcp-action-server")
                    yield Input(value="run_action", placeholder="One action tool name", id="settings-mcp-action-tool")
                    with Horizontal(classes="settings-actions"): yield Button("Stage MCP action", id="settings-mcp-action-stage", classes="settings-choice")
                elif owner._integration_kind == "skills":
                    yield Input(placeholder="Existing skill directories, comma separated", id="settings-skills-roots")
                    with Horizontal(classes="settings-actions"): yield Button("Stage skill roots", id="settings-skills-stage", classes="settings-choice")
                else:
                    yield Input(placeholder="Home Assistant HTTPS or loopback URL", id="settings-home-assistant-url")
                    yield Input(value="HASS_TOKEN", placeholder="Token environment variable name", id="settings-home-assistant-token-env")
                    yield Input(placeholder="Allowed entity patterns, comma separated", id="settings-home-assistant-entities")
                    yield Input(placeholder="Optional allowed services, comma separated", id="settings-home-assistant-services")
                    with Horizontal(classes="settings-actions"): yield Button("Stage Home Assistant", id="settings-home-assistant-stage", classes="settings-choice")
            if owner._page == "Messaging":
                yield Static("Choose an inbox or outbound app. Secret values remain outside Noruct.", classes="settings-row")
                with Grid(id="settings-channel-direction-grid"):
                    yield Button("Inbox", id="settings-channel-direction-inbound", classes=owner._app_choice_classes(owner._channel_direction, "inbound"))
                    yield Button("Outbound", id="settings-channel-direction-outbound", classes=owner._app_choice_classes(owner._channel_direction, "outbound"))
                with Grid(id="settings-channel-grid"):
                    choices = (
                        (
                            ("telegram", "Telegram"), ("slack", "Slack"),
                            ("discord", "Discord"), ("ntfy", "ntfy"),
                            ("email", "Email"), ("matrix", "Matrix"),
                            ("mattermost", "Mattermost"), ("custom", "Custom bridge"),
                        )
                        if owner._channel_direction == "inbound"
                        else (
                            ("slack", "Slack"), ("discord", "Discord"),
                            ("ntfy", "ntfy"), ("email", "Email"),
                            ("matrix", "Matrix"), ("mattermost", "Mattermost"),
                            ("teams", "Teams"), ("dingtalk", "DingTalk"),
                            ("custom", "Custom bridge"),
                        )
                    )
                    for channel_kind, label in choices:
                        yield Button(label, id=f"settings-channel-{channel_kind}", classes=owner._channel_choice_classes(channel_kind))
                with Horizontal(classes="settings-actions"):
                    yield Button("Stage selected app", id="settings-channel-stage", classes="settings-choice")
                if owner._channel_direction == "inbound" and owner._channel_kind == "telegram":
                    yield Input(value=str(getattr(snapshot, "workspace", "")), placeholder="Existing absolute workspace", id="settings-channel-field-one")
                    yield Input(placeholder="Allowed Telegram sender IDs, comma separated", id="settings-channel-field-two")
                    yield Input(value="TELEGRAM_BOT_TOKEN", placeholder="Bot-token environment variable name", id="settings-channel-field-three")
                elif owner._channel_direction == "inbound" and owner._channel_kind in {"slack", "discord"}:
                    app = owner._channel_kind.title()
                    default_env = "SLACK_SIGNING_SECRET" if owner._channel_kind == "slack" else "DISCORD_BOT_TOKEN"
                    yield Input(value=str(getattr(snapshot, "workspace", "")), placeholder="Existing absolute workspace", id="settings-channel-field-one")
                    yield Input(placeholder=f"Allowed {app} sender IDs, comma separated", id="settings-channel-field-two")
                    yield Input(placeholder=f"Allowed {app} channel IDs, comma separated", id="settings-channel-field-three")
                    yield Input(value=default_env, placeholder="Credential environment variable name", id="settings-channel-field-four")
                elif owner._channel_direction == "inbound" and owner._channel_kind == "ntfy":
                    yield Input(value=str(getattr(snapshot, "workspace", "")), placeholder="Existing absolute workspace", id="settings-channel-field-one")
                    yield Input(placeholder="Private ntfy topic", id="settings-channel-field-two")
                    yield Input(value="https://ntfy.sh", placeholder="HTTPS or loopback ntfy server", id="settings-channel-field-three")
                    yield Input(placeholder="Optional token environment variable name", id="settings-channel-field-four")
                elif owner._channel_direction == "inbound" and owner._channel_kind == "email":
                    yield Input(value=str(getattr(snapshot, "workspace", "")), placeholder="Existing absolute workspace", id="settings-channel-field-one")
                    yield Input(placeholder="Mailbox email address", id="settings-channel-field-two")
                    yield Input(placeholder="IMAP host", id="settings-channel-field-three")
                    yield Input(placeholder="Allowed sender addresses, comma separated", id="settings-channel-field-four")
                    yield Input(value="EMAIL_PASSWORD", placeholder="Password environment variable name", id="settings-channel-field-five")
                elif owner._channel_direction == "inbound" and owner._channel_kind in {"matrix", "mattermost"}:
                    yield Input(value=str(getattr(snapshot, "workspace", "")), placeholder="Existing absolute workspace", id="settings-channel-field-one")
                    yield Input(placeholder="Homeserver URL" if owner._channel_kind == "matrix" else "Mattermost base URL", id="settings-channel-field-two")
                    yield Input(placeholder="Canonical room ID" if owner._channel_kind == "matrix" else "Mattermost channel ID", id="settings-channel-field-three")
                    yield Input(placeholder="Allowed sender IDs, comma separated", id="settings-channel-field-four")
                    yield Input(value="MATRIX_ACCESS_TOKEN" if owner._channel_kind == "matrix" else "MATTERMOST_TOKEN", placeholder="Token environment variable name", id="settings-channel-field-five")
                elif owner._channel_direction == "inbound":
                    yield Input(placeholder="Stable source ID", id="settings-channel-field-one")
                    yield Input(placeholder="Absolute receiver executable", id="settings-channel-field-two")
                    yield Input(value=str(getattr(snapshot, "workspace", "")), placeholder="Existing absolute workspace", id="settings-channel-field-three")
                    yield Input(placeholder="Allowed sender IDs, comma separated", id="settings-channel-field-four")
                    yield Input(placeholder="Environment variable names, comma separated", id="settings-channel-field-five")
                elif owner._channel_kind == "slack":
                    yield Input(placeholder="Slack channel ID", id="settings-channel-field-one")
                    yield Input(value="SLACK_BOT_TOKEN", placeholder="Bot-token environment variable name", id="settings-channel-field-two")
                elif owner._channel_kind in {"discord", "teams", "dingtalk"}:
                    default_env = {"discord": "DISCORD_WEBHOOK_URL", "teams": "TEAMS_WEBHOOK_URL", "dingtalk": "DINGTALK_WEBHOOK_URL"}[owner._channel_kind]
                    yield Input(value=default_env, placeholder="Webhook environment variable name", id="settings-channel-field-one")
                elif owner._channel_kind == "ntfy":
                    yield Input(placeholder="Private ntfy topic", id="settings-channel-field-one")
                    yield Input(value="https://ntfy.sh", placeholder="HTTPS or loopback ntfy server", id="settings-channel-field-two")
                    yield Input(placeholder="Optional token environment variable name", id="settings-channel-field-three")
                elif owner._channel_kind == "email":
                    yield Input(placeholder="Sender email address", id="settings-channel-field-one")
                    yield Input(placeholder="Allowlisted recipient addresses, comma separated", id="settings-channel-field-two")
                    yield Input(placeholder="SMTP host", id="settings-channel-field-three")
                    yield Input(value="EMAIL_PASSWORD", placeholder="SMTP password environment variable name", id="settings-channel-field-four")
                elif owner._channel_kind == "matrix":
                    yield Input(placeholder="Matrix homeserver URL", id="settings-channel-field-one")
                    yield Input(placeholder="Canonical Matrix !room:server", id="settings-channel-field-two")
                    yield Input(value="MATRIX_ACCESS_TOKEN", placeholder="Access-token environment variable name", id="settings-channel-field-three")
                elif owner._channel_kind == "mattermost":
                    yield Input(placeholder="Mattermost base URL", id="settings-channel-field-one")
                    yield Input(placeholder="Mattermost channel ID", id="settings-channel-field-two")
                    yield Input(value="MATTERMOST_TOKEN", placeholder="Token environment variable name", id="settings-channel-field-three")
                else:
                    yield Input(placeholder="Absolute delivery executable", id="settings-channel-field-one")
                    yield Input(placeholder="Environment variable names, comma separated", id="settings-channel-field-two")
                    yield Input(placeholder="Optional fixed non-secret args, comma separated", id="settings-channel-field-three")
            if owner._page == "Automation":
                with Grid(id="settings-automation-grid"):
                    for option in panel_options("Automation"):
                        yield Button(option.label, id=f"settings-automation-{option.key}", classes=owner._app_choice_classes(owner._automation_kind, option.key))
                if owner._automation_kind == "schedule":
                    yield Input(placeholder="Self-contained future Company goal", id="settings-schedule-goal")
                    yield Input(placeholder="Interval minutes (1–43200)", id="settings-schedule-every")
                    yield Input(placeholder="Optional schedule name", id="settings-schedule-name")
                    yield Input(value=str(getattr(snapshot, "workspace", "")), placeholder="Existing absolute workspace", id="settings-schedule-workspace")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Stage schedule", id="settings-schedule-stage", classes="settings-choice")
                elif owner._automation_kind == "schedule-service":
                    yield Static("Local schedule dispatcher", classes="settings-row")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Status", id="settings-schedule-service-status", classes="settings-choice")
                        yield Button("Start", id="settings-schedule-service-start", classes="settings-choice")
                        yield Button("Stop", id="settings-schedule-service-stop", classes="settings-choice")
                elif owner._automation_kind == "gateway":
                    yield Input(placeholder="Receivers: telegram, slack, discord, email, ntfy, matrix, mattermost", id="settings-gateway-receivers")
                    with Horizontal(classes="settings-actions"):
                        yield Button("Status", id="settings-gateway-status", classes="settings-choice")
                        yield Button("Start", id="settings-gateway-start", classes="settings-choice")
                        yield Button("Stop", id="settings-gateway-stop", classes="settings-choice")
            if owner._page == "Environment":
                with Grid(id="settings-environment-grid"):
                    for option in panel_options("Environment"):
                        yield Button(option.label, id=f"settings-environment-{option.key}", classes=owner._app_choice_classes(owner._environment_kind, option.key))
                if owner._environment_kind == "browser":
                    yield Input(placeholder="Absolute Node executable", id="settings-browser-node")
                    yield Input(placeholder="Loopback CDP endpoint", id="settings-browser-endpoint")
                    yield Input(value="yes", placeholder="Enable approved control? yes or no", id="settings-browser-control")
                    yield Input(placeholder="Optional capture directory", id="settings-browser-capture-directory")
                    with Horizontal(classes="settings-actions"): yield Button("Stage Browser", id="settings-browser-stage", classes="settings-choice")
                elif owner._environment_kind == "computer":
                    yield Input(placeholder="Absolute desktop-driver executable", id="settings-computer-driver")
                    yield Input(placeholder="Allowed apps, comma separated", id="settings-computer-apps")
                    yield Input(value="yes", placeholder="Enable approved control? yes or no", id="settings-computer-control")
                    with Horizontal(classes="settings-actions"): yield Button("Stage Computer use", id="settings-computer-stage", classes="settings-choice")
                elif owner._environment_kind == "container":
                    yield Input(placeholder="Container image reference", id="settings-container-image")
                    yield Input(value="docker", placeholder="Docker executable or command", id="settings-container-docker")
                    yield Input(placeholder="Allowed programs: id=/absolute/program, comma separated", id="settings-container-programs")
                    with Horizontal(classes="settings-actions"): yield Button("Stage container", id="settings-container-stage", classes="settings-choice")
                elif owner._environment_kind == "remote":
                    yield Input(placeholder="Remote target ID", id="settings-remote-target-id")
                    yield Input(placeholder="Verified snapshot transfer receipt path", id="settings-remote-receipt")
                    yield Input(placeholder="Allowed remote programs: id=/absolute/program, comma separated", id="settings-remote-programs")
                    yield Input(placeholder="Optional SSH identity-file path", id="settings-remote-identity-file")
                    with Horizontal(classes="settings-actions"): yield Button("Stage remote worker", id="settings-remote-stage", classes="settings-choice")
            # The capability inventory is intentionally after page-local
            # controls: common edits remain visible even in a small
            # terminal, while every supported lane is still a selectable
            # GUI-like control rather than inert descriptive text.
            yield Static(f"{owner._page.upper()} CAPABILITY INVENTORY · {len(controls)}", classes="settings-section")
            for control in controls:
                classes = "settings-capability" + (
                    " settings-focused" if control.key == owner._focused_key else ""
                )
                yield Button(control.label, id=control.id, classes=classes)
            focused = owner._focused_control()
            if focused is None:
                yield Static(
                    "Select any capability to inspect its authority, configuration status, and next action.",
                    id="settings-detail", markup=False,
                )
            else:
                writable = "Agent proposals supported." if focused.agent_writable else "User-only configuration."
                setup = f"\nSetup: {focused.setup_hint}" if focused.setup_hint else ""
                yield Static(
                    f"{focused.title}\n{focused.scope} · {focused.effect} · {focused.state}\n"
                    f"{focused.summary}\n{writable}{setup}",
                    id="settings-detail", markup=False,
                )
