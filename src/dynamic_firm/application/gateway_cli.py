"""Gateway command adapter bound by the CLI composition root."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

def _gateway_service_record(
    *,
    action: str,
    state_path: cli.Path,
    service_state_path: cli.Path,
    record: Mapping[str, object],
) -> dict[str, object]:
    return {
        "gateway": "noruct_owned_local_service",
        "action": action,
        "job_state_path": str(state_path),
        "service_state_path": str(service_state_path),
        "background_service": True,
        "automatic_boot_start": False,
        "automatic_restart": False,
        "external_session_routing": False,
        "record": dict(record),
    }

def _gateway_service_log_tail(path: cli.Path | None, *, lines: int) -> dict[str, object]:
    """Read a small local log tail without following symlinks or exposing secrets."""

    if not 1 <= lines <= 400:
        raise ValueError("Gateway service log line count must be between 1 and 400")
    if path is None:
        return {"available": False, "reason": "no_recorded_log", "lines": []}
    target = path.expanduser()
    try:
        if target.is_symlink() or not target.is_file():
            return {"available": False, "reason": "recorded_log_unavailable", "lines": []}
        size = target.stat().st_size
        with target.open("rb") as handle:
            handle.seek(max(0, size - 262_144))
            raw = handle.read(262_144)
    except OSError:
        return {"available": False, "reason": "recorded_log_unavailable", "lines": []}
    text = raw.decode("utf-8", errors="replace")
    selected = text.splitlines()[-lines:]
    rendered = cli.redact_terminal_output("\n".join(selected), force=True)
    return {
        "available": True,
        "truncated_bytes": size > len(raw),
        "lines": rendered.splitlines(),
        "authority": "bounded_local_recorded_log_tail_terminal_redacted_no_service_control",
    }

def _gateway_receiver_configs(settings: dict) -> dict[str, object | None]:
    """Load receiver value objects; they contain secret *references*, never values."""

    return {
        "telegram": cli.telegram_channel_config_from_settings(settings),
        "slack": cli.slack_inbound_config_from_settings(settings),
        "discord": cli.discord_inbound_config_from_settings(settings),
        "email": cli.email_inbound_config_from_settings(settings),
        "ntfy": cli.ntfy_inbound_config_from_settings(settings),
        "matrix": cli.matrix_inbound_config_from_settings(settings),
        "mattermost": cli.mattermost_inbound_config_from_settings(settings),
    }

def _gateway_receiver_readiness(configs: Mapping[str, object | None]) -> dict[str, dict[str, object]]:
    """Return the non-secret configuration projection used for service admission."""

    return {
        "telegram": dict(cli.telegram_channel_status(configs["telegram"])),
        "slack": dict(cli.slack_inbound_status(configs["slack"])),
        "discord": dict(cli.discord_inbound_status(configs["discord"])),
        "email": dict(cli.email_inbound_status(configs["email"])),
        "ntfy": dict(cli.ntfy_inbound_status(configs["ntfy"])),
        "matrix": dict(cli.matrix_inbound_status(configs["matrix"])),
        "mattermost": dict(cli.mattermost_inbound_status(configs["mattermost"])),
    }

def _gateway_safe_config_projection(value: object) -> object:
    """Canonicalize known local config value objects without reading the environment."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, cli.Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [_gateway_safe_config_projection(item) for item in value]
    if cli.is_dataclass(value):
        return {field.name: _gateway_safe_config_projection(getattr(value, field.name)) for field in cli.fields(value)}
    raise TypeError(f"Unsupported gateway configuration value for attestation: {type(value).__name__}")

def _gateway_receiver_config_digest(
    receivers: Sequence[str], readiness: Mapping[str, Mapping[str, object]], configs: Mapping[str, object | None],
) -> str:
    """Bind a service record to its selected non-secret receiver configuration.

    The digest is only an operator attestation marker.  It is never passed to
    the child and never includes environment variable values or credentials.
    """

    projection = {
        "receivers": list(receivers),
        "readiness": {receiver: dict(readiness[receiver]) for receiver in receivers},
        "configuration": {receiver: _gateway_safe_config_projection(configs[receiver]) for receiver in receivers},
    }
    encoded = cli.json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return cli.hashlib.sha256(encoded).hexdigest()

