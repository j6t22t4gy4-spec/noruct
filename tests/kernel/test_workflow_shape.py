from __future__ import annotations

import unittest
from dataclasses import dataclass
from itertools import permutations

from dynamic_firm.kernel.workflow_shape import (
    MAX_CANONICAL_WORKFLOW_TASKS,
    WorkflowShapeError,
    canonical_workflow_shape,
)


@dataclass(frozen=True, slots=True)
class ShapeTask:
    key: str
    capabilities: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    final: bool = False


def identity(tasks: tuple[ShapeTask, ...]) -> str:
    return canonical_workflow_shape(
        tasks,
        key_of=lambda task: task.key,
        capabilities_of=lambda task: task.capabilities,
        dependencies_of=lambda task: task.dependencies,
        final_of=lambda task: task.final,
    )


def graph_tasks(node_count: int, edges: set[tuple[int, int]]) -> tuple[ShapeTask, ...]:
    return tuple(
        ShapeTask(
            str(index),
            ("analysis",),
            tuple(str(source) for source, target in edges if target == index),
            final=index == node_count - 1,
        )
        for index in range(node_count)
    )


def every_node_reaches_final(node_count: int, edges: set[tuple[int, int]]) -> bool:
    reaches = {node_count - 1}
    pending = [node_count - 1]
    while pending:
        target = pending.pop()
        for source, edge_target in edges:
            if edge_target == target and source not in reaches:
                reaches.add(source)
                pending.append(source)
    return len(reaches) == node_count


def exact_edge_oracle(
    node_count: int,
    edges: set[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    best: tuple[tuple[int, int], ...] | None = None
    for non_final_order in permutations(range(node_count - 1)):
        order = non_final_order + (node_count - 1,)
        position = {old: new for new, old in enumerate(order)}
        candidate = tuple(
            sorted((position[source], position[target]) for source, target in edges)
        )
        if best is None or candidate < best:
            best = candidate
    return best or ()


class WorkflowShapeTests(unittest.TestCase):
    def test_task_ids_tuple_order_and_capability_order_do_not_change_identity(self) -> None:
        original = (
            ShapeTask("spec", ("repository", "analysis")),
            ShapeTask("tests", ("analysis",)),
            ShapeTask(
                "write",
                ("implementation",),
                dependencies=("spec", "tests"),
                final=True,
            ),
        )
        renamed_and_reordered = (
            ShapeTask(
                "finish",
                ("implementation",),
                dependencies=("case_b", "case_a"),
                final=True,
            ),
            ShapeTask("case_b", ("analysis",)),
            ShapeTask("case_a", ("analysis", "repository")),
        )

        self.assertEqual(identity(original), identity(renamed_and_reordered))

    def test_dependency_and_capability_changes_produce_different_identities(self) -> None:
        fan_in = graph_tasks(3, {(0, 2), (1, 2)})
        chain = graph_tasks(3, {(0, 1), (1, 2)})
        changed_capability = (
            fan_in[0],
            ShapeTask("1", ("review",)),
            fan_in[2],
        )

        self.assertNotEqual(identity(fan_in), identity(chain))
        self.assertNotEqual(identity(fan_in), identity(changed_capability))

    def test_exact_form_separates_the_five_task_recursive_signature_collision(self) -> None:
        first_edges = {(0, 3), (0, 4), (1, 2), (2, 3), (3, 4)}
        second_edges = {(0, 1), (0, 4), (1, 3), (2, 3), (3, 4)}
        first = graph_tasks(5, first_edges)
        second = graph_tasks(5, second_edges)

        self.assertNotEqual(
            exact_edge_oracle(5, first_edges),
            exact_edge_oracle(5, second_edges),
        )
        self.assertNotEqual(identity(first), identity(second))

    def test_invalid_or_unbounded_graph_is_rejected(self) -> None:
        with self.assertRaisesRegex(WorkflowShapeError, "exactly one final"):
            identity((ShapeTask("a", ("analysis",)),))
        with self.assertRaisesRegex(WorkflowShapeError, "unknown dependency"):
            identity((ShapeTask("final", ("analysis",), ("missing",), True),))
        with self.assertRaisesRegex(WorkflowShapeError, "contains a cycle"):
            identity(
                (
                    ShapeTask("a", ("analysis",), ("b",)),
                    ShapeTask("b", ("analysis",), ("a",), True),
                )
            )
        with self.assertRaisesRegex(WorkflowShapeError, "canonical task limit"):
            identity(
                tuple(
                    ShapeTask(
                        str(index),
                        ("analysis",),
                        () if index == 0 else (str(index - 1),),
                        final=index == MAX_CANONICAL_WORKFLOW_TASKS,
                    )
                    for index in range(MAX_CANONICAL_WORKFLOW_TASKS + 1)
                )
            )

    def test_all_topologically_labeled_valid_dags_through_six_tasks_match_exact_oracle(self) -> None:
        for node_count in range(1, MAX_CANONICAL_WORKFLOW_TASKS + 1):
            possible_edges = tuple(
                (source, target)
                for target in range(1, node_count)
                for source in range(target)
            )
            oracle_by_identity: dict[str, tuple[tuple[int, int], ...]] = {}
            identity_by_oracle: dict[tuple[tuple[int, int], ...], str] = {}
            for mask in range(1 << len(possible_edges)):
                edges = {
                    edge
                    for bit, edge in enumerate(possible_edges)
                    if mask & (1 << bit)
                }
                if not every_node_reaches_final(node_count, edges):
                    continue
                shape = identity(graph_tasks(node_count, edges))
                oracle = exact_edge_oracle(node_count, edges)
                self.assertEqual(oracle_by_identity.setdefault(shape, oracle), oracle)
                self.assertEqual(identity_by_oracle.setdefault(oracle, shape), shape)


if __name__ == "__main__":
    unittest.main()
