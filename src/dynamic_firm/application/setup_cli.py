"""Connection and setup operator command adapter."""

from __future__ import annotations

import hashlib

from dynamic_firm.application.cli_component_contract import cli


_CAPABILITY_RECEIPT_SCHEMA = "noruct.capability-receipt.v1"


def _digest_receipt_payload(value: object) -> str:
    """Return an opaque exact configuration identity without retaining secrets."""

    encoded = cli.json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _external_skill_receipt(catalog: object) -> dict[str, object]:
    """Project the fresh user-owned Skill scan in the common receipt shape."""

    roots = tuple(getattr(catalog, "roots", ()))
    skills = tuple(getattr(catalog, "skills", ()))
    return {
        "schema": _CAPABILITY_RECEIPT_SCHEMA,
        "kind": "EXTERNAL_SKILL",
        "state": "CONNECTED_DISCOVERY" if roots else "NOT_CONFIGURED",
        "configured_root_count": len(roots),
        "artifacts": [
            {
                "content_id": item.snapshot.content_id,
                "revision": item.snapshot.revision,
                "package_manifest_sha256": item.snapshot.content_hash,
                "support_file_count": len(item.support_files),
            }
            for item in skills
        ],
        "automatic_replacement": False,
        "running_job_mutation": False,
        "authority": "fresh_read_only_discovery_only_future_job_selection_is_frozen",
    }


def _mcp_read_receipt(policy: object, status: dict[str, object]) -> dict[str, object]:
    return {
        "schema": _CAPABILITY_RECEIPT_SCHEMA,
        "kind": "MCP_READ",
        "state": "CONFIGURED" if policy is not None else "NOT_CONFIGURED",
        "binding_digest": cli.mcp_session_binding_digest(policy),
        "profile_count": int(status.get("profile_count", 0)),
        "runtime_tool_count": int(status.get("tool_count", 0)),
        "sidecar_ready": bool(status.get("sidecar_ready", False)),
        "automatic_replacement": False,
        "running_job_mutation": False,
        "authority": "configured_policy_only_future_job_grant_and_live_discovery_never_rebind_continuation",
    }


def _mcp_action_receipt(policy: object, status: dict[str, object]) -> dict[str, object]:
    profiles = (
        tuple(cli.mcp_action_configs(policy)) if policy is not None else ()
    )
    identity = [
        {
            "profile": item.profile,
            "python_command": str(item.python_command.expanduser().resolve()),
            "transport": item.transport,
            "server_command": (
                None
                if item.server_command is None
                else str(item.server_command.expanduser().resolve())
            ),
            "server_url": item.server_url,
            "tool_name": item.tool_name,
            "environment_names": list(item.environment_names),
            "header_environment": [list(pair) for pair in item.header_environment],
            "timeout_seconds": item.timeout_seconds,
            "max_result_bytes": item.max_result_bytes,
        }
        for item in profiles
    ]
    return {
        "schema": _CAPABILITY_RECEIPT_SCHEMA,
        "kind": "MCP_ACTION",
        "state": "CONFIGURED" if policy is not None else "NOT_CONFIGURED",
        "configuration_digest": _digest_receipt_payload(identity),
        "profile_count": len(profiles),
        "sidecar_ready": bool(status.get("sidecar_ready", False)),
        "automatic_replacement": False,
        "running_job_mutation": False,
        "authority": "configured_policy_only_each_future_job_action_remains_individually_approved",
    }