def _gateway_receiver_configuration_status(record: Mapping[str, object], settings: dict) -> dict[str, object]:
    receivers = tuple(str(receiver) for receiver in record.get("receivers", []) if isinstance(receiver, str))
    recorded_digest = record.get("receiver_config_digest")
    if not receivers:
        return {"status": "NO_RECORDED_RECEIVERS", "attested": False}
    if not isinstance(recorded_digest, str):
        return {"status": "LEGACY_UNATTESTED_CONFIGURATION", "attested": False}
    configs = _gateway_receiver_configs(settings)
    readiness = _gateway_receiver_readiness(configs)
    current_digest = _gateway_receiver_config_digest(receivers, readiness, configs)
    return {
        "status": "MATCHES_CURRENT_CONFIGURATION" if recorded_digest == current_digest else "DRIFTED_FROM_CURRENT_CONFIGURATION",
        "attested": True,
    }

def _run_gateway_service_command(
    args: argparse.Namespace,
    settings: dict,
    output: TextIO,
) -> int:
    """Operate one local child process without making it a state authority."""
    state_path = cli._state_path(args, settings)
    service_state = cli.gateway_service_state_path(state_path)
    action = args.gateway_service_command
    with cli.GatewayServiceStore(service_state) as store:
        if action == "status":
            current = store.status().to_dict()
            payload = _gateway_service_record(
                action=action,
                state_path=state_path,
                service_state_path=service_state,
                record=current,
            )
            payload["receiver_configuration"] = _gateway_receiver_configuration_status(current, settings)
        elif action == "logs":
            current = store.status().to_dict()
            payload = _gateway_service_record(
                action=action,
                state_path=state_path,
                service_state_path=service_state,
                record=current,
            )
            payload["log"] = _gateway_service_log_tail(
                cli.Path(str(current["log_path"])) if current.get("log_path") else None,
                lines=int(args.lines),
            )
        elif action == "stop":
            if not args.confirm:
                raise ValueError("Gateway service stop requires --confirm")
            payload = _gateway_service_record(
                action=action,
                state_path=state_path,
                service_state_path=service_state,
                record=store.stop().to_dict(),
            )
        elif action == "reset":
            if not args.confirm:
                raise ValueError("Gateway service reset requires --confirm")
            payload = _gateway_service_record(
                action=action,
                state_path=state_path,
                service_state_path=service_state,
                record=store.reset().to_dict(),
            )
        else:
            if not args.confirm:
                raise ValueError(f"Gateway service {action} requires --confirm because accepted messages can consume provider quota")
            if action == "restart":
                store.stop()
            if not 5 <= args.poll_seconds <= 3600:
                raise ValueError("Gateway service poll interval must be between 5 and 3600 seconds")
            if not 1 <= args.receiver_seconds <= 60:
                raise ValueError("Gateway service receiver timeout must be between 1 and 60 seconds")
            receivers = tuple(dict.fromkeys(args.receiver))
            configs = _gateway_receiver_configs(settings)
            readiness = _gateway_receiver_readiness(configs)
            unavailable = [name for name in receivers if not readiness[name].get("ready")]
            if unavailable:
                raise ValueError(f"Selected gateway receiver is not ready: {', '.join(unavailable)}")
            log_path = (
                args.log_file.expanduser().resolve()
                if args.log_file is not None
                else service_state.with_suffix(".log")
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            reservation = store.reserve_start(
                receivers=receivers,
                log_path=log_path,
                receiver_config_digest=_gateway_receiver_config_digest(receivers, readiness, configs),
            )
            command = [
                cli.sys.executable,
                "-m",
                "dynamic_firm",
                "--config",
                str(args.config.expanduser().resolve()),
                "gateway",
                "run",
                "--state",
                str(state_path),
                "--poll-seconds",
                str(args.poll_seconds),
                "--receiver-seconds",
                str(args.receiver_seconds),
                "--confirm",
            ]
            for receiver in receivers:
                command.extend(("--receiver", receiver))
            try:
                with log_path.open("ab", buffering=0) as log_file:
                    process = cli.subprocess.Popen(
                        command,
                        stdin=cli.subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=cli.subprocess.STDOUT,
                        start_new_session=True,
                        close_fds=True,
                    )
            except OSError:
                store.stop()
                raise
            started = store.mark_started(run_id=reservation.run_id or "", pid=process.pid)
            payload = _gateway_service_record(
                action=action,
                state_path=state_path,
                service_state_path=service_state,
                record=started.to_dict(),
            )
    if args.json:
        print(cli.json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), file=output)
    else:
        record = payload["record"]
        assert isinstance(record, dict)
        print(f"Gateway service {action} · {record['state']}", file=output)
        if action in {"start", "restart"}:
            print(f"PID {record['pid']} · logs: {record['log_path']}", file=output)
        elif action == "logs":
            log = payload["log"]
            assert isinstance(log, dict)
            if not log.get("available"):
                print("Gateway service log is not available.", file=output)
            else:
                for line in log["lines"]:
                    print(str(line), file=output)
        print("No automatic boot start, automatic restart, pairing, or additional platform authority was enabled.", file=output)
    return cli.EXIT_OK

