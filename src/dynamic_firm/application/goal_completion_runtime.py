"""Final audit, execution, and learning stages for a Company job."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GoalCompletionPorts:
    """Execution and audit collaborators owned by the goal composition root."""

    active_job_inspector: Any
    direct_company_executor: Any
    evidence_source: Any
    firm_kernel: Any
    initial_coordination_policy: Any
    input_route: Any
    product_event: Any
    product_event_type: Any
    sqlite_active_job_ledger: Any
    action_policy: Any
    emit_product_event: Any
    has_configured_external_read_capability: Any
    company_final_report: Any
    episode_from_runtime_ledger: Any
    organization_outcome_metrics: Any
    staffing_demands_from_runtime_ledger: Any
    product_event_from_assignment: Any
    company_work_mode: Any


async def publish_goal_finished(*, completed, event_sink, job_id, ports: GoalCompletionPorts):
    company_final_report = ports.company_final_report
    _emit_product_event = ports.emit_product_event
    ProductEvent = ports.product_event
    ProductEventType = ports.product_event_type
    final_report = company_final_report(completed)
    _emit_product_event(
        event_sink,
        ProductEvent(
            ProductEventType.JOB_FINISHED,
            f"Company job {completed.status.value.lower().replace('_', ' ')}",
            job_id=job_id,
            data={
                "status": completed.status.value,
                "company_work_mode": completed.company_work_mode,
                "unique_employee_count": completed.metrics.unique_employee_count,
                "temporary_role_count": completed.metrics.temporary_role_count,
                "maximum_parallelism": completed.metrics.maximum_parallelism,
                "graph_patch_count": completed.metrics.graph_patch_count,
                "task_mutation_count": completed.metrics.task_mutation_count,
                "organization_admission_count": completed.metrics.organization_admission_count,
                "manager_integration_count": completed.metrics.manager_integration_count,
                "company_report_mode": final_report.mode.value,
                "reporting_owner_employee_id": final_report.reporting_owner_employee_id,
                "execution_owner_employee_id": final_report.execution_owner_employee_id,
                "report_requires_attention": final_report.requires_attention,
                "final_graph_version": completed.final_graph_version,
            },
        ),
    )


async def publish_plan_decision(
    *, decision, compiler_request, operating_decision, route, event_sink, job_id,
    ports: GoalCompletionPorts,
):
    """Publish the one operator-facing result of the frozen planning stage."""
    if route == ports.input_route.CONVERSATION:
        return
    ProductEvent = ports.product_event
    ProductEventType = ports.product_event_type
    CompanyWorkMode = ports.company_work_mode
    plan_event_type = (
        ProductEventType.PLAN_FALLBACK
        if decision.mode.value == "SOLO_FALLBACK"
        else ProductEventType.PLAN_ACCEPTED
    )
    tasks = tuple(
        {
            "task_id": task.task_id,
            "depends_on": task.depends_on,
            "required_capabilities": task.required_capabilities,
            "final": task.task_id == decision.proposal.final_task_id,
            "execution_replica": None if task.execution_replica is None else {
                "group_id": task.execution_replica.group_id,
                "replica_id": task.execution_replica.replica_id,
                "strategy": task.execution_replica.strategy.value,
                "scope": task.execution_replica.scope,
                "aggregation_task_id": task.execution_replica.aggregation_task_id,
                "aggregation": task.execution_replica.aggregation.value,
                "marginal_value_reason": task.execution_replica.marginal_value_reason,
            },
        }
        for task in decision.proposal.tasks
    )
    brief = compiler_request.manager_planning_brief
    ports.emit_product_event(
        event_sink,
        ProductEvent(
            plan_event_type,
            f"{decision.mode.value.lower().replace('_', ' ')} plan · "
            f"{len(tasks)} task(s) · {decision.reason.value}",
            job_id=job_id,
            data={
                "mode": decision.mode.value,
                "reason": decision.reason.value,
                "task_count": len(tasks),
                "coordination_policy": operating_decision.coordination_policy.value,
                "requested_effect": operating_decision.requested_effect.value,
                "execution_replica_preference": operating_decision.execution_replica_preference.value,
                "suggested_execution_replica_strategy": (
                    None if operating_decision.suggested_execution_replica_strategy is None
                    else operating_decision.suggested_execution_replica_strategy.value
                ),
                "planning_owner_id": (
                    compiler_request.planning_owner.employee_id
                    if compiler_request.planning_owner is not None else ""
                ),
                "planning_owner_kind": (
                    "PERSISTENT_MANAGER" if compiler_request.planning_owner is not None
                    else "COMPILER_COMPATIBILITY"
                ),
                "manager_planning_brief_digest": decision.manager_planning_brief_digest,
                "manager_planning_skill_count": len(brief.skills) if brief is not None else 0,
                "manager_planning_outcome_count": (
                    brief.outcome_summary.observed_count if brief is not None else 0
                ),
                "execution_replica_count": sum(
                    task.execution_replica is not None for task in decision.proposal.tasks
                ),
                "company_work_mode": (
                    CompanyWorkMode.TEAM_JOB.value if len(tasks) > 1
                    else CompanyWorkMode.SOLO_JOB.value
                ),
                "rationale": decision.rationale,
                "tasks": tasks,
            },
        ),
    )


async def publish_capability_and_route_events(
    *,
    config,
    registry,
    route,
    session_key,
    manager_assignment,
    event_sink,
    job_id,
    operating_decision,
    firm_coordination,
    work_order,
    roster_snapshot,
    runtime_mcp_read_only,
    mcp_package_decision,
    ports: GoalCompletionPorts,
):
    InputRoute = ports.input_route
    InitialCoordinationPolicy = ports.initial_coordination_policy
    ProductEvent = ports.product_event
    ProductEventType = ports.product_event_type
    _action_policy = ports.action_policy
    _emit_product_event = ports.emit_product_event
    _has_configured_external_read_capability = ports.has_configured_external_read_capability
    direct_agent_tool_access = (
        route == InputRoute.CONVERSATION
        and (
            config.permission_mode == "ask"
            or _has_configured_external_read_capability(config)
        )
    )
    projection_audit = registry.audit_projection(
        _action_policy(
            config,
            workspace_access=(
                route == InputRoute.COMPANY_GOAL or direct_agent_tool_access
            ),
            session_key=session_key,
            manager_tools_enabled=manager_assignment is not None,
        )
    )
    if not projection_audit.valid:
        details = []
        if projection_audit.dangling_grant_names:
            details.append(
                "missing definitions: "
                + ", ".join(projection_audit.dangling_grant_names)
            )
        if projection_audit.effect_mismatch_names:
            details.append(
                "effect mismatches: "
                + ", ".join(projection_audit.effect_mismatch_names)
            )
        raise RuntimeError(
            "Capability contract is inconsistent ("
            + "; ".join(details)
            + "). Run `noruct capabilities` and correct the listed integration."
        )
    _emit_product_event(
        event_sink,
        ProductEvent(
            ProductEventType.CAPABILITY_READY,
            f"Employee capability surface ready · {len(projection_audit.exposed_tool_names)} tool(s)",
            job_id=job_id,
            data={
                "projection_valid": True,
                "exposed_tool_names": projection_audit.exposed_tool_names,
                "withheld_tool_names": projection_audit.withheld_tool_names,
            },
        ),
    )
    _emit_product_event(
        event_sink,
        ProductEvent(
            ProductEventType.INPUT_ROUTED,
            (
                "Company direct assignment · one persistent employee"
                if route == InputRoute.CONVERSATION
                else "Company managed job · plan first"
                if operating_decision.coordination_policy
                == InitialCoordinationPolicy.PLAN_FIRST
                else "Company managed job · bounded solo first"
            ),
            job_id=job_id,
            data={
                "route": route.value,
                "company_owned": True,
                "company_work_mode": operating_decision.work_mode.value,
                "coordination_policy": operating_decision.coordination_policy.value,
                "requested_effect": operating_decision.requested_effect.value,
                "operating_reason": operating_decision.reason.value,
                "execution_replica_preference": (
                    operating_decision.execution_replica_preference.value
                ),
                "suggested_execution_replica_strategy": (
                    None
                    if operating_decision.suggested_execution_replica_strategy
                    is None
                    else operating_decision.suggested_execution_replica_strategy.value
                ),
                "firm_coordinator_action": firm_coordination.action.value,
                "firm_coordinator_digest": firm_coordination.content_digest,
                "firm_coordinator_has_authority": False,
                "manager_employee_id": (
                    manager_assignment.manager_employee_id
                    if manager_assignment is not None
                    else ""
                ),
                "manager_assignment_mode": (
                    manager_assignment.mode.value
                    if manager_assignment is not None
                    else "UNAVAILABLE_PRE_M2_ROSTER"
                ),
                "manager_assignment_digest": (
                    manager_assignment.content_digest
                    if manager_assignment is not None
                    else ""
                ),
                "work_order_id": work_order.work_order_id,
                "work_order_digest": work_order.content_digest,
                "roster_revision": roster_snapshot.revision,
                "active_employee_count": roster_snapshot.active_employee_count,
            },
        ),
    )


async def execute_admitted_goal(
    *,
    firm_coordinator,
    work_order,
    firm_runtime_coordination,
    route,
    event_sink,
    workflow_priors,
    manager_assignment,
    replanner,
    assignment_sink,
    service,
    company_budget_authority,
    request,
    approval_port,
    evolution_artifact_pins,
    evolution_artifact_resolution,
    config,
    store,
    job_id,
    ports: GoalCompletionPorts,
    assignment_admission=None,
    task_action_policy_override=None,
):
    DirectCompanyExecutor = ports.direct_company_executor
    FirmKernel = ports.firm_kernel
    InputRoute = ports.input_route
    SQLiteActiveJobLedger = ports.sqlite_active_job_ledger
    _emit_product_event = ports.emit_product_event
    preflight_budget_lease = None
    if route == InputRoute.CONVERSATION:
        completed = await DirectCompanyExecutor(
            employee_execution=service,
            assignment_sink=assignment_sink,
            company_budget_authority=company_budget_authority,
        ).run(request)
    else:
        completed = await FirmKernel(
            employee_execution=service,
            replanner=replanner,
            assignment_sink=assignment_sink,
            graph_patch_sink=(
                None
                if event_sink is None
                else lambda event: _emit_product_event(
                    event_sink,
                    product_event_from_graph_patch(event, job_id=job_id),
                )
            ),
            graph_patch_proposal_sink=(
                None
                if event_sink is None
                else lambda event: _emit_product_event(
                    event_sink,
                    product_event_from_graph_patch_proposal(event, job_id=job_id),
                )
            ),
            approval_port=approval_port,
            active_job_ledger=SQLiteActiveJobLedger(
                store,
                evolution_artifact_pins=tuple(evolution_artifact_pins),
                evolution_artifact_effects=evolution_artifact_resolution.effects,
                company_coordination=config.company_coordination,
            ),
            company_budget_authority=company_budget_authority,
            mutation_sink=(
                None
                if event_sink is None
                else lambda event: _emit_product_event(
                    event_sink,
                    replace(product_event_from_mutation(event), job_id=job_id),
                )
            ),
            assignment_admission=assignment_admission,
            task_action_policy_override=task_action_policy_override,
        ).run(request)
    return completed, replanner, preflight_budget_lease


def prepare_admitted_execution(
    *,
    firm_coordinator,
    work_order,
    firm_runtime_coordination,
    route,
    event_sink,
    workflow_priors,
    manager_assignment,
    job_id,
    ports: GoalCompletionPorts,
):
    """Build the two execution callbacks from the frozen Job envelope."""

    InputRoute = ports.input_route
    ProductEvent = ports.product_event
    ProductEventType = ports.product_event_type
    _emit_product_event = ports.emit_product_event

    def admission_event(decision) -> None:
        capability = decision.capability or "invalid capability"
        _emit_product_event(
            event_sink,
            ProductEvent(
                ProductEventType.ORGANIZATION_ADMISSION,
                (
                    f"Organization expansion admitted for validation · {capability}"
                    if decision.admitted
                    else f"Rejected organization expansion · {decision.reason.value}"
                ),
                job_id=job_id,
                task_id=decision.trigger_task_id,
                data={
                    "admitted": decision.admitted,
                    "reason": decision.reason.value,
                    "capability": decision.capability,
                    "graph_version": decision.graph_version,
                    "expands_final_task": decision.expands_final_task,
                    "applied": False,
                },
            ),
        )

    replanner = firm_coordinator.runtime_replanner(
        work_order,
        firm_runtime_coordination,
        managed_job=(route == InputRoute.COMPANY_GOAL),
        decision_sink=admission_event,
        workflow_priors=workflow_priors,
        manager_employee_id=(
            manager_assignment.manager_employee_id
            if manager_assignment is not None
            else ""
        ),
    )
    assignment_sink = (
        None
        if event_sink is None
        else lambda event: _emit_product_event(
            event_sink,
            ports.product_event_from_assignment(event),
        )
    )
    return replanner, assignment_sink


async def record_goal_learning(
    *,
    route,
    workflow_context,
    store,
    job_id,
    completed,
    execution_profile,
    request,
    decision,
    replanner,
    workflow_priors,
    company_store,
    company_learning,
    roster_snapshot,
    hiring_learning,
    hire_observation,
    employee_skill_learning,
    event_sink,
    ports: GoalCompletionPorts,
):
    ActiveJobInspector = ports.active_job_inspector
    EvidenceSource = ports.evidence_source
    InputRoute = ports.input_route
    ProductEvent = ports.product_event
    ProductEventType = ports.product_event_type
    _emit_product_event = ports.emit_product_event
    episode_from_runtime_ledger = ports.episode_from_runtime_ledger
    organization_outcome_metrics = ports.organization_outcome_metrics
    staffing_demands_from_runtime_ledger = ports.staffing_demands_from_runtime_ledger
    if (
        route == InputRoute.COMPANY_GOAL
        and workflow_context
    ):
        try:
            try:
                outcome_metrics = organization_outcome_metrics(
                    ActiveJobInspector(store).inspect(job_id),
                    operator_signals=store.list_job_operator_signals(job_id),
                )
            except Exception:
                # Metrics are an optional read-only enrichment. The
                # immutable episode and its Workflow-Patch attribution
                # must remain available when an audit projection is
                # incomplete or from an older compatible ledger shape.
                outcome_metrics = None
            episode_projection = episode_from_runtime_ledger(
                completed,
                store.list_job_events(job_id),
                source=EvidenceSource.REAL_JOB,
                execution_profile=execution_profile.value,
                context_fingerprint=workflow_context or None,
                manager_employee_id=request.manager_employee_id,
                manager_assignment_digest=request.manager_assignment_digest,
                manager_delegation_digest=request.manager_delegation_digest,
                manager_supervision_count=len(
                    store.get_job_supervision_events(job_id)
                ),
                outcome_metrics=outcome_metrics,
            )
            episode, _ = company_store.record_episode(episode_projection)
            runtime_runs = store.list_job_runs(job_id)
            aligned_prior_ids = set(decision.aligned_workflow_prior_ids)
            aligned_prior_ids.update(replanner.aligned_workflow_prior_ids)
            exposed_prior_ids = list(decision.exposed_workflow_prior_ids)
            exposed_prior_ids.extend(
                pattern_id
                for pattern_id in replanner.exposed_workflow_prior_ids
                if pattern_id not in exposed_prior_ids
            )
            # A SOLO-first admission can preserve the prior as a bounded
            # runtime replay candidate without placing it in the initial
            # compiler response. The frozen matching prior was still
            # available to the replan lane, so retain that disclosure fact
            # for attribution rather than dropping the observation.
            exposed_prior_ids.extend(
                prior.pattern_id
                for prior in workflow_priors
                if prior.pattern_id not in exposed_prior_ids
            )
            # Workflow observations are the direct consumer of this Job's
            # Compiler/replanner evidence. Persist them before optional
            # staffing, hiring and skill projections.
            for pattern_id in exposed_prior_ids:
                applied_patch = company_store.find_applied_patch_for_pattern(pattern_id)
                if applied_patch is not None:
                    company_learning.observe(
                        applied_patch.patch_id,
                        episode,
                        prior_exposed=True,
                        proposal_aligned=pattern_id in aligned_prior_ids,
                    )
            # Learning projections are independent read-model consumers of
            # the same immutable episode. A malformed optional staffing
            # receipt must not erase a valid Workflow-Patch observation.
            try:
                for demand in staffing_demands_from_runtime_ledger(
                    completed,
                    runtime_runs,
                    episode=episode,
                    base_roster_revision=roster_snapshot.revision,
                ):
                    company_store.record_staffing_demand(demand)
                hiring_learning.curate()
            except Exception:
                pass
            for contract in company_store.list_hire_observation_contracts():
                hire_observation.observe(
                    contract.patch_id,
                    completed,
                    runtime_runs,
                    episode=episode,
                    base_roster_revision=roster_snapshot.revision,
                )
            for contract in company_store.list_employee_skill_observation_contracts():
                if contract.context_key != episode.context_fingerprint:
                    continue
                employee_skill_learning.observe(
                    contract.patch_id,
                    episode,
                    runtime_runs,
                )
        except Exception as error:
            # Organization learning is a bounded side effect. A failed curation record
            # must never suppress an otherwise completed user result.
            if event_sink is not None:
                _emit_product_event(
                    event_sink,
                    ProductEvent(
                        ProductEventType.LEARNING_PROJECTION_FAILED,
                        "Organization learning projection was not recorded",
                        job_id=job_id,
                        data={
                            "stage": "organization_episode_projection",
                            "failure_code": type(error).__name__,
                        },
                    ),
                )
