"""Company goal runtime lifecycle."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

from dynamic_firm.product import InputRoute
from dynamic_firm.application.goal_capability_runtime import (
    GoalCapabilityPorts,
    register_goal_capabilities,
)
from dynamic_firm.application.goal_completion_runtime import (
    GoalCompletionPorts,
    execute_admitted_goal,
    prepare_admitted_execution,
    publish_capability_and_route_events,
    publish_goal_finished,
    publish_plan_decision,
    record_goal_learning,
)
from dynamic_firm.application.goal_planning_runtime import GoalPlanningPorts, prepare_goal_plan
from dynamic_firm.application.goal_company_lifecycle import (
    prepare_frozen_company_goal_intake,
)
from dynamic_firm.application.goal_runtime_resources import _JobRuntimeResources
from dynamic_firm.company.work_order_portfolio import WorkOrderPortfolioStore
from dynamic_firm.application.frozen_route_goal_composition import (
    FrozenRouteGoalComposition,
)


def _frozen_route_runtime_kwargs(
    composition: FrozenRouteGoalComposition | None,
    *,
    config_path,
) -> dict[str, object]:
    """Keep the default foundation construction byte-for-byte un-routed."""
    if composition is None:
        return {}
    if not isinstance(composition, FrozenRouteGoalComposition):
        raise TypeError("frozen route composition must be a FrozenRouteGoalComposition")
    composition.require_config_path(config_path)
    # This must precede any resource, Employee service, Kernel, or provider
    # construction.  A missing route definition is a closure failure, not a
    # reason to instantiate one adapter and discover drift at task dispatch.
    composition.require_registry_closure()
    return composition.foundation_runtime_kwargs()


async def run_goal(
    config: RunCommandConfig,
    provider: ModelProviderPort,
    *,
    approval_port: ApprovalPort | None = None,
    coding_worker: CodingWorkerPort | None = None,
    event_sink: Callable[[cli.ProductEvent], None] | None = None,
    prior_context: tuple[str, ...] = (),
    route: InputRoute = InputRoute.COMPANY_GOAL,
    roster_snapshot: ActiveRosterSnapshot | None = None,
    session_key: str = "",
    request_id: str | None = None,
    job_id: str | None = None,
    task_evidence: TaskEvidencePack | None = None,
    execution_origin: ExecutionOriginBinding | None = None,
    work_order_override: cli.WorkOrder | None = None,
    frozen_route_composition: FrozenRouteGoalComposition | None = None,
) -> JobResult:
    started = cli.time.monotonic()
    request_id = request_id or f"request-{cli.uuid.uuid4()}"
    job_id = job_id or f"job-{cli.uuid.uuid4()}"
    operating_decision = cli._operating_decision_for_route(config.goal, route)
    if (
        not request_id.startswith("request-")
        or not job_id.startswith("job-")
        or len(request_id) > 160
        or len(job_id) > 160
        or any(character.isspace() for character in request_id + job_id)
    ):
        raise ValueError("Caller-supplied request or Job identity is invalid")
    if (task_evidence is None) != (execution_origin is None):
        raise ValueError("Knowledge evidence and execution origin must be supplied together")
    if task_evidence is not None:
        task_evidence.verify()
    # Artifact activation is local and may change between Jobs.  Pinning and
    # projection happen only after the persistent ROSTER is frozen, so a
    # company default and an employee-scoped selection are both stable for the
    # Job.  This has no network I/O and cannot activate, install, or update an
    # Artifact.
    evolution_state_path = config.state_path.with_name(
        f"{config.state_path.stem}.evolution.db"
    )
    shadow_coding_enabled = (
        config.provider_kind == "openai_codex" and config.permission_mode == "ask"
    )
    if (
        frozen_route_composition is not None
        and route != InputRoute.COMPANY_GOAL
    ):
        raise ValueError(
            "Frozen route composition is available only for managed Company goals"
        )
    if shadow_coding_enabled and frozen_route_composition is not None:
        raise ValueError(
            "Frozen native route composition is unavailable with shadow coding"
        )
    if shadow_coding_enabled and coding_worker is None:
        raise ValueError("Codex ask mode requires the external shadow coding worker.")
    frozen_route_runtime_kwargs = _frozen_route_runtime_kwargs(
        frozen_route_composition,
        config_path=config.config_path,
    )
    config.state_path.parent.mkdir(parents=True, exist_ok=True)
    resources = _JobRuntimeResources.acquire(config.state_path)
    company_store = resources.company_store
    graph_blueprint_registry = resources.graph_blueprint_registry
    try:
        intake = prepare_frozen_company_goal_intake(
            config=config,
            provider=provider,
            resources=resources,
            route=route,
            roster_snapshot=roster_snapshot,
            session_key=session_key,
            request_id=request_id,
            job_id=job_id,
            execution_origin=execution_origin,
            work_order_override=work_order_override,
            operating_decision=operating_decision,
            evolution_state_path=evolution_state_path,
        )
    except BaseException:
        await resources.close()
        raise
    config = intake.config
    operating_decision = intake.operating_decision
    company_snapshot = intake.company_snapshot
    roster_snapshot = intake.roster_snapshot
    roster = intake.roster
    executive_manager = intake.executive_manager
    evolution_artifact_resolution = intake.evolution_artifact_resolution
    evolution_artifact_pins = intake.evolution_artifact_pins
    runtime_mcp_read_only = intake.runtime_mcp_read_only
    mcp_package_decision = intake.mcp_package_decision
    authority_snapshot = intake.authority_snapshot
    work_order_budget_snapshot = intake.work_order_budget_snapshot
    work_order = intake.work_order
    manager_assignment = intake.manager_assignment
    manager_employee = intake.manager_employee
    execution_roster = intake.execution_roster
    firm_coordinator = intake.firm_coordinator
    firm_coordination = intake.firm_coordination
    firm_runtime_coordination = intake.firm_runtime_coordination
    try:
        company_learning = cli.CompanyLearningService(company_store)
        employee_skill_learning = cli.EmployeeSkillPatchService(company_store)
        hiring_learning = cli.HiringRecommendationService(company_store)
        hire_observation = cli.HireObservationService(company_store)
        store = await resources.acquire_run_store(config.state_path)
        company_budget_authority = cli.SQLiteCompanyBudgetAuthority(
            store,
            cli.CompanyCostBudgetPolicy.from_mapping(
                company_store.company_cost_budget_policy()
            ),
        )
        if event_sink is not None:
            def forward_event(event) -> None:
                mapped = cli.product_event_from_run(event)
                if mapped is not None:
                    cli._emit_product_event(event_sink, mapped)

            store.subscribe(forward_event)
    except BaseException:
        await resources.close()
        raise
    # A direct conversation still uses the full bounded tool registry when the
    # user selected ask mode.  The router only skips graph compilation; it
    # must not silently deactivate browser, MCP, knowledge, or settings tools.
    capability_lane = route == InputRoute.COMPANY_GOAL or (
        route == InputRoute.CONVERSATION
        and (
            config.permission_mode == "ask"
            or cli._has_configured_external_read_capability(config)
        )
    )
    from dynamic_firm.application.goal_runtime_assembly import assemble_goal_tool_registry

    try:
        registry, session_recall_store = assemble_goal_tool_registry(
            state_path=config.state_path, workspace=config.workspace, config_path=config.config_path,
            goal=config.goal, external_skill_dirs=config.external_skill_dirs, permission_mode=config.permission_mode,
            capability_lane=capability_lane, session_key=session_key, manager_assignment=manager_assignment,
            company_store=company_store, run_store=store, job_id=job_id, remote_worker=config.remote_worker,
            container_workspace=config.container_workspace, executable_plugins=config.executable_plugins,
            home_assistant=config.home_assistant, workspace_id=cli.WORKSPACE_ID,
        )
    except BaseException:
        await resources.close()
        raise
    resources.set_session_recall_store(session_recall_store)
    shadow_change_catalog = None
    if shadow_coding_enabled:
        # The frozen ActionPolicy grants this one local write effect in Codex
        # ask mode.  Register its exact process-local catalog before the
        # capability audit, while retaining ownership of both catalog and
        # real-workspace apply inside the later shadow runtime service.
        shadow_change_catalog = cli.ChangeSetCatalog(
            {cli.WORKSPACE_ID: config.workspace}
        )
        registry.register(shadow_change_catalog.definition())
    # PLAN_FIRST reserves the Company ceiling before the Compiler call. Keep
    # ownership here until the managed Kernel returns so cancellation or an
    # exception anywhere between planning and dispatch cannot strand an ACTIVE
    # lease. FirmKernel re-admits the same durable lease idempotently and uses
    # the same forfeiture reason codes, so exception cleanup is idempotent.
    preflight_budget_lease = None
    capability_ports = GoalCapabilityPorts(
        lambda policy: cli.McpReadOnlyConnectorGroup(policy)
        if isinstance(policy, cli.McpReadOnlyConfigSet) else cli.McpReadOnlyConnector(policy),
        cli.McpActionConfigSet, cli.McpActionConnector, cli.McpActionConnectorGroup,
        cli.BrowserReadOnlyConnector, cli.ComputerUseConnector, cli.OpenAIMediaConnector,
        cli.WebReadConnector, cli.SearxngSearchConnector, cli.ProductEvent, cli.ProductEventType,
        cli._emit_product_event, cli.mcp_action_configs, cli.WORKSPACE_ID,
    )
    planning_ports = GoalPlanningPorts(
        cli.asyncio, cli.time, cli.CompilerDecision, cli.CompilerReason, cli.FirmCoordinatorAction,
        InputRoute, cli.JobLimits, cli.ManagerOutcomeDecision, cli.PlanningMode, cli.ProductEvent,
        cli.ProductEventType, cli.WORKSPACE_ID, cli.WORKSPACE_STRUCTURE_PROJECTION_REVISION,
        cli.WorkspaceProjectionError, cli.WorkspaceReadTools, cli.WorkspaceTools,
        cli._company_operating_brief, cli._company_request, cli._emit_product_event,
        cli._shadow_exclusions, cli._workspace_manifest, cli.apply_organization_evidence_gate,
        cli.assess_manager_outcomes, cli.assess_organization_outcomes,
        cli.build_manager_planning_brief, cli.direct_conversation_decision,
        cli.kernel_content_digest, cli.project_network_workflow_priors,
        cli.project_workspace_structure, cli.solo_first_decision,
        cli.workflow_context_fingerprint_v2,
    )
    completion_ports = GoalCompletionPorts(
        cli.ActiveJobInspector, cli.DirectCompanyExecutor, cli.EvidenceSource, cli.FirmKernel,
        cli.InitialCoordinationPolicy, InputRoute, cli.ProductEvent, cli.ProductEventType,
        cli.SQLiteActiveJobLedger, cli._action_policy, cli._emit_product_event,
        cli._has_configured_external_read_capability, cli.company_final_report,
        cli.episode_from_runtime_ledger, cli.organization_outcome_metrics,
        cli.staffing_demands_from_runtime_ledger, cli.product_event_from_assignment,
        cli.CompanyWorkMode,
    )
    try:
        await register_goal_capabilities(
            config=config,
            registry=registry,
            capability_lane=capability_lane,
            runtime_mcp_read_only=runtime_mcp_read_only,
            mcp_package_decision=mcp_package_decision,
            event_sink=event_sink,
            job_id=job_id,
            ports=capability_ports,
        )
        # Settings, tool registration, and ActionPolicy used to be assembled
        # by separate route-specific branches.  Validate the frozen surface
        # before any employee starts so a configuration regression is an
        # actionable operator diagnostic rather than an "unknown tool" after
        # the model has already spent a turn.
        await publish_capability_and_route_events(
            config=config,
            registry=registry,
            route=route,
            session_key=session_key,
            manager_assignment=manager_assignment,
            event_sink=event_sink,
            job_id=job_id,
            operating_decision=operating_decision,
            firm_coordination=firm_coordination,
            work_order=work_order,
            roster_snapshot=roster_snapshot,
            runtime_mcp_read_only=runtime_mcp_read_only,
            mcp_package_decision=mcp_package_decision,
            ports=completion_ports,
        )
        if config.mcp_read_only is not None and config.external_read_mode == "blocked":
            cli._emit_product_event(
                event_sink,
                cli.ProductEvent(
                    cli.ProductEventType.CAPABILITY_READY,
                    "External reads are blocked by the global Settings Center",
                    job_id=job_id,
                    data={"decision": "GLOBAL_EXTERNAL_READ_BLOCKED"},
                ),
            )
        elif config.mcp_read_only is not None and runtime_mcp_read_only is None:
            cli._emit_product_event(
                event_sink,
                cli.ProductEvent(
                    cli.ProductEventType.CAPABILITY_READY,
                    "External read excluded by the pinned MCP policy package",
                    job_id=job_id,
                    data={"decision": mcp_package_decision},
                ),
            )
        execution_profile = cli._compiler_execution_profile(
            config,
            operating_decision,
            shadow_available=shadow_coding_enabled,
        )
        shadow_selected = execution_profile == cli.CompilerExecutionProfile.SHADOW_CODING
        workflow_context = ""
        workflow_priors = ()
        workspace_identity_status = "NOT_APPLICABLE"
        workspace_identity_failure_code = ""
        compiler_request = cli.CompilerRequest(
            request_id=request_id,
            goal=config.goal,
            workspace_manifest=(),
            available_capabilities=tuple(
                sorted(
                    {
                        capability
                        for employee in execution_roster
                        if employee.active and not employee.temporary
                        for capability in employee.capabilities
                    }
                )
            ),
            # When a persistent Manager exists, the one bounded structured
            # planning call is explicitly its semantic staffing proposal. The
            # Compiler remains only the schema/parser/validator adapter; it
            # does not receive a second model call or a new authority lane.
            model_profile=(
                manager_employee.model_profile
                if manager_assignment is not None and manager_employee is not None
                else config.model
            ),
            execution_profile=execution_profile,
            max_tasks=6,
            max_temporary_roles=2,
            max_total_model_calls=config.run_limits.max_model_calls,
            max_wall_time_ms=max(
                1,
                int(
                    config.run_limits.max_wall_time_ms
                    - (cli.time.monotonic() - started) * 1000
                ),
            ),
            requires_independent_review=(
                operating_decision.requires_independent_review
            ),
            execution_replica_preference=(
                operating_decision.execution_replica_preference
            ),
            suggested_execution_replica_strategy=(
                operating_decision.suggested_execution_replica_strategy
            ),
            planning_owner=(
                cli.PlanningOwner(
                    employee_id=manager_assignment.manager_employee_id,
                    role=manager_employee.role,
                    assignment_digest=manager_assignment.content_digest,
                    session_key=manager_assignment.session_key,
                )
                if manager_assignment is not None and manager_employee is not None
                else None
            ),
        )
        frozen_preplanned_blueprint_binding = None
        if frozen_route_composition is not None:
            preplanned_resolution = firm_coordinator.resolve_initial_blueprint(
                work_order,
                firm_coordination,
                compiler_request,
                limits=cli.JobLimits(
                    max_tasks=6,
                    max_concurrency=3,
                    max_graph_patches=1,
                    max_temporary_roles=2,
                    max_total_model_calls=config.run_limits.max_model_calls,
                    max_total_tool_calls=config.run_limits.max_tool_calls,
                    max_total_cost_usd=config.run_limits.max_cost_usd,
                    max_wall_time_ms=config.run_limits.max_wall_time_ms,
                ),
                pin_slot="default",
                constraints=graph_blueprint_registry.constraints("default"),
            )
            frozen_route_composition.require_preplanned_blueprint(
                preplanned_resolution.binding
            )
            # Carry the already validated immutable binding through planning.
            # ``prepare_goal_plan`` re-reads the local Blueprint registry as
            # part of its ordinary setup; frozen dispatch must prove that
            # second read is still this exact binding rather than allowing a
            # registry/pin change to turn into a planning-provider fallback.
            frozen_preplanned_blueprint_binding = preplanned_resolution.binding
        selected_blueprint_binding = frozen_preplanned_blueprint_binding
        (decision, compiler_request, operating_decision, manager_assignment, manager_employee, execution_roster, workflow_context, workflow_priors, workspace_identity_status, workspace_identity_failure_code, evolution_artifact_resolution, preflight_budget_lease, selected_blueprint_binding,) = await prepare_goal_plan(
            config=config,
            route=route,
            compiler_request=compiler_request,
            execution_profile=execution_profile,
            shadow_coding_enabled=shadow_coding_enabled,
            started=started,
            event_sink=event_sink,
            job_id=job_id,
            company_store=company_store,
            operating_decision=operating_decision,
            manager_assignment=manager_assignment,
            manager_employee=manager_employee,
            execution_roster=execution_roster,
            roster=roster,
            company_learning=company_learning,
            evolution_artifact_resolution=evolution_artifact_resolution,
            employee_skill_learning=employee_skill_learning,
            company_snapshot=company_snapshot,
            work_order=work_order,
            firm_coordinator=firm_coordinator,
            firm_coordination=firm_coordination,
            graph_blueprint_registry=graph_blueprint_registry,
            company_budget_authority=company_budget_authority,
            request_id=request_id,
            prior_context=prior_context,
            task_evidence=task_evidence,
            execution_origin=execution_origin,
            roster_snapshot=roster_snapshot,
            runtime_mcp_read_only=runtime_mcp_read_only,
            session_key=session_key,
            executive_manager=executive_manager,
            preflight_budget_lease=preflight_budget_lease,
            selected_blueprint_binding=selected_blueprint_binding,
            frozen_preplanned_blueprint_binding=frozen_preplanned_blueprint_binding,
            workflow_context=workflow_context,
            workflow_priors=workflow_priors,
            workspace_identity_status=workspace_identity_status,
            workspace_identity_failure_code=workspace_identity_failure_code,
            ports=planning_ports,
        )
        if frozen_route_composition is not None:
            frozen_route_composition.require_preplanned_blueprint(
                selected_blueprint_binding
            )
        employee_service = await resources.create_employee_service(
            store=store,
            provider=provider,
            registry=registry,
            approval_port=approval_port,
            company_coordination=config.company_coordination,
            python_executable=config.runtime_python,
            **frozen_route_runtime_kwargs,
        )
        try:
            if shadow_coding_enabled:
                assert coding_worker is not None
                assert shadow_change_catalog is not None
                shadow_service = cli.ShadowCodingEmployeeRuntimeService(
                    store=store,
                    worker=coding_worker,
                    shadow=cli.ShadowWorkspaceService(excluded_paths=cli._shadow_exclusions(config)),
                    catalog=shadow_change_catalog,
                    registry=registry,
                    approval_port=approval_port,
                )
                service = cli.RoutedEmployeeExecutionService(
                    native=employee_service,
                    shadow_coding=shadow_service,
                    host_direct_only=(
                        config.workspace.resolve() == cli.Path.home().resolve()
                    ),
                )
            else:
                service = employee_service
        except BaseException:
            await resources.close()
            raise
        resources.set_employee_service(service)
        await publish_plan_decision(
            decision=decision,
            compiler_request=compiler_request,
            operating_decision=operating_decision,
            route=route,
            event_sink=event_sink,
            job_id=job_id,
            ports=completion_ports,
        )
        local_employee_skill_snapshots = (
            employee_skill_learning.runtime_snapshots(
                tuple(item.employee_id for item in roster),
                context_key=workflow_context,
                query=config.goal,
            )
            if workflow_context
            else {}
        )
        external_skill_load = cli.load_external_skill_snapshots(
            config.external_skill_dirs,
            employee_ids=tuple(item.employee_id for item in roster),
            query=config.goal,
        )
        # Preserve the familiar progressive-disclosure shape of an external
        # skill package.  The registry receives only the query-selected
        # package set, not every configured directory, and it is still a
        # parent-owned READ capability rather than a worker filesystem mount.
        employee_skill_snapshots = cli.merge_employee_skill_snapshots(
            local_employee_skill_snapshots,
            external_skill_load.snapshots,
        )
        employee_skill_snapshots = cli.merge_employee_skill_snapshots(
            employee_skill_snapshots,
            evolution_artifact_resolution.employee_skills,
        )
        # Job-local specialists may reuse only the already selected,
        # user-configured external instructions. They never receive a
        # persistent employee's private procedure or a network-projected
        # skill, and no Skill Patch is created by this projection.
        job_local_skill_snapshots = tuple(
            dict.fromkeys(
                item
                for snapshots in external_skill_load.snapshots.values()
                for item in snapshots
            )
        )[:3]
        if config.external_skill_dirs:
            cli._emit_product_event(
                event_sink,
                cli.ProductEvent(
                    cli.ProductEventType.CAPABILITY_READY,
                    f"External skills ready · {external_skill_load.discovered_count} discovered",
                    job_id=job_id,
                    data={
                        "discovered_count": external_skill_load.discovered_count,
                        "skipped_count": external_skill_load.skipped_count,
                        "trust": "user-configured-read-only",
                    },
                ),
            )

        # External skill discovery and authority binding are part of the same
        # Company Job wall clock.  Recompute at the last possible point before
        # constructing the runtime request; a stale pre-scan slice must never
        # dispatch an Employee after the user-visible deadline has expired.
        remaining_wall_time_ms = int(
            config.run_limits.max_wall_time_ms - (cli.time.monotonic() - started) * 1000
        )
        manager_delegation = (
            cli.ManagerDelegation.from_proposal(manager_assignment, decision.proposal)
            if manager_assignment is not None
            and manager_assignment.mode.value == "DELEGATE"
            else None
        )
        wall_time_exhausted_before_dispatch = remaining_wall_time_ms <= 0
        dispatch_planning_reason = (
            "JOB_WALL_TIME_EXHAUSTED_BEFORE_DISPATCH"
            if wall_time_exhausted_before_dispatch
            and decision.reason != cli.CompilerReason.COMPILER_WALL_TIME_EXHAUSTED
            else (
                operating_decision.reason.value
                if route == InputRoute.CONVERSATION
                else decision.reason.value
            )
        )
        request = cli._company_request(
            cli.replace(config, mcp_read_only=runtime_mcp_read_only),
            roster=execution_roster,
            request_id=request_id,
            job_id=job_id,
            decision=decision,
            remaining_wall_time_ms=max(1, remaining_wall_time_ms),
            prior_context=prior_context,
            task_evidence=task_evidence,
            execution_origin=execution_origin,
            route=route,
            employee_skill_snapshots=employee_skill_snapshots,
            job_local_skill_snapshots=job_local_skill_snapshots,
            company_revision=company_snapshot.revision,
            roster_revision=roster_snapshot.revision,
            playbook_revision=company_store.playbook().revision,
            workflow_context_fingerprint=workflow_context,
            workspace_identity_status=workspace_identity_status,
            workspace_identity_failure_code=workspace_identity_failure_code,
            session_key=session_key,
            company_operating_brief=cli._company_operating_brief(company_snapshot),
            company_work_mode=operating_decision.work_mode.value,
            coordination_policy=operating_decision.coordination_policy.value,
            requested_effect=operating_decision.requested_effect.value,
            operating_reason=operating_decision.reason.value,
            planning_mode=(
                "DIRECT" if route == InputRoute.CONVERSATION else decision.mode.value
            ),
            planning_reason=dispatch_planning_reason,
            compiler_usage=decision.usage,
            compiler_provider_request_id=decision.provider_request_id,
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            work_order_authority_digest=(
                work_order.authority_snapshot.identity_digest
            ),
            graph_blueprint_id=(
                selected_blueprint_binding.blueprint_ref.blueprint_id
                if selected_blueprint_binding is not None
                else ""
            ),
            graph_blueprint_version=(
                selected_blueprint_binding.blueprint_ref.version
                if selected_blueprint_binding is not None
                else 0
            ),
            graph_blueprint_digest=(
                selected_blueprint_binding.blueprint_ref.content_digest
                if selected_blueprint_binding is not None
                else ""
            ),
            graph_mutation_policy=(
                selected_blueprint_binding.constraints.mutation_policy.value
                if selected_blueprint_binding is not None
                else "BOUNDED_AUTO"
            ),
            graph_constraints_digest=(
                cli.kernel_content_digest(selected_blueprint_binding.constraints)
                if selected_blueprint_binding is not None
                else ""
            ),
            graph_pinned_employee_ids=(
                selected_blueprint_binding.constraints.pinned_employee_ids
                if selected_blueprint_binding is not None
                else ()
            ),
            graph_excluded_employee_ids=(
                selected_blueprint_binding.constraints.excluded_employee_ids
                if selected_blueprint_binding is not None
                else ()
            ),
            graph_require_independent_review=(
                selected_blueprint_binding.constraints.require_independent_review
                if selected_blueprint_binding is not None
                else False
            ),
            graph_max_concurrency=(
                selected_blueprint_binding.constraints.max_concurrency
                if selected_blueprint_binding is not None
                else None
            ),
            graph_max_cost_usd=(
                selected_blueprint_binding.constraints.max_cost_usd
                if selected_blueprint_binding is not None
                else None
            ),
            graph_max_wall_time_ms=(
                selected_blueprint_binding.constraints.max_wall_time_ms
                if selected_blueprint_binding is not None
                else None
            ),
            manager_employee_id=(
                manager_assignment.manager_employee_id
                if manager_assignment is not None
                else ""
            ),
            manager_assignment_digest=(
                manager_assignment.content_digest
                if manager_assignment is not None
                else ""
            ),
            manager_session_key=(
                manager_assignment.session_key
                if manager_assignment is not None
                else ""
            ),
            manager_employee=manager_employee,
            manager_delegation_payload=(
                manager_delegation.canonical_payload()
                if manager_delegation is not None
                else None
            ),
            manager_delegation_digest=(
                manager_delegation.content_digest
                if manager_delegation is not None
                else ""
            ),
            manager_tools_enabled=executive_manager is not None,
        )
        firm_admission = cli.FirmAdmissionController().admit(
            work_order=work_order,
            proposal=decision.proposal,
            roster=execution_roster,
            limits=request.job_limits,
            constraints=(
                selected_blueprint_binding.constraints
                if selected_blueprint_binding is not None
                else None
            ),
        )
        from dynamic_firm.application.continuation_runtime_preflight import bind_company_run_request_runtime
        request = bind_company_run_request_runtime(
            request, firm_admission.content_digest, cli._provider_config(config),
            registry, config.company_coordination)
        frozen_route_assignment_admission = (
            None
            if frozen_route_composition is None
            else frozen_route_composition.assignment_admission_for(
                request,
                state_path=config.state_path,
            )
        )
        frozen_route_task_action_policy_override = (
            None
            if frozen_route_composition is None
            else frozen_route_composition.kernel_task_action_policy_override()
        )
        # Keep the complete immutable request only in the user-local Work
        # Order authority. ACTIVE JOB receives its frozen digest, while a
        # future ADR-0198 continuation needs this exact typed envelope to
        # prove it is not reconstructing a request from audit metadata.
        portfolio_path = config.state_path.with_name(
            f"{config.state_path.stem}.work-orders.db"
        )
        with WorkOrderPortfolioStore(portfolio_path) as work_order_authority:
            work_order_authority.retain_continuation_request(request)
            if frozen_route_composition is not None:
                bundle = frozen_route_composition.continuation_bundle_for(
                    request,
                    state_path=config.state_path,
                )
                work_order_authority.retain_frozen_route_continuation_bundle(
                    job_id=request.job_id,
                    request_id=request.request_id,
                    bundle_json=bundle.canonical_json(),
                    bundle_digest=bundle.digest,
                )
        cli._emit_product_event(
            event_sink,
            cli.ProductEvent(
                cli.ProductEventType.FIRM_ADMISSION,
                (
                    "Firm admitted the minimum execution shape"
                    if firm_admission.admitted
                    else f"Firm denied the proposed execution shape · {firm_admission.reason}"
                ),
                job_id=job_id,
                data={
                    "admission_id": firm_admission.admission_id,
                    "admission_digest": firm_admission.content_digest,
                    "admitted": firm_admission.admitted,
                    "reason": firm_admission.reason,
                    "initial_company_work_mode": firm_admission.initial_work_mode,
                    "effective_company_work_mode": firm_admission.effective_work_mode,
                    "task_count": firm_admission.task_count,
                    "dependency_width": firm_admission.dependency_width,
                    "concurrency_ceiling": firm_admission.concurrency_ceiling,
                    "persistent_employee_count": firm_admission.persistent_employee_count,
                    "temporary_role_demand": firm_admission.temporary_role_demand,
                    "distinct_staffing_profile_count": (
                        firm_admission.distinct_staffing_profile_count
                    ),
                    "staffing_difference_dimensions": (
                        firm_admission.staffing_difference_dimensions
                    ),
                    "uncovered_task_ids": firm_admission.uncovered_task_ids,
                    # The TUI/GUI-facing event exposes only frozen delegation
                    # metadata. It never exposes another Employee's prompt,
                    # transcript, hidden reasoning, credential, or tool payload.
                    "manager_delegation_digest": (
                        manager_delegation.content_digest
                        if manager_delegation is not None
                        else ""
                    ),
                    "manager_delegation_task_count": (
                        len(manager_delegation.tasks)
                        if manager_delegation is not None
                        else 0
                    ),
                    "manager_context_lanes": (
                        tuple(
                            {
                                "task_id": item.task_id,
                                "context_lane": item.context_lane.value,
                                "deliverable_kind": item.deliverable_kind,
                                "validator_ids": item.validator_ids,
                            }
                            for item in manager_delegation.tasks
                        )
                        if manager_delegation is not None
                        else ()
                    ),
                    "applied": firm_admission.admitted,
                },
            ),
        )
        if not firm_admission.admitted:
            raise RuntimeError(
                "Firm admission denied the compiled plan before Employee dispatch: "
                f"{firm_admission.reason}"
            )
        # Re-bind against the policy actually projected into the employee
        # request. This catches drift between Front Door construction and tool
        # registration before either DIRECT or managed execution starts.
        cli.verify_work_order_binding(
            work_order,
            authority_snapshot=cli.replace(
                authority_snapshot,
                action_policy_digest=cli.kernel_content_digest(request.action_policy),
            ),
            budget_snapshot=work_order_budget_snapshot,
        )

        replanner, assignment_sink = prepare_admitted_execution(
            firm_coordinator=firm_coordinator,
            work_order=work_order,
            firm_runtime_coordination=firm_runtime_coordination,
            route=route,
            event_sink=event_sink,
            workflow_priors=workflow_priors,
            manager_assignment=manager_assignment,
            job_id=job_id,
            ports=completion_ports,
        )
        completed, replanner, preflight_budget_lease = await execute_admitted_goal(
            firm_coordinator=firm_coordinator,
            work_order=work_order,
            firm_runtime_coordination=firm_runtime_coordination,
            route=route,
            event_sink=event_sink,
            workflow_priors=workflow_priors,
            manager_assignment=manager_assignment,
            replanner=replanner,
            assignment_sink=assignment_sink,
            service=service,
            company_budget_authority=company_budget_authority,
            request=request,
            approval_port=approval_port,
            assignment_admission=frozen_route_assignment_admission,
            task_action_policy_override=frozen_route_task_action_policy_override,
            evolution_artifact_pins=evolution_artifact_pins,
            evolution_artifact_resolution=evolution_artifact_resolution,
            config=config,
            store=store,
            job_id=job_id,
            ports=completion_ports,
        )
        await publish_goal_finished(
            completed=completed,
            event_sink=event_sink,
            job_id=job_id,
            ports=completion_ports,
        )
        await record_goal_learning(
            route=route,
            workflow_context=workflow_context,
            store=store,
            job_id=job_id,
            completed=completed,
            execution_profile=execution_profile,
            request=request,
            decision=decision,
            replanner=replanner,
            workflow_priors=workflow_priors,
            company_store=company_store,
            company_learning=company_learning,
            roster_snapshot=roster_snapshot,
            hiring_learning=hiring_learning,
            hire_observation=hire_observation,
            employee_skill_learning=employee_skill_learning,
            event_sink=event_sink,
            ports=completion_ports,
        )
        return completed
    except BaseException as error:
        if preflight_budget_lease is not None:
            reason = (
                "MANAGED_JOB_CANCELLED"
                if isinstance(error, cli.asyncio.CancelledError)
                else "MANAGED_JOB_ABORTED"
            )
            try:
                company_budget_authority.forfeit_job(
                    preflight_budget_lease,
                    reason=reason,
                )
            except BaseException as finalization_error:
                error.add_note(
                    "Preflight Company budget lease forfeiture failed; the "
                    "reservation remains fail-closed. "
                    f"{type(finalization_error).__name__}: {finalization_error}"
                )
        raise
    finally:
        await resources.close()
