"""Work Order binding, provider-free lookup, preview, and run-record helpers."""

from __future__ import annotations

from typing import Mapping

from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import (
    EmployeeRecord,
    ExecutionReplicaSpec,
    GraphPatchExpectedImpact,
    GraphPatchObservedOutcome,
    GraphPatchValidationReceipt,
    JobGraph,
    JobLimits,
    JobTask,
    PlanProposal,
)

from .frontdoor import WorkOrder
from .graph_blueprint_models import (
    BlueprintBinding,
    BlueprintResolution,
    BlueprintResolutionReason,
    GraphBlueprint,
    GraphBlueprintRef,
    GraphMutationPolicy,
    GraphPreview,
    GraphPreviewTask,
    GraphRevision,
    GraphRunRecord,
    GraphUserConstraints,
    PLACEHOLDER,
    text,
)
from .graph_blueprint_registry import GraphBlueprintRegistry


def bind_blueprint(
    blueprint: GraphBlueprint,
    *,
    work_order: WorkOrder,
    parameters: Mapping[str, str] | None = None,
    constraints: GraphUserConstraints = GraphUserConstraints(),
    limits: JobLimits = JobLimits(),
) -> BlueprintBinding:
    """Bind inert templates to one Work Order and reuse the Kernel DAG validator."""

    blueprint.verify()
    work_order.verify()
    supplied = dict(parameters or {})
    values = {
        "objective": work_order.objective,
        "requested_outcome": work_order.requested_outcome,
        **supplied,
    }
    unknown = set(supplied) - set(blueprint.parameters)
    if unknown:
        raise ValueError("Blueprint binding supplied undeclared parameters: " + ", ".join(sorted(unknown)))
    missing = set(blueprint.parameters) - set(values)
    if missing:
        raise ValueError("Blueprint binding is missing parameters: " + ", ".join(sorted(missing)))
    for value in values.values():
        text(value, "Blueprint parameter value")
    validate_constraint_caps(constraints, work_order)

    def render(template: str) -> str:
        rendered = PLACEHOLDER.sub(lambda match: values[match.group(1)], template)
        if PLACEHOLDER.search(rendered):
            raise ValueError("Blueprint template contains an unresolved parameter")
        return text(rendered, "Rendered Blueprint field")

    proposal = PlanProposal(
        proposal_id=(
            f"blueprint-{blueprint.blueprint_id}-v{blueprint.version}-{work_order.work_order_id}"
        ),
        goal=work_order.objective,
        tasks=tuple(
            JobTask(
                task_id=task.task_id,
                objective=render(task.objective_template),
                depends_on=task.depends_on,
                required_capabilities=task.required_capabilities,
                acceptance_criteria=tuple(render(item) for item in task.acceptance_templates),
                risk_level=task.risk_level,
                execution_replica=(
                    None
                    if task.execution_replica is None
                    else ExecutionReplicaSpec(
                        group_id=task.execution_replica.group_id,
                        replica_id=task.execution_replica.replica_id,
                        strategy=task.execution_replica.strategy,
                        scope=render(task.execution_replica.scope_template),
                        aggregation_task_id=task.execution_replica.aggregation_task_id,
                        aggregation=task.execution_replica.aggregation,
                        marginal_value_reason=render(
                            task.execution_replica.marginal_value_reason_template
                        ),
                    )
                ),
            )
            for task in blueprint.tasks
        ),
        final_task_id=blueprint.final_task_id,
    )
    graph_from_proposal(proposal, max_tasks=limits.max_tasks)
    return BlueprintBinding(
        blueprint_ref=blueprint.ref,
        work_order_id=work_order.work_order_id,
        work_order_digest=work_order.content_digest,
        parameters=tuple(sorted((key, values[key]) for key in blueprint.parameters)),
        constraints=constraints,
        proposal=proposal,
    )


