"""Continuation, ACP, and interactive terminal command adapter."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

import shlex

async def _continue_read_only_partial_runtime(
    *,
    config: RunCommandConfig,
    provider_config: ProviderConfig,
    provider_factory: ProviderFactory,
    job_id: str,
    frozen_route_composition=None,
    frozen_route_catalog=None,
) -> JobResult:
    """Run the sole receipt-bound partial continuation path for every surface."""

    state_path = config.state_path
    portfolio_path = state_path.with_name(f"{state_path.stem}.work-orders.db")
    try:
        with cli.WorkOrderPortfolioStore(portfolio_path) as work_orders:
            request = work_orders.continuation_request(job_id)
    except KeyError as exc:
        raise ValueError(
            f"No retained read-only continuation request exists for Job {job_id!r}"
        ) from exc
    if frozen_route_composition is not None and frozen_route_catalog is not None:
        raise ValueError("continuation accepts one frozen route composition source")
    if frozen_route_catalog is not None:
        from dynamic_firm.application.frozen_route_goal_composition import (
            FrozenRouteContinuationBundle,
        )

        with cli.WorkOrderPortfolioStore(portfolio_path) as work_orders:
            persisted = work_orders.frozen_route_continuation_bundle(request.job_id)
        if persisted is None:
            raise ValueError("frozen route continuation catalog has no retained Job bundle")
        raw, digest = persisted
        bundle = FrozenRouteContinuationBundle.from_canonical_json(raw)
        if bundle.digest != digest:
            raise ValueError("persisted frozen route continuation bundle digest drifted")
        frozen_route_composition = frozen_route_catalog.reassemble(bundle)
    from dynamic_firm.application.job_continuation import ReceiptBoundContinuationService
    from dynamic_firm.application import continuation_artifact_preflight as artifact_preflight_module
    from dynamic_firm.application.continuation_artifact_preflight import preflight_continuation_artifacts_from_state
    from dynamic_firm.application.continuation_capability_assembly import assemble_continuation_capabilities
    from dynamic_firm.application import continuation_runtime_preflight as runtime_preflight
    from dynamic_firm.foundation.runtime import NoructEmployeeRuntimeService

    frozen_route_runtime_kwargs = {}
    if frozen_route_composition is not None:
        frozen_route_composition.require_config_path(config.config_path)
        frozen_route_composition.require_registry_closure()
        frozen_route_runtime_kwargs = frozen_route_composition.foundation_runtime_kwargs()

    store = cli.RunStore(config.state_path)
    company_store = cli.CompanyStateStore(config.state_path)
    employee_service = None
    capability_assembly = None
    try:
        inspector = cli.ActiveJobInspector(
            store,
            company_coordination=config.company_coordination,
        )
        inspection = inspector.inspect(request.job_id)
        artifact_preflight = preflight_continuation_artifacts_from_state(
            request=request,
            audit_pins=inspection.evolution_artifact_pins,
            runtime_state_path=state_path,
        )
        runtime_preflight.require_company_run_request_runtime_bindings(request)
        capability_assembly = assemble_continuation_capabilities(
            config=config,
            request=request,
            run_store=store,
            company_store=company_store,
            workspace_id=cli.WORKSPACE_ID,
            graph_decision=False,
        )
        registry = capability_assembly.registry
        provider = runtime_preflight.build_validated_company_run_request_provider(
            request=request,
            provider_config=provider_config,
            registry=registry,
            provider_factory=provider_factory,
            company_coordination=config.company_coordination,
        )
        employee_service = NoructEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=registry,
            approval_port=None,
            company_coordination=config.company_coordination,
            python_executable=config.runtime_python,
            **frozen_route_runtime_kwargs,
        )
        budget = cli.SQLiteCompanyBudgetAuthority(
            store,
            cli.CompanyCostBudgetPolicy.from_mapping(company_store.company_cost_budget_policy()),
        )
        ledger = cli.SQLiteActiveJobLedger(
            store,
            evolution_artifact_pins=(
                ()
                if artifact_preflight.resolution is None
                else artifact_preflight.resolution.pins
            ),
            evolution_artifact_effects=(
                ()
                if artifact_preflight.resolution is None
                else artifact_preflight.resolution.effects
            ),
            company_coordination=config.company_coordination,
        )

        async def continue_partial(
            restored: CompanyRunRequest,
            pending_execution_session_key: str,
        ) -> JobResult:
            if restored != request:
                raise RuntimeError("Continuation request changed before Kernel entry")
            assignment_admission = (
                None
                if frozen_route_composition is None
                else frozen_route_composition.assignment_admission_for(
                    restored,
                    state_path=config.state_path,
                )
            )
            task_action_policy_override = (
                None
                if frozen_route_composition is None
                else frozen_route_composition.kernel_task_action_policy_override()
            )
            return await cli.FirmKernel(
                employee_execution=employee_service,
                active_job_ledger=ledger,
                company_budget_authority=budget,
                assignment_admission=assignment_admission,
                task_action_policy_override=task_action_policy_override,
            ).continue_partial_read_only_job(
                restored,
                pending_execution_session_key=pending_execution_session_key,
            )

        with cli.WorkOrderPortfolioStore(portfolio_path) as authority:
            outcome = await ReceiptBoundContinuationService(
                work_orders=authority,
                inspector=inspector,
                continue_partial=continue_partial,
                frozen_route_composition=frozen_route_composition,
                state_path=(
                    config.state_path
                    if frozen_route_composition is not None
                    else None
                ),
            ).resume_partial_read_only_job(request.job_id)
        return outcome.result
    except (
        artifact_preflight_module.ContinuationArtifactPreflightError,
        runtime_preflight.ContinuationRuntimePreflightError,
    ) as error:
        store.append_job_continuation_preflight_refusal(
            job_id=request.job_id,
            continuation_kind="READ_ONLY_PARTIAL",
            code=error.code.value,
        )
        raise
    finally:
        if employee_service is not None:
            await employee_service.close()
        if (
            capability_assembly is not None
            and capability_assembly.session_store is not None
        ):
            capability_assembly.session_store.close()
        company_store.close()
        store.close()

def _run_read_only_partial_continuation(
    args: cli.argparse.Namespace,
    settings: dict,
    *,
    provider_factory: ProviderFactory,
    stdout: TextIO,
) -> int:
    """Compose the parsed command with the surface-neutral continuation runtime."""

    from dynamic_firm.application.read_only_continuation_cli import (
        ReadOnlyContinuationCliPorts,
        run_read_only_continuation_command,
    )

    return run_read_only_continuation_command(
        args,
        state_path=cli._state_path(args, settings),
        ports=ReadOnlyContinuationCliPorts(
            load_config=lambda candidate: cli._run_config(candidate, settings),
            provider_config_for=cli._provider_config,
            provider_factory=provider_factory,
            continue_partial=_continue_read_only_partial_runtime,
            handoff_partial=_handoff_read_only_partial_runtime,
            render_result=cli._render_result,
        ),
        output=stdout,
    )

def _run_read_only_partial_handoff(
    args: cli.argparse.Namespace,
    settings: dict,
    *,
    stdout: TextIO,
) -> int:
    """Transfer only an unclaimed local read-only continuation authority."""

    from dynamic_firm.application.read_only_continuation_cli import (
        ReadOnlyContinuationCliPorts,
        run_read_only_continuation_command,
    )

    return run_read_only_continuation_command(
        args,
        state_path=cli._state_path(args, settings),
        ports=ReadOnlyContinuationCliPorts(
            load_config=lambda candidate: cli._run_config(candidate, settings),
            provider_config_for=lambda _config: None,
            provider_factory=lambda _config: None,
            continue_partial=_continue_read_only_partial_runtime,
            handoff_partial=_handoff_read_only_partial_runtime,
            render_result=cli._render_result,
        ),
        output=stdout,
    )

def _handoff_read_only_partial_runtime(
    *,
    config: RunCommandConfig,
    job_id: str,
    target_device_id: str,
) -> object:
    """Shared non-executing handoff path for CLI, TUI and future GUI."""

    if config.company_coordination is None:
        raise ValueError("Company coordination must be enabled before a device handoff")
    from dynamic_firm.application.job_continuation import ReceiptBoundContinuationService

    async def unavailable_continuation(_: CompanyRunRequest, __: str) -> JobResult:
        raise RuntimeError("Continuation execution is unavailable during handoff")

    portfolio_path = config.state_path.with_name(
        f"{config.state_path.stem}.work-orders.db"
    )
    store = cli.RunStore(config.state_path)
    try:
        with cli.WorkOrderPortfolioStore(portfolio_path) as work_orders:
            return ReceiptBoundContinuationService(
                work_orders=work_orders,
                inspector=cli.ActiveJobInspector(
                    store,
                    company_coordination=config.company_coordination,
                ),
                continue_partial=unavailable_continuation,
            ).handoff_partial_read_only_job(
                job_id,
                target_device_id=target_device_id,
            )
    finally:
        store.close()

async def _continue_graph_proposal_runtime(
    *,
    config: RunCommandConfig,
    provider_config: ProviderConfig,
    provider_factory: ProviderFactory,
    job_id: str,
    proposal_id: str,
    approve: bool,
    approval_port: ApprovalPort | None,
) -> JobResult:
    """Use local Work Order authority and the exact pending Graph receipt."""

    from dynamic_firm.application.graph_proposal_continuation import (
        GraphProposalContinuationService,
    )
    from dynamic_firm.application import continuation_artifact_preflight as artifact_preflight_module
    from dynamic_firm.application.continuation_artifact_preflight import preflight_continuation_artifacts_from_state
    from dynamic_firm.application.continuation_capability_assembly import assemble_continuation_capabilities
    from dynamic_firm.application import continuation_runtime_preflight as runtime_preflight
    from dynamic_firm.foundation.runtime import NoructEmployeeRuntimeService

    portfolio_path = config.state_path.with_name(
        f"{config.state_path.stem}.work-orders.db"
    )
    try:
        with cli.WorkOrderPortfolioStore(portfolio_path) as work_orders:
            request = work_orders.continuation_request(job_id)
    except KeyError as exc:
        raise ValueError(
            f"No retained Graph continuation request exists for Job {job_id!r}"
        ) from exc

    store = cli.RunStore(config.state_path)
    company_store = cli.CompanyStateStore(config.state_path)
    employee_service = None
    capability_assembly = None
    try:
        inspection = cli.ActiveJobInspector(
            store,
            company_coordination=config.company_coordination,
        ).inspect(request.job_id)
        artifact_preflight = preflight_continuation_artifacts_from_state(
            request=request,
            audit_pins=inspection.evolution_artifact_pins,
            runtime_state_path=config.state_path,
        )
        runtime_preflight.require_company_run_request_runtime_bindings(request)
        capability_assembly = assemble_continuation_capabilities(
            config=config,
            request=request,
            run_store=store,
            company_store=company_store,
            workspace_id=cli.WORKSPACE_ID,
            graph_decision=True,
        )
        registry = capability_assembly.registry
        provider = runtime_preflight.build_validated_company_run_request_provider(
            request=request,
            provider_config=provider_config,
            registry=registry,
            provider_factory=provider_factory,
            company_coordination=config.company_coordination,
        )
        employee_service = NoructEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=registry,
            approval_port=approval_port,
            company_coordination=config.company_coordination,
            python_executable=config.runtime_python,
        )
        budget = cli.SQLiteCompanyBudgetAuthority(
            store,
            cli.CompanyCostBudgetPolicy.from_mapping(
                company_store.company_cost_budget_policy()
            ),
        )
        ledger = cli.SQLiteActiveJobLedger(
            store,
            evolution_artifact_pins=(
                ()
                if artifact_preflight.resolution is None
                else artifact_preflight.resolution.pins
            ),
            evolution_artifact_effects=(
                ()
                if artifact_preflight.resolution is None
                else artifact_preflight.resolution.effects
            ),
            company_coordination=config.company_coordination,
        )
        kernel = cli.FirmKernel(
            employee_execution=employee_service,
            active_job_ledger=ledger,
            company_budget_authority=budget,
        )

        async def approved(
            restored: CompanyRunRequest,
            proposal,
        ) -> JobResult:
            if restored != request:
                raise RuntimeError("Graph continuation request changed before Kernel entry")
            return await kernel.continue_approved_graph_proposal(restored, proposal)

        async def rejected(
            restored: CompanyRunRequest,
            proposal,
        ) -> JobResult:
            if restored != request:
                raise RuntimeError("Graph continuation request changed before Kernel entry")
            return await kernel.continue_rejected_graph_proposal(restored, proposal)

        with cli.WorkOrderPortfolioStore(portfolio_path) as authority:
            outcome = await GraphProposalContinuationService(
                work_orders=authority,
                ledger=ledger,
                continue_approved=approved,
                continue_rejected=rejected,
            ).decide(
                job_id=job_id,
                proposal_id=proposal_id,
                approve=approve,
            )
        return outcome.result
    except (
        artifact_preflight_module.ContinuationArtifactPreflightError,
        runtime_preflight.ContinuationRuntimePreflightError,
    ) as error:
        store.append_job_continuation_preflight_refusal(
            job_id=request.job_id,
            continuation_kind="GRAPH_PROPOSAL",
            code=error.code.value,
        )
        raise
    finally:
        if employee_service is not None:
            await employee_service.close()
        if (
            capability_assembly is not None
            and capability_assembly.session_store is not None
        ):
            capability_assembly.session_store.close()
        company_store.close()
        store.close()

def _run_graph_proposal_continuation(
    args: cli.argparse.Namespace,
    settings: dict,
    *,
    provider_factory: ProviderFactory,
    stdin: TextIO,
    stdout: TextIO,
) -> int:
    """Resolve one durable Graph decision through the only same-Job path."""

    if not args.confirm:
        raise ValueError("Graph proposal decision requires --confirm")
    state_path = cli._state_path(args, settings)
    portfolio_path = state_path.with_name(f"{state_path.stem}.work-orders.db")
    try:
        with cli.WorkOrderPortfolioStore(portfolio_path) as work_orders:
            request = work_orders.continuation_request(args.job_id)
    except KeyError as exc:
        raise ValueError(
            f"No retained Graph continuation request exists for Job {args.job_id!r}"
        ) from exc
    config_args = cli.argparse.Namespace(**{**vars(args), "goal": request.goal})
    config = cli._run_config(config_args, settings)
    if config.state_path != state_path:
        raise ValueError("Graph continuation state path does not match the selected Company state")
    provider_config = cli._provider_config(config)
    show_tui = cli._isatty(stdin) and cli._isatty(stdout) and not args.json and not args.plain
    ui = (
        cli.InlineTerminalUI(stdin=stdin, stdout=stdout, plain=args.plain)
        if show_tui or config.permission_mode == "ask"
        else None
    )
    approval_port = (
        cli.InteractiveApprovalController(ui)
        if ui and cli._interactive_approval_available_for(config)
        else None
    )
    result = cli.asyncio.run(
        _continue_graph_proposal_runtime(
            config=config,
            provider_config=provider_config,
            provider_factory=provider_factory,
            job_id=args.job_id,
            proposal_id=args.proposal_id,
            approve=args.decision == "approve",
            approval_port=approval_port,
        )
    )
    cli._render_result(result, as_json=args.json, output=stdout)
    return cli.EXIT_OK if result.status == cli.JobStatus.SUCCEEDED else cli.EXIT_JOB_FAILED

def _run_once(
    args: cli.argparse.Namespace,
    settings: dict,
    *,
    provider_factory: ProviderFactory,
    coding_worker_factory: CodingWorkerFactory,
    stdin: TextIO,
    stdout: TextIO,
) -> int:
    execution = cli._goal_execution_services(
        provider_factory=provider_factory,
        coding_worker_factory=coding_worker_factory,
    )
    prepared = execution.prepare(args, settings)
    config = prepared.config
    roster_snapshot = prepared.roster_snapshot
    route = (
        cli.route_interactive_input(config.goal).route
        if args.command == "ask"
        else cli.InputRoute.COMPANY_GOAL
    )
    show_tui = cli._isatty(stdin) and cli._isatty(stdout) and not args.json and not args.plain
    ui = (
        cli.InlineTerminalUI(stdin=stdin, stdout=stdout, plain=args.plain)
        if show_tui or config.permission_mode == "ask"
        else None
    )
    approval_port = (
        cli.InteractiveApprovalController(ui)
        if ui and cli._interactive_approval_available_for(config)
        else None
    )
    if show_tui and ui:
        ui.banner(
            workspace=str(config.workspace),
            model=config.model,
            provider=cli._provider_display(config),
            authority=cli._authority_display(config),
            version=cli.__version__,
            roster_revision=roster_snapshot.revision,
            active_employee_count=roster_snapshot.active_employee_count,
            **cli._tui_company_facts(config, roster_snapshot),
        )
        ui.begin_goal(config.goal)
    try:
        result = cli.asyncio.run(
            execution.execute(
                prepared,
                approval_port=approval_port,
                event_sink=ui.handle_event if show_tui and ui else None,
                route=route,
            )
        )
        if show_tui and ui:
            ui.answer(result.summary or f"Job ended with status {result.status.value}.")
            report = cli.company_final_report(result)
            if report.manager_employee_id:
                ui.commit(report.operator_line(), tone="muted")
            ui.result_details(result)
    finally:
        if ui:
            ui.close()
    if not show_tui:
        cli._render_result(result, as_json=args.json, output=stdout)
    return cli.EXIT_OK if result.status == cli.JobStatus.SUCCEEDED else cli.EXIT_JOB_FAILED

def _run_acp_command(
    args: cli.argparse.Namespace,
    settings: dict,
    *,
    provider_factory: ProviderFactory,
    coding_worker_factory: CodingWorkerFactory,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run the local ACP transport without giving it a second state authority.

    ACP has no positional goal.  The synthetic goal below is only used to
    validate the normal provider/runtime configuration before the protocol
    server begins; every real editor prompt replaces it before ``run_goal``.
    """

    config_args = cli.argparse.Namespace(**vars(args))
    config_args.goal = "ACP editor session configuration"
    config = cli._run_config(config_args, settings)
    if args.check:
        # Construct the transport exactly as a prompt would, but never call a
        # provider or open the stdio server.  This catches selected runtime
        # profile and local executable configuration errors early.
        cli._provider_config(config)
        print(
            f"Noruct ACP check OK · {config.provider_kind} · {config.model} · {config.workspace}",
            file=stdout,
        )
        return cli.EXIT_OK

    async def run_turn(
        session: CompanySession,
        goal: str,
        event_sink: Callable[[ProductEvent], None],
        approval_port: AcpApprovalPort | None,
    ) -> JobResult:
        turn_config = cli.replace(
            config,
            goal=goal,
            workspace=cli.Path(session.workspace).resolve(),
            model=session.model,
            codex_model=(
                session.model
                if config.provider_kind == "openai_codex" and session.model != "codex-default"
                else None
            ),
        )
        roster_snapshot = cli._load_active_roster(turn_config)
        provider_config = cli._provider_config(turn_config)
        provider = provider_factory(provider_config)
        coding_worker = (
            coding_worker_factory(provider_config)
            if isinstance(provider_config, cli.CodexExecProviderConfig)
            and turn_config.permission_mode == "ask"
            else None
        )
        sessions = cli.CompanySessionStore(turn_config.state_path)
        try:
            result = await cli.run_goal(
                turn_config,
                provider,
                approval_port=approval_port,
                coding_worker=coding_worker,
                event_sink=event_sink,
                prior_context=sessions.recent_context(session.session_id),
                route=cli.route_interactive_input(goal).route,
                roster_snapshot=roster_snapshot,
                session_key=session.session_id,
            )
            sessions.append_turn(
                session_id=session.session_id,
                goal=goal,
                job_id=result.job_id,
                status=result.status.value,
                summary=result.summary,
                usage=result.metrics.usage,
            )
            return result
        finally:
            sessions.close()

    return cli.asyncio.run(
        cli.serve_acp_stdio(
            state_path=config.state_path,
            default_workspace=config.workspace,
            default_model=config.model,
            provider_binding=cli.session_provider_binding(config),
            permission_mode=config.permission_mode,
            turn_runner=run_turn,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
    )

def _interactive_help(ui: cli.InlineTerminalUI) -> None:
    ui.show_help()

def _interactive_skill_messages(
    config: RunCommandConfig,
    query: str = "",
) -> tuple[str, ...]:
    """Return a read-only, fresh view for `/skills` in either terminal UI."""

    catalog = cli.discover_external_skills(config.external_skill_dirs)
    if query:
        selected = cli.select_external_skills(catalog, query=query, limit=3)
        lines = [
            f"External skill preview · {len(selected)} selected for this goal · no Job created"
        ]
        lines.extend(
            f"{item.name} · {item.snapshot.revision}"
            + (f" · {item.description}" if item.description else "")
            for item in selected
        )
        return tuple(lines)
    lines = [
        f"External skills · {len(catalog.skills)} compatible · {catalog.skipped_count} skipped · Job-local read only"
    ]
    if not catalog.roots:
        lines.append("No skill roots configured. Start chat with --skills-dir PATH or set [skills].external_dirs.")
    else:
        lines.extend(
            f"{item.name} · {item.relative_path}"
            + (f" · {item.description}" if item.description else "")
            for item in catalog.skills[:8]
        )
        if len(catalog.skills) > 8:
            lines.append(f"… {len(catalog.skills) - 8} more · use `noruct skills list` for all")
    lines.append("Use /skills <goal> to preview the up to three instructions selected for a Job.")
    return tuple(lines)

def _session_browse_response(
    sessions: cli.CompanySessionStore,
    raw_args: str,
    *,
    current_session_id: str,
) -> tuple[CompanySession | None, tuple[str, ...]]:
    """Resolve one local `/sessions` interaction without changing Company state."""

    try:
        browse = cli.browse_company_sessions(
            sessions,
            raw_args,
            current_session_id=current_session_id,
        )
    except ValueError:
        return None, ("Session command is invalid. Quote titles containing spaces.",)
    if browse.target:
        selected = sessions.resolve(browse.target)
        if selected is None:
            return None, (f"Company session was not found: {browse.target}",)
        return selected, (f"Resuming company session · {selected.session_id[:12]}",)
    if browse.search_query == "":
        return None, ("Provide a search query: /sessions search <title-or-id>.",)
    if not browse.items:
        qualifier = " matching the search" if browse.search_query else ""
        return None, (f"No other saved company sessions{qualifier}.",)
    messages = ["Saved company sessions · /sessions <id-or-title> resumes one"]
    for item in browse.items:
        preview = f" · {item.preview[:96]}" if item.preview else ""
        messages.append(
            f"{item.session_id[:12]} · {item.turn_count} turn(s) · "
            f"{item.title} · {item.model}{preview}"
        )
    return None, tuple(messages)

def _activate_interactive_session(
    args: cli.argparse.Namespace,
    settings: dict,
    session: CompanySession,
):
    """Restore a saved session's workspace and, when known, provider binding.

    A bound session is authoritative for its transport so a later global
    provider setting cannot silently send its retained local context to a
    different service.  Legacy rows created before this additive field use the
    current configuration explicitly because no historical transport exists.
    """

    args.workspace = cli.Path(session.workspace)
    if not cli.os.environ.get("NORUCT_MODEL"):
        args.model = session.model
    args.cost_mode = session.cost_efficiency_mode
    if session.has_provider_binding:
        args.provider_kind = session.provider_kind
        args.base_url = session.provider_base_url or None
        args.api_key_env = session.provider_api_key_env
        args.no_auth = session.provider_kind != "openai_codex" and session.provider_api_key_env is None
    args.goal = "Validate the company interface"
    config = cli._run_config(args, settings)
    if (
        session.has_mcp_binding
        and session.mcp_binding_digest != cli.session_mcp_binding(config)["mcp_binding_digest"]
    ):
        raise ValueError(
            "Saved company session requires its original MCP configuration. "
            "Restore that configuration or start a new company session."
        )
    return config

def _modern_controller_ports() -> cli.ModernControllerPorts:
    """Bind CLI ingress helpers to the reusable Modern terminal controller.

    The controller remains independently importable for a future desktop GUI
    or alternate terminal host.  These callbacks are intentionally the only
    route back into CLI-specific argument/configuration behavior.
    """

    return cli.ModernControllerPorts(
        activate_interactive_session=_activate_interactive_session,
        authority_display=cli._authority_display,
        company_settings_entries=cli._company_settings_entries,
        handoff_read_only_partial=_handoff_read_only_partial_runtime,
        continue_read_only_partial=_continue_read_only_partial_runtime,
        continue_graph_proposal=_continue_graph_proposal_runtime,
        goal_execution_services=cli._goal_execution_services,
        graph_preview_for_config=cli._graph_preview_for_config,
        interactive_skill_messages=_interactive_skill_messages,
        load_active_roster=cli._load_active_roster,
        load_config=cli._load_config,
        plugin_root=cli._plugin_root,
        provider_display=cli._provider_display,
        render_graph_control=cli._render_graph_control,
        run_capabilities_command=cli._run_capabilities_command,
        run_config=cli._run_config,
        run_gateway_service_command=cli._run_gateway_service_command,
        run_portfolio_command=_run_modern_portfolio_command,
        run_schedule_service_command=cli._run_schedule_service_command,
        session_browse_response=_session_browse_response,
        state_path_for=cli._state_path,
        tui_company_facts=cli._tui_company_facts,
    )


def _run_modern_portfolio_command(owner: object, argument: str) -> tuple[str, ...]:
    """Render the existing CLI portfolio runtime inside the Modern terminal.

    The terminal supplies no parallel queue, policy, provider, or budget
    implementation.  It only translates a small command grammar into the
    same parsed ingress used by ``noruct portfolio``.  Explicit confirmation
    remains visible in the terminal command itself.
    """

    try:
        tokens = shlex.split(argument)
    except ValueError:
        return ("Portfolio command has unmatched quoting.",)
    action = tokens.pop(0).lower() if tokens else "status"
    values = dict(vars(owner.args))
    values.update(
        command="portfolio",
        portfolio_command=action,
        state=owner.state_path,
        json=True,
    )
    if action == "status":
        pass
    elif action == "preview":
        values.update(
            context_fingerprint="",
            manager_employee_id="",
            automatic_blueprint_requested=False,
            manager_campaign_directory=None,
        )
    elif action == "submit":
        confirmed = "--confirm" in tokens
        goal = " ".join(item for item in tokens if item != "--confirm").strip()
        if not goal:
            return ("Usage: /portfolio submit --confirm GOAL",)
        values.update(
            goal=goal,
            priority=50,
            reserved_cost_usd=None,
            confirm=confirmed,
        )
    elif action == "drain":
        values["confirm"] = "--confirm" in tokens
    else:
        return (
            "Usage: /portfolio [status|preview|submit --confirm GOAL|drain --confirm]",
        )
    output = cli.io.StringIO()
    try:
        code = cli._run_portfolio(
            cli.argparse.Namespace(**values),
            owner.settings,
            output,
            provider_factory=owner.provider_factory,
            coding_worker_factory=owner.coding_worker_factory,
        )
        payload = cli.json.loads(output.getvalue())
    except (TypeError, ValueError, OSError, RuntimeError, cli.json.JSONDecodeError) as exc:
        return (f"Portfolio command blocked · {exc}",)
    if action == "status":
        entries = payload.get("entries", ())
        rendered = tuple(
            f"{item['work_order_id']} · {item['status']} · {item['reason']}"
            for item in entries[:8]
        )
        return (
            f"Portfolio · entries={len(entries)} · settlements={len(payload.get('settlements', ()))}",
            *(rendered or ("No local Work Orders.",)),
        )
    if action == "preview":
        next_entry = payload.get("next_entry")
        return (
            "Portfolio preview · "
            f"reuse={payload.get('reuse_decision', 'SOLO_ONLY')} · "
            + (
                f"next={next_entry.get('work_order_id')}"
                if isinstance(next_entry, dict)
                else "no admitted Work Order"
            ),
            *tuple(f"Blocked/limited · {reason}" for reason in payload.get("reasons", ())[:4]),
        )
    if action == "submit":
        entry = payload["entry"]
        return (
            f"Portfolio Work Order queued · {entry['work_order_id']} · priority={entry['priority']}",
            "No provider call or Job execution occurred.",
        )
    result = payload["result"]
    return (
        f"Portfolio drain {'completed' if code == cli.EXIT_OK else 'blocked'} · waves={result['waves']} · settled={len(result['settled_job_ids'])}",
        *(
            ("Bound Jobs require inspection before another drain.",)
            if result["blocked_job_ids"]
            else ()
        ),
    )

def _ModernInteractiveController(
    args: cli.argparse.Namespace,
    settings: dict,
    *,
    provider_factory: ProviderFactory,
    coding_worker_factory: CodingWorkerFactory,
) -> cli.ModernInteractiveController:
    """Compatibility factory for the former private CLI controller symbol."""

    return cli.ModernInteractiveController(
        args,
        settings,
        provider_factory=provider_factory,
        coding_worker_factory=coding_worker_factory,
        ports=_modern_controller_ports(),
    )

def _run_modern_interactive(
    args: cli.argparse.Namespace,
    settings: dict,
    *,
    provider_factory: ProviderFactory,
    coding_worker_factory: CodingWorkerFactory,
) -> int:
    controller = cli.ModernInteractiveController(
        args,
        settings,
        provider_factory=provider_factory,
        coding_worker_factory=coding_worker_factory,
        ports=_modern_controller_ports(),
    )
    try:
        cli.run_modern_terminal(controller)
    finally:
        controller.close()
    return cli.EXIT_OK

def _is_alternate_screen_terminal(stream: TextIO) -> bool:
    """Return whether *stream* is a real OS terminal, not a test/piped shim."""

    try:
        return cli.os.isatty(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False

def _resolve_interactive_terminal_ui(
    args: cli.argparse.Namespace,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> str:
    """Choose a renderer without making the optional UI a runtime authority.

    ``auto`` is the product default for a real terminal: a present, audited
    modern profile gets the fixed Company console while a minimal installation
    stays usable. Stream shims remain native so programmatic callers and tests
    never receive an alternate-screen renderer unexpectedly. Explicit
    ``modern`` remains fail-closed and actionable when the profile is absent.
    Plain and scrollback-safe native modes always keep their existing contract.
    """

    requested = getattr(args, "terminal_ui", "auto")
    if args.plain or args.no_live_screen or requested == "native":
        return "native"
    if (
        requested == "auto"
        and stdin is not None
        and stdout is not None
        and not (_is_alternate_screen_terminal(stdin) and _is_alternate_screen_terminal(stdout))
    ):
        return "native"
    if cli.modern_terminal_available():
        return "modern"
    if requested == "modern":
        raise cli.ModernTerminalUnavailable(cli.modern_terminal_install_hint())
    return "native"
