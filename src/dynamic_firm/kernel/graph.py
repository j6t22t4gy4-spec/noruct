"""Executable work-structure operations for one managed Company Job.

This module engineers task dependencies, readiness, and bounded rewrites.  Its
``graph`` is neither a visual/UI graph nor a Knowledge Runtime entity graph.
"""

from __future__ import annotations

from dataclasses import replace

from .models import (
    ExecutionReplicaAggregation,
    ExecutionReplicaStrategy,
    GraphPatch,
    JobGraph,
    JobTask,
    PatchOperationKind,
    PlanProposal,
    SemanticOperation,
    TaskStatus,
)


class GraphValidationError(ValueError):
    pass


def graph_from_proposal(proposal: PlanProposal, *, max_tasks: int) -> JobGraph:
    graph = JobGraph(version=1, tasks=proposal.tasks, final_task_id=proposal.final_task_id)
    validate_graph(graph, max_tasks=max_tasks, initial=True)
    return graph


def validate_graph(graph: JobGraph, *, max_tasks: int, initial: bool = False) -> None:
    if graph.version < 1:
        raise GraphValidationError("Graph version must be positive")
    if not graph.tasks:
        raise GraphValidationError("Graph must contain at least one task")
    if len(graph.tasks) > max_tasks:
        raise GraphValidationError(f"Graph exceeds the task limit of {max_tasks}")
    task_ids = [task.task_id for task in graph.tasks]
    if any(not task_id.strip() for task_id in task_ids):
        raise GraphValidationError("Task ids must be non-empty")
    if len(task_ids) != len(set(task_ids)):
        raise GraphValidationError("Task ids must be unique")
    tasks = {task.task_id: task for task in graph.tasks}
    if graph.final_task_id not in tasks:
        raise GraphValidationError("Final task does not exist")
    if tasks[graph.final_task_id].status == TaskStatus.CANCELLED:
        raise GraphValidationError("Final task cannot be cancelled")

    indegree = {task_id: 0 for task_id in tasks}
    dependents: dict[str, list[str]] = {task_id: [] for task_id in tasks}
    for task in graph.tasks:
        if not task.objective.strip():
            raise GraphValidationError(f"Task {task.task_id} has no objective")
        if not task.required_capabilities:
            raise GraphValidationError(f"Task {task.task_id} has no required capability")
        if any(not item.strip() for item in task.required_capabilities):
            raise GraphValidationError(f"Task {task.task_id} has an empty required capability")
        if len(task.required_capabilities) != len(set(task.required_capabilities)):
            raise GraphValidationError(f"Task {task.task_id} has duplicate required capabilities")
        if not task.acceptance_criteria:
            raise GraphValidationError(f"Task {task.task_id} has no acceptance criteria")
        if any(not item.strip() for item in task.acceptance_criteria):
            raise GraphValidationError(f"Task {task.task_id} has an empty acceptance criterion")
        if task.attempt < 1:
            raise GraphValidationError(f"Task {task.task_id} has an invalid attempt")
        if len(task.depends_on) != len(set(task.depends_on)):
            raise GraphValidationError(f"Task {task.task_id} has duplicate dependencies")
        if task.task_id in task.depends_on:
            raise GraphValidationError(f"Task {task.task_id} depends on itself")
        if initial and (
            task.status != TaskStatus.PENDING
            or task.assignee_id is not None
            or task.runtime_result is not None
        ):
            raise GraphValidationError("Initial proposal tasks must be unassigned and pending")
        for dependency_id in task.depends_on:
            if dependency_id not in tasks:
                raise GraphValidationError(
                    f"Task {task.task_id} has unknown dependency {dependency_id}"
                )
            indegree[task.task_id] += 1
            dependents[dependency_id].append(task.task_id)

    ready = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        task_id = ready.pop(0)
        visited += 1
        for dependent_id in sorted(dependents[task_id]):
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                ready.append(dependent_id)
                ready.sort()
    if visited != len(tasks):
        raise GraphValidationError("Graph contains a dependency cycle")

    reaches_final = {graph.final_task_id}
    pending = [graph.final_task_id]
    while pending:
        task_id = pending.pop()
        for dependency_id in tasks[task_id].depends_on:
            if dependency_id not in reaches_final:
                reaches_final.add(dependency_id)
                pending.append(dependency_id)
    # A cancelled branch is retained for ACTIVE JOB audit, but is no longer
    # required to contribute to the final task. All non-cancelled work must
    # still be connected so a rewrite cannot silently add dead work.
    disconnected = sorted(
        task_id
        for task_id, task in tasks.items()
        if task.status != TaskStatus.CANCELLED and task_id not in reaches_final
    )
    if disconnected:
        raise GraphValidationError(
            "Every task must contribute to the final task; disconnected tasks: "
            + ", ".join(disconnected)
        )
    _validate_execution_replicas(graph)