def resolve_blueprint(
    registry: GraphBlueprintRegistry,
    *,
    work_order: WorkOrder,
    objective_class: str,
    execution_profile: str,
    available_capabilities: tuple[str, ...],
    limits: JobLimits,
    pin_slot: str | None = None,
    constraints: GraphUserConstraints = GraphUserConstraints(),
) -> BlueprintResolution:
    """Return a bound local candidate or reasons to continue to the Compiler."""

    work_order.verify()
    candidates = registry.compatible(
        objective_class=objective_class,
        execution_profile=execution_profile,
        available_capabilities=available_capabilities,
        pin_slot=pin_slot,
    )
    if not candidates:
        return BlueprintResolution(
            reason=BlueprintResolutionReason.NO_COMPATIBLE_BLUEPRINT,
            detail="No local Blueprint matched the requested objective, profile, and capability supply.",
        )
    rejected: list[GraphBlueprintRef] = []
    pinned = registry.pinned(pin_slot) if pin_slot is not None else None
    for candidate in candidates:
        try:
            binding = bind_blueprint(
                candidate, work_order=work_order, constraints=constraints, limits=limits
            )
        except ValueError:
            rejected.append(candidate.ref)
            continue
        return BlueprintResolution(
            reason=(
                BlueprintResolutionReason.PINNED_HIT
                if pinned is not None and candidate.ref == pinned
                else BlueprintResolutionReason.LOCAL_HIT
            ),
            binding=binding,
            rejected_refs=tuple(rejected),
        )
    return BlueprintResolution(
        reason=BlueprintResolutionReason.BINDING_REJECTED,
        rejected_refs=tuple(rejected),
        detail="Compatible Blueprint candidates failed Work Order binding or graph validation.",
    )


