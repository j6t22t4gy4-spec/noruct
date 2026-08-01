from __future__ import annotations

"""Surface-neutral Modern terminal adapters for future-Job Graph controls.

Graph blueprints remain inert local preferences until a later Work Order is
compiled and validated by the Firm Kernel.  This component projects and saves
those preferences; it never mutates an active Job, permission, or budget lease.
The ``editor_tasks`` projection is intentionally complete (up to the canonical
64-task Blueprint bound); a terminal page is only a renderer concern, so a
future GUI/IPC client never receives a silently truncated topology.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import io
from dynamic_firm.company import (
    GraphBlueprint,
    GraphBlueprintControlService,
    GraphBlueprintOrigin,
    GraphBlueprintTask,
    GraphMutationPolicy,
    GraphUserConstraints,
    SQLiteGraphBlueprintRegistry,
)
from dynamic_firm.product.modern_tui import ModernTerminalCommandResult
from dynamic_firm.product.graph_cli_values import graph_registry_path

def graph_control_snapshot(state_path: Path) -> Mapping[str, object]:
    """Return a redacted, inert Graph preference projection for any UI.

    Blueprint preferences are deliberately separate from a Work Order.  A
    UI can stage the next Job's structure, but it receives neither active
    graph state nor a capability to alter authority, a budget lease, or a
    running Employee.
    """

    registry = SQLiteGraphBlueprintRegistry(graph_registry_path(state_path))
    try:
        control = GraphBlueprintControlService(registry)
        catalog = control.catalog()
        blueprints = tuple(
            (
                item,
                control.revision_receipts(item.blueprint_id),
                control.revision_diff(item.ref),
            )
            for item in catalog.blueprints
        )
    finally:
        registry.close()
    selected = catalog.selection.blueprint_ref
    constraints = catalog.selection.constraints
    return {
        "selection": {
            "blueprint_id": selected.blueprint_id if selected is not None else None,
            "version": selected.version if selected is not None else None,
            "pinned_employee_ids": constraints.pinned_employee_ids,
            "excluded_employee_ids": constraints.excluded_employee_ids,
            "require_independent_review": constraints.require_independent_review,
            "max_concurrency": constraints.max_concurrency,
            "max_cost_usd": constraints.max_cost_usd,
            "max_wall_time_ms": constraints.max_wall_time_ms,
            "mutation_policy": constraints.mutation_policy.value,
        },
        "blueprints": tuple(
            {
                "blueprint_id": item.blueprint_id,
                "version": item.version,
                "origin": item.origin.value,
                "objective_class": item.objective_class,
                "execution_profiles": item.execution_profiles,
                "parameters": item.parameters,
                "final_task_id": item.final_task_id,
                "editor_tasks": tuple(
                    {
                        "task_id": task.task_id,
                        "objective_template": task.objective_template,
                        "depends_on": task.depends_on,
                        "required_capabilities": task.required_capabilities,
                        "acceptance_templates": task.acceptance_templates,
                    }
                    for task in item.tasks
                ),
                "parent": (
                    {
                        "blueprint_id": item.parent_ref.blueprint_id,
                        "version": item.parent_ref.version,
                    }
                    if item.parent_ref is not None
                    else None
                ),
                "task_count": len(item.tasks),
                "execution_replica_count": sum(
                    task.execution_replica is not None for task in item.tasks
                ),
                "execution_replica_groups": tuple(
                    {
                        "group_id": group_id,
                        "strategy": members[0].execution_replica.strategy.value,
                        "member_task_ids": tuple(task.task_id for task in members),
                        "aggregation_task_id": members[0].execution_replica.aggregation_task_id,
                        "aggregation": members[0].execution_replica.aggregation.value,
                        "marginal_value_reason_template": (
                            members[0].execution_replica.marginal_value_reason_template
                        ),
                    }
                    for group_id in sorted(
                        {
                            task.execution_replica.group_id
                            for task in item.tasks
                            if task.execution_replica is not None
                        }
                    )
                    for members in (
                        tuple(
                            task
                            for task in item.tasks
                            if task.execution_replica is not None
                            and task.execution_replica.group_id == group_id
                        ),
                    )
                ),
                "revision_receipts": tuple(
                    {
                        "status": receipt.status.value,
                        "reason": receipt.reason,
                        # A revision rationale is an explicit local operator
                        # statement about a future-Job template.  It is not a
                        # Work Order, prompt, ACTIVE JOB payload, or runtime
                        # authority; expose the bounded field so every GUI
                        # can explain *why* a user-authored revision exists.
                        "rationale": receipt.rationale[:512],
                        "source_version": receipt.source_ref.version,
                        "candidate_version": receipt.candidate_ref.version,
                    }
                    for receipt in receipts
                    if receipt.candidate_ref == item.ref
                ),
                "revision_diff": (
                    {
                        "source_version": diff.source_ref.version,
                        "added_task_ids": diff.added_task_ids,
                        "removed_task_ids": diff.removed_task_ids,
                        "changed_tasks": tuple(
                            {"task_id": task_id, "fields": fields}
                            for task_id, fields in diff.changed_tasks
                        ),
                        "changed_envelope_fields": diff.changed_envelope_fields,
                    }
                    if diff is not None
                    else None
                ),
            }
            for item, receipts, diff in blueprints
        ),
    }


def save_future_graph_constraints(
    state_path: Path,
    *,
    max_concurrency: int | None,
    max_cost_usd: float | None,
    max_wall_time_ms: int | None,
    mutation_policy: GraphMutationPolicy,
):
    """Persist only the next-Job envelope while preserving other selection facts.

    This is the common typed write boundary for a future GUI and the terminal.
    It never creates a Work Order, alters an ACTIVE JOB, or touches a budget
    lease; admission still validates the saved envelope against a specific
    Work Order and Company hard limits.
    """

    registry = SQLiteGraphBlueprintRegistry(graph_registry_path(state_path))
    try:
        control = GraphBlueprintControlService(registry)
        current = control.selection()
        prior = current.constraints
        constraints = GraphUserConstraints(
            pinned_employee_ids=prior.pinned_employee_ids,
            excluded_employee_ids=prior.excluded_employee_ids,
            require_independent_review=prior.require_independent_review,
            max_concurrency=max_concurrency,
            max_cost_usd=max_cost_usd,
            max_wall_time_ms=max_wall_time_ms,
            mutation_policy=mutation_policy,
        )
        return control.select(current.blueprint_ref, constraints=constraints)
    finally:
        registry.close()


def _identifiers(value: object, *, label: str) -> tuple[str, ...]:
    """Parse a compact workbench field; model validation remains canonical."""

    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (tuple, list)):
        raw = value
    else:
        raise ValueError(f"{label} must be a comma-separated list")
    values = tuple(str(item).strip() for item in raw if str(item).strip())
    if not values:
        raise ValueError(f"{label} must include at least one identifier")
    return values


def _reference(control: GraphBlueprintControlService, submission: Mapping[str, object]):
    blueprint_id = str(submission.get("source_blueprint_id") or "").strip()
    raw_version = submission.get("source_version")
    if not blueprint_id or raw_version is None:
        raise ValueError("Choose an exact Blueprint revision first")
    return control.revision(blueprint_id, int(raw_version)).ref


def apply_graph_blueprint_action(owner: Any, submission: Mapping[str, object]) -> tuple[str, ...]:
    """Author one inert Blueprint artifact through the canonical local service.

    The compact workbench can save a valid one-task Draft, fork a selected
    immutable revision, or revise the compatibility envelope of a user-owned
    revision.  It never selects implicitly, creates a Job, or reserves a
    budget; the ordinary Graph control remains the separate select/pin step.
    """

    action = str(submission.get("action") or "").strip().lower()
    registry = SQLiteGraphBlueprintRegistry(graph_registry_path(owner.state_path))
    try:
        control = GraphBlueprintControlService(registry)
        topology = submission.get("topology")
        if action in {"save_topology_draft", "revise_topology"}:
            if not isinstance(topology, Mapping):
                raise ValueError("Blueprint topology must contain typed task rows")
            tasks = topology.get("tasks")
            if not isinstance(tasks, list):
                raise ValueError("Blueprint topology tasks must be a list")
            if action == "save_topology_draft":
                saved = control.import_payload(
                    {
                        "blueprint_id": str(submission.get("blueprint_id") or "").strip(),
                        "version": 1,
                        "objective_class": str(submission.get("objective_class") or "general").strip(),
                        "execution_profiles": list(
                            _identifiers(
                                submission.get("execution_profiles") or "read_only",
                                label="Execution profiles",
                            )
                        ),
                        "parameters": list(
                            _identifiers(
                                topology.get("parameters") or "objective,requested_outcome",
                                label="Parameters",
                            )
                        ),
                        "tasks": tasks,
                        "final_task_id": str(topology.get("final_task_id") or "").strip(),
                        "origin": GraphBlueprintOrigin.DRAFT.value,
                        "parent_ref": None,
                    }
                )
                return (
                    f"Topology Blueprint Draft saved · {saved.blueprint_id}@{saved.version}",
                    "The validated topology is inert and unselected. Choose it explicitly for a future Job.",
                )
            source_ref = _reference(control, submission)
            source = control.revision(source_ref.blueprint_id, source_ref.version)
            candidate = control.parse_payload(
                {
                    "blueprint_id": source.blueprint_id,
                    "version": source.version + 1,
                    "objective_class": str(
                        submission.get("objective_class") or source.objective_class
                    ).strip(),
                    "execution_profiles": list(
                        _identifiers(
                            submission.get("execution_profiles") or source.execution_profiles,
                            label="Execution profiles",
                        )
                    ),
                    "parameters": list(
                        _identifiers(
                            topology.get("parameters") or source.parameters,
                            label="Parameters",
                        )
                    ),
                    "tasks": tasks,
                    "final_task_id": str(topology.get("final_task_id") or "").strip(),
                    "origin": GraphBlueprintOrigin.USER_REVISION.value,
                    "parent_ref": {
                        "blueprint_id": source.ref.blueprint_id,
                        "version": source.ref.version,
                        "content_digest": source.ref.content_digest,
                    },
                }
            )
            saved, receipt = control.revise(
                source.ref,
                candidate,
                rationale=str(submission.get("rationale") or "").strip(),
            )
            return (
                f"Topology Blueprint revision saved · {saved.blueprint_id}@{saved.version} · {receipt.status.value}",
                "The prior topology remains immutable and the new revision is not selected automatically.",
            )
        if action == "create_draft":
            saved = control.save(
                GraphBlueprint(
                    blueprint_id=str(submission.get("blueprint_id") or "").strip(),
                    version=1,
                    objective_class=str(submission.get("objective_class") or "general").strip(),
                    execution_profiles=_identifiers(
                        submission.get("execution_profiles") or "read_only",
                        label="Execution profiles",
                    ),
                    parameters=("objective", "requested_outcome"),
                    tasks=(
                        GraphBlueprintTask(
                            task_id="execute",
                            objective_template=str(
                                submission.get("objective_template")
                                or "Complete {{objective}}"
                            ).strip(),
                            depends_on=(),
                            required_capabilities=_identifiers(
                                submission.get("required_capabilities") or "analysis",
                                label="Required capabilities",
                            ),
                            acceptance_templates=(
                                str(
                                    submission.get("acceptance_template")
                                    or "Complete {{requested_outcome}}"
                                ).strip(),
                            ),
                        ),
                    ),
                    final_task_id="execute",
                    origin=GraphBlueprintOrigin.DRAFT,
                )
            )
            return (
                f"Blueprint Draft saved · {saved.blueprint_id}@{saved.version}",
                "It is inert and unselected. Choose it explicitly for a future Job.",
            )
        if action == "fork":
            saved = control.fork(
                _reference(control, submission),
                blueprint_id=str(submission.get("blueprint_id") or "").strip(),
            )
            return (
                f"Blueprint fork saved · {saved.blueprint_id}@{saved.version}",
                "The source is unchanged; the fork is inert and unselected.",
            )
        if action == "revise_envelope":
            source_ref = _reference(control, submission)
            source = control.revision(source_ref.blueprint_id, source_ref.version)
            saved, receipt = control.revise(
                source.ref,
                GraphBlueprint(
                    blueprint_id=source.blueprint_id,
                    version=source.version + 1,
                    objective_class=str(
                        submission.get("objective_class") or source.objective_class
                    ).strip(),
                    execution_profiles=_identifiers(
                        submission.get("execution_profiles") or source.execution_profiles,
                        label="Execution profiles",
                    ),
                    parameters=source.parameters,
                    tasks=source.tasks,
                    final_task_id=source.final_task_id,
                    origin=GraphBlueprintOrigin.USER_REVISION,
                    parent_ref=source.ref,
                ),
                rationale=str(submission.get("rationale") or "").strip(),
            )
            return (
                f"Blueprint revision saved · {saved.blueprint_id}@{saved.version} · {receipt.status.value}",
                "The prior revision remains immutable and the new revision is not selected automatically.",
            )
        raise ValueError("Unknown Blueprint workbench action")
    except (TypeError, ValueError) as exc:
        return (f"Blueprint change was not saved · {exc}",)
    finally:
        registry.close()

def apply_graph_control(owner: Any, submission: Mapping[str, object]) -> tuple[str, ...]:
    """Persist validated local Graph defaults for a future Work Order only."""

    try:
        blueprint_id = str(submission.get("blueprint_id") or "").strip()
        raw_version = submission.get("version")
        if bool(blueprint_id) != bool(raw_version):
            raise ValueError("Choose a complete Blueprint revision or select No Blueprint")
        constraints = GraphUserConstraints(
            pinned_employee_ids=tuple(
                str(item).strip()
                for item in submission.get("pinned_employee_ids", ())
                if str(item).strip()
            ),
            excluded_employee_ids=tuple(
                str(item).strip()
                for item in submission.get("excluded_employee_ids", ())
                if str(item).strip()
            ),
            require_independent_review=bool(
                submission.get("require_independent_review", False)
            ),
            max_concurrency=(
                int(submission["max_concurrency"])
                if submission.get("max_concurrency") is not None
                else None
            ),
            max_cost_usd=(
                float(submission["max_cost_usd"])
                if submission.get("max_cost_usd") is not None
                else None
            ),
            max_wall_time_ms=(
                int(submission["max_wall_time_ms"])
                if submission.get("max_wall_time_ms") is not None
                else None
            ),
            mutation_policy=GraphMutationPolicy(
                str(submission.get("mutation_policy", GraphMutationPolicy.BOUNDED_AUTO.value))
            ),
        )
        # A reusable preference may narrow a Job's envelope, never enlarge
        # the separately configured hard limits that admission will bind.
        hard_limits = owner.config.run_limits
        if (
            constraints.max_cost_usd is not None
            and constraints.max_cost_usd > hard_limits.max_cost_usd
        ):
            raise ValueError("Graph cost ceiling cannot exceed the global Job cost limit")
        if (
            constraints.max_wall_time_ms is not None
            and constraints.max_wall_time_ms > hard_limits.max_wall_time_ms
        ):
            raise ValueError("Graph time ceiling cannot exceed the global Job time limit")
        registry = SQLiteGraphBlueprintRegistry(graph_registry_path(owner.state_path))
        try:
            control = GraphBlueprintControlService(registry)
            reference = (
                control.revision(blueprint_id, int(raw_version)).ref
                if blueprint_id
                else None
            )
            selection = control.select(
                reference,
                constraints=constraints,
            )
        finally:
            registry.close()
    except (TypeError, ValueError) as exc:
        return (f"Graph controls were not saved · {exc}",)
    selected = selection.blueprint_ref
    reference_label = (
        f"{selected.blueprint_id}@{selected.version}"
        if selected is not None
        else "no Blueprint"
    )
    return (
        f"Future Job Graph defaults saved · {reference_label} · mutation={selection.constraints.mutation_policy.value}",
        "These defaults apply only when a later Work Order is compiled; no active Job, permission, or budget lease changed.",
    )

def execute_graph_command(owner: Any, argument: str) -> ModernTerminalCommandResult:
    """Render or preview a future-Job Graph without changing active authority."""
    preview_prefix = "preview "
    if argument.lower().startswith(preview_prefix):
        goal = argument[len(preview_prefix):].strip()
        if not goal:
            return ModernTerminalCommandResult(
                messages=("Use /graph preview <future Company goal> to bind the selected Blueprint without starting a Job.",)
            )
        registry = SQLiteGraphBlueprintRegistry(graph_registry_path(owner.state_path))
        try:
            control = GraphBlueprintControlService(registry)
            selection = control.selection()
            if selection.blueprint_ref is None:
                return ModernTerminalCommandResult(
                    messages=("No Graph Blueprint is selected. Use `noruct graph select … --confirm`, then return to /graph preview.",)
                )
            preview = owner.ports.graph_preview_for_config(
                replace(owner.config, goal=goal),
                control=control,
                ref=selection.blueprint_ref,
                constraints=selection.constraints,
            )
            rendered = io.StringIO()
            owner.ports.render_graph_control(preview, as_json=False, output=rendered)
            return ModernTerminalCommandResult(
                messages=tuple(
                    line for line in rendered.getvalue().splitlines() if line
                )
            )
        finally:
            registry.close()
    if argument:
        return ModernTerminalCommandResult(
            messages=(
                "Use /graph to open the local Draft/fork/revision/select workbench, or /graph preview <goal> for a no-execution future-Job view. For detailed Blueprint topology changes use `noruct graph list`, `show`, `import`, `fork`, `revise`, `history`, `select`, or `clear`; each mutation requires --confirm.",
                "Same-Employee replicas are edited only as immutable Blueprint revisions. Compare exact observed pairs with `noruct graph replica-evaluate --pair SINGLE_JSON REPLICA_JSON`.",
            )
        )
    return ModernTerminalCommandResult(
        messages=(
            "Future Job Graph controls · choose a reusable Blueprint and bounded local constraints.",
            "Saved preferences affect only a later Work Order; preview before execution with /graph preview <goal>.",
        ),
        open_graph_controls=True,
    )
