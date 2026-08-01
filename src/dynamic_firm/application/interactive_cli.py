"""Interactive Product session command adapter."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

from dynamic_firm import __version__

def _run_interactive(
    args: cli.argparse.Namespace,
    settings: dict,
    *,
    provider_factory: ProviderFactory,
    coding_worker_factory: CodingWorkerFactory,
    stdin: TextIO,
    stdout: TextIO,
) -> int:
    if not (cli._isatty(stdin) and cli._isatty(stdout)):
        raise ValueError("The company interface requires an interactive terminal.")
    if cli._resolve_interactive_terminal_ui(args, stdin=stdin, stdout=stdout) == "modern":
        return cli._run_modern_interactive(
            args,
            settings,
            provider_factory=provider_factory,
            coding_worker_factory=coding_worker_factory,
        )
    state_path = cli._state_path(args, settings)
    sessions = cli.CompanySessionStore(state_path)
    ui = cli.LiveTerminalUI(
        stdin=stdin,
        stdout=stdout,
        plain=args.plain,
        live_screen=False if args.no_live_screen else None,
    )
    try:
        session = sessions.resolve(args.session) if args.command == "resume" else None
        if args.command == "resume" and session is None:
            reference = args.session or "latest"
            raise ValueError(f"Company session was not found: {reference}")
        if session is not None:
            config = cli._activate_interactive_session(args, settings, session)
        else:
            args.goal = "Validate the company interface"
            config = cli._run_config(args, settings)
        roster_snapshot = cli._load_active_roster(config)
        if session is None:
            session = sessions.create(
                workspace=config.workspace,
                model=config.model,
                **cli.session_provider_binding(config),
                **cli.session_mcp_binding(config),
                **cli.session_cost_mode_binding(config),
            )
        ui.banner(
            workspace=str(config.workspace),
            session_id=session.session_id,
            model=config.model,
            provider=cli._provider_display(config),
            authority=cli._authority_display(config),
            version=__version__,
            roster_revision=roster_snapshot.revision,
            active_employee_count=roster_snapshot.active_employee_count,
            **cli._tui_company_facts(config, roster_snapshot),
        )
        ui.seed_input_history(sessions.input_history(session.session_id))
        audit_store = cli.RunStore(state_path)
        try:
            interrupted = next(
                (
                    item
                    for item in cli.ActiveJobInspector(audit_store).list(20)
                    if item.audit_status.value == "INTERRUPTED"
                ),
                None,
            )
        finally:
            audit_store.close()
        if interrupted is not None:
            ui.commit(
                f"△ Interrupted job {interrupted.job_id} · inspect with "
                f"noruct job inspect {interrupted.job_id}",
                tone="warning",
            )
        approval_port = cli.InteractiveApprovalController(ui)
        session_usage = sessions.usage(session.session_id)
        turn_count = session.turn_count
        execution = cli._goal_execution_services(
            provider_factory=provider_factory,
            coding_worker_factory=coding_worker_factory,
        )
        while True:
            goal = ui.read_goal()
            if goal is None or goal.lower() in {"/quit", "/exit"}:
                break
            if not goal:
                continue
            command, _, command_arg = goal.partition(" ")
            if command in {"/help", "?", "/"}:
                cli._interactive_help(ui)
                continue
            if command == "/view":
                ui.toggle_live_view(command_arg)
                continue
            if command == "/details":
                ui.toggle_details(command_arg)
                continue
            if command in {"/remember", "/knowledge", "/intent", "/decision", "/question", "/research", "/workbench"}:
                from dynamic_firm.product.knowledge_commands import (
                    execute_local_knowledge_command,
                )

                try:
                    messages = execute_local_knowledge_command(
                        state_path,
                        command,
                        command_arg,
                    )
                except (OSError, ValueError) as exc:
                    ui.commit(f"Local Knowledge command failed safely · {exc}", tone="warning")
                else:
                    for message in messages:
                        ui.commit(message, tone="muted")
                continue
            if command == "/skills":
                for message in cli._interactive_skill_messages(config, command_arg.strip()):
                    ui.commit(message, tone="muted")
                continue
            if command == "/model":
                selected_model = command_arg.strip()
                if not selected_model:
                    selected_model = ui.choose_model(
                        cli.model_options(config.provider_kind, config.model),
                        provider=cli._provider_display(config),
                    ) or ""
                if not selected_model:
                    continue
                if selected_model.lower().startswith("search"):
                    _, _, query = selected_model.partition(" ")
                    try:
                        matches = cli.filter_model_options(
                            cli.model_options(config.provider_kind, config.model), query
                        )
                    except ValueError as exc:
                        ui.commit(str(exc), tone="warning")
                        continue
                    if not matches:
                        ui.commit("No local model id matches that search.", tone="warning")
                    else:
                        ui.commit(
                            "Matching local models · "
                            + ", ".join(option.model_id for option in matches),
                            tone="muted",
                        )
                    continue
                candidate_args = cli.argparse.Namespace(**vars(args))
                candidate_args.goal = "Validate the selected session model"
                candidate_args.model = selected_model
                candidate_config = cli._run_config(candidate_args, settings)
                previous_model = config.model
                sessions.update_model(session.session_id, candidate_config.model)
                args.model = selected_model
                config = candidate_config
                ui.model_switched(previous=previous_model, current=config.model)
                continue
            if command == "/usage":
                ui.show_usage(session_usage)
                continue
            if command == "/mode":
                selected_mode = command_arg.strip().lower().replace("_", "-")
                if not selected_mode:
                    ui.commit(
                        f"Cost mode · {config.run_limits.cost_efficiency_mode.value}",
                        tone="muted",
                    )
                    ui.commit(
                        "economy compacts only noisy successful tool output before a model call; raw receipts, failures, and approval previews stay exact.",
                        tone="muted",
                    )
                    continue
                if selected_mode not in {"standard", "economy"}:
                    ui.commit("Cost mode must be standard or economy.", tone="warning")
                    continue
                previous_mode = config.run_limits.cost_efficiency_mode.value
                sessions.update_cost_efficiency_mode(session.session_id, selected_mode)
                args.cost_mode = selected_mode
                candidate_args = cli.argparse.Namespace(**vars(args))
                candidate_args.goal = "Validate the selected cost mode"
                config = cli._run_config(candidate_args, settings)
                ui.commit(
                    f"✓ Cost mode · {previous_mode} → {selected_mode}",
                    tone="success",
                )
                ui.commit(
                    "Provider receipts remain authoritative; economy is not a price guarantee.",
                    tone="muted",
                )
                continue
            if command == "/review":
                with cli.CompanyStateStore(state_path) as company_store:
                    previous_mode = company_store.retention_review_mode()
                    selected_mode = ui.choose_review_mode(previous_mode.value)
                    if selected_mode is None:
                        continue
                    company_store.set_retention_review_mode(
                        cli.RetentionReviewMode(selected_mode),
                        actor="user:tui",
                    )
                    current_mode = company_store.retention_review_mode()
                ui.review_mode_switched(
                    previous=previous_mode.value,
                    current=current_mode.value,
                )
                continue
            if command == "/evolution":
                selected_mode = command_arg.strip().lower().replace("_", "-")
                with cli.CompanyStateStore(state_path) as company_store:
                    previous_mode = company_store.evolution_autonomy_mode()
                    if not selected_mode:
                        ui.commit(
                            f"Company evolution · {previous_mode.value}", tone="muted"
                        )
                        ui.commit(
                            "Use /evolution never, /evolution propose, or /evolution always-approve. Always-approve affects future Jobs only; authority, budget, signature, compatibility, and running Job pins remain protected.",
                            tone="muted",
                        )
                        continue
                    try:
                        mode = cli.EvolutionAutonomyMode(selected_mode)
                    except ValueError:
                        ui.commit(
                            "Evolution mode must be never, propose, or always-approve.",
                            tone="warning",
                        )
                        continue
                    company_store.set_evolution_autonomy_mode(mode, actor="user:tui")
                ui.commit(
                    f"✓ Company evolution · {previous_mode.value} → {mode.value}",
                    tone="success",
                )
                continue
            if command == "/status":
                with cli.CompanyStateStore(state_path) as company_store:
                    review_mode = company_store.retention_review_mode().value
                    evolution_mode = company_store.evolution_autonomy_mode().value
                ui.show_status(
                    session_id=session.session_id,
                    turn_count=turn_count,
                    usage=session_usage,
                    review_mode=f"evolution {evolution_mode} · review {review_mode}",
                )
                continue
            if command == "/clear":
                ui.clear_screen()
                continue
            if command == "/sessions":
                selected, messages = cli._session_browse_response(
                    sessions,
                    command_arg,
                    current_session_id=session.session_id,
                )
                if selected is not None:
                    session = selected
                    config = cli._activate_interactive_session(args, settings, session)
                    roster_snapshot = cli._load_active_roster(config)
                    approval_port = cli.InteractiveApprovalController(ui)
                    session_usage = sessions.usage(session.session_id)
                    turn_count = session.turn_count
                    ui.seed_input_history(sessions.input_history(session.session_id))
                    ui.banner(
                        workspace=str(config.workspace),
                        session_id=session.session_id,
                        model=config.model,
                        provider=cli._provider_display(config),
                        authority=cli._authority_display(config),
                        version=__version__,
                        roster_revision=roster_snapshot.revision,
                        active_employee_count=roster_snapshot.active_employee_count,
                        **cli._tui_company_facts(config, roster_snapshot),
                    )
                for message in messages:
                    ui.commit(message, tone="muted")
                continue
            if command == "/new":
                session = sessions.create(
                    workspace=config.workspace,
                    model=config.model,
                    **cli.session_provider_binding(config),
                    **cli.session_mcp_binding(config),
                    **cli.session_cost_mode_binding(config),
                )
                roster_snapshot = cli._load_active_roster(config)
                approval_port = cli.InteractiveApprovalController(ui)
                session_usage = cli.Usage()
                turn_count = 0
                ui.seed_input_history(())
                ui.banner(
                    workspace=str(config.workspace),
                    session_id=session.session_id,
                    model=config.model,
                    provider=cli._provider_display(config),
                    authority=cli._authority_display(config),
                    version=__version__,
                    roster_revision=roster_snapshot.revision,
                    active_employee_count=roster_snapshot.active_employee_count,
                    **cli._tui_company_facts(config, roster_snapshot),
                )
                continue
            turn_args = cli.argparse.Namespace(**vars(args))
            turn_args.goal = goal
            prepared = execution.prepare(turn_args, settings)
            config = prepared.config
            roster_snapshot = prepared.roster_snapshot
            ui.set_roster(
                revision=roster_snapshot.revision,
                active_employee_count=roster_snapshot.active_employee_count,
            )
            ui.begin_goal(goal, echo=False)
            prior_context = sessions.recent_context(session.session_id)
            routing = cli.route_interactive_input(goal)
            result = cli.asyncio.run(
                execution.execute(
                    prepared,
                    approval_port=approval_port,
                    event_sink=None if args.plain else ui.handle_event,
                    prior_context=prior_context,
                    route=routing.route,
                    session_key=session.session_id,
                )
            )
            if args.plain:
                cli._render_result(result, as_json=False, output=stdout)
            else:
                ui.answer(result.summary or f"Job ended with status {result.status.value}.")
                report = cli.company_final_report(result)
                if report.manager_employee_id:
                    ui.commit(report.operator_line(), tone="muted")
                ui.result_details(result)
            sessions.append_turn(
                session_id=session.session_id,
                goal=goal,
                job_id=result.job_id,
                status=result.status.value,
                summary=result.summary,
                usage=result.metrics.usage,
            )
            session_usage = session_usage.plus(result.metrics.usage)
            turn_count += 1
    finally:
        ui.close()
        sessions.close()
    return cli.EXIT_OK