def preview_binding(
    binding: BlueprintBinding,
    *,
    work_order: WorkOrder,
    roster: tuple[EmployeeRecord, ...],
    limits: JobLimits,
) -> GraphPreview:
    """Produce a read-only operator view using existing Firm admission facts."""

    from .firm_admission import FirmAdmissionController

    if (
        binding.work_order_id != work_order.work_order_id
        or binding.work_order_digest != work_order.content_digest
    ):
        raise ValueError("Blueprint binding does not match the Work Order")
    admission = FirmAdmissionController().admit(
        work_order=work_order,
        proposal=binding.proposal,
        roster=roster,
        limits=limits,
        constraints=binding.constraints,
    )
    roster_ids = {employee.employee_id for employee in roster if employee.active}
    warnings: list[str] = []
    for label, values in (
        ("Pinned", binding.constraints.pinned_employee_ids),
        ("Excluded", binding.constraints.excluded_employee_ids),
    ):
        missing = set(values) - roster_ids
        if missing:
            warnings.append(f"{label} Employees are unavailable: " + ", ".join(sorted(missing)))
    if binding.constraints.require_independent_review and len(binding.proposal.tasks) < 2:
        warnings.append("Independent review is required but the Blueprint has only one task.")
    if (
        binding.constraints.max_concurrency is not None
        and binding.constraints.max_concurrency < admission.concurrency_ceiling
    ):
        warnings.append("Requested concurrency is below the Blueprint dependency width.")
    replica_groups = tuple(
        sorted(
            {
                task.execution_replica.group_id
                for task in binding.proposal.tasks
                if task.execution_replica is not None
            }
        )
    )
    if replica_groups and admission.concurrency_ceiling < 2:
        warnings.append(
            "The Blueprint contains execution replicas but the effective concurrency ceiling is below two."
        )
    proposal_tasks = {task.task_id: task for task in binding.proposal.tasks}

    def preview_task(item: object) -> GraphPreviewTask:
        task = proposal_tasks[str(getattr(item, "task_id"))]
        replica = task.execution_replica
        return GraphPreviewTask(
            task_id=task.task_id,
            depends_on=task.depends_on,
            required_capabilities=tuple(getattr(item, "required_capabilities")),
            proposed_employee_id=getattr(item, "persistent_employee_id"),
            temporary_role_required=bool(getattr(item, "temporary_role_required")),
            execution_replica_group_id="" if replica is None else replica.group_id,
            execution_replica_id="" if replica is None else replica.replica_id,
            execution_replica_strategy="" if replica is None else replica.strategy.value,
            execution_replica_scope="" if replica is None else replica.scope,
            execution_replica_aggregation_task_id=(
                "" if replica is None else replica.aggregation_task_id
            ),
            execution_replica_aggregation=(
                "" if replica is None else replica.aggregation.value
            ),
            execution_replica_value_reason=(
                "" if replica is None else replica.marginal_value_reason
            ),
        )
    return GraphPreview(
        binding_digest=binding.content_digest,
        work_order_digest=work_order.content_digest,
        blueprint_ref=binding.blueprint_ref,
        work_mode=admission.effective_work_mode,
        final_task_id=binding.proposal.final_task_id,
        task_count=len(binding.proposal.tasks),
        dependency_width=admission.dependency_width,
        distinct_staffing_profile_count=admission.distinct_staffing_profile_count,
        staffing_difference_dimensions=admission.staffing_difference_dimensions,
        execution_replica_group_ids=replica_groups,
        execution_replica_count=sum(
            task.execution_replica is not None for task in binding.proposal.tasks
        ),
        tasks=tuple(preview_task(item) for item in admission.staffing),
        proposed_employee_ids=tuple(
            item.persistent_employee_id
            for item in admission.staffing
            if item.persistent_employee_id is not None
        ),
        uncovered_task_ids=admission.uncovered_task_ids,
        admission_status=admission.status.value,
        admission_reason=admission.reason,
        hard_cap_cost_usd=work_order.budget_snapshot.max_cost_usd,
        hard_cap_wall_time_ms=work_order.budget_snapshot.max_wall_time_ms,
        effective_max_cost_usd=min(
            work_order.budget_snapshot.max_cost_usd,
            binding.constraints.max_cost_usd
            if binding.constraints.max_cost_usd is not None
            else work_order.budget_snapshot.max_cost_usd,
        ),
        effective_max_wall_time_ms=min(
            work_order.budget_snapshot.max_wall_time_ms,
            binding.constraints.max_wall_time_ms
            if binding.constraints.max_wall_time_ms is not None
            else work_order.budget_snapshot.max_wall_time_ms,
        ),
        requires_independent_review=binding.constraints.require_independent_review,
        mutation_policy=binding.constraints.mutation_policy,
        constraint_warnings=tuple(warnings),
    )


def graph_run_record(
    *,
    job_id: str,
    work_order: WorkOrder,
    graph: JobGraph,
    blueprint_ref: GraphBlueprintRef | None = None,
) -> GraphRunRecord:
    """Create retained audit evidence from an already validated initial graph."""

    from dynamic_firm.kernel.mutation import graph_structure_digest

    work_order.verify()
    return GraphRunRecord(
        job_id=job_id,
        work_order_digest=work_order.content_digest,
        initial_graph_digest=graph_structure_digest(graph),
        blueprint_ref=blueprint_ref,
    )