def _validate_execution_replicas(graph: JobGraph) -> None:
    """Validate a complete value contract before any Employee is dispatched."""

    tasks = task_map(graph)
    groups: dict[str, list[JobTask]] = {}
    for task in graph.tasks:
        spec = task.execution_replica
        if spec is None:
            continue
        values = (
            spec.group_id,
            spec.replica_id,
            spec.scope,
            spec.aggregation_task_id,
            spec.marginal_value_reason,
        )
        if any(not value.strip() for value in values):
            raise GraphValidationError(
                f"Execution replica {task.task_id} has incomplete value metadata"
            )
        if task.task_id == graph.final_task_id:
            raise GraphValidationError("Execution replica cannot be the final task")
        if spec.aggregation_task_id == task.task_id:
            raise GraphValidationError("Execution replica cannot aggregate itself")
        groups.setdefault(spec.group_id, []).append(task)

    for group_id, members in sorted(groups.items()):
        if not 2 <= len(members) <= 4:
            raise GraphValidationError(
                f"Execution replica group {group_id} must contain between 2 and 4 members"
            )
        specs = tuple(task.execution_replica for task in members)
        assert all(spec is not None for spec in specs)
        first = specs[0]
        assert first is not None
        if len({spec.replica_id for spec in specs if spec is not None}) != len(members):
            raise GraphValidationError(
                f"Execution replica group {group_id} has duplicate replica ids"
            )
        if any(
            spec is None
            or spec.strategy != first.strategy
            or spec.aggregation != first.aggregation
            or spec.aggregation_task_id != first.aggregation_task_id
            or spec.marginal_value_reason != first.marginal_value_reason
            for spec in specs
        ):
            raise GraphValidationError(
                f"Execution replica group {group_id} has inconsistent value metadata"
            )
        aggregation = tasks.get(first.aggregation_task_id)
        if aggregation is None or aggregation.execution_replica is not None:
            raise GraphValidationError(
                f"Execution replica group {group_id} requires a separate aggregation task"
            )
        member_ids = {task.task_id for task in members}
        if not member_ids.issubset(set(aggregation.depends_on)):
            raise GraphValidationError(
                f"Execution replica aggregation {aggregation.task_id} must directly depend on every member"
            )
        if any(member_ids & set(task.depends_on) for task in members):
            raise GraphValidationError(
                f"Execution replica group {group_id} members cannot depend on each other"
            )
        if len({task.depends_on for task in members}) != 1:
            raise GraphValidationError(
                f"Execution replica group {group_id} must share upstream dependencies"
            )
        if len({task.required_capabilities for task in members}) != 1:
            raise GraphValidationError(
                f"Execution replica group {group_id} must share required capabilities"
            )
        scopes = tuple(spec.scope for spec in specs if spec is not None)
        if first.strategy is ExecutionReplicaStrategy.CANDIDATE:
            if len(set(scopes)) != 1:
                raise GraphValidationError(
                    f"CANDIDATE replica group {group_id} must use one shared scope"
                )
            if first.aggregation is not ExecutionReplicaAggregation.VALIDATOR_SELECT:
                raise GraphValidationError(
                    f"CANDIDATE replica group {group_id} requires VALIDATOR_SELECT"
                )
            if not any(
                capability in {"validation", "verification", "review", "independent_review"}
                or capability.endswith("_review")
                for capability in aggregation.required_capabilities
            ):
                raise GraphValidationError(
                    f"CANDIDATE replica aggregation {aggregation.task_id} requires a validator capability"
                )
        else:
            if len(set(scopes)) != len(scopes):
                raise GraphValidationError(
                    f"{first.strategy.value} replica group {group_id} requires distinct scopes"
                )
            allowed = (
                {ExecutionReplicaAggregation.JOIN}
                if first.strategy is ExecutionReplicaStrategy.PARTITION
                else {
                    ExecutionReplicaAggregation.JOIN,
                    ExecutionReplicaAggregation.MANAGER_SYNTHESIS,
                }
            )
            if first.aggregation not in allowed:
                raise GraphValidationError(
                    f"{first.strategy.value} replica group {group_id} has an invalid aggregation method"
                )


