"""Operator diagnostic command adapter."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

from dynamic_firm import __version__

def _run_doctor(
    args: argparse.Namespace,
    settings: dict,
    config_path: cli.Path,
    output: TextIO,
) -> int:
    run_settings = cli._table(settings, "run")
    requested_runtime_python = str(
        cli._first(cli.os.environ.get("NORUCT_RUNTIME_PYTHON"), run_settings.get("runtime_python"), "")
    )
    try:
        employee_runtime_python = cli._resolve_foundation_runtime_python(requested_runtime_python)
        employee_runtime_ready = True
        employee_runtime_issue = None
    except ValueError as exc:
        employee_runtime_python = ""
        employee_runtime_ready = False
        employee_runtime_issue = str(exc)
    employee_runtime_record = {
        "default_request": "noruct",
        "resolved_runtime": "noruct" if employee_runtime_ready else "unavailable",
        "required_distribution": "PyYAML==6.0.3",
        "worker_python": employee_runtime_python or None,
        "profile_ready": employee_runtime_ready,
        "installer_contract": "release-manifest-v2-target-profile",
        "missing_profile_action": employee_runtime_issue,
    }
    provider = cli._table(settings, "provider")
    provider_kind = cli._provider_kind(
        cli._first(cli.os.environ.get("NORUCT_PROVIDER"), provider.get("kind"), "openai_api")
    )
    profile = cli.provider_profile(provider_kind) if provider_kind not in {"openai_codex", "external_exec"} else None
    base_url = str(
        cli._first(
            cli.os.environ.get("NORUCT_BASE_URL"),
            provider.get("base_url"),
            profile.base_url if profile else "",
        )
    )
    model = str(cli._first(cli.os.environ.get("NORUCT_MODEL"), provider.get("model"), ""))
    default_api_key_env = profile.api_key_env if profile else cli.DEFAULT_API_KEY_ENV
    api_key_env = str(cli._first(provider.get("api_key_env"), default_api_key_env, ""))
    no_auth = bool(cli._first(provider.get("no_auth"), default_api_key_env is None))
    issues: list[str] = []
    if employee_runtime_issue:
        issues.append(employee_runtime_issue)
    if provider_kind == "openai_codex":
        codex_command = str(
            cli._first(
                cli.os.environ.get("NORUCT_CODEX_COMMAND"),
                provider.get("codex_command"),
                "codex",
            )
        )
        status = cli.CodexExecProvider.login_status(codex_command)
        if not status.installed:
            issues.append(f"Codex executable was not found: {codex_command}")
        elif not status.authenticated:
            issues.append("Codex is not authenticated; run `codex login` or sign in through the IDE")
        provider_record = {
            "kind": provider_kind,
            "authentication": "codex_chatgpt_login",
            "codex_command": codex_command,
            "executable": status.executable,
            "installed": status.installed,
            "credential_configured": status.authenticated,
            "model_configured": bool(model.strip()),
            "product_inclusion": "user_managed_external_runtime_not_bundled",
            "authority": "parent_approved_host_tools_or_disposable_shadow",
            "host_direct_operations": "noruct_user_approval_required",
            "real_workspace_apply": "noruct_validated_user_approval_required",
            "service_terms_owner": "user_and_openai",
            "cost_accounting": "subscription_quota_usd_unavailable",
            "credential_raw_access": "prohibited",
            "subscription_entitlement": "not_determined_by_noruct",
            "external_provider_review": "pending_human_release_review",
        }
    elif provider_kind == "external_exec":
        command = str(cli._first(cli.os.environ.get("NORUCT_EXTERNAL_COMMAND"), provider.get("external_command"), "")).strip()
        executable = cli.ExternalExecProvider.resolve_executable(command)
        if executable is None:
            issues.append("external provider executable was not found or is not a single executable path")
        if not model.strip():
            issues.append("provider model is not configured")
        provider_record = {
            "kind": provider_kind,
            "transport": "noruct.external-model-exec.v1",
            "external_command": command or None,
            "executable": executable,
            "model_configured": bool(model.strip()),
            "credential_configured": "not_inspected_user_managed_external_cli",
            "credential_raw_access": "prohibited",
            "authentication": "user_managed_external_cli",
            "automatic_login": False,
            "service_terms_owner": "user_and_external_provider",
        }
    else:
        if not base_url.strip():
            issues.append("provider base URL is not configured")
        if not model.strip():
            issues.append("provider model is not configured")
        if base_url.strip() and model.strip():
            try:
                if profile is not None and profile.transport == "anthropic-messages":
                    provider_config = cli.AnthropicProviderConfig(
                        base_url=base_url.strip(),
                        model=model.strip(),
                        api_key_env=api_key_env,
                    )
                    cli.AnthropicProvider._validate_and_build_endpoint(provider_config)
                else:
                    provider_config = cli.OpenAICompatProviderConfig(
                        base_url=base_url.strip(),
                        model=model.strip(),
                        api_key_env=None if no_auth else api_key_env,
                        credential_header=profile.credential_header if profile else "Authorization",
                        credential_prefix=profile.credential_prefix if profile else "Bearer ",
                    )
                    cli.OpenAICompatProvider._validate_and_build_endpoint(provider_config)
            except ValueError as exc:
                issues.append(str(exc))
        credential_ready = no_auth or bool(api_key_env and cli.os.environ.get(api_key_env))
        if not credential_ready:
            issues.append(f"credential environment variable is not set: {api_key_env}")
        provider_record = {
            "kind": provider_kind,
            "transport": profile.transport if profile else "external-cli",
            "service_owner": profile.service_owner if profile else "OpenAI",
            "base_url_configured": bool(base_url.strip()),
            "model_configured": bool(model.strip()),
            "authentication": "disabled" if no_auth else "environment",
            "api_key_env": None if no_auth else api_key_env,
            "credential_configured": credential_ready,
        }
    mcp_config = cli.mcp_config_from_settings(settings)
    if mcp_config is None:
        external_read_record = {"enabled": False}
    else:
        mcp_profiles = mcp_config.configs if isinstance(mcp_config, cli.McpReadOnlyConfigSet) else (mcp_config,)
        installed_versions = cli.configured_sdk_versions(mcp_config)
        for profile, version in installed_versions.items():
            if version != cli.AUDITED_MCP_VERSION:
                issues.append(
                    f"configured MCP Python for {profile} must contain mcp=={cli.AUDITED_MCP_VERSION}"
                )
        header_environment_names = tuple(
            name for item in mcp_profiles for _, name in item.header_environment
        )
        oauth_environment_names = tuple(
            name
            for item in mcp_profiles
            for name in (item.oauth_client_id_environment, item.oauth_client_secret_environment)
            if name is not None
        )
        missing_environment = tuple(
            name
            for name in (*mcp_config.environment_names, *header_environment_names, *oauth_environment_names)
            if name not in cli.os.environ
        )
        for name in missing_environment:
            issues.append(f"external capability environment variable is not set: {name}")
        profile_records = [
            {
                "profile": item.profile,
                "sdk_installed": installed_versions[item.profile],
                "transport": item.transport,
                "server_executable": str(item.server_command) if item.server_command else None,
                "server_endpoint_configured": bool(item.server_url),
                "runtime_tools": list(
                    mcp_config.selected_runtime_tool_names()
                    if len(mcp_profiles) == 1
                    else tuple(
                        name for name in mcp_config.selected_runtime_tool_names()
                        if mcp_config.profile_for_runtime_tool(name) == item.profile
                    )
                ),
                "environment_names": list(item.environment_names),
                "header_environment_names": [name for _, name in item.header_environment],
                "oauth_enabled": item.oauth_enabled,
                "oauth_client_environment_names": [
                    name
                    for name in (item.oauth_client_id_environment, item.oauth_client_secret_environment)
                    if name is not None
                ],
            }
            for item in mcp_profiles
        ]
        external_read_record = {
            "enabled": True,
            "transport": "user_managed_mcp_sidecar",
            "sdk_required": cli.AUDITED_MCP_VERSION,
            "sdk_installed": installed_versions[mcp_profiles[0].profile] if len(mcp_profiles) == 1 else None,
            "server_executable": (
                str(mcp_profiles[0].server_command)
                if len(mcp_profiles) == 1 and mcp_profiles[0].server_command
                else None
            ),
            "profile": mcp_profiles[0].profile if len(mcp_profiles) == 1 else None,
            "public_tools": list(mcp_config.selected_runtime_tool_names()),
            "profiles": profile_records,
            "authority": "explicit_read_only_allowlist_one_call_per_selected_tool_per_job",
            "environment_names": list(mcp_config.environment_names),
            "header_environment_names": list(header_environment_names),
            "oauth_client_environment_names": list(oauth_environment_names),
            "credential_values_stored_in_config": False,
            "oauth_token_storage": (
                "local_owner_only_per_profile" if any(item.oauth_enabled for item in mcp_profiles) else "not_configured"
            ),
            "external_service_terms_owner": "user_and_server_operator",
        }
    try:
        mcp_action_config = cli.mcp_action_config_from_settings(settings)
        external_action_record = cli._mcp_action_status_record(mcp_action_config)
    except ValueError as exc:
        issues.append(f"external action configuration is invalid: {exc}")
        mcp_action_config = None
        external_action_record = {
            **cli._mcp_action_status_record(None),
            "configuration_error": str(exc),
        }
    if mcp_action_config is not None:
        if not bool(external_action_record["sidecar_ready"]):
            issues.append(
                f"configured external action Python must contain mcp=={cli.AUDITED_MCP_VERSION}"
            )
        for action in cli.mcp_action_configs(mcp_action_config):
            for name in action.environment_names:
                if name not in cli.os.environ:
                    issues.append(f"external action environment variable is not set: {name}")
    try:
        browser_config = cli.browser_config_from_settings(settings)
        browser_record = cli._browser_status_record(browser_config)
    except ValueError as exc:
        issues.append(f"local browser configuration is invalid: {exc}")
        browser_record = {
            **cli._browser_status_record(None),
            "configuration_error": str(exc),
        }
    configured_channel = cli.channel_config_from_settings(settings)
    channel_record = dict(cli.channel_status(configured_channel))
    # A notification bridge is deliberately optional.  Its missing environment
    # variables must be visible to the operator without turning a local Company
    # run into a provider-configuration failure.
    channel_record["delivery"] = "operator_confirmed_test_only"
    environment_record = cli.execution_environment_status(cli.Path.cwd()).to_dict()
    try:
        configured_remote_worker = cli.remote_worker_config_from_settings(settings)
        remote_worker_record = dict(cli.remote_worker_status(configured_remote_worker))
    except ValueError as exc:
        issues.append(f"remote worker configuration is invalid: {exc}")
        remote_worker_record = {
            **cli.remote_worker_status(None),
            "configuration_error": str(exc),
        }
    try:
        configured_container_workspace = cli.container_config_from_settings(settings)
        container_workspace_record = dict(cli.container_status(configured_container_workspace))
    except ValueError as exc:
        issues.append(f"container workspace configuration is invalid: {exc}")
        container_workspace_record = {
            **cli.container_status(None),
            "configuration_error": str(exc),
        }
    release_record = dict(cli.release_installation_status().to_dict())
    terminal_crash_log = cli.modern_terminal_crash_log_path()
    terminal_diagnostics_record = {
        "crash_log_path": str(terminal_crash_log),
        "exists": terminal_crash_log.is_file(),
        "bytes": terminal_crash_log.stat().st_size if terminal_crash_log.is_file() else 0,
        "contents": "redacted_exception_type_and_stack_locations_only",
    }
    record = {
        "version": __version__,
        "python": cli.platform.python_version(),
        "config_path": str(config_path.expanduser().resolve()),
        "config_exists": config_path.expanduser().is_file(),
        "employee_runtime": employee_runtime_record,
        "provider": provider_record,
        "external_read": external_read_record,
        "external_action": external_action_record,
        "local_browser_read": browser_record,
        "outbound_channel": channel_record,
        "execution_environment": {
            "local_execution": environment_record["local_execution"],
            "shadow_workspace": environment_record["shadow_workspace"],
            "remote_job_execution": environment_record["remote_job_execution"],
            "remote_worker": remote_worker_record,
            "container_workspace": container_workspace_record,
            "os_sandbox": environment_record["os_sandbox"],
        },
        "release_installation": release_record,
        "terminal_diagnostics": terminal_diagnostics_record,
        "run_ready": not issues,
        "issues": issues,
    }
    if args.json:
        print(cli.json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    else:
        status = "ready" if record["run_ready"] else "needs configuration"
        print(f"Noruct {__version__}: {status}", file=output)
        print(f"Config: {record['config_path']} ({'found' if record['config_exists'] else 'not found'})", file=output)
        print(f"Python: {record['python']}", file=output)
        print(
            "Employee runtime: "
            + ("ready · Noruct runtime" if employee_runtime_ready else "profile missing · no fallback"),
            file=output,
        )
        print(f"Provider: {provider_kind.replace('_', '-')}", file=output)
        print(
            "Modern terminal diagnostics: "
            f"{terminal_diagnostics_record['crash_log_path']} "
            f"({'present' if terminal_diagnostics_record['exists'] else 'no crash records'})",
            file=output,
        )
        remote_worker = record["execution_environment"]["remote_worker"]
        print(
            "Remote Company worker: "
            + ("configured · frozen capability trust policy" if remote_worker["enabled"] else "not configured"),
            file=output,
        )
        if provider_kind == "openai_codex":
            print("Runtime: user-managed external Codex CLI (not bundled with Noruct)", file=output)
            print("Cost accounting: ChatGPT subscription USD cost is unavailable", file=output)
            print("Credential access: Noruct does not read or store provider credentials", file=output)
            print("Entitlement: provider subscription and workspace terms are not determined by Noruct", file=output)
            print("Release review: external-provider terms/data/trademark review is pending", file=output)
        elif provider_kind == "external_exec":
            print("Runtime: user-managed external JSON bridge (not bundled with Noruct)", file=output)
            print("Credential access: Noruct does not read or store external provider credentials", file=output)
        else:
            print(
                f"Transport: {profile.transport} · service/runtime not bundled with Noruct",
                file=output,
            )
        print(
            "External read: "
            + ("configured · user-managed MCP sidecar" if mcp_config else "disabled"),
            file=output,
        )
        print(
            "Local browser read: "
            + ("configured · bounded local evidence" if browser_record["enabled"] else "disabled"),
            file=output,
        )
        if configured_channel is None:
            print("Outbound channel: disabled", file=output)
        else:
            channel_state = "ready" if channel_record.get("ready") else "needs environment"
            print(f"Outbound channel: {channel_state} · explicit test only", file=output)
        container_workspace = record["execution_environment"]["container_workspace"]
        remote_state = "configured; capability trust policy" if remote_worker["enabled"] else "not configured"
        container_state = "configured; capability trust policy" if container_workspace["enabled"] else "not configured"
        print(
            f"Execution: local authority plus frozen capability trust policy · remote worker {remote_state} · "
            f"container workspace {container_state} · OS sandbox not claimed",
            file=output,
        )
        release_version = release_record["active_version"] or "not installed"
        print(
            f"Release installation: {release_version} · {release_record['command_state']} · "
            f"{len(release_record['installed_versions'])} managed version(s)",
            file=output,
        )
        for issue in issues:
            print(f"- {issue}", file=output)
    return cli.EXIT_OK if record["run_ready"] else cli.EXIT_INPUT