def graph_run_record_from_active_job(inspection: object) -> GraphRunRecord:
    """Project a durable ACTIVE JOB audit into immutable Graph lineage.

    The ACTIVE JOB chain remains canonical storage. This function deliberately
    derives a portable, data-only GraphRunRecord from a replay-verified audit
    instead of creating a second mutable execution authority or copying raw
    task output into the Blueprint store.
    """

    job_id = str(getattr(inspection, "job_id", ""))
    work_order_digest = str(getattr(inspection, "work_order_digest", ""))
    initial_graph_digest = str(getattr(inspection, "initial_graph_digest", ""))
    if not bool(getattr(inspection, "replay_matches", False)):
        raise ValueError("Graph Run Record requires a replay-verified ACTIVE JOB audit")
    blueprint_id = str(getattr(inspection, "graph_blueprint_id", ""))
    blueprint_version = int(getattr(inspection, "graph_blueprint_version", 0) or 0)
    blueprint_digest = str(getattr(inspection, "graph_blueprint_digest", ""))
    blueprint_ref = (
        GraphBlueprintRef(blueprint_id, blueprint_version, blueprint_digest)
        if blueprint_id and blueprint_version and blueprint_digest
        else None
    )
    policy = GraphMutationPolicy(str(getattr(inspection, "graph_mutation_policy", "")))
    record = GraphRunRecord(
        job_id=job_id,
        work_order_digest=work_order_digest,
        initial_graph_digest=initial_graph_digest,
        blueprint_ref=blueprint_ref,
    )
    terminal = getattr(inspection, "terminal", None)
    terminal_status = (
        str(terminal.get("status", "")) if isinstance(terminal, Mapping) else ""
    )
    observed_terminal_outcome = {
        "SUCCEEDED": GraphPatchObservedOutcome.JOB_SUCCEEDED,
        "FAILED": GraphPatchObservedOutcome.JOB_FAILED,
        "STALLED": GraphPatchObservedOutcome.JOB_STALLED,
        "BUDGET_EXHAUSTED": GraphPatchObservedOutcome.JOB_BUDGET_EXHAUSTED,
    }.get(terminal_status, GraphPatchObservedOutcome.NOT_OBSERVED)
    for payload in tuple(getattr(inspection, "graph_patches", ())):
        if not isinstance(payload, Mapping):
            raise ValueError("ACTIVE JOB Graph patch projection is malformed")
        patch = payload.get("patch")
        if not isinstance(patch, Mapping):
            raise ValueError("ACTIVE JOB Graph patch has no typed patch")
        lease = payload.get("mutation_lease", {})
        if lease is None:
            lease = {}
        if not isinstance(lease, Mapping):
            raise ValueError("ACTIVE JOB Graph patch lease is malformed")
        try:
            expected_impact = GraphPatchExpectedImpact(
                str(
                    payload.get(
                        "expected_impact",
                        GraphPatchExpectedImpact.CAPABILITY_COVERAGE.value,
                    )
                )
            )
            validation_receipt = GraphPatchValidationReceipt(
                str(
                    payload.get(
                        "validation_receipt",
                        GraphPatchValidationReceipt.KERNEL_GRAPH_AND_LEASE_VALIDATED.value,
                    )
                )
            )
        except ValueError as exc:
            raise ValueError("ACTIVE JOB Graph patch validation projection is malformed") from exc
        revision = GraphRevision(
            sequence=int(payload.get("sequence", 0) or 0),
            previous_graph_digest=str(payload.get("before_graph_digest", "")),
            next_graph_digest=str(payload.get("after_graph_digest", "")),
            operation=str(patch.get("semantic_operation", "")),
            proposer="kernel-replanner",
            trigger_evidence=(
                f"trigger-task:{str(patch.get('trigger_task_id', ''))}",
                f"patch:{str(patch.get('patch_id', ''))}",
                *tuple(
                    f"semantic-evidence:{str(reference)}"
                    for reference in patch.get("semantic_evidence_refs", ())
                    if isinstance(reference, str)
                ),
            ),
            budget_delta=float(lease.get("cost_usd", 0.0)),
            approval_policy=policy,
            expected_impact=expected_impact,
            validation_receipt=validation_receipt,
            observed_terminal_outcome=observed_terminal_outcome,
        )
        record = record.append(revision)
    return record


def validate_constraint_caps(constraints: GraphUserConstraints, work_order: WorkOrder) -> None:
    budget = work_order.budget_snapshot
    if constraints.max_cost_usd is not None and constraints.max_cost_usd > budget.max_cost_usd:
        raise ValueError("Blueprint cost constraint exceeds the Work Order hard cap")
    if (
        constraints.max_wall_time_ms is not None
        and constraints.max_wall_time_ms > budget.max_wall_time_ms
    ):
        raise ValueError("Blueprint time constraint exceeds the Work Order hard cap")