def task_map(graph: JobGraph) -> dict[str, JobTask]:
    return {task.task_id: task for task in graph.tasks}


def replace_task(graph: JobGraph, updated: JobTask) -> JobGraph:
    return replace(
        graph,
        tasks=tuple(updated if task.task_id == updated.task_id else task for task in graph.tasks),
    )


def ready_tasks(graph: JobGraph) -> tuple[JobTask, ...]:
    tasks = task_map(graph)
    return tuple(
        task
        for task in sorted(graph.tasks, key=lambda item: item.task_id)
        if task.status == TaskStatus.PENDING
        and all(tasks[dependency].status == TaskStatus.SUCCEEDED for dependency in task.depends_on)
    )


def apply_patch(graph: JobGraph, patch: GraphPatch, *, max_tasks: int) -> JobGraph:
    if patch.base_graph_version != graph.version:
        raise GraphValidationError(
            f"Patch targets graph v{patch.base_graph_version}, current graph is v{graph.version}"
        )
    if not patch.operations:
        raise GraphValidationError("Patch must contain at least one operation")

    tasks = task_map(graph)
    added_task_ids: set[str] = set()
    if not patch.patch_id.strip() or not patch.rationale.strip() or not patch.expected_gain.strip():
        raise GraphValidationError("Patch requires id, rationale, and expected gain")
    if (
        len(patch.semantic_evidence_refs) > 8
        or len(patch.semantic_evidence_refs) != len(set(patch.semantic_evidence_refs))
        or any(
            not ref.strip()
            or len(ref.encode("utf-8")) > 160
            or any(ord(character) < 32 or ord(character) == 127 for character in ref)
            for ref in patch.semantic_evidence_refs
        )
    ):
        raise GraphValidationError("Patch semantic evidence references are invalid")
    if patch.trigger_task_id not in tasks:
        raise GraphValidationError(f"Patch trigger task does not exist: {patch.trigger_task_id}")
    final_task_id = graph.final_task_id
    for operation in patch.operations:
        if operation.kind == PatchOperationKind.ADD_TASK:
            task = operation.task
            if task is None:
                raise GraphValidationError("ADD_TASK requires a task")
            if task.task_id in tasks:
                raise GraphValidationError(f"Task already exists: {task.task_id}")
            if (
                task.status != TaskStatus.PENDING
                or task.assignee_id is not None
                or task.runtime_result is not None
            ):
                raise GraphValidationError("Added tasks must be unassigned and pending")
            tasks[task.task_id] = task
            added_task_ids.add(task.task_id)
            continue

        if operation.kind == PatchOperationKind.SET_FINAL_TASK:
            if operation.task_id not in tasks:
                raise GraphValidationError(
                    f"Patch references unknown final task {operation.task_id}"
                )
            if operation.task_id not in added_task_ids:
                raise GraphValidationError(
                    "New final task must be added by the same atomic patch"
                )
            if tasks[operation.task_id].status != TaskStatus.PENDING:
                raise GraphValidationError("New final task must be pending")
            final_task_id = operation.task_id
            continue

        task = tasks.get(operation.task_id)
        if task is None:
            raise GraphValidationError(f"Patch references unknown task {operation.task_id}")
        if operation.kind == PatchOperationKind.CANCEL_TASK:
            # A bounded semantic recovery may retire exactly its own failed
            # trigger after preserving its durable attempt record and moving
            # final ownership to a newly added replacement.  Without this
            # narrow exception a VALIDATION_FAILED signal can be observed but
            # can never recover: the failed trigger remains structurally
            # required by the old final task forever.  No other completed or
            # failed work may be hidden by a cancellation.
            failed_trigger_replacement = (
                task.status == TaskStatus.FAILED
                and task.task_id == patch.trigger_task_id
                and patch.semantic_operation == SemanticOperation.INSERT
                and final_task_id != task.task_id
            )
            if task.status != TaskStatus.PENDING and not failed_trigger_replacement:
                raise GraphValidationError("Only pending tasks can be cancelled")
            tasks[task.task_id] = replace(task, status=TaskStatus.CANCELLED)
            continue
        if task.status != TaskStatus.PENDING:
            raise GraphValidationError("Dependencies can change only on pending tasks")
        if operation.kind == PatchOperationKind.REPLACE_DEPENDENCIES:
            dependencies = tuple(sorted(set(operation.dependencies)))
            if not dependencies:
                raise GraphValidationError("Replacement dependencies must be non-empty")
            if task.task_id in dependencies:
                raise GraphValidationError("Task cannot depend on itself")
            unknown = sorted(set(dependencies) - set(tasks))
            if unknown:
                raise GraphValidationError(
                    "Patch references unknown replacement dependency "
                    + ", ".join(unknown)
                )
            tasks[task.task_id] = replace(task, depends_on=dependencies)
            continue
        if operation.dependency_id not in tasks:
            raise GraphValidationError(
                f"Patch references unknown dependency {operation.dependency_id}"
            )
        dependencies = list(task.depends_on)
        if operation.kind == PatchOperationKind.ADD_DEPENDENCY:
            if operation.dependency_id in dependencies:
                raise GraphValidationError("Dependency already exists")
            dependencies.append(operation.dependency_id)
        elif operation.kind == PatchOperationKind.REMOVE_DEPENDENCY:
            if operation.dependency_id not in dependencies:
                raise GraphValidationError("Dependency does not exist")
            dependencies.remove(operation.dependency_id)
        else:
            raise GraphValidationError(f"Unsupported patch operation: {operation.kind}")
        tasks[task.task_id] = replace(task, depends_on=tuple(sorted(dependencies)))

    candidate = JobGraph(
        version=graph.version + 1,
        tasks=tuple(tasks[task_id] for task_id in sorted(tasks)),
        final_task_id=final_task_id,
    )
    validate_graph(candidate, max_tasks=max_tasks)
    _validate_semantic_operation(graph, candidate, patch)
    return candidate