def _run_gateway_command(
    args: argparse.Namespace,
    settings: dict,
    output: TextIO,
    *,
    provider_factory: ProviderFactory,
) -> int:
    """Supervise selected existing foreground receivers with Noruct authority.

    This is deliberately not an upstream gateway import: no pairing, external
    session routing, background service, reconnect daemon, delivery/reply or
    platform SDK is introduced.  It reuses configured receivers and materializes
    each accepted item through their ordinary read-only Company Job paths.
    """
    if args.gateway_command == "service":
        return _run_gateway_service_command(args, settings, output)
    email_config = cli.email_inbound_config_from_settings(settings)
    ntfy_config = cli.ntfy_inbound_config_from_settings(settings)
    telegram_config = cli.telegram_channel_config_from_settings(settings)
    slack_config = cli.slack_inbound_config_from_settings(settings)
    discord_config = cli.discord_inbound_config_from_settings(settings)
    matrix_config = cli.matrix_inbound_config_from_settings(settings)
    mattermost_config = cli.mattermost_inbound_config_from_settings(settings)
    readiness = {
        "telegram": dict(cli.telegram_channel_status(telegram_config)),
        "slack": dict(cli.slack_inbound_status(slack_config)),
        "discord": dict(cli.discord_inbound_status(discord_config)),
        "email": dict(cli.email_inbound_status(email_config)),
        "ntfy": dict(cli.ntfy_inbound_status(ntfy_config)),
        "matrix": dict(cli.matrix_inbound_status(matrix_config)),
        "mattermost": dict(cli.mattermost_inbound_status(mattermost_config)),
    }
    if args.gateway_command == "status":
        record = {"gateway": "foreground_operator_started_only", "receivers": readiness, "background_service": False, "automatic_delivery": False}
        if args.json: print(cli.json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print("Gateway supervisor: stopped · foreground operator start only", file=output)
            for name, status in readiness.items(): print(f"  {name}: {'ready' if status.get('ready') else 'not ready'}", file=output)
            print("No pairing, detached service, auto-restart, reply delivery, or external session routing is enabled.", file=output)
        return cli.EXIT_OK
    if args.gateway_command == "dashboard":
        if not args.confirm:
            raise ValueError("Gateway dashboard requires --confirm because it starts a local loopback HTTP listener")
        state_path = cli._state_path(args, settings)
        service_state = cli.gateway_service_state_path(state_path)

        def snapshot() -> dict[str, object]:
            current_readiness = {
                "telegram": dict(cli.telegram_channel_status(telegram_config)),
                "slack": dict(cli.slack_inbound_status(slack_config)),
                "discord": dict(cli.discord_inbound_status(discord_config)),
                "email": dict(cli.email_inbound_status(email_config)),
                "ntfy": dict(cli.ntfy_inbound_status(ntfy_config)),
                "matrix": dict(cli.matrix_inbound_status(matrix_config)),
                "mattermost": dict(cli.mattermost_inbound_status(mattermost_config)),
            }
            with cli.GatewayServiceStore(service_state) as store:
                service_record = store.status().to_dict()
            return {
                "authority": "loopback_read_only_gateway_projection",
                "receivers": current_readiness,
                "service": service_record,
                "writes": "disabled",
                "external_bind": "disabled",
            }

        def announce(host: str, port: int) -> None:
            record = {"started": True, "url": f"http://{host}:{port}/", "authority": "loopback_read_only_gateway_projection"}
            if args.json:
                print(cli.json.dumps(record, ensure_ascii=False, sort_keys=True), file=output, flush=True)
            else:
                print(f"Gateway dashboard · {record['url']}", file=output, flush=True)
                print("Read-only loopback projection; stop with Ctrl+C.", file=output, flush=True)

        try:
            cli.serve_gateway_dashboard(
                snapshot=snapshot,
                port=int(args.port),
                maximum_requests=args.max_requests,
                on_ready=announce,
            )
        except KeyboardInterrupt:
            pass
        return cli.EXIT_OK
    if not args.confirm:
        raise ValueError("Gateway run requires --confirm because accepted messages can consume provider quota")
    if not 5 <= args.poll_seconds <= 3600: raise ValueError("Gateway poll interval must be between 5 and 3600 seconds")
    if not 1 <= args.receiver_seconds <= 60: raise ValueError("Gateway receiver timeout must be between 1 and 60 seconds")
    if args.max_cycles is not None and not 1 <= args.max_cycles <= 10_000: raise ValueError("Gateway max_cycles must be between 1 and 10000")
    receivers = tuple(dict.fromkeys(args.receiver))
    unavailable = [name for name in receivers if not readiness[name].get("ready")]
    if unavailable: raise ValueError(f"Selected gateway receiver is not ready: {', '.join(unavailable)}")
    state_path = cli._state_path(args, settings)

    def run_email_once() -> dict[str, object]:
        assert email_config is not None
        async def dispatch(message: EmailInboundMessage) -> tuple[str, str]:
            config = cli._email_inbound_run_config(message, email_config, args, settings)
            result = await cli.run_goal(config, provider_factory(cli._provider_config(config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(config))
            return result.job_id, result.status.value
        with cli.InboundMessageStore(cli.inbound_state_path(state_path)) as store:
            result = cli.asyncio.run(cli.run_email_inbound(email_config, store=store, maximum_seconds=args.receiver_seconds, dispatch=dispatch))
        return {"receiver": "email", "accepted_count": result.accepted_count, "duplicate_count": result.duplicate_count, "ignored_count": result.ignored_count, "dispatches": list(result.dispatches)}

    def run_ntfy_once() -> dict[str, object]:
        assert ntfy_config is not None
        async def dispatch(message: NtfyInboundMessage) -> tuple[str, str]:
            config = cli._ntfy_inbound_run_config(message, ntfy_config, args, settings)
            result = await cli.run_goal(config, provider_factory(cli._provider_config(config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(config))
            return result.job_id, result.status.value
        with cli.InboundMessageStore(cli.inbound_state_path(state_path)) as store:
            result = cli.asyncio.run(cli.run_ntfy_inbound(ntfy_config, store=store, maximum_seconds=args.receiver_seconds, dispatch=dispatch))
        return {"receiver": "ntfy", "accepted_count": result.accepted_count, "duplicate_count": result.duplicate_count, "ignored_count": result.ignored_count, "dispatches": list(result.dispatches)}

    def run_telegram_once() -> dict[str, object]:
        assert telegram_config is not None
        async def dispatch(message: TelegramInboundMessage) -> tuple[str, str, str]:
            config = cli._telegram_run_config(message, telegram_config, args, settings)
            result = await cli.run_goal(config, provider_factory(cli._provider_config(config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(config))
            return result.job_id, result.status.value, result.summary
        with cli.TelegramChannelStore(cli.telegram_state_path(state_path)) as store:
            result = cli.asyncio.run(cli.run_telegram_channel(telegram_config, store=store, maximum_seconds=args.receiver_seconds, dispatch=dispatch))
        return {"receiver": "telegram", "accepted_count": result.accepted_count, "duplicate_count": result.duplicate_count, "ignored_count": result.ignored_count, "rejected_count": result.rejected_count, "dispatches": [{"message_id": item.message_id, "job_id": item.job_id, "job_status": item.job_status, "outcome": item.outcome, "replied": item.replied} for item in result.dispatches]}

    def run_slack_once() -> dict[str, object]:
        assert slack_config is not None
        async def dispatch(message: SlackInboundMessage) -> tuple[str, str]:
            config = cli._slack_inbound_run_config(message, slack_config, args, settings)
            result = await cli.run_goal(config, provider_factory(cli._provider_config(config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(config))
            return result.job_id, result.status.value
        with cli.SlackInboundStore(cli.slack_inbound_state_path(state_path)) as store:
            result = cli.asyncio.run(cli.run_slack_inbound_channel(slack_config, store=store, dispatch=dispatch, maximum_seconds=args.receiver_seconds))
        return {"receiver": "slack", "accepted_count": result.accepted_count, "duplicate_count": result.duplicate_count, "ignored_count": result.ignored_count, "rejected_count": result.rejected_request_count, "bound_port": result.bound_port, "dispatches": [{"event_id": item.event_id, "job_id": item.job_id, "job_status": item.job_status, "outcome": item.outcome} for item in result.dispatches]}

    def run_discord_once() -> dict[str, object]:
        assert discord_config is not None
        async def dispatch(message: DiscordInboundMessage) -> tuple[str, str]:
            config = cli._discord_inbound_run_config(message, discord_config, args, settings)
            result = await cli.run_goal(config, provider_factory(cli._provider_config(config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(config))
            return result.job_id, result.status.value
        with cli.DiscordInboundStore(cli.discord_inbound_state_path(state_path)) as store:
            result = cli.asyncio.run(
                cli.run_discord_inbound_channel(
                    discord_config,
                    store=store,
                    maximum_seconds=args.receiver_seconds,
                    dispatch=dispatch,
                )
            )
        return {
            "receiver": "discord",
            "accepted_count": result.accepted_count,
            "duplicate_count": result.duplicate_count,
            "ignored_count": result.ignored_count,
            "dispatches": [
                {"message_id": item.message_id, "job_id": item.job_id, "job_status": item.job_status, "outcome": item.outcome}
                for item in result.dispatches
            ],
        }

    def run_matrix_once() -> dict[str, object]:
        assert matrix_config is not None
        async def dispatch(message: MatrixInboundMessage) -> tuple[str, str]:
            config = cli._matrix_inbound_run_config(message, matrix_config, args, settings)
            result = await cli.run_goal(config, provider_factory(cli._provider_config(config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(config))
            return result.job_id, result.status.value
        with cli.MatrixInboundCursorStore(cli.matrix_inbound_state_path(state_path)) as cursor_store, cli.InboundMessageStore(cli.inbound_state_path(state_path)) as message_store:
            result = cli.asyncio.run(cli.run_matrix_inbound(matrix_config, cursor_store=cursor_store, message_store=message_store, dispatch=dispatch))
        return {"receiver": "matrix", "primed": result.primed, "accepted_count": result.accepted_count, "duplicate_count": result.duplicate_count, "ignored_count": result.ignored_count, "dispatches": list(result.dispatches)}

    def run_mattermost_once() -> dict[str, object]:
        assert mattermost_config is not None
        async def dispatch(message: MattermostInboundMessage) -> tuple[str, str]:
            config = cli._mattermost_inbound_run_config(message, mattermost_config, args, settings)
            result = await cli.run_goal(config, provider_factory(cli._provider_config(config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(config))
            return result.job_id, result.status.value
        with cli.MattermostInboundCursorStore(cli.mattermost_inbound_state_path(state_path)) as cursor_store, cli.InboundMessageStore(cli.inbound_state_path(state_path)) as message_store:
            result = cli.asyncio.run(cli.run_mattermost_inbound(mattermost_config, cursor_store=cursor_store, message_store=message_store, dispatch=dispatch))
        return {"receiver": "mattermost", "primed": result.primed, "accepted_count": result.accepted_count, "duplicate_count": result.duplicate_count, "ignored_count": result.ignored_count, "dispatches": list(result.dispatches)}

    handlers = {"telegram": run_telegram_once, "slack": run_slack_once, "discord": run_discord_once, "email": run_email_once, "ntfy": run_ntfy_once, "matrix": run_matrix_once, "mattermost": run_mattermost_once}; cycles: list[dict[str, object]] = []; consecutive_failures = 0
    try:
        while args.max_cycles is None or len(cycles) < args.max_cycles:
            results: list[dict[str, object]] = []; failed = False
            for receiver in receivers:
                try: results.append(handlers[receiver]())
                except Exception as exc:
                    failed = True; results.append({"receiver": receiver, "error": type(exc).__name__})
            consecutive_failures = consecutive_failures + 1 if failed else 0
            cycles.append({"cycle": len(cycles) + 1, "receivers": results, "consecutive_failures": consecutive_failures})
            if consecutive_failures >= 3: break
            if args.max_cycles is None or len(cycles) < args.max_cycles: cli.time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        pass
    record = {"gateway": "foreground_operator_confirmed_receiver_supervisor", "receivers": list(receivers), "poll_seconds": args.poll_seconds, "cycles": cycles, "stopped": "terminal_interrupt_cycle_limit_or_three_consecutive_failed_rounds", "state_path": str(state_path.expanduser().resolve())}
    if args.json: print(cli.json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2), file=output)
    else:
        print(f"Gateway supervisor stopped · {len(cycles)} round(s) · foreground only", file=output)
        if consecutive_failures >= 3: print("Stopped after three consecutive failed rounds; inspect configuration or receiver output before restarting.", file=output)
        print("No detached service, automatic restart, reply delivery, pairing, or permission escalation was created.", file=output)
    return cli.EXIT_JOB_FAILED if consecutive_failures >= 3 else cli.EXIT_OK
