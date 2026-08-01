from __future__ import annotations

import unittest
from dataclasses import replace

from dynamic_firm.kernel.graph import GraphValidationError, apply_patch, graph_from_proposal
from dynamic_firm.kernel.models import (
    ExecutionReplicaAggregation,
    ExecutionReplicaSpec,
    ExecutionReplicaStrategy,
    GraphPatch,
    GraphPatchOperation,
    JobTask,
    PatchOperationKind,
    PlanProposal,
    SemanticOperation,
)
from tests.kernel.helpers import task


class GraphContractTests(unittest.TestCase):
    @staticmethod
    def replica_task(
        task_id: str,
        *,
        replica_id: str,
        scope: str,
        strategy: ExecutionReplicaStrategy = ExecutionReplicaStrategy.PARTITION,
        aggregation: ExecutionReplicaAggregation = ExecutionReplicaAggregation.JOIN,
    ) -> JobTask:
        return replace(
            task(task_id),
            execution_replica=ExecutionReplicaSpec(
                group_id="wide_analysis",
                replica_id=replica_id,
                strategy=strategy,
                scope=scope,
                aggregation_task_id="final",
                aggregation=aggregation,
                marginal_value_reason="Bounded fan-out increases coverage before one integration.",
            ),
        )

    def test_partition_replica_group_requires_explicit_disjoint_scopes_and_join(self) -> None:
        proposal = PlanProposal(
            proposal_id="partition-replicas",
            goal="cover two disjoint source ranges",
            tasks=(
                self.replica_task("range_a", replica_id="a", scope="sources 1-50"),
                self.replica_task("range_b", replica_id="b", scope="sources 51-100"),
                task("final", depends_on=("range_a", "range_b")),
            ),
            final_task_id="final",
        )

        graph = graph_from_proposal(proposal, max_tasks=4)

        self.assertEqual(len(graph.tasks), 3)

        duplicate_scope = replace(
            proposal,
            tasks=(
                self.replica_task("range_a", replica_id="a", scope="same range"),
                self.replica_task("range_b", replica_id="b", scope="same range"),
                task("final", depends_on=("range_a", "range_b")),
            ),
        )
        with self.assertRaisesRegex(GraphValidationError, "requires distinct scopes"):
            graph_from_proposal(duplicate_scope, max_tasks=4)

    def test_candidate_replicas_require_same_scope_and_validator_selection(self) -> None:
        proposal = PlanProposal(
            proposal_id="candidate-replicas",
            goal="select the strongest candidate",
            tasks=(
                self.replica_task(
                    "candidate_a",
                    replica_id="a",
                    scope="pricing proposal",
                    strategy=ExecutionReplicaStrategy.CANDIDATE,
                    aggregation=ExecutionReplicaAggregation.VALIDATOR_SELECT,
                ),
                self.replica_task(
                    "candidate_b",
                    replica_id="b",
                    scope="pricing proposal",
                    strategy=ExecutionReplicaStrategy.CANDIDATE,
                    aggregation=ExecutionReplicaAggregation.VALIDATOR_SELECT,
                ),
                task(
                    "final",
                    depends_on=("candidate_a", "candidate_b"),
                    capabilities=("validation",),
                ),
            ),
            final_task_id="final",
        )

        graph_from_proposal(proposal, max_tasks=4)

        invalid = replace(
            proposal,
            tasks=proposal.tasks[:-1]
            + (
                task(
                    "final",
                    depends_on=("candidate_a", "candidate_b"),
                    capabilities=("analysis",),
                ),
            ),
        )
        with self.assertRaisesRegex(GraphValidationError, "validator capability"):
            graph_from_proposal(invalid, max_tasks=4)

    def test_replica_group_rejects_missing_member_and_aggregation_edge(self) -> None:
        single = PlanProposal(
            proposal_id="single-replica",
            goal="reject performative fan-out",
            tasks=(
                self.replica_task("range_a", replica_id="a", scope="sources 1-50"),
                task("final", depends_on=("range_a",)),
            ),
            final_task_id="final",
        )
        with self.assertRaisesRegex(GraphValidationError, "between 2 and 4"):
            graph_from_proposal(single, max_tasks=3)

        missing_edge = PlanProposal(
            proposal_id="missing-aggregation-edge",
            goal="reject partial aggregation",
            tasks=(
                self.replica_task("range_a", replica_id="a", scope="sources 1-50"),
                self.replica_task("range_b", replica_id="b", scope="sources 51-100"),
                task("bridge", depends_on=("range_b",)),
                task("final", depends_on=("range_a", "bridge")),
            ),
            final_task_id="final",
        )
        with self.assertRaisesRegex(GraphValidationError, "directly depend on every member"):
            graph_from_proposal(missing_edge, max_tasks=4)

    def test_cycle_is_rejected_by_deterministic_topological_validation(self) -> None:
        proposal = PlanProposal(
            proposal_id="cycle",
            goal="reject cycle",
            tasks=(
                task("a", depends_on=("b",)),
                task("b", depends_on=("a",)),
            ),
            final_task_id="b",
        )
        with self.assertRaisesRegex(GraphValidationError, "cycle"):
            graph_from_proposal(proposal, max_tasks=2)

    def test_patch_is_atomic_and_rejects_stale_version(self) -> None:
        proposal = PlanProposal(
            proposal_id="base",
            goal="base",
            tasks=(task("a"), task("final", depends_on=("a",))),
            final_task_id="final",
        )
        graph = graph_from_proposal(proposal, max_tasks=3)
        stale = GraphPatch(
            patch_id="stale",
            base_graph_version=2,
            trigger_task_id="a",
            semantic_operation=SemanticOperation.INSERT,
            rationale="test",
            expected_gain="none",
            operations=(
                GraphPatchOperation(PatchOperationKind.ADD_TASK, task=task("new")),
            ),
        )
        with self.assertRaisesRegex(GraphValidationError, "current graph is v1"):
            apply_patch(graph, stale, max_tasks=3)
        self.assertEqual(tuple(item.task_id for item in graph.tasks), ("a", "final"))
        self.assertEqual(graph.version, 1)

    def test_patch_cycle_and_task_limit_are_rejected_without_mutation(self) -> None:
        proposal = PlanProposal(
            proposal_id="base",
            goal="base",
            tasks=(task("a"), task("final", depends_on=("a",))),
            final_task_id="final",
        )
        graph = graph_from_proposal(proposal, max_tasks=3)
        cycle = GraphPatch(
            patch_id="cycle",
            base_graph_version=1,
            trigger_task_id="a",
            semantic_operation=SemanticOperation.JOIN,
            rationale="test cycle",
            expected_gain="none",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="a",
                    dependency_id="final",
                ),
            ),
        )
        with self.assertRaisesRegex(GraphValidationError, "cycle"):
            apply_patch(graph, cycle, max_tasks=3)

        extra = GraphPatch(
            patch_id="extra",
            base_graph_version=1,
            trigger_task_id="a",
            semantic_operation=SemanticOperation.INSERT,
            rationale="test limit",
            expected_gain="none",
            operations=(
                GraphPatchOperation(PatchOperationKind.ADD_TASK, task=task("extra")),
            ),
        )
        with self.assertRaisesRegex(GraphValidationError, "task limit"):
            apply_patch(graph, extra, max_tasks=2)
        self.assertEqual(graph.version, 1)

    def test_patch_requires_an_existing_typed_trigger(self) -> None:
        proposal = PlanProposal(
            proposal_id="base",
            goal="base",
            tasks=(task("final"),),
            final_task_id="final",
        )
        graph = graph_from_proposal(proposal, max_tasks=2)
        patch = GraphPatch(
            patch_id="unknown-trigger",
            base_graph_version=1,
            trigger_task_id="missing",
            semantic_operation=SemanticOperation.INSERT,
            rationale="typed trigger contract",
            expected_gain="reject an unauditable mutation",
            operations=(
                GraphPatchOperation(PatchOperationKind.ADD_TASK, task=task("extra")),
            ),
        )
        with self.assertRaisesRegex(GraphValidationError, "trigger task"):
            apply_patch(graph, patch, max_tasks=2)

    def test_disconnected_work_is_rejected(self) -> None:
        proposal = PlanProposal(
            proposal_id="disconnected",
            goal="reject work that cannot affect the result",
            tasks=(
                task("useful"),
                task("unused"),
                task("final", depends_on=("useful",)),
            ),
            final_task_id="final",
        )
        with self.assertRaisesRegex(GraphValidationError, "disconnected tasks: unused"):
            graph_from_proposal(proposal, max_tasks=3)

    def test_split_adds_independent_follow_up_work_after_the_trigger(self) -> None:
        proposal = PlanProposal(
            proposal_id="split-base",
            goal="split",
            tasks=(task("trigger"), task("final", depends_on=("trigger",))),
            final_task_id="final",
        )
        graph = graph_from_proposal(proposal, max_tasks=5)
        patch = GraphPatch(
            patch_id="split-evidence",
            base_graph_version=1,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.SPLIT,
            rationale="The completed discovery step exposed two independent evidence lanes.",
            expected_gain="Collect independent evidence concurrently before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task("security", depends_on=("trigger",), capabilities=("security",)),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task("pricing", depends_on=("trigger",), capabilities=("pricing",)),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="security",
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="pricing",
                ),
            ),
        )

        rewritten = apply_patch(graph, patch, max_tasks=5)

        self.assertEqual(rewritten.version, 2)
        self.assertEqual(
            next(item for item in rewritten.tasks if item.task_id == "final").depends_on,
            ("pricing", "security", "trigger"),
        )

    def test_join_requires_a_new_task_that_combines_the_trigger_and_another_input(self) -> None:
        proposal = PlanProposal(
            proposal_id="join-base",
            goal="join",
            tasks=(
                task("trigger"),
                task("other"),
                task("final", depends_on=("trigger", "other")),
            ),
            final_task_id="final",
        )
        graph = graph_from_proposal(proposal, max_tasks=4)
        patch = GraphPatch(
            patch_id="join-evidence",
            base_graph_version=1,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.JOIN,
            rationale="The two evidence lanes need one explicit reconciliation step.",
            expected_gain="Keep reconciliation separate from final integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task("reconcile", depends_on=("trigger", "other"), capabilities=("analysis",)),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="reconcile",
                ),
            ),
        )

        rewritten = apply_patch(graph, patch, max_tasks=4)

        self.assertEqual(
            next(item for item in rewritten.tasks if item.task_id == "reconcile").depends_on,
            ("trigger", "other"),
        )

    def test_merge_replaces_pending_sibling_work_and_rewires_all_dependents(self) -> None:
        proposal = PlanProposal(
            proposal_id="merge-base",
            goal="merge",
            tasks=(
                task("trigger"),
                JobTask(
                    task_id="alpha",
                    objective="Analyze alpha",
                    depends_on=("trigger",),
                    required_capabilities=("alpha",),
                    acceptance_criteria=("Alpha evidence",),
                ),
                JobTask(
                    task_id="beta",
                    objective="Analyze beta",
                    depends_on=("trigger",),
                    required_capabilities=("beta",),
                    acceptance_criteria=("Beta evidence",),
                ),
                task("final", depends_on=("alpha", "beta"), capabilities=("analysis",)),
            ),
            final_task_id="final",
        )
        graph = graph_from_proposal(proposal, max_tasks=5)
        patch = GraphPatch(
            patch_id="merge-siblings",
            base_graph_version=1,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.MERGE,
            rationale="The pending sibling analyses share one bounded evidence source.",
            expected_gain="Avoid duplicate model and tool work without dropping acceptance criteria.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=JobTask(
                        task_id="combined",
                        objective="Analyze the combined bounded evidence",
                        depends_on=("trigger",),
                        required_capabilities=("alpha", "beta"),
                        acceptance_criteria=("Alpha evidence", "Beta evidence"),
                    ),
                ),
                GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id="alpha"),
                GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id="beta"),
                GraphPatchOperation(
                    PatchOperationKind.REPLACE_DEPENDENCIES,
                    task_id="final",
                    dependencies=("combined",),
                ),
            ),
        )

        rewritten = apply_patch(graph, patch, max_tasks=5)

        states = {item.task_id: item for item in rewritten.tasks}
        self.assertEqual(states["alpha"].status.value, "CANCELLED")
        self.assertEqual(states["beta"].status.value, "CANCELLED")
        self.assertEqual(states["final"].depends_on, ("combined",))

    def test_merge_rejects_an_unrewired_dependent(self) -> None:
        proposal = PlanProposal(
            proposal_id="merge-invalid",
            goal="merge invalid",
            tasks=(
                task("trigger"),
                task("alpha", depends_on=("trigger",), capabilities=("alpha",)),
                task("beta", depends_on=("trigger",), capabilities=("beta",)),
                task("final", depends_on=("alpha", "beta")),
            ),
            final_task_id="final",
        )
        graph = graph_from_proposal(proposal, max_tasks=5)
        patch = GraphPatch(
            patch_id="bad-merge",
            base_graph_version=1,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.MERGE,
            rationale="test",
            expected_gain="test",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=JobTask(
                        task_id="combined",
                        objective="Combined",
                        depends_on=("trigger",),
                        required_capabilities=("alpha", "beta"),
                        acceptance_criteria=("Evidence for alpha", "Evidence for beta"),
                    ),
                ),
                GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id="alpha"),
                GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id="beta"),
            ),
        )

        with self.assertRaisesRegex(GraphValidationError, "disconnected tasks: combined"):
            apply_patch(graph, patch, max_tasks=5)

    def test_cancel_requires_one_pending_branch_and_atomic_dependent_rewire(self) -> None:
        proposal = PlanProposal(
            proposal_id="cancel",
            goal="cancel optional work",
            tasks=(
                task("trigger"),
                task("optional", depends_on=("trigger",)),
                task("final", depends_on=("trigger", "optional")),
            ),
            final_task_id="final",
        )
        graph = graph_from_proposal(proposal, max_tasks=6)
        patch = GraphPatch(
            patch_id="cancel-optional",
            base_graph_version=1,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.CANCEL,
            rationale="The optional branch is not needed after the trigger evidence.",
            expected_gain="Avoid an unnecessary pending task.",
            operations=(
                GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id="optional"),
                GraphPatchOperation(
                    PatchOperationKind.REMOVE_DEPENDENCY,
                    task_id="final",
                    dependency_id="optional",
                ),
            ),
        )

        rewritten = apply_patch(graph, patch, max_tasks=6)

        tasks = {item.task_id: item for item in rewritten.tasks}
        self.assertEqual(tasks["optional"].status.value, "CANCELLED")
        self.assertEqual(tasks["final"].depends_on, ("trigger",))

    def test_cancel_rejects_an_unrewired_dependent(self) -> None:
        proposal = PlanProposal(
            proposal_id="cancel-invalid",
            goal="cancel optional work",
            tasks=(
                task("trigger"),
                task("optional", depends_on=("trigger",)),
                task("final", depends_on=("trigger", "optional")),
            ),
            final_task_id="final",
        )
        graph = graph_from_proposal(proposal, max_tasks=6)
        patch = GraphPatch(
            patch_id="cancel-unrewired",
            base_graph_version=1,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.CANCEL,
            rationale="This must not leave final waiting on cancelled work.",
            expected_gain="Reject an unsafe cancellation.",
            operations=(
                GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id="optional"),
            ),
        )

        with self.assertRaisesRegex(GraphValidationError, "CANCEL must atomically remove"):
            apply_patch(graph, patch, max_tasks=6)


if __name__ == "__main__":
    unittest.main()
