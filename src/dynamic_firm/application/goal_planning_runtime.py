"""Planning stage for a frozen Company goal."""

from __future__ import annotations

from dataclasses import replace
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GoalPlanningPorts:
    """Named collaborators required to prepare a frozen Company plan."""

    asyncio: Any
    time: Any
    compiler_decision: Any
    compiler_reason: Any
    firm_coordinator_action: Any
    input_route: Any
    job_limits: Any
    manager_outcome_decision: Any
    planning_mode: Any
    product_event: Any
    product_event_type: Any
    workspace_id: str
    workspace_structure_projection_revision: str
    workspace_projection_error: type[Exception]
    workspace_read_tools: Any
    workspace_tools: Any
    company_operating_brief: Any
    company_request: Any
    emit_product_event: Any
    shadow_exclusions: Any
    workspace_manifest: Any
    apply_organization_evidence_gate: Any
    assess_manager_outcomes: Any
    assess_organization_outcomes: Any
    build_manager_planning_brief: Any
    direct_conversation_decision: Any
    kernel_content_digest: Any
    project_network_workflow_priors: Any
    project_workspace_structure: Any
    solo_first_decision: Any
    workflow_context_fingerprint: Any


async def prepare_goal_plan(
    *,
    config,
    route,
    compiler_request,
    execution_profile,
    shadow_coding_enabled,
    started,
    event_sink,
    job_id,
    company_store,
    operating_decision,
    manager_assignment,
    manager_employee,
    execution_roster,
    roster,
    company_learning,
    evolution_artifact_resolution,
    employee_skill_learning,
    company_snapshot,
    work_order,
    firm_coordinator,
    firm_coordination,
    graph_blueprint_registry,
    company_budget_authority,
    request_id,
    prior_context,
    task_evidence,
    execution_origin,
    roster_snapshot,
    runtime_mcp_read_only,
    session_key,
    executive_manager,
    preflight_budget_lease,
    selected_blueprint_binding,
    frozen_preplanned_blueprint_binding,
    workflow_context,
    workflow_priors,
    workspace_identity_status,
    workspace_identity_failure_code,
    ports: GoalPlanningPorts,
):
    asyncio = ports.asyncio
    time = ports.time
    CompilerDecision = ports.compiler_decision
    CompilerReason = ports.compiler_reason
    FirmCoordinatorAction = ports.firm_coordinator_action
    InputRoute = ports.input_route
    JobLimits = ports.job_limits
    ManagerOutcomeDecision = ports.manager_outcome_decision
    PlanningMode = ports.planning_mode
    ProductEvent = ports.product_event
    ProductEventType = ports.product_event_type
    WORKSPACE_ID = ports.workspace_id
    WORKSPACE_STRUCTURE_PROJECTION_REVISION = ports.workspace_structure_projection_revision
    WorkspaceProjectionError = ports.workspace_projection_error
    WorkspaceReadTools = ports.workspace_read_tools
    WorkspaceTools = ports.workspace_tools
    _company_operating_brief = ports.company_operating_brief
    _company_request = ports.company_request
    _emit_product_event = ports.emit_product_event
    _shadow_exclusions = ports.shadow_exclusions
    _workspace_manifest = ports.workspace_manifest
    apply_organization_evidence_gate = ports.apply_organization_evidence_gate
    assess_manager_outcomes = ports.assess_manager_outcomes
    assess_organization_outcomes = ports.assess_organization_outcomes
    build_manager_planning_brief = ports.build_manager_planning_brief
    direct_conversation_decision = ports.direct_conversation_decision
    kernel_content_digest = ports.kernel_content_digest
    project_network_workflow_priors = ports.project_network_workflow_priors
    project_workspace_structure = ports.project_workspace_structure
    solo_first_decision = ports.solo_first_decision
    workflow_context_fingerprint_v2 = ports.workflow_context_fingerprint
    if route == InputRoute.CONVERSATION:
        decision = direct_conversation_decision(compiler_request)
    else:
        workspace_identity_status = "READY"
        try:
            projection = await asyncio.to_thread(
                project_workspace_structure,
                config.workspace,
                execution_profile.value,
                excluded_paths=_shadow_exclusions(config),
            )
        except WorkspaceProjectionError as exc:
            workspace_identity_status = "FAILED"
            workspace_identity_failure_code = exc.code.value
        except Exception:
            workspace_identity_status = "FAILED"
            workspace_identity_failure_code = "INTERNAL_ERROR"
        else:
            workflow_context = workflow_context_fingerprint_v2(projection)
        _emit_product_event(
            event_sink,
            ProductEvent(
                ProductEventType.WORKSPACE_IDENTITY,
                (
                    "Workspace identity ready"
                    if workspace_identity_status == "READY"
                    else (
                        "Workspace identity unavailable · "
                        f"{workspace_identity_failure_code}"
                    )
                ),
                job_id=job_id,
                data={
                    "status": workspace_identity_status,
                    "revision": WORKSPACE_STRUCTURE_PROJECTION_REVISION,
                    "failure_code": workspace_identity_failure_code,
                    "truncated": (
                        projection.truncated
                        if workspace_identity_status == "READY"
                        else False
                    ),
                },
            )
        )
        workspace_tools = (
            WorkspaceTools({WORKSPACE_ID: config.workspace})
            if config.permission_mode == "ask"
            else WorkspaceReadTools({WORKSPACE_ID: config.workspace})
        )
        try:
            manifest = await _workspace_manifest(workspace_tools)
        except Exception:
            manifest = ()
        compiler_request = replace(
            compiler_request,
            workspace_manifest=manifest,
            workflow_context_fingerprint=workflow_context,
        )
        # A semantic coordination opportunity is not sufficient to spend
        # a Company Job on a team.  Reuse only outcome-qualified topology
        # from this exact structural workflow context.  Explicit user
        # required independent review remains intact; it is an instruction
        # rather than a performance heuristic.
        organization_assessment = assess_organization_outcomes(
            company_store.list_episodes(limit=256),
            context_fingerprint=workflow_context,
        )
        admitted_operating_decision = apply_organization_evidence_gate(
            operating_decision,
            organization_assessment,
        )
        if admitted_operating_decision != operating_decision:
            operating_decision = admitted_operating_decision
            compiler_request = replace(
                compiler_request,
                requires_independent_review=(
                    operating_decision.requires_independent_review
                ),
                execution_replica_preference=(
                    operating_decision.execution_replica_preference
                ),
                suggested_execution_replica_strategy=(
                    operating_decision.suggested_execution_replica_strategy
                ),
            )
        # A Manager is a proven staffing advisor, not a default wrapper
        # around every Company Job.  The Manager only participates after
        # the same exact workflow context has a positive, baselined
        # production cohort.  Existing Manager identity and state remain
        # untouched when this gate denies the current Job.
        manager_evidence_decision = "UNAVAILABLE"
        if manager_assignment is not None and manager_employee is not None:
            manager_assessments = assess_manager_outcomes(
                company_store.list_episodes(limit=256),
                manager_employee_id=manager_assignment.manager_employee_id,
                context_fingerprint=workflow_context,
            )
            manager_evidence_decision = (
                manager_assessments[0].decision.value
                if manager_assessments
                else ManagerOutcomeDecision.INSUFFICIENT_EVIDENCE.value
            )
            if not manager_assessments or (
                manager_assessments[0].decision
                is not ManagerOutcomeDecision.KEEP_UNDER_OBSERVATION
            ):
                manager_employee_id = manager_employee.employee_id
                manager_assignment = None
                manager_employee = None
                execution_roster = tuple(
                    employee
                    for employee in roster
                    if employee.employee_id != manager_employee_id
                )
                compiler_request = replace(
                    compiler_request,
                    model_profile=config.model,
                    planning_owner=None,
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
                )
        # ``INPUT_ROUTED`` retains the immutable candidate and the later
        # plan/admission events retain the admitted result.  Do not add a
        # second narrative event here: it would make one user request look
        # like a second organization action in compact terminal surfaces.
        if workflow_context:
            try:
                workflow_priors = company_learning.compiler_priors(
                    execution_profile,
                    context_fingerprint=workflow_context,
                )
            except Exception:
                workflow_priors = ()
        network_workflow_priors, network_workflow_effects = (
            project_network_workflow_priors(
                evolution_artifact_resolution,
                execution_profile=execution_profile,
                available_capabilities=compiler_request.available_capabilities,
            )
        )
        remaining_prior_slots = max(0, 8 - len(workflow_priors))
        admitted_network_priors = network_workflow_priors[:remaining_prior_slots]
        if len(admitted_network_priors) != len(network_workflow_priors):
            network_workflow_effects = (
                *network_workflow_effects,
                {
                    "kind": "WORKFLOW_PLAYBOOK",
                    "decision": "IGNORED_WORKFLOW_ADAPTER_COMPILER_PRIOR_LIMIT",
                },
            )
        evolution_artifact_resolution = replace(
            evolution_artifact_resolution,
            effects=(
                *evolution_artifact_resolution.effects,
                *network_workflow_effects,
            ),
        )
        workflow_priors = (*workflow_priors, *admitted_network_priors)
        compiler_request = replace(
            compiler_request,
            workflow_priors=workflow_priors,
        )
        if (
            manager_assignment is not None
            and manager_employee is not None
        ):
            manager_skill_snapshots = (
                employee_skill_learning.runtime_snapshots(
                    (manager_employee.employee_id,),
                    context_key=workflow_context,
                    query=config.goal,
                ).get(manager_employee.employee_id, ())
                if workflow_context
                else ()
            )
            compiler_request = replace(
                compiler_request,
                manager_planning_brief=build_manager_planning_brief(
                    company_revision=company_snapshot.revision,
                    company_purpose=company_snapshot.purpose,
                    work_order_constraints=work_order.constraints,
                    manager_skill_snapshots=manager_skill_snapshots,
                    recent_episodes=company_store.list_episodes(limit=24),
                    workflow_context_fingerprint=workflow_context,
                    task_evidence=task_evidence,
                ),
            )
        blueprint_resolution = firm_coordinator.resolve_initial_blueprint(
            work_order,
            firm_coordination,
            compiler_request,
            limits=JobLimits(
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
        if frozen_preplanned_blueprint_binding is not None:
            # The caller validated this binding before entering the planning
            # stage.  The registry is intentionally re-read here for normal
            # Company work, but a frozen route handoff may not silently turn
            # a registry/pin drift into a model-planning fallback.  Continue
            # only when the second resolution still produces the exact
            # immutable binding; retain the first validated value so later
            # request construction cannot consume a mutable re-read.
            if blueprint_resolution.binding != frozen_preplanned_blueprint_binding:
                raise ValueError(
                    "Frozen route composition Blueprint changed before planning"
                )
            selected_blueprint_binding = frozen_preplanned_blueprint_binding
        elif blueprint_resolution.hit:
            selected_blueprint_binding = blueprint_resolution.binding
        if (
            firm_coordination.action
            == FirmCoordinatorAction.REQUEST_PLAN_PROPOSAL
        ):
            # Reserve the full Company Job ceiling before a planning model
            # call.  The Kernel repeats this admission idempotently and
            # settles one lease with compiler + employee usage, so a
            # paused/exhausted Company never spends another compiler call.
            preflight_decision = (
                CompilerDecision(
                    proposal=selected_blueprint_binding.proposal,
                    mode=PlanningMode.BLUEPRINT,
                    reason=CompilerReason.BLUEPRINT_REUSED,
                    rationale=(
                        "A compatible exact local Graph Blueprint was bound to "
                        "this Work Order before any planning provider call."
                    ),
                )
                if selected_blueprint_binding is not None
                else solo_first_decision(compiler_request)
            )
            preflight_request = _company_request(
                replace(config, mcp_read_only=runtime_mcp_read_only),
                roster=execution_roster,
                request_id=request_id,
                job_id=job_id,
                decision=preflight_decision,
                remaining_wall_time_ms=max(
                    1,
                    int(
                        config.run_limits.max_wall_time_ms
                        - (time.monotonic() - started) * 1000
                    ),
                ),
                prior_context=prior_context,
                task_evidence=task_evidence,
                execution_origin=execution_origin,
                route=route,
                company_revision=company_snapshot.revision,
                roster_revision=roster_snapshot.revision,
                playbook_revision=company_store.playbook().revision,
                workflow_context_fingerprint=workflow_context,
                workspace_identity_status=workspace_identity_status,
                workspace_identity_failure_code=workspace_identity_failure_code,
                session_key=session_key,
                company_operating_brief=_company_operating_brief(company_snapshot),
                company_work_mode=operating_decision.work_mode.value,
                coordination_policy=operating_decision.coordination_policy.value,
                requested_effect=operating_decision.requested_effect.value,
                operating_reason=operating_decision.reason.value,
                planning_mode=preflight_decision.mode.value,
                planning_reason=preflight_decision.reason.value,
                compiler_usage=preflight_decision.usage,
                compiler_provider_request_id=(
                    preflight_decision.provider_request_id
                ),
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
                    kernel_content_digest(selected_blueprint_binding.constraints)
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
                # The frozen Work Order records the Company Manager read
                # catalog as an available bounded capability whenever the
                # ROSTER contains that Manager. Runtime evidence may deny
                # this Job's Manager assignment, but it must not mutate
                # the action-policy digest after admission.
                manager_tools_enabled=executive_manager is not None,
            )
            compiler_admission = company_budget_authority.admit_job(
                preflight_request
            )
            if compiler_admission.allowed:
                preflight_budget_lease = compiler_admission.lease
                _emit_product_event(
                    event_sink,
                    ProductEvent(
                        ProductEventType.COMPILER_STARTED,
                        (
                            "Company is validating dependencies and "
                            "performance-first staffing"
                        ),
                        job_id=job_id,
                        data={
                            "coordination_policy": operating_decision.coordination_policy.value,
                            "requested_effect": operating_decision.requested_effect.value,
                            "reason": operating_decision.reason.value,
                            "execution_replica_preference": (
                                operating_decision.execution_replica_preference.value
                            ),
                            "suggested_execution_replica_strategy": (
                                None
                                if operating_decision.suggested_execution_replica_strategy
                                is None
                                else operating_decision.suggested_execution_replica_strategy.value
                            ),
                        },
                    ),
                )
                # Workspace projection and Company-budget admission are
                # part of the same Job wall clock. Refresh the planning
                # slice immediately before the provider call so the
                # Compiler cannot borrow time from Employee execution.
                if selected_blueprint_binding is not None:
                    decision = preflight_decision
                else:
                    remaining_planning_wall_time_ms = int(
                        config.run_limits.max_wall_time_ms
                        - (time.monotonic() - started) * 1000
                    )
                    if remaining_planning_wall_time_ms <= 0:
                        decision = replace(
                            preflight_decision,
                            mode=PlanningMode.SOLO_FALLBACK,
                            reason=(
                                CompilerReason.COMPILER_WALL_TIME_EXHAUSTED
                            ),
                            rationale=(
                                "The Job wall-time budget expired before planning; "
                                "no Employee may be dispatched."
                            ),
                        )
                    else:
                        compiler_request = replace(
                            compiler_request,
                            max_wall_time_ms=remaining_planning_wall_time_ms,
                        )
                        decision = await firm_coordinator.propose_initial_plan(
                            work_order,
                            firm_coordination,
                            compiler_request,
                        )
                        if (
                            time.monotonic() - started
                        ) * 1000 >= config.run_limits.max_wall_time_ms:
                            decision = replace(
                                decision,
                                mode=PlanningMode.SOLO_FALLBACK,
                                reason=(
                                    CompilerReason.COMPILER_WALL_TIME_EXHAUSTED
                                ),
                                rationale=(
                                    "Planning reached the Job wall-time ceiling; "
                                    "no Employee may be dispatched."
                                ),
                            )
            else:
                # The ordinary Kernel path writes the durable terminal
                # denial; this branch only guarantees that no planning
                # provider is called before that authority decision.
                decision = preflight_decision
        else:
            decision = solo_first_decision(compiler_request)
    return (
        decision,
        compiler_request,
        operating_decision,
        manager_assignment,
        manager_employee,
        execution_roster,
        workflow_context,
        workflow_priors,
        workspace_identity_status,
        workspace_identity_failure_code,
        evolution_artifact_resolution,
        preflight_budget_lease,
        selected_blueprint_binding,
    )
