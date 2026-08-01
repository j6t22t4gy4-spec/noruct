"""Configuration command adapter bound by the CLI composition root."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

def _load_config(path: cli.Path) -> dict:
    resolved = path.expanduser().resolve()
    try:
        with resolved.open("rb") as handle:
            config = cli.tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, cli.tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Cannot read configuration {resolved}: {type(exc).__name__}") from None
    if not isinstance(config, dict):
        raise ValueError("Configuration root must be a TOML table.")
    _reject_config_secrets(config)
    return config

def _reject_config_secrets(config: dict) -> None:
    forbidden_exact = {"api_key", "api_key_value", "password", "secret", "token"}
    forbidden_suffixes = ("_api_key", "_password", "_secret", "_token")

    def walk(value, path: tuple[str, ...] = ()) -> None:
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in forbidden_exact or normalized.endswith(forbidden_suffixes):
                location = ".".join((*path, str(key)))
                raise ValueError(
                    f"Secret value field is not allowed in config: {location}. "
                    "Store the value in the named environment variable instead."
                )
            walk(item, (*path, str(key)))

    walk(config)

def _provider_kind(value: object | None) -> str:
    normalized = str(value or "openai_api").strip().lower().replace("-", "_")
    aliases = {
        "api": "openai_api",
        "codex": "openai_codex",
        "openai_api": "openai_api",
        "openai_codex": "openai_codex",
        "external_exec": "external_exec",
        "external": "external_exec",
        "external_process": "external_exec",
        "anthropic": "anthropic_api",
        "anthropic_api": "anthropic_api",
        "gemini": "gemini_api",
        "gemini_api": "gemini_api",
        "openrouter": "openrouter",
        "azure_foundry": "azure_foundry",
        "azure": "azure_foundry",
        "azure_ai": "azure_foundry",
        "azure_ai_foundry": "azure_foundry",
        "vertex": "vertex",
        "vertex_ai": "vertex",
        "google_vertex": "vertex",
        "gcp_vertex": "vertex",
        "nous": "nous",
        "nous_portal": "nous",
        "nousresearch": "nous",
        "bedrock": "bedrock",
        "aws_bedrock": "bedrock",
        "amazon_bedrock": "bedrock",
        "deepseek": "deepseek",
        "deep_seek": "deepseek",
        "fireworks": "fireworks",
        "fireworks_ai": "fireworks",
        "xai": "xai",
        "x_ai": "xai",
        "grok": "xai",
        "huggingface": "huggingface",
        "hugging_face": "huggingface",
        "hf": "huggingface",
        "minimax": "minimax",
        "mini_max": "minimax",
        "deepinfra": "deepinfra",
        "deep_infra": "deepinfra",
        "nvidia": "nvidia",
        "nvidia_nim": "nvidia",
        "nim": "nvidia",
        "alibaba": "alibaba",
        "dashscope": "alibaba",
        "alibaba_cloud": "alibaba",
        "qwen_dashscope": "alibaba",
        "zai": "zai",
        "z_ai": "zai",
        "glm": "zai",
        "glm_api": "zai",
        "arcee": "arcee",
        "arcee_ai": "arcee",
        "arceeai": "arcee",
        "gmi": "gmi",
        "gmi_cloud": "gmi",
        "gmicloud": "gmi",
        "stepfun": "stepfun",
        "step_fun": "stepfun",
        "step": "stepfun",
        "kilo": "kilo",
        "kilocode": "kilo",
        "kilo_code": "kilo",
        "kimi": "kimi",
        "moonshot": "kimi",
        "moonshot_ai": "kimi",
        "novita": "novita",
        "novita_ai": "novita",
        "novitaai": "novita",
        "xiaomi": "xiaomi",
        "mimo": "xiaomi",
        "xiaomi_mimo": "xiaomi",
        "opencode_zen": "opencode_zen",
        "opencode": "opencode_zen",
        "zen": "opencode_zen",
        "ollama_cloud": "ollama_cloud",
        "alibaba_coding": "alibaba_coding",
        "dashscope_coding": "alibaba_coding",
        "ollama": "ollama",
        "lmstudio": "lmstudio",
        "lm_studio": "lmstudio",
    }
    try:
        return aliases[normalized]
    except KeyError:
        supported = ", ".join(cli._provider_cli_choices())
        raise ValueError(f"Provider kind must be {supported}.") from None

def _execution_provider_kind(args: argparse.Namespace, provider: dict) -> str:
    explicit_api_transport = any(
        (
            getattr(args, "base_url", None) is not None,
            getattr(args, "api_key_env", None) is not None,
            getattr(args, "no_auth", None) is True,
        )
    )
    return _provider_kind(
        cli._first(
            getattr(args, "provider_kind", None),
            "openai_api" if explicit_api_transport else None,
            cli.os.environ.get("NORUCT_PROVIDER"),
            provider.get("kind"),
            "openai_api",
        )
    )

def _foundation_worker_has_required_profile(executable: str) -> bool:
    """Return whether one executable has the audited worker dependency profile."""

    try:
        probe = cli.subprocess.run(
            [
                executable,
                "-c",
                "from importlib.metadata import version; "
                "raise SystemExit(0 if version('PyYAML') == '6.0.3' else 1)",
            ],
            check=False,
            capture_output=True,
            timeout=3,
        )
    except (OSError, cli.subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0

def _resolve_foundation_runtime_python(requested: str) -> str:
    """Find a local, audited worker interpreter without silently changing engine.

    The foundation is the only product runtime.  Discovery merely locates a
    Python executable with the exact, already-audited dependency profile; it
    never installs packages, contacts a network, or falls back to the former
    native loop.
    """

    explicit = requested.strip()
    if explicit:
        candidate = str(cli.Path(explicit).expanduser())
        if not cli.os.access(candidate, cli.os.X_OK):
            raise ValueError(f"Runtime Python executable is not executable: {candidate}")
        if not _foundation_worker_has_required_profile(candidate):
            raise ValueError(
                "Selected Noruct runtime Python lacks required PyYAML==6.0.3. "
                "Install the Noruct employee-runtime profile there or choose a qualified Python."
            )
        return str(cli.Path(candidate).resolve())

    candidates = [cli.sys.executable]
    # Deterministic local discovery supports a common side-by-side Python
    # installation on macOS/Linux. Windows users can select an absolute
    # interpreter through setup/global settings when the launcher differs.
    for command in ("python3.11", "python3", "python"):
        located = cli.shutil.which(command)
        if located:
            candidates.append(located)
    seen: set[str] = set()
    for candidate in candidates:
        resolved = str(cli.Path(candidate).expanduser().resolve())
        if resolved in seen or not cli.os.access(resolved, cli.os.X_OK):
            continue
        seen.add(resolved)
        if _foundation_worker_has_required_profile(resolved):
            return resolved
    raise ValueError(
        "Noruct Employee Runtime requires a local Python with PyYAML==6.0.3. "
        "Run setup or configure run.runtime_python with a qualified interpreter; no legacy runtime exists."
    )

def _run_config(
    args: argparse.Namespace,
    settings: dict,
    *,
    validate_runtime: bool = True,
) -> cli.RunCommandConfig:
    provider = cli._table(settings, "provider")
    run = cli._table(settings, "run")
    skills = cli._table(settings, "skills")
    provider_kind = _execution_provider_kind(args, provider)
    profile = cli.provider_profile(provider_kind) if provider_kind not in {"openai_codex", "external_exec"} else None
    goal = args.goal.strip()
    workspace = cli.Path(cli._first(args.workspace, cli.Path.cwd())).expanduser().resolve()
    state_path = cli.Path(cli._first(args.state, run.get("state"), cli.DEFAULT_STATE_PATH)).expanduser().resolve()
    base_url = str(
        cli._first(
            args.base_url,
            cli.os.environ.get("NORUCT_BASE_URL"),
            provider.get("base_url"),
            profile.base_url if profile else "",
        )
    )
    raw_model = str(
        cli._first(args.model, cli.os.environ.get("NORUCT_MODEL"), provider.get("model"), "")
    ).strip()
    codex_command = str(
        cli._first(
            getattr(args, "codex_command", None),
            cli.os.environ.get("NORUCT_CODEX_COMMAND"),
            provider.get("codex_command"),
            "codex",
        )
    ).strip()
    external_command = str(
        cli._first(
            getattr(args, "external_command", None),
            cli.os.environ.get("NORUCT_EXTERNAL_COMMAND"),
            provider.get("external_command"),
            "",
        )
    ).strip()
    default_api_key_env = profile.api_key_env if profile else cli.DEFAULT_API_KEY_ENV
    api_key_env = str(
        cli._first(args.api_key_env, provider.get("api_key_env"), default_api_key_env, "")
    )
    no_auth = bool(
        cli._first(args.no_auth, provider.get("no_auth"), default_api_key_env is None)
    )
    # Model work is allowed to be long-running.  The short operator-facing
    # failure path is the separate progress/stale deadline below, modelled on
    # the registered Hermes runtime rather than a one-number timeout.
    default_request_timeout = 1_800.0 if provider_kind == "openai_codex" else 120.0
    request_timeout = float(
        cli._first(getattr(args, "request_timeout", None), provider.get("request_timeout"), default_request_timeout)
    )
    stale_timeout = float(
        cli._first(getattr(args, "stale_timeout", None), provider.get("stale_timeout"), 90.0)
    )
    max_wall_time = float(cli._first(args.max_wall_time, run.get("max_wall_time"), 86_400.0))
    max_model_calls = int(cli._first(args.max_model_calls, run.get("max_model_calls"), 2_048))
    max_tool_calls = int(cli._first(args.max_tool_calls, run.get("max_tool_calls"), 8_192))
    max_cost_usd = float(cli._first(args.max_cost_usd, run.get("max_cost_usd"), 1_000_000.0))
    raw_cost_mode = str(
        cli._first(getattr(args, "cost_mode", None), run.get("cost_mode"), "standard")
    ).strip().lower()
    permission_mode = str(cli._first(args.permission_mode, run.get("permission_mode"), "ask"))
    capability_trust_mode = str(
        cli._first(
            getattr(args, "capability_trust_mode", None),
            run.get("capability_trust_mode"),
            "trusted",
        )
    ).strip()
    external_read_mode = str(run.get("external_read_mode", "allow")).strip()
    external_state_mode = str(run.get("external_state_mode", "ask")).strip()
    agent_settings_mode = str(run.get("agent_settings_mode", "ask")).strip()
    requested_employee_runtime = str(
        cli._first(
            getattr(args, "employee_runtime", None),
            cli.os.environ.get("NORUCT_EMPLOYEE_RUNTIME"),
            run.get("employee_runtime"),
            "noruct",
        )
    ).strip().lower()
    runtime_python = str(
        cli._first(
            getattr(args, "runtime_python", None),
            cli.os.environ.get("NORUCT_RUNTIME_PYTHON"),
            run.get("runtime_python"),
            "",
        )
    ).strip()
    remote_worker = cli.remote_worker_config_from_settings(settings)
    container_workspace = cli.container_config_from_settings(settings)
    company_coordination_config = cli.company_coordination_config_from_settings(settings)
    computer_use = cli.computer_use_config_from_settings(settings)
    openai_media = cli.media_config_from_settings(settings)
    executable_plugins = cli.plugin_config_from_settings(settings)
    if not goal:
        raise ValueError("Goal must be non-empty.")
    if not workspace.is_dir():
        raise ValueError(f"Workspace is not a directory: {workspace}")
    if provider_kind not in {"openai_codex", "external_exec"} and not base_url.strip():
        raise ValueError("Model base URL is required (--base-url or NORUCT_BASE_URL).")
    if provider_kind != "openai_codex" and not raw_model:
        raise ValueError("Model identifier is required (--model or NORUCT_MODEL).")
    if provider_kind == "openai_codex" and not codex_command:
        raise ValueError("Codex command is required (--codex-command or NORUCT_CODEX_COMMAND).")
    if provider_kind == "external_exec" and not external_command:
        raise ValueError("External provider command is required (--external-command or NORUCT_EXTERNAL_COMMAND).")
    if request_timeout <= 0 or stale_timeout <= 0 or max_wall_time <= 0:
        raise ValueError("Timeouts must be positive.")
    if max_model_calls <= 0 or max_tool_calls <= 0 or max_cost_usd < 0:
        raise ValueError("Run limits must be bounded positive values.")
    try:
        cost_efficiency_mode = cli.CostEfficiencyMode(raw_cost_mode)
    except ValueError:
        raise ValueError("Cost mode must be standard or economy.") from None
    if profile is not None and profile.transport == "anthropic-messages" and no_auth:
        raise ValueError(f"{profile.service_owner} requires an environment-backed credential.")
    if provider_kind not in {"openai_codex", "external_exec"} and not no_auth and not api_key_env.strip():
        raise ValueError("API key environment variable name must be non-empty, or use --no-auth.")
    if permission_mode not in {"read-only", "ask"}:
        raise ValueError("Permission mode must be read-only or ask.")
    if capability_trust_mode not in {"strict", "trusted", "autonomous"}:
        raise ValueError("Trust mode must be strict, trusted, or autonomous.")
    if external_read_mode not in {"blocked", "ask", "allow"}:
        raise ValueError("External read mode must be blocked, ask, or allow.")
    if external_state_mode not in {"blocked", "ask", "user-authorized-auto"}:
        raise ValueError("External state mode must be blocked, ask, or user-authorized-auto.")
    if agent_settings_mode not in {"blocked", "ask"}:
        raise ValueError("Agent settings mode must be blocked or ask.")
    if remote_worker is not None and permission_mode != "ask":
        raise ValueError("Configured remote worker requires interactive --permission-mode ask.")
    if container_workspace is not None and permission_mode != "ask":
        raise ValueError("Configured container workspace requires interactive --permission-mode ask.")
    if computer_use is not None and permission_mode != "ask":
        raise ValueError("Configured computer-use requires interactive --permission-mode ask.")
    if openai_media is not None and permission_mode != "ask":
        raise ValueError("Configured media tools require interactive --permission-mode ask.")
    if executable_plugins is not None and executable_plugins.plugins and permission_mode != "ask":
        raise ValueError("Enabled executable plugins require interactive --permission-mode ask.")
    fallback_routes = cli._configured_fallback_routes(settings, args)
    if any(str(route["kind"]) == provider_kind and str(route["model"]) == (raw_model or "codex-default") for route in fallback_routes):
        raise ValueError("A fallback route cannot duplicate the active provider and model")
    moa_reference_routes = cli._configured_moa_reference_routes(settings, args)
    if any(str(route["kind"]) == provider_kind and str(route["model"]) == (raw_model or "codex-default") for route in moa_reference_routes):
        raise ValueError("A Mixture of Agents reference cannot duplicate the active provider and model")
    if requested_employee_runtime != "noruct":
        raise ValueError(
            "Employee runtime must be noruct. The legacy runtime was removed; "
            "configure a qualified runtime Python instead."
        )
    # A Graph preview needs the exact configuration-derived authority and
    # limits, but neither starts an Employee nor imports the worker process.
    # Do not make this read-only planning view depend on a local runtime
    # interpreter that is relevant only at dispatch time.
    runtime_python = (
        _resolve_foundation_runtime_python(runtime_python)
        if validate_runtime
        else runtime_python
    )
    employee_runtime = "noruct"
    return cli.RunCommandConfig(
        goal=goal,
        workspace=workspace,
        state_path=state_path,
        provider_kind=provider_kind,
        base_url=base_url.strip(),
        model=raw_model or "codex-default",
        codex_model=(
            raw_model
            if provider_kind == "openai_codex" and raw_model and raw_model != "codex-default"
            else None
        ),
        codex_command=codex_command,
        external_command=external_command,
        api_key_env=(
            None
            if provider_kind in {"openai_codex", "external_exec"} or no_auth
            else api_key_env.strip()
        ),
        request_timeout_seconds=request_timeout,
        permission_mode=permission_mode,
        capability_trust_mode=capability_trust_mode,
        run_limits=cli.RunLimits(
            max_wall_time_ms=int(max_wall_time * 1000),
            max_model_calls=max_model_calls,
            max_tool_calls=max_tool_calls,
            max_input_tokens=(
                1_000_000 if provider_kind == "openai_codex" else 100_000
            ),
            max_cost_usd=max_cost_usd,
            cost_efficiency_mode=cost_efficiency_mode,
        ),
        mcp_read_only=cli.mcp_config_from_settings(settings),
        mcp_action=cli.mcp_action_config_from_settings(settings),
        browser_read_only=cli.browser_config_from_settings(settings),
        computer_use=computer_use,
        openai_media=openai_media,
        web_read=cli.web_read_config_from_settings(settings),
        web_search=cli.web_search_config_from_settings(settings),
        home_assistant=cli.home_assistant_config_from_settings(settings),
        executable_plugins=executable_plugins,
        employee_runtime=employee_runtime,
        runtime_python=runtime_python,
        remote_worker=remote_worker,
        container_workspace=container_workspace,
        external_skill_dirs=cli.external_skill_directories(
            args.skills_dir if args.skills_dir is not None else skills.get("external_dirs")
        ),
        fallback_routes=fallback_routes,
        moa_reference_routes=moa_reference_routes,
        config_path=cli.Path(getattr(args, "config", cli.DEFAULT_CONFIG_PATH)).expanduser().resolve(),
        external_read_mode=external_read_mode,
        external_state_mode=external_state_mode,
        agent_settings_mode=agent_settings_mode,
        company_coordination=(
            cli.RemoteCompanyCoordinationClient(company_coordination_config)
            if company_coordination_config is not None
            else None
        ),
        stale_timeout_seconds=stale_timeout,
    )
