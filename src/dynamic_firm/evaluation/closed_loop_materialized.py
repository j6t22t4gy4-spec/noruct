"""Provider-free materialized coding evaluation engine."""

from __future__ import annotations

from . import closed_loop as _facade

globals().update(
    {name: value for name, value in vars(_facade).items() if not name.startswith("__")}
)


async def _run_materialized_evaluation(
    *,
    fixture: StrEnum,
    strategy: CodingStrategyKind,
    root: Path,
    workspace: Path,
    provider,
    worker: CodingWorkerPort,
    model_profile: str,
    run_kind: str,
    max_total_model_calls: int,
    max_wall_time_ms: int,
    company_revision: int = 0,
    roster_revision: int = 0,
    playbook_revision: int = 0,
    distribution_sha256: str = "",
    roster_override: tuple[EmployeeRecord, ...] | None = None,
    validator_override: CodingValidatorPort | None = None,
    score_candidate_override: Callable[[Path, CodingTrajectory], object] | None = None,
    fixture_revision_override: str | None = None,
    manager_employee: EmployeeRecord | None = None,
    manager_roster_revision: int | None = None,
    manager_supervisor: ManagerSupervisionPort | None = None,
    manager_planning_provenance: bool = False,
) -> ClosedLoopCodingRecord:
    baseline_digest = _workspace_digest(workspace)
    roster = (
        roster_override
        if roster_override is not None
        else _roster(strategy, CodingFixtureKind(fixture.value))
    )
    job_id = f"{run_kind}-job-{fixture.value}-{strategy.value}"
    goal = (workspace / "TASK.md").read_text(encoding="utf-8")
    workspace_manifest = tuple(
        sorted(
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")
            if path.is_file()
        )
    )
    manager_assignment = None
    manager_delegation = None
    manager_planning_brief = None
    work_order = None
    # Manager provenance is intentionally opt-in for the qualification campaign.
    # Ordinary closed-loop baselines retain their historical compiler call shape:
    # a manager may supervise execution, but does not get an extra planning turn
    # merely because it was attached to an evaluation fixture.
    if manager_employee is not None and manager_planning_provenance:
        (
            manager_assignment,
            work_order,
            manager_planning_brief,
        ) = _prepare_manager_evaluation_context(
            manager_employee=manager_employee,
            goal=goal,
            job_id=job_id,
            fixture=fixture,
            company_revision=company_revision,
            roster_revision=roster_revision,
            playbook_revision=playbook_revision,
            manager_roster_revision=manager_roster_revision,
            max_total_model_calls=max_total_model_calls,
            max_wall_time_ms=max_wall_time_ms,
        )

    compiler_request = CompilerRequest(
        request_id=f"{run_kind}-{fixture.value}-{strategy.value}",
        goal=goal,
        workspace_manifest=workspace_manifest,
        available_capabilities=tuple(
            sorted(
                {
                    capability
                    for employee in roster
                    for capability in employee.capabilities
                }
            )
        ),
        model_profile=model_profile,
        execution_profile=CompilerExecutionProfile.SHADOW_CODING,
        max_tasks=min(6, max_total_model_calls - 1),
        max_temporary_roles=2,
        max_total_model_calls=max_total_model_calls,
        planning_owner=(
            PlanningOwner(
                employee_id=manager_assignment.manager_employee_id,
                role=manager_employee.role,
                assignment_digest=manager_assignment.content_digest,
                session_key=manager_assignment.session_key,
            )
            if manager_assignment is not None and manager_employee is not None
            else None
        ),
        manager_planning_brief=manager_planning_brief,
    )
    decision = await DynamicWorkflowCompiler(provider).compile(compiler_request)
    if manager_employee is not None and manager_assignment is None:
        (
            manager_assignment,
            work_order,
            _,
        ) = _prepare_manager_evaluation_context(
            manager_employee=manager_employee,
            goal=goal,
            job_id=job_id,
            fixture=fixture,
            company_revision=company_revision,
            roster_revision=roster_revision,
            playbook_revision=playbook_revision,
            manager_roster_revision=manager_roster_revision,
            max_total_model_calls=max_total_model_calls,
            max_wall_time_ms=max_wall_time_ms,
        )
    remaining_model_calls = max(1, max_total_model_calls - decision.usage.model_calls)
    store = RunStore(root / "runtime.db")
    registry = ToolRegistry()
    workspace_tools = WorkspaceReadTools({_WORKSPACE_ID: workspace})
    for definition in workspace_tools.definitions():
        registry.register(definition)
    catalog = ChangeSetCatalog({_WORKSPACE_ID: workspace})
    registry.register(catalog.definition())
    approval = _InvariantApproval(workspace, baseline_digest)
    native = NativeEmployeeRuntimeService(
        store=store,
        provider=provider,
        registry=registry,
        approval_port=approval,
    )
    shadow = ShadowCodingEmployeeRuntimeService(
        store=store,
        worker=worker,
        validator=(
            validator_override
            if validator_override is not None
            else _FixtureValidator(CodingFixtureKind(fixture.value))
        ),
        shadow=ShadowWorkspaceService(),
        catalog=catalog,
        registry=registry,
        approval_port=approval,
    )
    service = RoutedEmployeeExecutionService(native=native, shadow_coding=shadow)
    if manager_assignment is not None:
        assert work_order is not None
        manager_delegation = ManagerDelegation.from_proposal(
            manager_assignment,
            decision.proposal,
        )

    request = CompanyRunRequest(
        request_id=compiler_request.request_id,
        job_id=job_id,
        goal=compiler_request.goal,
        plan_proposal=decision.proposal,
        roster=roster,
        context_snapshot=ContextBundle(
            company_policy_excerpt=(
                "Evaluation workspace only: external worker may edit a disposable shadow; "
                "one validated and approved change set may reach the disposable fixture."
            ),
            workspace_id=_WORKSPACE_ID,
        ),
        runtime_limits=RunLimits(
            max_wall_time_ms=max_wall_time_ms,
            max_model_calls=remaining_model_calls,
            max_tool_calls=4,
            # Evaluation workers must retain a bounded output lease.  Without
            # an explicit cap this path inherits the product-scale default and
            # an exhausted recovery is mis-recorded as validation failure.
            max_output_tokens=12_000,
            max_input_tokens=(
                1_000_000 if run_kind in _LIVE_RUN_KINDS else 100_000
            ),
            max_cost_usd=1.0,
        ),
        action_policy=ActionPolicy(
            tool_grants=(
                ToolGrant(
                    tool_name=APPLY_CHANGE_SET_TOOL,
                    allowed_effects=(ToolEffect.WRITE,),
                    resource_patterns=(f"workspace:{_WORKSPACE_ID}:change-set:*",),
                    max_calls=1,
                    requires_approval=True,
                ),
            ),
            filesystem_policy="WORKSPACE_WRITE",
            sandbox_profile="shadow-workspace-approved",
        ),
        job_limits=JobLimits(
            max_tasks=6,
            max_concurrency=3,
            max_graph_patches=1,
            max_temporary_roles=2,
            max_total_model_calls=remaining_model_calls,
            max_total_tool_calls=8,
            max_total_cost_usd=2.0,
            max_wall_time_ms=max_wall_time_ms,
        ),
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        manager_employee_id=(
            "" if manager_assignment is None else manager_assignment.manager_employee_id
        ),
        manager_assignment_digest=(
            "" if manager_assignment is None else manager_assignment.content_digest
        ),
        manager_session_key=(
            "" if manager_assignment is None else manager_assignment.session_key
        ),
        manager_employee=manager_employee,
        manager_delegation_payload=(
            {} if manager_delegation is None else manager_delegation.canonical_payload()
        ),
        manager_delegation_digest=(
            "" if manager_delegation is None else manager_delegation.content_digest
        ),
        company_work_mode=(
            CompanyWorkMode.TEAM_JOB.value
            if manager_assignment is not None
            else "UNSPECIFIED"
        ),
        coordination_policy=(
            InitialCoordinationPolicy.PLAN_FIRST.value
            if manager_assignment is not None
            else "PRECOMPILED"
        ),
        requested_effect=(
            RequestedEffect.WORKSPACE_CHANGE.value
            if manager_assignment is not None
            else "UNSPECIFIED"
        ),
        operating_reason=(
            OperatingReason.STRUCTURED_MULTI_WORKSTREAM.value
            if manager_assignment is not None
            else "LEGACY_PRECOMPILED"
        ),
        planning_mode=decision.mode.value,
        planning_reason=decision.reason.value,
        compiler_provider_request_id=decision.provider_request_id,
        work_order_id=("" if manager_assignment is None else manager_assignment.work_order_id),
        work_order_digest=(
            "" if manager_assignment is None else manager_assignment.work_order_digest
        ),
    )
    try:
        ledger = SQLiteActiveJobLedger(store)
        result = await FirmKernel(
            employee_execution=service,
            active_job_ledger=ledger,
            manager_supervisor=manager_supervisor,
        ).run(request)
        active_job = ActiveJobInspector(store).inspect(job_id)
        trajectory = trajectory_from_ledger(store, job_id)
        runs = store.list_job_runs(job_id)
        events = store.list_job_events(job_id)
        tool_actions = tuple(
            action
            for run in runs
            for action in store.list_tool_actions(str(run["run_id"]))
        )
        external_effect_actions = tuple(
            action
            for action in tool_actions
            if str(action.get("effect", ""))
            in {
                ToolEffect.NETWORK.value,
                ToolEffect.EXTERNAL_COMMUNICATION.value,
            }
        )
        score = (
            score_candidate_override(workspace, trajectory)
            if score_candidate_override is not None
            else _stable_score(
                score_candidate(CodingFixtureKind(fixture.value), workspace, trajectory)
            )
        )
        dependency_ids = {
            dependency
            for task in result.final_tasks
            for dependency in task.depends_on
        }
        final_candidates = {
            task.task_id
            for task in result.final_tasks
            if task.task_id not in dependency_ids
        }
        plan_template = tuple(
            WorkflowTaskTemplate(
                task_key=task.task_id,
                required_capabilities=tuple(sorted(task.required_capabilities)),
                depends_on=tuple(sorted(task.depends_on)),
                final=(
                    task.task_id in final_candidates
                    and len(final_candidates) == 1
                ),
            )
            for task in result.final_tasks
        )
        compiler_plan_template = tuple(
            WorkflowTaskTemplate(
                task_key=task.task_id,
                required_capabilities=tuple(sorted(task.required_capabilities)),
                depends_on=tuple(sorted(task.depends_on)),
                final=(task.task_id == decision.proposal.final_task_id),
            )
            for task in decision.proposal.tasks
        )
        return ClosedLoopCodingRecord(
            fixture=fixture,
            strategy=strategy,
            status=result.status,
            planning_mode=decision.mode.value,
            planning_reason=decision.reason.value,
            planning_owner_id=decision.planning_owner_id,
            planning_owner_assignment_digest=decision.planning_owner_assignment_digest,
            manager_planning_brief_digest=decision.manager_planning_brief_digest,
            failure_reason=_failure_reason_with_validation(
                result.failure_reason,
                result.status,
                events,
            ),
            employee_failure_codes=tuple(
                task.failure.code
                for task in result.task_results
                if task.failure is not None
            ),
            budget_limit_reasons=tuple(
                str(event.payload["limit"])
                for event in events
                if event.type == EventType.RUN_BUDGET_EXHAUSTED
                and isinstance(event.payload.get("limit"), str)
                and event.payload["limit"]
            ),
            trajectory_source="append-only-runtime-ledger",
            ledger_run_count=len(runs),
            ledger_event_count=len(events),
            ledger_matches_kernel=(
                trajectory.employee_count == result.metrics.unique_employee_count
                and trajectory.maximum_parallelism == result.metrics.maximum_parallelism
            ),
            workspace_unchanged_before_approval=approval.workspace_unchanged,
            compiler_model_calls=decision.usage.model_calls,
            runtime_usage=decision.usage.plus(result.metrics.usage),
            trajectory=trajectory,
            score=score,
            plan_template=plan_template,
            compiler_plan_template=compiler_plan_template,
            fixture_revision=(
                fixture_revision_override
                if fixture_revision_override is not None
                else coding_fixture_contract(
                    CodingFixtureKind(fixture.value)
                ).fixture_revision
            ),
            company_revision=company_revision,
            roster_revision=roster_revision,
            playbook_revision=playbook_revision,
            permission_mode="shadow-workspace-approved",
            approval_mode="allow-once",
            configured_model_call_limit=max_total_model_calls,
            configured_wall_time_ms=max_wall_time_ms,
            distribution_sha256=distribution_sha256,
            active_job_audit_status=active_job.audit_status.value,
            execution_replica_count=result.metrics.execution_replica_count,
            replica_group_count=result.metrics.replica_group_count,
            task_attempts=tuple(
                {
                    "task_id": str(item.get("task_id", "")),
                    "sequence": int(item.get("sequence", 0)),
                    "employee_id": str(item.get("employee_id", "")),
                    "status": str(item.get("status", "")),
                    "failure_kind": str(item.get("failure_kind", "")),
                    "source_attempt_id": item.get("source_attempt_id"),
                    "execution_instance_id": str(
                        item.get("execution_instance_id", "")
                    ),
                    "replica_group_id": str(item.get("replica_group_id", "")),
                }
                for item in active_job.attempts
            ),
            task_mutations=tuple(
                {
                    "sequence": int(item.get("sequence", 0)),
                    "mutation_type": str(item.get("mutation_type", "")),
                    "task_id": str(item.get("task_id", "")),
                    "source_attempt_id": str(item.get("source_attempt_id", "")),
                    "target_attempt_id": str(item.get("target_attempt_id", "")),
                    "from_employee_id": str(item.get("from_employee_id", "")),
                    "to_employee_id": str(item.get("to_employee_id", "")),
                }
                for item in active_job.mutations
            ),
            runtime_user_intervention_count=len(
                store.list_job_operator_signals(job_id)
            ),
            external_effect_error_count=sum(
                str(action.get("status", "")) in {"FAILED", "INDETERMINATE"}
                for action in external_effect_actions
            ),
            external_effect_unknown_count=sum(
                str(action.get("status", "")) == "INDETERMINATE"
                for action in external_effect_actions
            ),
        )
    finally:
        await service.close()
        store.close()
