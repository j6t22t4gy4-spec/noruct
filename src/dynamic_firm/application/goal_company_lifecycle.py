"""Frozen Company, ROSTER, Work Order, and Manager intake for one goal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dynamic_firm.application.cli_component_contract import cli
from dynamic_firm.application.goal_runtime_resources import _JobRuntimeResources
from dynamic_firm.company.work_order_portfolio import WorkOrderPortfolioStore
from dynamic_firm.product import InputRoute


@dataclass(frozen=True, slots=True)
class FrozenCompanyGoalIntake:
    """Immutable intake outputs before capability or Employee construction."""

    config: Any
    operating_decision: Any
    company_snapshot: Any
    roster_snapshot: Any
    roster: tuple[Any, ...]
    executive_manager: Any | None
    evolution_artifact_resolution: Any
    evolution_artifact_pins: tuple[Any, ...]
    runtime_mcp_read_only: Any | None
    mcp_package_decision: str
    authority_snapshot: Any
    work_order_budget_snapshot: Any
    work_order: Any
    manager_assignment: Any | None
    manager_employee: Any | None
    execution_roster: tuple[Any, ...]
    firm_coordinator: Any
    firm_coordination: Any
    firm_runtime_coordination: Any


def prepare_frozen_company_goal_intake(
    *,
    config: Any,
    provider: Any,
    resources: _JobRuntimeResources,
    route: InputRoute,
    roster_snapshot: Any | None,
    session_key: str,
    request_id: str,
    job_id: str,
    execution_origin: Any | None,
    work_order_override: Any | None,
    operating_decision: Any,
    evolution_state_path: Any,
) -> FrozenCompanyGoalIntake:
    """Freeze Company-owned admission inputs before runtime construction."""

    company_store = resources.company_store
    company_snapshot = company_store.company()
    active_roster = cli.decode_active_roster(
        company_store.ensure_roster_baseline(cli._default_roster(config))
    )
    if roster_snapshot is not None and roster_snapshot != active_roster:
        raise ValueError("Active ROSTER changed before job start; reload the company snapshot")
    roster_snapshot = roster_snapshot or active_roster
    roster = roster_snapshot.resolve_execution_profiles(config.model)
    executive_manager = cli.PersistentExecutiveManager.optional_from_roster(
        roster,
        roster_revision=roster_snapshot.revision,
    )
    if company_store.evolution_autonomy_mode() == cli.EvolutionAutonomyMode.ALWAYS_APPROVE:
        cli._advance_preapproved_evolution_artifacts(
            state_path=evolution_state_path,
            roster=roster,
        )
    evolution_artifact_resolution = cli._resolve_evolution_artifacts_for_job(
        state_path=evolution_state_path,
        job_id=job_id,
        roster=roster,
    )
    runtime_mcp_read_only, mcp_package_decision = cli._mcp_policy_for_frozen_artifacts(
        config.mcp_read_only,
        evolution_artifact_resolution,
    )
    evolution_artifact_resolution = cli.replace(
        evolution_artifact_resolution,
        effects=(
            *evolution_artifact_resolution.effects,
            {"kind": "TOOL_PACKAGE", "decision": mcp_package_decision},
        ),
    )
    work_order_policy = cli._action_policy(
        cli.replace(config, mcp_read_only=runtime_mcp_read_only),
        workspace_access=(
            route == InputRoute.COMPANY_GOAL
            or (
                route == InputRoute.CONVERSATION
                and (
                    config.permission_mode == "ask"
                    or cli._has_configured_external_read_capability(config)
                )
            )
        ),
        session_key=session_key,
        manager_tools_enabled=executive_manager is not None,
    )
    authority_snapshot = cli.AuthoritySnapshotIdentity(
        company_id="company-local",
        company_revision=company_snapshot.revision,
        roster_revision=roster_snapshot.revision,
        playbook_revision=company_store.playbook().revision,
        action_policy_digest=cli.kernel_content_digest(work_order_policy),
    )
    work_order_budget_snapshot = cli.WorkOrderBudgetSnapshot(
        max_model_calls=config.run_limits.max_model_calls,
        max_tool_calls=config.run_limits.max_tool_calls,
        max_cost_usd=config.run_limits.max_cost_usd,
        max_wall_time_ms=config.run_limits.max_wall_time_ms,
    )
    generated_work_order = cli.normalize_work_order(
        config.goal,
        work_order_id=f"work-order-{request_id.removeprefix('request-')}",
        requested_outcome=config.goal,
        constraints=(
            "All effects must remain inside the frozen Company authority and approval policy.",
        ),
        acceptance_criteria=(
            "Return one explicit user-facing result with evidence or unresolved issues.",
        ),
        context_refs=(
            (
                f"knowledge-pack:{execution_origin.pack_id}"
                f"@{execution_origin.pack_revision}:{execution_origin.pack_digest}"
            ),
            (
                f"decision-context:{execution_origin.decision_context_id}:"
                f"{execution_origin.decision_context_digest}"
            ),
            (
                f"oracle-contract:{execution_origin.oracle_contract_id}:"
                f"{execution_origin.oracle_contract_digest}"
            ),
        ) if execution_origin is not None else (),
        workspace_ref=(
            f"workspace:{cli.WORKSPACE_ID}"
            if route == InputRoute.COMPANY_GOAL
            or operating_decision.requested_effect != cli.RequestedEffect.READ
            else None
        ),
        authority_snapshot=authority_snapshot,
        budget_snapshot=work_order_budget_snapshot,
        requested_at=cli.datetime.now(cli.timezone.utc),
        operating_decision=operating_decision,
    )
    work_order = work_order_override or generated_work_order
    if work_order_override is not None and work_order.requested_outcome != config.goal:
        raise ValueError("Portfolio Work Order goal does not match the selected execution request")
    cli.verify_work_order_binding(
        work_order,
        authority_snapshot=authority_snapshot,
        budget_snapshot=work_order_budget_snapshot,
    )
    portfolio_path = config.state_path.with_name(
        f"{config.state_path.stem}.work-orders.db"
    )
    with WorkOrderPortfolioStore(portfolio_path) as work_order_authority:
        work_order_authority.retain_work_order(work_order)
    manager_assignment = (
        executive_manager.initial_assignment(work_order, session_key=session_key)
        if executive_manager is not None
        else None
    )
    if manager_assignment is not None:
        executive_manager.validate_assignment(manager_assignment, work_order)
    manager_employee = (
        next(
            employee
            for employee in roster
            if employee.employee_id == manager_assignment.manager_employee_id
        )
        if manager_assignment is not None
        else None
    )
    execution_roster = (
        tuple(
            employee
            for employee in roster
            if employee.employee_id != manager_assignment.manager_employee_id
        )
        if manager_assignment is not None and route == InputRoute.COMPANY_GOAL
        else roster
    )
    firm_coordinator = cli.ManagerProposalAdapter(
        provider,
        graph_blueprints=resources.graph_blueprint_registry,
    )
    firm_coordination = firm_coordinator.initial_decision(work_order)
    firm_runtime_coordination = firm_coordinator.runtime_decision(work_order)
    return FrozenCompanyGoalIntake(
        config=cli.replace(config, goal=work_order.objective),
        operating_decision=work_order.operating_decision,
        company_snapshot=company_snapshot,
        roster_snapshot=roster_snapshot,
        roster=roster,
        executive_manager=executive_manager,
        evolution_artifact_resolution=evolution_artifact_resolution,
        evolution_artifact_pins=evolution_artifact_resolution.pins,
        runtime_mcp_read_only=runtime_mcp_read_only,
        mcp_package_decision=mcp_package_decision,
        authority_snapshot=authority_snapshot,
        work_order_budget_snapshot=work_order_budget_snapshot,
        work_order=work_order,
        manager_assignment=manager_assignment,
        manager_employee=manager_employee,
        execution_roster=execution_roster,
        firm_coordinator=firm_coordinator,
        firm_coordination=firm_coordination,
        firm_runtime_coordination=firm_runtime_coordination,
    )
