"""Provider, session, and Company command adapter."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

def _goal_execution_services(
    *,
    provider_factory: ProviderFactory,
    coding_worker_factory: CodingWorkerFactory,
) -> cli.GoalExecutionServices:
    """Expose the one Company-goal assembly path to product ingress adapters.

    CLI, modern TUI and future GUI surfaces may choose different rendering, but
    they must not independently decide how provider, ROSTER or shadow coding
    dependencies are assembled.  The service is intentionally created here:
    the Firm Runtime function remains the single Kernel entry and this module
    does not introduce a second state authority.
    """

    def coding_worker_for(
        provider_config: ProviderConfig,
        config: RunCommandConfig,
    ) -> CodingWorkerPort | None:
        if (
            isinstance(provider_config, cli.CodexExecProviderConfig)
            and config.permission_mode == "ask"
        ):
            return coding_worker_factory(provider_config)
        return None

    return cli.GoalExecutionServices(
        config_for=cli._run_config,
        roster_for=cli._load_active_roster,
        provider_config_for=_provider_config,
        provider_factory=provider_factory,
        coding_worker_for=coding_worker_for,
        approval_available_for=cli._interactive_approval_available_for,
        runner=cli.run_goal,
    )

def _default_provider(config: ProviderConfig) -> ModelProviderPort:
    if isinstance(config, cli.MoAProviderConfig):
        return cli.MixtureOfAgentsProvider(
            _default_provider(config.aggregator),  # type: ignore[arg-type]
            tuple((label, _default_provider(child)) for label, child in config.references),  # type: ignore[arg-type]
        )
    if isinstance(config, cli.FallbackProviderConfig):
        routes: list[tuple[str, ModelProviderPort]] = [("primary", _default_provider(config.primary))]  # type: ignore[arg-type]
        routes.extend((label, _default_provider(child)) for label, child in config.fallbacks)  # type: ignore[arg-type]
        return cli.FallbackModelProvider(routes)
    if isinstance(config, cli.CodexExecProviderConfig):
        return cli.CodexExecProvider(config)
    if isinstance(config, cli.ExternalExecProviderConfig):
        return cli.ExternalExecProvider(config)
    if isinstance(config, cli.AnthropicProviderConfig):
        return cli.AnthropicProvider(config)
    if isinstance(config, cli.VertexProviderConfig):
        return cli.VertexProvider(config)
    if isinstance(config, cli.BedrockProviderConfig):
        return cli.BedrockProvider(config)
    return cli.OpenAICompatProvider(config)

def _default_coding_worker(config: cli.CodexExecProviderConfig) -> CodingWorkerPort:
    return cli.CodexExecCodingWorker(config)

def _provider_display(config: RunCommandConfig) -> str:
    if config.provider_kind == "openai_codex":
        return "openai-codex (external)"
    if config.provider_kind == "external_exec":
        return "external process (user-managed)"
    return config.provider_kind.replace("_", "-")

def _authority_display(config: RunCommandConfig) -> str:
    if config.provider_kind == "openai_codex" and config.permission_mode == "ask":
        return "ask · shadow-only worker"
    return config.permission_mode

def _tui_company_facts(
    config: RunCommandConfig,
    roster: ActiveRosterSnapshot,
) -> dict[str, tuple[str, ...]]:
    tools = tuple(
        cli._TUI_TOOL_NAMES.get(grant.tool_name, grant.tool_name.replace("_", " "))
        + ("*" if grant.requires_approval else "")
        for grant in cli._action_policy(config).tool_grants
    )
    return {
        "employee_roles": tuple(employee.role for employee in roster.employees),
        "capabilities": tuple(
            capability.replace("_", " ")
            for capability in roster.available_capabilities
        ),
        "tools": tools,
    }

def _company_settings_entries(
    roster: ActiveRosterSnapshot,
    *,
    manager_report: object | None,
) -> tuple[cli.SettingsEntry, ...]:
    """Build Company-scoped Settings rows from the authoritative ROSTER.

    These rows intentionally live beside global TOML entries only at the
    product projection boundary.  Editing one creates a ROSTER Patch; no
    Settings click can rewrite the active roster or a running Job.
    """

    manager_id = str(getattr(manager_report, "manager_employee_id", "") or "")
    manager = next(
        (item for item in roster.employees if item.employee_id == manager_id),
        None,
    )
    rows: list[cli.SettingsEntry] = [
        cli.SettingsEntry(
            "company.manager.model_profile",
            "Company",
            "Manager model profile",
            "COMPANY",
            "configured" if manager is not None else "not-configured",
            "roster-patch",
            "A change is proposed as an immutable ROSTER Patch; approval and apply remain separate and running Jobs retain their Employee snapshot.",
            manager.model_profile if manager is not None else "Manager migration required",
            False,
        ),
        cli.SettingsEntry(
            "company.manager.role",
            "Company",
            "Manager role",
            "COMPANY",
            "configured" if manager is not None else "not-configured",
            "roster-patch",
            "Manager identity is persistent. This setting revises its bounded profile, not Company authority.",
            manager.role if manager is not None else "Manager migration required",
            False,
        ),
        cli.SettingsEntry(
            "company.delegation",
            "Company",
            "Future Job delegation controls",
            "LOCAL",
            "configured",
            "future-job-graph",
            "Blueprint, employee constraints, concurrency, cost/time ceilings, independent review, and mutation posture apply only to a future Work Order.",
            "open Graph controls",
            False,
        ),
    ]
    for employee in roster.employees[:32]:
        rows.append(
            cli.SettingsEntry(
                f"company.employee.{employee.employee_id}",
                "Company",
                f"Employee · {employee.employee_id}",
                "COMPANY",
                "configured" if employee.active else "dormant",
                "roster-patch",
                "Role, capability, active state, and model profile changes are proposed then explicitly approved and applied. Capabilities do not grant tool or permission authority.",
                f"{employee.role} · {', '.join(employee.capabilities)} · {employee.model_profile}",
                False,
            )
        )
    return tuple(rows)

def _render_result(result: JobResult, *, as_json: bool, output: TextIO) -> None:
    if as_json:
        print(cli.json.dumps(cli.to_primitive(result), ensure_ascii=False, sort_keys=True), file=output)
        return
    report = cli.company_final_report(result)
    print(report.summary, file=output)
    if report.manager_employee_id:
        print(f"\n{report.operator_line()}", file=output)
    if result.planning_mode == "SOLO_FALLBACK":
        print(f"\nPlanning: safe solo fallback ({result.planning_reason})", file=output)
    if result.acceptance_evidence:
        print("\nEvidence:", file=output)
        for item in result.acceptance_evidence:
            print(f"- {item}", file=output)
    if result.unresolved_issues:
        print("\nUnresolved:", file=output)
        for item in result.unresolved_issues:
            print(f"- {item}", file=output)

def _run_demo(args: cli.argparse.Namespace, output: TextIO) -> int:
    from dynamic_firm.evaluation.organization import run_evaluation

    record = cli.asyncio.run(run_evaluation(args.fixture, args.strategy))
    if args.json:
        print(cli.json.dumps(cli.to_primitive(record), ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            f"{record.fixture.value}/{record.strategy.value}: {record.status.value} "
            f"quality={record.evidence_hits}/{record.evidence_required} "
            f"employees={record.employee_count} parallelism={record.maximum_parallelism} "
            f"patches={record.graph_mutations}",
            file=output,
        )
    return cli.EXIT_OK if record.status.value == "SUCCEEDED" else cli.EXIT_JOB_FAILED

def _write_evaluation_record(path: Path, payload: str) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{cli.uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(payload + ("" if payload.endswith("\n") else "\n"), encoding="utf-8")
        cli.os.chmod(temporary, 0o600)
        cli.os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target

def _single_provider_config(config: RunCommandConfig) -> ProviderConfig:
    if config.provider_kind == "openai_codex":
        return cli.CodexExecProviderConfig(
            workspace=config.workspace,
            command=config.codex_command,
            model=config.codex_model,
            timeout_seconds=config.request_timeout_seconds,
            stale_timeout_seconds=config.stale_timeout_seconds,
        )
    if config.provider_kind == "external_exec":
        return cli.ExternalExecProviderConfig(
            workspace=config.workspace,
            command=config.external_command,
            model=config.model,
            timeout_seconds=config.request_timeout_seconds,
        )
    if config.provider_kind == "vertex":
        return cli.VertexProviderConfig(
            base_url=config.base_url,
            model=config.model,
            timeout_seconds=config.request_timeout_seconds,
        )
    if config.provider_kind == "bedrock":
        assert config.api_key_env is not None
        return cli.BedrockProviderConfig(base_url=config.base_url, model=config.model, api_key_env=config.api_key_env, timeout_seconds=config.request_timeout_seconds)
    profile = cli.provider_profile(config.provider_kind)
    if profile.transport == "anthropic-messages":
        assert config.api_key_env is not None
        return cli.AnthropicProviderConfig(
            base_url=config.base_url,
            model=config.model,
            api_key_env=config.api_key_env,
            timeout_seconds=config.request_timeout_seconds,
            stream_responses=True,
        )
    return cli.OpenAICompatProviderConfig(
        base_url=config.base_url,
        model=config.model,
        api_key_env=config.api_key_env,
        timeout_seconds=config.request_timeout_seconds,
        stream_responses=True,
        stream_include_usage=config.provider_kind == "openai_api",
        credential_header=profile.credential_header,
        credential_prefix=profile.credential_prefix,
    )

def _fallback_route_from_text(value: object) -> dict[str, object]:
    if not isinstance(value, str) or value.count(":") != 1:
        raise ValueError("Fallback route must use PROVIDER:MODEL")
    kind, model = (part.strip() for part in value.split(":", 1))
    if not kind or not model:
        raise ValueError("Fallback route must use PROVIDER:MODEL")
    return {"kind": cli._provider_kind(kind), "model": model}

def _configured_fallback_routes(settings: dict, args: cli.argparse.Namespace) -> tuple[dict[str, object], ...]:
    provider = cli._table(settings, "provider")
    configured = provider.get("fallbacks", [])
    if configured is None:
        configured = []
    if not isinstance(configured, list):
        raise ValueError("provider.fallbacks must be an array of TOML inline tables")
    values: list[object] = list(configured) + list(getattr(args, "fallback", None) or ())
    if len(values) > 4:
        raise ValueError("At most four fallback routes may be configured")
    routes: list[dict[str, object]] = []
    for raw in values:
        route = _fallback_route_from_text(raw) if isinstance(raw, str) else dict(raw) if isinstance(raw, cli.Mapping) else None
        if route is None or not set(route).issubset({"kind", "provider", "model", "base_url", "api_key_env", "no_auth", "codex_command", "external_command"}):
            raise ValueError("Fallback route is malformed")
        kind = cli._provider_kind(route.get("kind", route.get("provider")))
        model = str(route.get("model", "")).strip()
        if not model:
            raise ValueError("Fallback route requires a model")
        normalized: dict[str, object] = {"kind": kind, "model": model}
        for key in ("base_url", "api_key_env", "codex_command", "external_command"):
            if key in route:
                value = route[key]
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Fallback route {key} must be a non-empty string")
                normalized[key] = value.strip()
        if "no_auth" in route:
            if not isinstance(route["no_auth"], bool):
                raise ValueError("Fallback route no_auth must be true or false")
            normalized["no_auth"] = route["no_auth"]
        routes.append(normalized)
    identities = [(str(route["kind"]), str(route["model"]), str(route.get("base_url", ""))) for route in routes]
    if len(set(identities)) != len(identities):
        raise ValueError("Fallback routes must be unique")
    return tuple(routes)

def _configured_moa_reference_routes(settings: dict, args: cli.argparse.Namespace) -> tuple[dict[str, object], ...]:
    provider = cli._table(settings, "provider")
    configured = provider.get("moa_references", [])
    if configured is None: configured = []
    if not isinstance(configured, list): raise ValueError("provider.moa_references must be an array of routes")
    values = list(configured) + list(getattr(args, "moa_reference", None) or ())
    if values and not 1 <= len(values) <= 8: raise ValueError("Mixture of Agents requires one through eight reference routes")
    routes: list[dict[str, object]] = []
    for value in values:
        route = _fallback_route_from_text(value) if isinstance(value, str) else dict(value) if isinstance(value, cli.Mapping) else None
        if route is None or not set(route).issubset({"kind", "provider", "model", "base_url", "api_key_env", "no_auth", "codex_command", "external_command"}):
            raise ValueError("Mixture of Agents reference route is malformed")
        kind = cli._provider_kind(route.get("kind", route.get("provider"))); model = str(route.get("model", "")).strip()
        if not model or kind == "moa": raise ValueError("Mixture of Agents reference requires a non-MoA provider and model")
        normalized: dict[str, object] = {"kind": kind, "model": model}
        for key in ("base_url", "api_key_env", "codex_command", "external_command"):
            if key in route:
                if not isinstance(route[key], str) or not str(route[key]).strip(): raise ValueError(f"Mixture of Agents route {key} must be a non-empty string")
                normalized[key] = str(route[key]).strip()
        if "no_auth" in route:
            if not isinstance(route["no_auth"], bool): raise ValueError("Mixture of Agents route no_auth must be true or false")
            normalized["no_auth"] = route["no_auth"]
        routes.append(normalized)
    identities = [(str(item["kind"]), str(item["model"]), str(item.get("base_url", ""))) for item in routes]
    if len(set(identities)) != len(identities): raise ValueError("Mixture of Agents reference routes must be unique")
    return tuple(routes)

def _provider_config(config: RunCommandConfig) -> ProviderConfig:
    primary = _single_provider_config(config)
    children: list[tuple[str, object]] = []
    for route in config.fallback_routes:
        kind = str(route["kind"])
        profile = cli.provider_profile(kind) if kind not in {"openai_codex", "external_exec"} else None
        child = cli.replace(
            config,
            provider_kind=kind,
            model=str(route["model"]),
            codex_model=str(route["model"]) if kind == "openai_codex" else None,
            base_url=str(route.get("base_url", profile.base_url if profile else config.base_url)),
            api_key_env=(None if bool(route.get("no_auth", False)) or kind in {"openai_codex", "external_exec"} else str(route.get("api_key_env", profile.api_key_env if profile else config.api_key_env)) if (route.get("api_key_env", profile.api_key_env if profile else config.api_key_env) is not None) else None),
            codex_command=str(route.get("codex_command", config.codex_command)),
            external_command=str(route.get("external_command", config.external_command)),
            fallback_routes=(),
            moa_reference_routes=(),
        )
        children.append((f"{kind}:{child.model}", _single_provider_config(child)))
    aggregate: object = cli.FallbackProviderConfig(primary=primary, fallbacks=tuple(children)) if children else primary
    references: list[tuple[str, object]] = []
    for route in config.moa_reference_routes:
        kind = str(route["kind"]); profile = cli.provider_profile(kind) if kind not in {"openai_codex", "external_exec"} else None
        child = cli.replace(config, provider_kind=kind, model=str(route["model"]), codex_model=str(route["model"]) if kind == "openai_codex" else None, base_url=str(route.get("base_url", profile.base_url if profile else config.base_url)), api_key_env=(None if bool(route.get("no_auth", False)) or kind in {"openai_codex", "external_exec"} else str(route.get("api_key_env", profile.api_key_env if profile else config.api_key_env)) if (route.get("api_key_env", profile.api_key_env if profile else config.api_key_env) is not None) else None), codex_command=str(route.get("codex_command", config.codex_command)), external_command=str(route.get("external_command", config.external_command)), fallback_routes=(), moa_reference_routes=())
        references.append((f"{kind}:{child.model}", _single_provider_config(child)))
    return cli.MoAProviderConfig(aggregator=aggregate, references=tuple(references)) if references else aggregate  # type: ignore[return-value]

def _prepare_permission_mode(
    args: cli.argparse.Namespace,
    settings: dict,
    *,
    stdin: TextIO,
    stdout: TextIO,
) -> None:
    terminal = cli._isatty(stdin) and cli._isatty(stdout)
    provider = cli._table(settings, "provider")
    provider_kind = cli._execution_provider_kind(args, provider)
    if args.permission_mode is None:
        args.permission_mode = "ask" if terminal else "read-only"
    if args.permission_mode == "ask" and not terminal:
        raise ValueError("Permission mode 'ask' requires an interactive input and output terminal.")
    if getattr(args, "json", False) and args.permission_mode == "ask":
        raise ValueError("Structured JSON runs must use --permission-mode read-only.")

def _prompt_value(
    label: str,
    *,
    default: str,
    stdin: TextIO,
    stdout: TextIO,
) -> str:
    suffix = f" [{default}]" if default else ""
    stdout.write(f"{label}{suffix}: ")
    stdout.flush()
    value = stdin.readline()
    if value == "":
        return default
    return value.strip() or default

def _prompt_choice(
    label: str,
    options: tuple[tuple[str, str, str], ...],
    *,
    default_kind: str,
    stdin: TextIO,
    stdout: TextIO,
) -> str:
    """Choose one non-secret connection contract in a real terminal.

    This intentionally keeps the first-run surface dependency-free.  The
    selected value is a Noruct provider kind, while each external account and
    credential remains owned by its provider or the user's local runtime.
    """

    default_index = next(
        (index for index, option in enumerate(options) if option[0] == default_kind),
        0,
    )
    print(f"\n{label}", file=stdout)
    for index, (_, title, detail) in enumerate(options, start=1):
        marker = "●" if index - 1 == default_index else "○"
        print(f"  {marker} {index}. {title}", file=stdout)
        print(f"       {detail}", file=stdout)
    while True:
        stdout.write(f"Select [1-{len(options)}] ({default_index + 1}): ")
        stdout.flush()
        raw = stdin.readline()
        if raw == "":
            return options[default_index][0]
        value = raw.strip()
        if not value:
            return options[default_index][0]
        try:
            selected = int(value) - 1
        except ValueError:
            selected = -1
        if 0 <= selected < len(options):
            return options[selected][0]
        print(f"Enter a number from 1 to {len(options)}.", file=stdout)

def _prompt_yes_no(
    label: str,
    *,
    default: bool,
    stdin: TextIO,
    stdout: TextIO,
) -> bool:
    suffix = "Y/n" if default else "y/N"
    stdout.write(f"{label} [{suffix}]: ")
    stdout.flush()
    raw = stdin.readline()
    if raw == "":
        return default
    value = raw.strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}

def _has_explicit_setup_transport(args: cli.argparse.Namespace) -> bool:
    return any(
        getattr(args, field, None) is not None
        for field in ("base_url", "model", "codex_command", "external_command", "api_key_env", "no_auth")
    )

def _provider_is_ready_without_network(settings: dict) -> bool:
    """Check whether first-run onboarding may safely enter the company UI.

    It never invokes a model or provider metadata endpoint.  A missing API
    key is a normal incomplete onboarding state, not a reason to launch chat
    and immediately fail with a transport error.
    """

    provider = cli._table(settings, "provider")
    kind = cli._provider_kind(provider.get("kind"))
    if kind == "openai_codex":
        command = str(cli._first(provider.get("codex_command"), "codex"))
        return cli.CodexExecProvider.login_status(command).authenticated
    if kind == "external_exec":
        command = str(cli._first(provider.get("external_command"), ""))
        return bool(str(provider.get("model") or "").strip() and cli.ExternalExecProvider.resolve_executable(command))
    profile = cli.provider_profile(kind)
    if not str(provider.get("base_url") or profile.base_url).strip():
        return False
    if not str(provider.get("model") or "").strip():
        return False
    no_auth = bool(cli._first(provider.get("no_auth"), profile.api_key_env is None))
    if no_auth:
        return True
    key_name = str(cli._first(provider.get("api_key_env"), profile.api_key_env, ""))
    return bool(key_name and cli.os.environ.get(key_name))

def _needs_first_run_onboarding(args: cli.argparse.Namespace, config_path: Path) -> bool:
    if args.command != "chat" or config_path.expanduser().is_file():
        return False
    return not any(
        getattr(args, field, None) is not None
        for field in ("provider_kind", "base_url", "model", "codex_command", "api_key_env", "no_auth")
    )

def _first_run_setup_args() -> cli.argparse.Namespace:
    return cli.argparse.Namespace(
        provider_kind=None,
        base_url=None,
        model=None,
        codex_command=None,
        request_timeout=None,
        api_key_env=None,
        no_auth=None,
        state=None,
        force=False,
    )

def _run_sessions(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    path = cli._state_path(args, settings)
    if not path.exists():
        sessions = ()
    else:
        store = cli.CompanySessionStore(path)
        try:
            sessions = store.list(args.limit)
        finally:
            store.close()
    if args.json:
        print(cli.json.dumps(cli.to_primitive(sessions), ensure_ascii=False, sort_keys=True), file=output)
        return cli.EXIT_OK
    if not sessions:
        print("No company sessions yet.", file=output)
        return cli.EXIT_OK
    for session in sessions:
        print(
            f"{session.session_id[:12]}  {session.turn_count:>3} turn(s)  "
            f"{session.title}  [{session.workspace}]",
            file=output,
        )
    return cli.EXIT_OK

def _run_session_command(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    """Expose advanced local session controls without opening a provider."""
    path = cli._state_path(args, settings)
    store = cli.CompanySessionStore(path)
    try:
        if args.session_command == "search":
            hits = store.search_messages(args.query, session_id=args.session_id, limit=args.limit)
            if args.json:
                print(cli.json.dumps(cli.to_primitive(hits), ensure_ascii=False, sort_keys=True), file=output)
                return cli.EXIT_OK
            if not hits:
                print("No matching local session messages.", file=output)
                return cli.EXIT_OK
            for hit in hits:
                print(f"{hit.session_id[:12]}  message={hit.message_id:<6} {hit.role:<10} {hit.snippet}", file=output)
            return cli.EXIT_OK
        selected = store.resolve(args.session_id)
        if selected is None:
            raise ValueError(f"Unknown company session: {args.session_id}")
        if not args.confirm:
            raise ValueError("Session state change requires --confirm")
        if args.session_command == "branch":
            branched = store.branch(selected.session_id, title=args.title, through_message_id=args.through_message)
            if args.json:
                print(cli.json.dumps(cli.to_primitive(branched), ensure_ascii=False, sort_keys=True), file=output)
            else:
                print(f"Created session branch {branched.session_id} from {selected.session_id}.", file=output)
            return cli.EXIT_OK
        removed = store.rewind_messages(selected.session_id, args.through_message)
        result = {"session_id": selected.session_id, "through_message": args.through_message, "removed_messages": removed, "firm_turns_unchanged": True}
        if args.json:
            print(cli.json.dumps(result, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(f"Rewound transcript to message {args.through_message}; removed {removed} message(s). Firm turns unchanged.", file=output)
        return cli.EXIT_OK
    finally:
        store.close()

def _run_job(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    """Keep CLI ingress while delegating ACTIVE JOB lifecycle work."""

    from dynamic_firm.application.job_cli import run_job_command

    return run_job_command(
        args,
        state_path=cli._state_path(args, settings),
        settings=settings,
        output=output,
    )

def _run_company(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    """Keep CLI parsing/configuration as ingress; delegate Company lifecycle work."""

    if args.company_command in {
        "coordination-enrollment-preview",
        "coordination-preflight",
    }:
        raw = settings.get("company_coordination")
        if not isinstance(raw, cli.Mapping):
            raise ValueError(
                "Company coordination is not configured; save an enabled multi-device profile first"
            )
        profile = cli.CompanyCoordinationSettings(
            enabled=raw.get("enabled") is True,
            endpoint=str(raw.get("endpoint", "")).strip(),
            company_scope_digest=str(raw.get("company_scope_digest", "")).strip(),
            device_id=str(raw.get("device_id", "")).strip(),
            token_env=str(raw.get("token_env", "NORUCT_COMPANY_COORDINATION_TOKEN")).strip(),
            allow_insecure_loopback=raw.get("allow_insecure_loopback") is True,
        )
        if args.company_command == "coordination-enrollment-preview":
            payload = cli.company_coordination_enrollment_preview(profile)
        else:
            payload = cli.company_coordination_preflight(profile)
        if args.json:
            print(cli.json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output)
        elif args.company_command == "coordination-preflight":
            print(
                f"Company coordination enrollment · {payload['status']} · "
                f"device={payload['device_id']} · no remote state changed",
                file=output,
            )
        else:
            entry = payload["worker_allowlist_entry"]
            print("Company coordination enrollment preview · no network request", file=output)
            print(cli.json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=2), file=output)
            print(
                "Apply this device-bound hash entry to the private Worker allowlist; "
                "the token value was not displayed.",
                file=output,
            )
        return cli.EXIT_OK

    return cli.run_company_command(
        args,
        state_path=cli._state_path(args, settings),
        output=output,
    )


def _run_portfolio(
    args: cli.argparse.Namespace,
    settings: dict,
    output: TextIO,
    *,
    provider_factory=None,
    coding_worker_factory=None,
) -> int:
    """Compose the read-only/local portfolio operator surface.

    Company outcome episodes are a projection input only.  Missing runtime
    state means no evidence, never a synthetic qualification.
    """

    from dynamic_firm.application.portfolio_cli import run_portfolio_command

    def company_episodes(state_path: cli.Path) -> tuple[object, ...]:
        if not state_path.exists():
            return ()
        store = cli.CompanyStateStore(state_path)
        try:
            return tuple(store.list_episodes(limit=256))
        finally:
            store.close()

    def drain(policy: object) -> object:
        if args.portfolio_command != "drain":
            raise RuntimeError("Portfolio drain port used outside the drain command")
        from dynamic_firm.company import PortfolioExecutionService, WorkOrderPortfolioStore

        state_path = cli._state_path(args, settings)
        portfolio_path = state_path.with_name(f"{state_path.stem}.work-orders.db")
        execution = cli._goal_execution_services(
            provider_factory=(
                cli._default_provider if provider_factory is None else provider_factory
            ),
            coding_worker_factory=(
                cli._default_coding_worker
                if coding_worker_factory is None
                else coding_worker_factory
            ),
        )

        async def dispatch(order: object, job_id: str) -> object:
            # The retained canonical Work Order, rather than ACTIVE JOB audit
            # data, supplies the goal and exact authority identity.  The
            # normal Front Door then freezes the request and takes its own
            # live Company-budget lease immediately before Kernel dispatch.
            candidate = cli.argparse.Namespace(
                **{**vars(args), "goal": order.requested_outcome}
            )
            prepared = execution.prepare(candidate, settings)
            return await execution.execute(
                prepared,
                route=cli.InputRoute.COMPANY_GOAL,
                job_id=job_id,
                work_order_override=order,
            )

        with WorkOrderPortfolioStore(portfolio_path) as store:
            return cli.asyncio.run(
                PortfolioExecutionService(store).execute_work_orders_until_idle(
                    policy=policy,
                    job_id_for=lambda _order: f"job-{cli.uuid.uuid4()}",
                    dispatch=dispatch,
                )
            )

    def submit(
        priority: int,
        reserved_cost_usd: float | None,
        dependency_work_order_ids: tuple[str, ...],
        deadline_at: cli.datetime | None,
        required_capabilities: tuple[str, ...],
    ) -> object:
        """Freeze a user-owned order without planning, provider, or dispatch."""

        from dynamic_firm.company import (
            AuthoritySnapshotIdentity,
            PersistentExecutiveManager,
            WorkOrderBudgetSnapshot,
            WorkOrderPortfolioStore,
            decode_active_roster,
            normalize_work_order,
        )

        config = cli._run_config(args, settings)
        company_store = cli.CompanyStateStore(config.state_path)
        try:
            company = company_store.company()
            roster_snapshot = decode_active_roster(
                company_store.ensure_roster_baseline(cli._default_roster(config))
            )
            roster = roster_snapshot.resolve_execution_profiles(config.model)
            manager = PersistentExecutiveManager.optional_from_roster(
                roster,
                roster_revision=roster_snapshot.revision,
            )
            operating = cli._operating_decision_for_route(
                config.goal, cli.InputRoute.COMPANY_GOAL
            )
            action_policy = cli._action_policy(
                config,
                workspace_access=True,
                manager_tools_enabled=manager is not None,
            )
            authority = AuthoritySnapshotIdentity(
                company_id="company-local",
                company_revision=company.revision,
                roster_revision=roster_snapshot.revision,
                playbook_revision=company_store.playbook().revision,
                action_policy_digest=cli.kernel_content_digest(action_policy),
            )
            budget = WorkOrderBudgetSnapshot(
                max_model_calls=config.run_limits.max_model_calls,
                max_tool_calls=config.run_limits.max_tool_calls,
                max_cost_usd=config.run_limits.max_cost_usd,
                max_wall_time_ms=config.run_limits.max_wall_time_ms,
            )
            order = normalize_work_order(
                config.goal,
                work_order_id=f"work-order-{cli.uuid.uuid4()}",
                requested_outcome=config.goal,
                constraints=(
                    "All effects must remain inside the frozen Company authority and approval policy.",
                ),
                acceptance_criteria=(
                    "Return one explicit user-facing result with evidence or unresolved issues.",
                ),
                workspace_ref=f"workspace:{cli.WORKSPACE_ID}",
                authority_snapshot=authority,
                budget_snapshot=budget,
                requested_at=cli.datetime.now(cli.timezone.utc),
                operating_decision=operating,
            )
            path = config.state_path.with_name(f"{config.state_path.stem}.work-orders.db")
            with WorkOrderPortfolioStore(path) as work_orders:
                return work_orders.submit(
                    order,
                    priority=priority,
                    reserved_cost_usd=reserved_cost_usd,
                    dependency_work_order_ids=dependency_work_order_ids,
                    deadline_at=deadline_at,
                    required_capabilities=required_capabilities,
                )
        finally:
            company_store.close()

    return run_portfolio_command(
        args,
        state_path=cli._state_path(args, settings),
        output=output,
        company_episodes=company_episodes,
        drain=drain,
        submit=submit,
    )
