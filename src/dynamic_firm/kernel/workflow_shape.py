from __future__ import annotations

import json
from collections import defaultdict
from itertools import permutations, product
from typing import Callable, Iterable, TypeVar


MAX_CANONICAL_WORKFLOW_TASKS = 6

_Task = TypeVar("_Task")
_NodeLabel = tuple[tuple[str, ...], bool]


class WorkflowShapeError(ValueError):
    pass


def canonical_workflow_shape(
    tasks: Iterable[_Task],
    *,
    key_of: Callable[[_Task], str],
    capabilities_of: Callable[[_Task], Iterable[str]],
    dependencies_of: Callable[[_Task], Iterable[str]],
    final_of: Callable[[_Task], bool],
) -> str:
    """Return the exact bounded canonical form of a labeled workflow DAG."""

    materialized = tuple(tasks)
    if not materialized:
        raise WorkflowShapeError("Workflow shape requires at least one task")
    if len(materialized) > MAX_CANONICAL_WORKFLOW_TASKS:
        raise WorkflowShapeError(
            "Workflow shape exceeds the canonical task limit of "
            f"{MAX_CANONICAL_WORKFLOW_TASKS}"
        )

    keys = tuple(key_of(task) for task in materialized)
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        raise WorkflowShapeError("Workflow shape task keys must be non-empty strings")
    if len(keys) != len(set(keys)):
        raise WorkflowShapeError("Workflow shape task keys must be unique")
    index_by_key = {key: index for index, key in enumerate(keys)}

    labels: list[_NodeLabel] = []
    dependencies: list[tuple[int, ...]] = []
    final_indices: list[int] = []
    edges: set[tuple[int, int]] = set()
    for task_index, task in enumerate(materialized):
        raw_capabilities = tuple(capabilities_of(task))
        if (
            not raw_capabilities
            or any(
                not isinstance(item, str) or not item.strip()
                for item in raw_capabilities
            )
            or len(raw_capabilities) != len(set(raw_capabilities))
        ):
            raise WorkflowShapeError(
                "Workflow shape capabilities must be non-empty and unique"
            )
        capabilities = tuple(sorted(raw_capabilities))
        final = bool(final_of(task))
        if final:
            final_indices.append(task_index)
        labels.append((capabilities, final))

        dependency_keys = tuple(dependencies_of(task))
        if any(
            not isinstance(item, str) or not item.strip() for item in dependency_keys
        ):
            raise WorkflowShapeError(
                "Workflow shape dependencies must be non-empty strings"
            )
        if len(dependency_keys) != len(set(dependency_keys)):
            raise WorkflowShapeError("Workflow shape dependencies must be unique")
        dependency_indices: list[int] = []
        for dependency_key in dependency_keys:
            if dependency_key not in index_by_key:
                raise WorkflowShapeError(
                    f"Workflow shape contains unknown dependency {dependency_key}"
                )
            dependency_index = index_by_key[dependency_key]
            if dependency_index == task_index:
                raise WorkflowShapeError("Workflow shape contains a self dependency")
            dependency_indices.append(dependency_index)
            edges.add((dependency_index, task_index))
        dependencies.append(tuple(dependency_indices))

    if len(final_indices) != 1:
        raise WorkflowShapeError("Workflow shape requires exactly one final task")

    indegree = [0] * len(materialized)
    dependents: list[list[int]] = [[] for _ in materialized]
    for dependency_index, task_index in edges:
        indegree[task_index] += 1
        dependents[dependency_index].append(task_index)
    ready = [index for index, degree in enumerate(indegree) if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if visited != len(materialized):
        raise WorkflowShapeError("Workflow shape contains a cycle")

    reaches_final = {final_indices[0]}
    pending = [final_indices[0]]
    while pending:
        current = pending.pop()
        for dependency in dependencies[current]:
            if dependency not in reaches_final:
                reaches_final.add(dependency)
                pending.append(dependency)
    if len(reaches_final) != len(materialized):
        raise WorkflowShapeError("Every workflow shape task must reach the final task")

    partitions: dict[_NodeLabel, list[int]] = defaultdict(list)
    for index, label in enumerate(labels):
        partitions[label].append(index)
    ordered_labels = tuple(sorted(partitions))
    canonical_labels = tuple(
        label
        for label in ordered_labels
        for _ in range(len(partitions[label]))
    )

    best_adjacency: tuple[int, ...] | None = None
    partition_permutations = tuple(
        tuple(permutations(partitions[label])) for label in ordered_labels
    )
    for partition_order in product(*partition_permutations):
        order = tuple(index for group in partition_order for index in group)
        adjacency = tuple(
            int((source, target) in edges)
            for source in order
            for target in order
        )
        if best_adjacency is None or adjacency < best_adjacency:
            best_adjacency = adjacency

    payload = {
        "adjacency": list(best_adjacency or ()),
        "nodes": [
            {"capabilities": list(capabilities), "final": final}
            for capabilities, final in canonical_labels
        ],
        "version": 1,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