def _validate_semantic_operation(
    before: JobGraph,
    after: JobGraph,
    patch: GraphPatch,
) -> None:
    """Keep named graph rewrites narrow, auditable, and mechanically true.

    Generic INSERT patches retain their existing primitive behavior. Named
    rewrites are stricter because they are user-visible Company
    mutations: a label such as ``MERGE`` must describe the actual atomic graph
    rewrite rather than merely annotate an arbitrary patch.
    """

    before_tasks = task_map(before)
    after_tasks = task_map(after)
    added_ids = tuple(sorted(set(after_tasks) - set(before_tasks)))
    cancelled_ids = tuple(
        sorted(
            task_id
            for task_id, task in before_tasks.items()
            if task.status == TaskStatus.PENDING
            and after_tasks[task_id].status == TaskStatus.CANCELLED
        )
    )

    if patch.semantic_operation == SemanticOperation.SPLIT:
        if len(added_ids) < 2 or cancelled_ids:
            raise GraphValidationError(
                "SPLIT must add at least two tasks and cannot cancel existing work"
            )
        missing_trigger = [
            task_id
            for task_id in added_ids
            if patch.trigger_task_id not in after_tasks[task_id].depends_on
        ]
        if missing_trigger:
            raise GraphValidationError(
                "Every SPLIT task must depend on the completed trigger: "
                + ", ".join(missing_trigger)
            )
        return

    if patch.semantic_operation == SemanticOperation.JOIN:
        if len(added_ids) != 1 or cancelled_ids:
            raise GraphValidationError(
                "JOIN must add exactly one task and cannot cancel existing work"
            )
        joined = after_tasks[added_ids[0]]
        if (
            len(joined.depends_on) < 2
            or patch.trigger_task_id not in joined.depends_on
        ):
            raise GraphValidationError(
                "JOIN task must combine the trigger with at least one other dependency"
            )
        return

    if patch.semantic_operation == SemanticOperation.CANCEL:
        if len(cancelled_ids) != 1 or added_ids:
            raise GraphValidationError(
                "CANCEL must cancel exactly one pending task and cannot add work"
            )
        cancelled_id = cancelled_ids[0]
        if cancelled_id == before.final_task_id:
            raise GraphValidationError("CANCEL cannot cancel the current final task")
        # A cancellation may only remove a pending branch. Any direct
        # dependent must be explicitly rewired in the same patch; otherwise
        # ready-set scheduling could wait forever on a cancelled input.
        for task_id, before_task in before_tasks.items():
            if cancelled_id not in before_task.depends_on:
                continue
            after_dependencies = after_tasks[task_id].depends_on
            expected = tuple(
                dependency
                for dependency in before_task.depends_on
                if dependency != cancelled_id
            )
            if after_dependencies != expected:
                raise GraphValidationError(
                    "CANCEL must atomically remove the cancelled task from every dependent"
                )
        return

    if patch.semantic_operation != SemanticOperation.MERGE:
        return

    if len(cancelled_ids) < 2 or len(added_ids) != 1:
        raise GraphValidationError(
            "MERGE must replace at least two pending tasks with exactly one task"
        )
    if before.final_task_id in cancelled_ids:
        raise GraphValidationError("MERGE cannot cancel the current final task")
    sources = tuple(before_tasks[task_id] for task_id in cancelled_ids)
    source_dependencies = sources[0].depends_on
    if any(task.depends_on != source_dependencies for task in sources[1:]):
        raise GraphValidationError("MERGE sources must be pending sibling tasks")
    merged = after_tasks[added_ids[0]]
    if merged.depends_on != source_dependencies:
        raise GraphValidationError(
            "MERGE replacement dependencies must exactly preserve the sibling dependencies"
        )
    required_capabilities = tuple(
        sorted({item for source in sources for item in source.required_capabilities})
    )
    if merged.required_capabilities != required_capabilities:
        raise GraphValidationError(
            "MERGE replacement capabilities must be the exact union of the sources"
        )
    acceptance_criteria = tuple(
        sorted({item for source in sources for item in source.acceptance_criteria})
    )
    if merged.acceptance_criteria != acceptance_criteria:
        raise GraphValidationError(
            "MERGE replacement acceptance criteria must be the exact union of the sources"
        )
    for task_id, before_task in before_tasks.items():
        if task_id in cancelled_ids:
            continue
        replaced_sources = set(before_task.depends_on) & set(cancelled_ids)
        if not replaced_sources:
            continue
        after_dependencies = after_tasks[task_id].depends_on
        expected = tuple(
            sorted((set(before_task.depends_on) - set(cancelled_ids)) | {merged.task_id})
        )
        if after_dependencies != expected:
            raise GraphValidationError(
                "MERGE must atomically rewire every dependent to the replacement task"
            )