def _run_capabilities_command(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    """One non-mutating onboarding surface; configuration remains explicit."""
    browser = cli._browser_status_record(cli.browser_config_from_settings(settings))
    computer_use = cli._computer_use_status_record(cli.computer_use_config_from_settings(settings))
    media = cli._media_status_record(cli.media_config_from_settings(settings))
    mcp_policy = cli.mcp_config_from_settings(settings)
    mcp = cli._mcp_status_record(mcp_policy)
    mcp_action_policy = cli.mcp_action_config_from_settings(settings)
    mcp_action = cli._mcp_action_status_record(mcp_action_policy)
    web = cli.web_read_config_from_settings(settings)
    web_search = cli.web_search_config_from_settings(settings)
    home_assistant = cli.home_assistant_config_from_settings(settings)
    plugins = cli.plugin_config_from_settings(settings)
    plugin_status = cli._plugin_status_record(plugins)
    skill_catalog = cli.discover_external_skills(
        cli.external_skill_directories(cli._table(settings, "skills").get("external_dirs"))
    )
    provider_settings = cli._table(settings, "provider")
    raw_moa_references = provider_settings.get("moa_references", [])
    moa_enabled = isinstance(raw_moa_references, list) and bool(raw_moa_references)
    channel = cli.channel_status(cli.channel_config_from_settings(settings))
    slack = cli.slack_channel_status(cli.slack_channel_config_from_settings(settings))
    slack_inbound = cli.slack_inbound_status(cli.slack_inbound_config_from_settings(settings))
    discord = cli.discord_channel_status(cli.discord_channel_config_from_settings(settings))
    discord_inbound = cli.discord_inbound_status(cli.discord_inbound_config_from_settings(settings))
    ntfy = cli.ntfy_channel_status(cli.ntfy_channel_config_from_settings(settings))
    ntfy_inbound = cli.ntfy_inbound_status(cli.ntfy_inbound_config_from_settings(settings))
    email = cli.email_channel_status(cli.email_channel_config_from_settings(settings))
    email_inbound = cli.email_inbound_status(cli.email_inbound_config_from_settings(settings))
    mattermost = cli.mattermost_channel_status(cli.mattermost_channel_config_from_settings(settings))
    mattermost_inbound = cli.mattermost_inbound_status(cli.mattermost_inbound_config_from_settings(settings))
    matrix = cli.matrix_channel_status(cli.matrix_channel_config_from_settings(settings))
    matrix_inbound = cli.matrix_inbound_status(cli.matrix_inbound_config_from_settings(settings))
    dingtalk = cli.dingtalk_channel_status(cli.dingtalk_channel_config_from_settings(settings))
    teams = cli.teams_channel_status(cli.teams_channel_config_from_settings(settings))
    remote = cli.remote_worker_status(cli.remote_worker_config_from_settings(settings))
    container = cli.container_status(cli.container_config_from_settings(settings))
    record = {
        "provider": {"configured": bool(cli._table(settings, "provider")), "setup": "noruct setup"},
        "global_authority": {
            "workspace": str(cli._table(settings, "run").get("permission_mode", "ask")),
            "capability_trust": str(cli._table(settings, "run").get("capability_trust_mode", "trusted")),
            "external_read": str(cli._table(settings, "run").get("external_read_mode", "allow")),
            "external_state": str(cli._table(settings, "run").get("external_state_mode", "ask")),
            "agent_settings": str(cli._table(settings, "run").get("agent_settings_mode", "ask")),
        },
        "mixture_of_agents": {"enabled": moa_enabled, "ready": moa_enabled, "setup": "[provider] moa_references = [{ kind = \"gemini\", model = \"MODEL\" }]"},
        "external_context": {
            "enabled": bool(mcp.get("enabled")),
            "setup": "noruct mcp configure",
            "receipt": _mcp_read_receipt(mcp_policy, mcp),
        },
        "external_action": {
            "enabled": bool(mcp_action.get("enabled")),
            "ready": bool(mcp_action.get("sidecar_ready", False)),
            "setup": "noruct mcp action-configure",
            "receipt": _mcp_action_receipt(mcp_action_policy, mcp_action),
        },
        "local_browser": {"enabled": bool(browser.get("enabled")), "ready": bool(browser.get("node_ready", False)), "setup": "noruct browser configure"},
        "local_computer_use": {"enabled": bool(computer_use.get("enabled")), "ready": bool(computer_use.get("driver_ready", False)), "setup": "noruct computer-use configure"},
        "direct_media": {"enabled": bool(media.get("enabled")), "ready": bool(media.get("ready", False)), "setup": "noruct media configure --enable image"},
        "public_web_read": {"enabled": web is not None, "allowed_domain_count": len(web.allowed_domains) if web else 0, "setup": "[web_read] enabled = true; allowed_domains = [...]"},
        "web_search": {"enabled": web_search is not None, "ready": web_search is not None, "setup": "noruct web-search configure --base-url http://127.0.0.1:8080"},
        "home_assistant": {"enabled": home_assistant is not None, "ready": bool(home_assistant and cli.os.environ.get(home_assistant.token_env)), "setup": "noruct home-assistant configure --base-url HTTPS_OR_LOOPBACK_URL --allow-entity light.example"},
        "executable_plugins": {
            "enabled": plugins is not None,
            "ready": bool(plugins and plugins.plugins),
            "setup": "noruct plugin install <local-plugin-directory> --confirm",
            "receipt_schema": _CAPABILITY_RECEIPT_SCHEMA,
            "receipts": plugin_status.get("receipts", ()),
        },
        "external_skills": {
            "enabled": bool(skill_catalog.roots),
            "ready": bool(skill_catalog.skills),
            "configured_root_count": len(skill_catalog.roots),
            "discovered_count": len(skill_catalog.skills),
            "skipped_count": skill_catalog.skipped_count,
            "setup": "noruct skills connect LOCAL_SKILL_ROOT",
            "receipt": _external_skill_receipt(skill_catalog),
        },
        "outbound_channel": {"enabled": bool(channel.get("enabled")), "setup": "noruct channel configure"},
        "slack_channel": {"enabled": bool(slack.get("enabled")), "ready": bool(slack.get("ready", False)), "setup": "noruct channel slack-configure"},
        "slack_inbound": {"enabled": bool(slack_inbound.get("enabled")), "ready": bool(slack_inbound.get("ready", False)), "setup": "noruct channel slack-inbox-configure"},
        "discord_channel": {"enabled": bool(discord.get("enabled")), "ready": bool(discord.get("ready", False)), "setup": "noruct channel discord-configure"},
        "discord_inbound": {"enabled": bool(discord_inbound.get("enabled")), "ready": bool(discord_inbound.get("ready", False)), "setup": "noruct channel discord-inbox-configure"},
        "ntfy_channel": {"enabled": bool(ntfy.get("enabled")), "ready": bool(ntfy.get("ready", False)), "setup": "noruct channel ntfy-configure --topic PRIVATE_TOPIC"},
        "ntfy_inbound": {"enabled": bool(ntfy_inbound.get("enabled")), "ready": bool(ntfy_inbound.get("ready", False)), "setup": "noruct channel ntfy-inbox-configure --workspace PATH --topic PRIVATE_TOPIC"},
        "email_channel": {"enabled": bool(email.get("enabled")), "ready": bool(email.get("ready", False)), "setup": "noruct channel email-configure --sender SENDER --to RECIPIENT --smtp-host HOST"},
        "email_inbound": {"enabled": bool(email_inbound.get("enabled")), "ready": bool(email_inbound.get("ready", False)), "setup": "noruct channel email-inbox-configure --workspace PATH --mailbox ADDRESS --imap-host HOST --allow-sender ADDRESS"},
        "mattermost_channel": {"enabled": bool(mattermost.get("enabled")), "ready": bool(mattermost.get("ready", False)), "setup": "noruct channel mattermost-configure --base-url HTTPS_URL --channel-id CHANNEL_ID"},
        "mattermost_inbound": {"enabled": bool(mattermost_inbound.get("enabled")), "ready": bool(mattermost_inbound.get("ready", False)), "setup": "noruct channel mattermost-inbox-configure --workspace PATH --base-url HTTPS_URL --channel-id ID --allow-sender USER_ID"},
        "matrix_channel": {"enabled": bool(matrix.get("enabled")), "ready": bool(matrix.get("ready", False)), "setup": "noruct channel matrix-configure --homeserver-url HTTPS_URL --room-id !ROOM:SERVER"},
        "matrix_inbound": {"enabled": bool(matrix_inbound.get("enabled")), "ready": bool(matrix_inbound.get("ready", False)), "setup": "noruct channel matrix-inbox-configure --workspace PATH --homeserver-url HTTPS_URL --room-id !ROOM:SERVER --allow-sender @USER:SERVER"},
        "dingtalk_channel": {"enabled": bool(dingtalk.get("enabled")), "ready": bool(dingtalk.get("ready", False)), "setup": "noruct channel dingtalk-configure"},
        "teams_channel": {"enabled": bool(teams.get("enabled")), "ready": bool(teams.get("ready", False)), "setup": "noruct channel teams-configure"},
        "remote_worker": {"enabled": bool(remote.get("enabled")), "setup": "noruct environment worker-configure"},
        "container_workspace": {"enabled": bool(container.get("enabled")), "setup": "noruct environment container-configure"},
        "authority": "status_only_no_capability_is_started_or_installed",
    }
    # A missing local configuration is not a disabled implementation.  Make
    # the lifecycle explicit everywhere the catalog is consumed so the TUI,
    # CLI, and future operator surfaces can distinguish a connectable feature
    # from one that is configured but still needs a local dependency or named
    # credential.
    external_read_only = {"external_context", "public_web_read", "web_search", "external_skills"}
    external_state_only = {
        "external_action", "local_computer_use", "direct_media",
        "executable_plugins", "remote_worker", "container_workspace",
    }
    global_authority = record["global_authority"]
    for name, value in record.items():
        if not isinstance(value, dict) or "enabled" not in value:
            continue
        enabled = bool(value["enabled"])
        ready = bool(value.get("ready", enabled))
        if enabled and name in external_read_only and global_authority["external_read"] == "blocked":
            value["lifecycle"] = "withheld"
            value["authority"] = "global external-read policy is blocked"
        elif enabled and name in external_state_only and global_authority["external_state"] == "blocked":
            value["lifecycle"] = "withheld"
            value["authority"] = "global external-state policy is blocked"
        else:
            value["lifecycle"] = (
                "available" if not enabled else "ready" if ready else "configured"
            )
            value["authority"] = "eligible for the next Job; immutable Job grants and approvals still apply"
    package_lifecycle = {
        "schema": "noruct.capability-package-center.v1",
        "lifecycle": ("discover", "inspect", "install_or_connect", "enable", "freeze_for_job", "run", "audit", "rollback_or_disconnect"),
        "adapters": {
            "skills": {
                "status_key": "external_skills",
                "input": "user-owned SKILL.md directory",
                "runtime_projection": "immutable Job instruction and bounded support-file reads",
                "next_action": record["external_skills"]["setup"],
            },
            "plugins": {
                "status_key": "executable_plugins",
                "input": "reviewed executable package",
                "runtime_projection": "versioned out-of-process tool",
                "next_action": record["executable_plugins"]["setup"],
            },
            "mcp": {
                "status_key": "external_context",
                "input": "user-configured MCP profile",
                "runtime_projection": "bounded read or explicit action connector",
                "next_action": record["external_context"]["setup"],
            },
        },
        "trust": record["global_authority"]["capability_trust"],
        "invariant": "A capability may be connected and enabled only before a Job; each Job receives an immutable granted snapshot. Audit remains enabled for every execution.",
    }
    record["capability_packages"] = package_lifecycle
    if args.json:
        print(cli.json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            "Noruct tools · configured surfaces"
            if hasattr(args, "tools_command")
            else "Noruct Capability Center",
            file=output,
        )
        authority = record["global_authority"]
        print(
            "Global authority · "
            f"workspace={authority['workspace']} · "
            f"external-read={authority['external_read']} · "
            f"external-state={authority['external_state']} · "
            f"trust={authority['capability_trust']}",
            file=output,
        )
        if getattr(args, "capabilities_command", "status") == "guide":
            print("\nExtension lifecycle · discover → inspect → connect/install → enable → frozen Job → audit → rollback", file=output)
            for name, adapter in package_lifecycle["adapters"].items():
                status = record[str(adapter["status_key"])]
                print(
                    f"- {name}: {status['lifecycle']} · {adapter['runtime_projection']}\n"
                    f"  next: {adapter['next_action']}",
                    file=output,
                )
            print("Trusted and autonomous profiles remove repeated prompts only for already enabled, job-granted capabilities.", file=output)
        else:
            for name, value in record.items():
                if isinstance(value, dict) and "enabled" in value:
                    print(f"- {name}: {value['lifecycle']} · {value['setup']}", file=output)
    return cli.EXIT_OK

def _run_update_command(args: cli.argparse.Namespace, output: TextIO) -> int:
    if args.update_command == "activate":
        if not args.confirm:
            raise ValueError("Release activation requires --confirm")
        record = cli.activate_installed_release(
            args.version,
            install_root=args.install_root,
            bin_dir=args.bin_dir,
        ).to_dict()
        record["changed"] = True
    else:
        record = cli.release_installation_status(
            install_root=args.install_root,
            bin_dir=args.bin_dir,
        ).to_dict()
        record["changed"] = False
    if args.json:
        print(cli.json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    elif args.update_command == "activate":
        print(f"Noruct command now uses {record['active_version']}", file=output)
        print(f"Active release receipt: {record['active_receipt_state']}", file=output)
        print("No release was downloaded and no Company state or provider credential was read.", file=output)
    else:
        active = record["active_version"] or "none"
        versions = ", ".join(record["installed_versions"]) or "none"
        print(f"Managed releases: {versions}", file=output)
        print(
            f"Active release: {active} · command state: {record['command_state']} · "
            f"install root: {record['install_root_state']}",
            file=output,
        )
        print(
            "Active release receipt: "
            f"{record['active_receipt_state']} · verified receipts: "
            f"{', '.join(record['verified_receipt_versions']) or 'none'}",
            file=output,
        )
        print("Network, provider credentials, and Company state: not accessed", file=output)
    return cli.EXIT_OK

def _run_provider_command(
    args: cli.argparse.Namespace,
    settings: dict,
    output: TextIO,
    *,
    stdin: TextIO,
) -> int:
    config = cli._provider_preflight_config(settings, timeout_seconds=getattr(args, "timeout_seconds", 10.0))
    if args.provider_command == "login":
        if config.kind != "openai_codex":
            raise ValueError("Interactive provider login is available only for the user-managed Codex CLI provider")
        if not args.confirm:
            raise ValueError("External CLI login requires --confirm because it may modify the external provider session")
        if not (cli._isatty(stdin) and cli._isatty(output)):
            raise ValueError("External CLI login requires an interactive terminal; run `noruct provider login --confirm` in a terminal")
        provider = cli._table(settings, "provider")
        command = str(cli._first(cli.os.environ.get("NORUCT_CODEX_COMMAND"), provider.get("codex_command"), "codex"))
        executable = cli.CodexExecProvider.resolve_executable(command)
        if executable is None:
            raise ValueError("Configured Codex executable was not found; install it or configure codex_command")
        print("Opening the user-managed Codex login flow. Noruct does not receive, persist, or display credentials.", file=output)
        try:
            completed = cli.subprocess.run(
                [executable, "login"],
                env=cli.CodexExecProvider._child_environment(cli.os.environ),
                stdin=stdin,
                stdout=output,
                stderr=output,
                check=False,
            )
        except OSError as exc:
            raise ValueError("Could not start the configured Codex login command") from exc
        return cli.EXIT_OK if completed.returncode == 0 else cli.EXIT_INPUT
    if config.kind == "openai_codex":
        provider = cli._table(settings, "provider")
        command = str(cli._first(cli.os.environ.get("NORUCT_CODEX_COMMAND"), provider.get("codex_command"), "codex"))
        login = cli.CodexExecProvider.login_status(command)
        record: dict[str, object] = {
            "kind": "openai_codex",
            "network_attempted": False,
            "model_invocation": False,
            "credential_value_exposed": False,
            "outcome": "AUTHENTICATED_EXTERNAL_CLI" if login.authenticated else "EXTERNAL_CLI_LOGIN_REQUIRED",
            "executable": login.executable,
            "installed": login.installed,
            "authenticated": login.authenticated,
            "details": "Noruct uses the user-managed Codex executable and does not read its credentials.",
        }
    elif config.kind == "external_exec":
        provider = cli._table(settings, "provider")
        command = str(cli._first(cli.os.environ.get("NORUCT_EXTERNAL_COMMAND"), provider.get("external_command"), "")).strip()
        executable = cli.ExternalExecProvider.resolve_executable(command)
        ready = executable is not None and bool(config.model.strip())
        record = {
            "kind": "external_exec",
            "network_attempted": False,
            "model_invocation": False,
            "credential_value_exposed": False,
            "outcome": "EXTERNAL_PROCESS_READY" if ready else "EXTERNAL_PROCESS_CONFIGURATION_REQUIRED",
            "executable": executable,
            "configured_model": config.model,
            "details": "The external bridge is a user-managed executable. Noruct does not perform login, inspect credentials, or invoke a metadata endpoint for it.",
        }
    elif config.kind == "vertex":
        vertex = cli.VertexProvider(cli.VertexProviderConfig(base_url=config.base_url, model=config.model, timeout_seconds=config.timeout_seconds))
        if args.provider_command == "preflight":
            if not args.confirm:
                raise ValueError("Vertex ADC preflight requires --confirm")
            vertex.secret_resolver.resolve("NORUCT_VERTEX_EPHEMERAL_ACCESS_TOKEN")
            record = {
                "kind": "vertex", "network_attempted": False, "model_invocation": False,
                "credential_value_exposed": False, "outcome": "VERTEX_ADC_READY",
                "details": "A user-managed gcloud Application Default Credentials token was obtained in memory and immediately discarded; no model request was made.",
            }
        else:
            record = {
                "kind": "vertex", "network_attempted": False, "model_invocation": False,
                "credential_value_exposed": False, "outcome": "VERTEX_ADC_PREFLIGHT_REQUIRED",
                "details": "Run `noruct provider preflight --confirm` to check user-managed gcloud Application Default Credentials without invoking a model.",
            }
    elif args.provider_command == "preflight":
        if not args.confirm:
            raise ValueError("Provider metadata preflight requires --confirm")
        record = dict(cli.probe_provider_metadata(config).to_dict())
    else:
        record = dict(cli.provider_preflight_status(config).to_dict())
    if args.json:
        print(cli.json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(f"Provider: {record['kind'].replace('_', '-')}", file=output)
        print(f"Status: {record['outcome']}", file=output)
        print("Model invocation: no · credential value exposed: no", file=output)
        if record.get("network_attempted"):
            print("Network metadata request: completed", file=output)
        print(str(record["details"]), file=output)
    outcome = str(record["outcome"])
    return cli.EXIT_OK if outcome in {
        "READY_FOR_OPERATOR_CONFIRMED_METADATA_PROBE",
        "METADATA_REACHABLE",
        "METADATA_PREFLIGHT_UNSUPPORTED",
        "AUTHENTICATED_EXTERNAL_CLI",
        "EXTERNAL_PROCESS_READY",
        "VERTEX_ADC_READY",
    } else cli.EXIT_INPUT

def _run_setup(
    args: cli.argparse.Namespace,
    settings: dict,
    config_path: Path,
    *,
    stdin: TextIO,
    output: TextIO,
) -> int:
    provider = cli._table(settings, "provider")
    run = cli._table(settings, "run")
    terminal = cli._isatty(stdin) and cli._isatty(output)
    if terminal and args.provider_kind is None and not cli._has_explicit_setup_transport(args):
        provider_kind = cli._prompt_choice(
            "Connect Noruct to a model",
            cli.PROVIDER_SETUP_OPTIONS,
            default_kind=cli._provider_kind(provider.get("kind") or "openai_codex"),
            stdin=stdin,
            stdout=output,
        )
    else:
        provider_kind = cli._provider_kind(
            cli._first(args.provider_kind, provider.get("kind"), "openai_api")
        )
    profile = cli.provider_profile(provider_kind) if provider_kind not in {"openai_codex", "external_exec"} else None
    base_url = str(
        cli._first(args.base_url, provider.get("base_url"), profile.base_url if profile else "")
    )
    model = str(cli._first(args.model, provider.get("model"), ""))
    codex_command = str(
        cli._first(args.codex_command, provider.get("codex_command"), "codex")
    )
    external_command = str(
        cli._first(getattr(args, "external_command", None), provider.get("external_command"), "")
    )
    # The generic profile remains available for compatible endpoints, but a
    # first-time OpenAI API choice should work with the official public URL
    # and its conventional environment variable without making users memorize
    # either value.
    selected_openai_default = (
        terminal
        and provider_kind == "openai_api"
        and not provider
        and args.base_url is None
        and args.api_key_env is None
    )
    if selected_openai_default:
        base_url = "https://api.openai.com/v1"
    if terminal and provider_kind not in {"openai_codex", "external_exec"}:
        base_url = cli._prompt_value(
            "Provider base URL",
            default=base_url,
            stdin=stdin,
            stdout=output,
        )
        model = cli._prompt_value(
            "Model",
            default=model,
            stdin=stdin,
            stdout=output,
        )
    elif terminal and provider_kind == "openai_codex":
        codex_command = cli._prompt_value(
            "Codex executable",
            default=codex_command,
            stdin=stdin,
            stdout=output,
        )
        model = cli._prompt_value(
            "Codex model (optional; blank uses the Codex default)",
            default=model,
            stdin=stdin,
            stdout=output,
        )
    elif terminal:
        external_command = cli._prompt_value(
            "External bridge executable",
            default=external_command,
            stdin=stdin,
            stdout=output,
        )
        model = cli._prompt_value(
            "External provider model",
            default=model,
            stdin=stdin,
            stdout=output,
        )
    default_api_key_env = profile.api_key_env if profile else cli.DEFAULT_API_KEY_ENV
    if selected_openai_default:
        default_api_key_env = "OPENAI_API_KEY"
    api_key_env = str(
        cli._first(args.api_key_env, provider.get("api_key_env"), default_api_key_env, "")
    )
    no_auth = bool(
        cli._first(args.no_auth, provider.get("no_auth"), default_api_key_env is None)
    )
    state_value = str(cli._first(args.state, run.get("state"), cli.DEFAULT_STATE_PATH))
    target = cli.write_setup_config(
        config_path,
        cli.SetupConfig(
            provider_kind=provider_kind,
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            no_auth=no_auth,
            codex_command=codex_command,
            external_command=external_command,
            request_timeout_seconds=args.request_timeout,
            stale_timeout_seconds=getattr(args, "stale_timeout", None),
            state_path=state_value,
            max_wall_time_seconds=float(cli._first(run.get("max_wall_time"), 86_400.0)),
            max_model_calls=int(cli._first(run.get("max_model_calls"), 2_048)),
            max_tool_calls=int(cli._first(run.get("max_tool_calls"), 8_192)),
            max_cost_usd=float(cli._first(run.get("max_cost_usd"), 1_000_000.0)),
        ),
        overwrite=args.force,
    )
    print(f"Configuration written: {target}", file=output)
    if provider_kind == "openai_codex":
        status = cli.CodexExecProvider.login_status(codex_command)
        if status.authenticated:
            print("Authentication: existing Codex ChatGPT login detected.", file=output)
        elif status.installed:
            print("Authentication required: run `codex login` or sign in through the Codex IDE extension.", file=output)
            if terminal and cli._prompt_yes_no(
                "Open the official Codex login now",
                default=True,
                stdin=stdin,
                stdout=output,
            ):
                login_args = cli.argparse.Namespace(provider_command="login", confirm=True)
                login_exit = _run_provider_command(
                    login_args,
                    cli._load_config(target),
                    output,
                    stdin=stdin,
                )
                if login_exit != cli.EXIT_OK:
                    return login_exit
        else:
            print(
                "Codex CLI was not found. Install it, then run `codex login`; "
                "Noruct will not download or read its credentials.",
                file=output,
            )
        print("Runtime: user-managed external Codex CLI; not bundled with Noruct.", file=output)
        print(
            "Authority: approval-gated by default; every workspace edit and terminal command still asks before execution.",
            file=output,
        )
        print(
            "Real workspace: Noruct validates the change set and asks before applying it.",
            file=output,
        )
        print(
            "Terms: the user remains responsible for OpenAI account, subscription, data, and usage terms.",
            file=output,
        )
    elif provider_kind == "external_exec":
        print("Authentication: user-managed external CLI; Noruct never reads, stores, or forwards credentials.", file=output)
        print("Bridge protocol: noruct.external-model-exec.v1 JSON stdin/stdout; login remains the external CLI's responsibility.", file=output)
    elif no_auth:
        print("Authentication: disabled", file=output)
        if provider_kind == "ollama":
            print("Runtime: user-managed local Ollama and model; not bundled or downloaded.", file=output)
    else:
        print(f"Set the credential value in environment variable {api_key_env}.", file=output)
        print(
            f"Service: {profile.service_owner if profile else 'configured endpoint'}; "
            "not bundled with Noruct.",
            file=output,
        )
        print("Terms and data policy: governed by the user's external service account.", file=output)
    if terminal:
        print("Optional tool connections: run `noruct tools status` after the model connection is ready.", file=output)
    return cli.EXIT_OK
