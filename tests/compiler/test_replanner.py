from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from dynamic_firm.compiler import (
    CapabilityInsertReplanner,
    CompilerExecutionProfile,
    SemanticSignalReplanner,
    WorkflowPrior,
    WorkflowPriorTask,
)
from dynamic_firm.compiler.replanner import ManagerFollowUpReplanner
from dynamic_firm.kernel.graph import apply_patch, graph_from_proposal
from dynamic_firm.kernel.models import (
    EmployeeRecord,
    JobLimits,
    ReplanContext,
    TaskStatus,
)
from dynamic_firm.runtime.models import (
    EmployeeRunResult,
    RunSignal,
    RunStatus,
    SemanticReplanDirective,
    SemanticReplanOperation,
    SignalCode,
    Usage,
)
from tests.kernel.helpers import company_request, task


class CapabilityInsertReplannerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _workflow_prior(pattern_id: str = "workflow-sealed-review") -> WorkflowPrior:
        return WorkflowPrior(
            pattern_id=pattern_id,
            task_family="typed-gap.sealed-review",
            context_fingerprint="workspace-fixture",
            execution_profile=CompilerExecutionProfile.READ_ONLY,
            rationale="Repeated matched jobs verified a review pass before integration.",
            tasks=(
                WorkflowPriorTask(
                    "resolve_gap",
                    ("sealed_review",),
                ),
                WorkflowPriorTask(
                    "review_evidence",
                    ("evidence_review",),
                    depends_on=("resolve_gap",),
                ),
                WorkflowPriorTask(
                    "integrate_answer",
                    ("repository_analysis",),
                    depends_on=("review_evidence",),
                    final=True,
                ),
            ),
            evidence_count=2,
        )

    @staticmethod
    def _successful_result(task_id: str, employee_id: str) -> EmployeeRunResult:
        now = datetime.now(UTC)
        return EmployeeRunResult(
            run_id=f"run-{task_id}",
            request_id=f"request-{task_id}",
            job_id="fixture-job",
            task_id=task_id,
            employee_id=employee_id,
            status=RunStatus.SUCCEEDED,
            summary="Typed capability gap observed.",
            output_artifact_refs=(),
            acceptance_evidence=(),
            unresolved_issues=(),
            observations=(),
            suggested_followups=(),
            signals=(),
            partial_result=False,
            usage=Usage(model_calls=1),
            last_event_seq=1,
            started_at=now,
            finished_at=now,
        )

    async def test_typed_capability_gap_inserts_one_connected_specialist_task(self) -> None:
        request = company_request(
            (
                task("scout", capabilities=("discovery",)),
                task("final", depends_on=("scout",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("generalist", "Generalist", ("discovery", "integration")),),
        )
        graph = graph_from_proposal(request.plan_proposal, max_tasks=4)
        scout = replace(
            next(item for item in graph.tasks if item.task_id == "scout"),
            status=TaskStatus.SUCCEEDED,
            assignee_id="generalist",
            runtime_result=self._successful_result("scout", "generalist"),
        )
        graph = replace(
            graph,
            tasks=tuple(
                scout if item.task_id == scout.task_id else item
                for item in graph.tasks
            ),
        )
        signal = RunSignal(SignalCode.CAPABILITY_MISSING, "compliance_review")
        patch = await CapabilityInsertReplanner().propose(
            ReplanContext(request, graph, scout, signal, request.roster)
        )
        self.assertIsNotNone(patch)
        updated = apply_patch(graph, patch, max_tasks=4)

        tasks = {item.task_id: item for item in updated.tasks}
        specialist = tasks["specialist_compliance_review"]
        self.assertEqual(specialist.depends_on, ("scout",))
        self.assertIn(specialist.task_id, tasks["final"].depends_on)

    async def test_solo_final_gap_creates_specialist_and_new_final_integrator(self) -> None:
        request = company_request(
            (task("analyze_goal", capabilities=("repository_analysis",)),),
            final_task_id="analyze_goal",
            roster=(
                EmployeeRecord(
                    "generalist",
                    "Generalist",
                    ("repository_analysis",),
                ),
            ),
        )
        graph = graph_from_proposal(request.plan_proposal, max_tasks=4)
        original = replace(
            graph.tasks[0],
            status=TaskStatus.SUCCEEDED,
            assignee_id="generalist",
            runtime_result=self._successful_result("analyze_goal", "generalist"),
        )
        graph = replace(graph, tasks=(original,))
        patch = await CapabilityInsertReplanner().propose(
            ReplanContext(
                request,
                graph,
                original,
                RunSignal(SignalCode.CAPABILITY_MISSING, "sealed_review"),
                request.roster,
            )
        )
        self.assertIsNotNone(patch)

        updated = apply_patch(graph, patch, max_tasks=4)
        tasks = {item.task_id: item for item in updated.tasks}
        self.assertEqual(updated.final_task_id, "integrate_goal")
        self.assertEqual(
            tasks["specialist_sealed_review"].depends_on,
            ("analyze_goal",),
        )
        self.assertEqual(
            tasks["integrate_goal"].depends_on,
            ("analyze_goal", "specialist_sealed_review"),
        )

    async def test_solo_final_gap_replays_one_verified_workflow_prior(self) -> None:
        request = company_request(
            (task("analyze_goal", capabilities=("repository_analysis",)),),
            final_task_id="analyze_goal",
            roster=(
                EmployeeRecord(
                    "generalist",
                    "Generalist",
                    ("repository_analysis",),
                ),
            ),
            limits=JobLimits(
                max_tasks=6,
                max_temporary_roles=2,
                max_wall_time_ms=5_000,
            ),
        )
        graph = graph_from_proposal(request.plan_proposal, max_tasks=6)
        original = replace(
            graph.tasks[0],
            status=TaskStatus.SUCCEEDED,
            assignee_id="generalist",
            runtime_result=self._successful_result("analyze_goal", "generalist"),
        )
        graph = replace(graph, tasks=(original,))
        replanner = CapabilityInsertReplanner(
            workflow_priors=(self._workflow_prior(),),
        )

        patch = await replanner.propose(
            ReplanContext(
                request,
                graph,
                original,
                RunSignal(SignalCode.CAPABILITY_MISSING, "sealed_review"),
                request.roster,
            )
        )

        self.assertIsNotNone(patch)
        updated = apply_patch(graph, patch, max_tasks=6)
        tasks = {item.task_id: item for item in updated.tasks}
        self.assertEqual(
            tasks["resolve_gap"].depends_on,
            ("analyze_goal",),
        )
        self.assertEqual(
            tasks["review_evidence"].depends_on,
            ("resolve_gap",),
        )
        self.assertEqual(updated.final_task_id, "integrate_answer")
        self.assertEqual(
            tasks["integrate_answer"].depends_on,
            ("review_evidence",),
        )
        self.assertEqual(
            replanner.exposed_workflow_prior_ids,
            ["workflow-sealed-review"],
        )
        self.assertEqual(
            replanner.aligned_workflow_prior_ids,
            ["workflow-sealed-review"],
        )

    async def test_ambiguous_workflow_priors_are_exposed_but_fall_back_to_generic_insert(
        self,
    ) -> None:
        request = company_request(
            (task("analyze_goal", capabilities=("repository_analysis",)),),
            final_task_id="analyze_goal",
            roster=(
                EmployeeRecord(
                    "generalist",
                    "Generalist",
                    ("repository_analysis",),
                ),
            ),
        )
        graph = graph_from_proposal(request.plan_proposal, max_tasks=16)
        original = replace(
            graph.tasks[0],
            status=TaskStatus.SUCCEEDED,
            assignee_id="generalist",
            runtime_result=self._successful_result("analyze_goal", "generalist"),
        )
        graph = replace(graph, tasks=(original,))
        replanner = CapabilityInsertReplanner(
            workflow_priors=(
                self._workflow_prior("workflow-a"),
                self._workflow_prior("workflow-b"),
            ),
        )

        patch = await replanner.propose(
            ReplanContext(
                request,
                graph,
                original,
                RunSignal(SignalCode.CAPABILITY_MISSING, "sealed_review"),
                request.roster,
            )
        )

        self.assertIsNotNone(patch)
        updated = apply_patch(graph, patch, max_tasks=16)
        self.assertEqual(updated.final_task_id, "integrate_goal")
        self.assertEqual(
            replanner.exposed_workflow_prior_ids,
            ["workflow-a", "workflow-b"],
        )
        self.assertEqual(replanner.aligned_workflow_prior_ids, [])

    async def test_replayed_runtime_pattern_reuses_existing_solo_root(self) -> None:
        runtime_pattern = WorkflowPrior(
            pattern_id="workflow-runtime-shape",
            task_family="typed-gap.sealed-review",
            context_fingerprint="workspace-fixture",
            execution_profile=CompilerExecutionProfile.READ_ONLY,
            rationale="The learned graph includes the original SOLO root.",
            tasks=(
                WorkflowPriorTask("analyze_goal", ("repository_analysis",)),
                WorkflowPriorTask(
                    "resolve_gap",
                    ("sealed_review",),
                    depends_on=("analyze_goal",),
                ),
                WorkflowPriorTask(
                    "integrate_answer",
                    ("repository_analysis",),
                    depends_on=("resolve_gap",),
                    final=True,
                ),
            ),
            evidence_count=2,
        )
        request = company_request(
            (task("analyze_goal", capabilities=("repository_analysis",)),),
            final_task_id="analyze_goal",
            roster=(
                EmployeeRecord(
                    "generalist",
                    "Generalist",
                    ("repository_analysis",),
                ),
            ),
            limits=JobLimits(
                max_tasks=4,
                max_temporary_roles=1,
                max_wall_time_ms=5_000,
            ),
        )
        graph = graph_from_proposal(request.plan_proposal, max_tasks=4)
        original = replace(
            graph.tasks[0],
            status=TaskStatus.SUCCEEDED,
            assignee_id="generalist",
            runtime_result=self._successful_result("analyze_goal", "generalist"),
        )
        graph = replace(graph, tasks=(original,))

        patch = await CapabilityInsertReplanner(
            workflow_priors=(runtime_pattern,),
        ).propose(
            ReplanContext(
                request,
                graph,
                original,
                RunSignal(SignalCode.CAPABILITY_MISSING, "sealed_review"),
                request.roster,
            )
        )

        self.assertIsNotNone(patch)
        updated = apply_patch(graph, patch, max_tasks=4)
        self.assertEqual(
            tuple(task.task_id for task in updated.tasks).count("analyze_goal"),
            1,
        )
        self.assertEqual(len(updated.tasks), 3)
        self.assertEqual(
            next(task for task in updated.tasks if task.task_id == "resolve_gap").depends_on,
            ("analyze_goal",),
        )

    async def test_workflow_prior_that_exceeds_temporary_role_budget_falls_back_safely(
        self,
    ) -> None:
        request = company_request(
            (task("analyze_goal", capabilities=("repository_analysis",)),),
            final_task_id="analyze_goal",
            roster=(
                EmployeeRecord(
                    "generalist",
                    "Generalist",
                    ("repository_analysis",),
                ),
            ),
            limits=JobLimits(
                max_tasks=4,
                max_temporary_roles=1,
                max_wall_time_ms=5_000,
            ),
        )
        graph = graph_from_proposal(request.plan_proposal, max_tasks=4)
        original = replace(
            graph.tasks[0],
            status=TaskStatus.SUCCEEDED,
            assignee_id="generalist",
            runtime_result=self._successful_result("analyze_goal", "generalist"),
        )
        graph = replace(graph, tasks=(original,))
        replanner = CapabilityInsertReplanner(
            workflow_priors=(self._workflow_prior(),),
        )

        patch = await replanner.propose(
            ReplanContext(
                request,
                graph,
                original,
                RunSignal(SignalCode.CAPABILITY_MISSING, "sealed_review"),
                request.roster,
            )
        )

        self.assertIsNotNone(patch)
        updated = apply_patch(graph, patch, max_tasks=4)
        self.assertEqual(updated.final_task_id, "integrate_goal")
        self.assertEqual(replanner.exposed_workflow_prior_ids, [])
        self.assertEqual(replanner.aligned_workflow_prior_ids, [])

    async def test_free_form_or_final_task_signal_does_not_mutate_graph(self) -> None:
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        graph = graph_from_proposal(request.plan_proposal, max_tasks=2)
        final = graph.tasks[0]
        invalid = await CapabilityInsertReplanner().propose(
            ReplanContext(
                request,
                graph,
                final,
                RunSignal(SignalCode.CAPABILITY_MISSING, "Need a lawyer now"),
                request.roster,
            )
        )
        terminal = await CapabilityInsertReplanner().propose(
            ReplanContext(
                request,
                graph,
                final,
                RunSignal(SignalCode.CAPABILITY_MISSING, "legal_review"),
                request.roster,
            )
        )
        self.assertIsNone(invalid)
        self.assertIsNone(terminal)

    async def test_manager_follow_up_requires_exact_typed_capability_envelope(self) -> None:
        request = company_request(
            (
                task("scout", capabilities=("discovery",)),
                task("final", depends_on=("scout",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("generalist", "Generalist", ("discovery", "integration")),),
        )
        graph = graph_from_proposal(request.plan_proposal, max_tasks=4)
        scout = replace(
            next(item for item in graph.tasks if item.task_id == "scout"),
            status=TaskStatus.SUCCEEDED,
            assignee_id="generalist",
            runtime_result=self._successful_result("scout", "generalist"),
        )
        graph = replace(graph, tasks=tuple(scout if item.task_id == "scout" else item for item in graph.tasks))
        replanner = ManagerFollowUpReplanner(
            CapabilityInsertReplanner(), manager_employee_id="employee-manager"
        )
        context = lambda value: ReplanContext(
            request, graph, scout,
            RunSignal(SignalCode.ASSUMPTION_INVALIDATED, value), request.roster,
        )

        self.assertIsNone(await replanner.propose(context("Need more evidence.")))
        patch = await replanner.propose(context("follow_up_capability:compliance_review"))

        self.assertIsNotNone(patch)
        assert patch is not None
        updated = apply_patch(graph, patch, max_tasks=4)
        self.assertIn("specialist_compliance_review", {item.task_id for item in updated.tasks})

    async def test_manager_follow_up_preserves_prior_attribution_receipts(self) -> None:
        delegate = CapabilityInsertReplanner()
        delegate.exposed_workflow_prior_ids.append("workflow-observed")
        delegate.aligned_workflow_prior_ids.append("workflow-observed")
        replanner = ManagerFollowUpReplanner(
            delegate,
            manager_employee_id="employee-manager",
        )

        self.assertIs(replanner.exposed_workflow_prior_ids, delegate.exposed_workflow_prior_ids)
        self.assertIs(replanner.aligned_workflow_prior_ids, delegate.aligned_workflow_prior_ids)


class SemanticSignalReplannerTests(CapabilityInsertReplannerTests):
    async def _context(self, tasks, *, final_task_id: str, trigger_id: str):
        request = company_request(
            tasks,
            final_task_id=final_task_id,
            roster=(
                EmployeeRecord("generalist", "Generalist", ("discovery", "integration", "security", "pricing", "alpha", "beta")),
            ),
            limits=JobLimits(max_tasks=10, max_temporary_roles=2),
        )
        graph = graph_from_proposal(request.plan_proposal, max_tasks=10)
        trigger = replace(
            next(item for item in graph.tasks if item.task_id == trigger_id),
            status=TaskStatus.SUCCEEDED,
            assignee_id="generalist",
            runtime_result=self._successful_result(trigger_id, "generalist"),
        )
        graph = replace(
            graph,
            tasks=tuple(trigger if item.task_id == trigger_id else item for item in graph.tasks),
        )
        return request, graph, trigger

    async def test_assumption_split_signal_builds_and_validates_two_branches(self) -> None:
        request, graph, trigger = await self._context(
            (
                task("trigger", capabilities=("discovery",)),
                task("final", depends_on=("trigger",), capabilities=("integration",)),
            ),
            final_task_id="final",
            trigger_id="trigger",
        )
        patch = await SemanticSignalReplanner(CapabilityInsertReplanner()).propose(
            ReplanContext(
                request, graph, trigger,
                RunSignal(SignalCode.ASSUMPTION_INVALIDATED, "split:security,pricing"),
                request.roster,
            )
        )
        self.assertIsNotNone(patch)
        assert patch is not None
        self.assertEqual(patch.semantic_operation.value, "SPLIT")
        updated = apply_patch(graph, patch, max_tasks=10)
        final = next(item for item in updated.tasks if item.task_id == "final")
        self.assertTrue({"check_security", "check_pricing"}.issubset(set(final.depends_on)))

    async def test_constraint_merge_signal_replaces_pending_siblings(self) -> None:
        request, graph, trigger = await self._context(
            (
                task("trigger", capabilities=("discovery",)),
                task("alpha", depends_on=("trigger",), capabilities=("alpha",)),
                task("beta", depends_on=("trigger",), capabilities=("beta",)),
                task("final", depends_on=("alpha", "beta"), capabilities=("integration",)),
            ),
            final_task_id="final",
            trigger_id="trigger",
        )
        patch = await SemanticSignalReplanner(CapabilityInsertReplanner()).propose(
            ReplanContext(
                request, graph, trigger,
                RunSignal(SignalCode.CONSTRAINT_CHANGED, "merge:alpha,beta"),
                request.roster,
            )
        )
        self.assertIsNotNone(patch)
        assert patch is not None
        self.assertEqual(patch.semantic_operation.value, "MERGE")
        updated = apply_patch(graph, patch, max_tasks=10)
        by_id = {item.task_id: item for item in updated.tasks}
        self.assertEqual(by_id["alpha"].status, TaskStatus.CANCELLED)
        self.assertEqual(by_id["beta"].status, TaskStatus.CANCELLED)
        replacement = next(item for item in updated.tasks if item.task_id.startswith("merge_alpha_beta"))
        self.assertEqual(by_id["final"].depends_on, (replacement.task_id,))

    async def test_constraint_join_signal_requires_completed_named_branch(self) -> None:
        request, graph, trigger = await self._context(
            (
                task("trigger", capabilities=("discovery",)),
                task("other", capabilities=("security",)),
                task("final", depends_on=("trigger", "other"), capabilities=("integration",)),
            ),
            final_task_id="final",
            trigger_id="trigger",
        )
        other = replace(
            next(item for item in graph.tasks if item.task_id == "other"),
            status=TaskStatus.SUCCEEDED,
            assignee_id="generalist",
            runtime_result=self._successful_result("other", "generalist"),
        )
        graph = replace(graph, tasks=tuple(other if item.task_id == "other" else item for item in graph.tasks))
        patch = await SemanticSignalReplanner(CapabilityInsertReplanner()).propose(
            ReplanContext(
                request, graph, trigger,
                RunSignal(SignalCode.CONSTRAINT_CHANGED, "join:other"),
                request.roster,
            )
        )
        self.assertIsNotNone(patch)
        assert patch is not None
        updated = apply_patch(graph, patch, max_tasks=10)
        joined = next(item for item in updated.tasks if item.task_id.startswith("join_trigger_other"))
        self.assertEqual(joined.depends_on, ("other", "trigger"))

    async def test_typed_assumption_directive_preserves_opaque_evidence_refs(self) -> None:
        request, graph, trigger = await self._context(
            (
                task("trigger", capabilities=("discovery",)),
                task("final", depends_on=("trigger",), capabilities=("integration",)),
            ),
            final_task_id="final",
            trigger_id="trigger",
        )
        patch = await SemanticSignalReplanner(CapabilityInsertReplanner()).propose(
            ReplanContext(
                request,
                graph,
                trigger,
                RunSignal(
                    SignalCode.ASSUMPTION_INVALIDATED,
                    semantic_replan=SemanticReplanDirective(
                        SemanticReplanOperation.SPLIT,
                        capability_ids=("security", "pricing"),
                        assumption_refs=("intent-assumption:pricing-window:r2",),
                    ),
                ),
                request.roster,
            )
        )
        self.assertIsNotNone(patch)
        assert patch is not None
        self.assertEqual(
            patch.semantic_evidence_refs,
            ("intent-assumption:pricing-window:r2",),
        )
        updated = apply_patch(graph, patch, max_tasks=10)
        final = next(item for item in updated.tasks if item.task_id == "final")
        self.assertTrue({"check_security", "check_pricing"}.issubset(final.depends_on))

    async def test_typed_constraint_cancel_retires_one_final_branch(self) -> None:
        request, graph, trigger = await self._context(
            (
                task("trigger", capabilities=("discovery",)),
                task("alpha", depends_on=("trigger",), capabilities=("alpha",)),
                task("beta", depends_on=("trigger",), capabilities=("beta",)),
                task("final", depends_on=("alpha", "beta"), capabilities=("integration",)),
            ),
            final_task_id="final",
            trigger_id="trigger",
        )
        patch = await SemanticSignalReplanner(CapabilityInsertReplanner()).propose(
            ReplanContext(
                request,
                graph,
                trigger,
                RunSignal(
                    SignalCode.CONSTRAINT_CHANGED,
                    semantic_replan=SemanticReplanDirective(
                        SemanticReplanOperation.CANCEL,
                        task_ids=("alpha",),
                        constraint_refs=("intent-constraint:no-alpha:r4",),
                    ),
                ),
                request.roster,
            )
        )
        self.assertIsNotNone(patch)
        assert patch is not None
        self.assertEqual(patch.semantic_operation.value, "CANCEL")
        self.assertEqual(patch.semantic_evidence_refs, ("intent-constraint:no-alpha:r4",))
        updated = apply_patch(graph, patch, max_tasks=10)
        by_id = {item.task_id: item for item in updated.tasks}
        self.assertEqual(by_id["alpha"].status, TaskStatus.CANCELLED)
        self.assertEqual(by_id["final"].depends_on, ("beta",))

    async def test_malformed_typed_directive_does_not_fall_back_to_legacy_text(self) -> None:
        request, graph, trigger = await self._context(
            (
                task("trigger", capabilities=("discovery",)),
                task("final", depends_on=("trigger",), capabilities=("integration",)),
            ),
            final_task_id="final",
            trigger_id="trigger",
        )
        patch = await SemanticSignalReplanner(CapabilityInsertReplanner()).propose(
            ReplanContext(
                request,
                graph,
                trigger,
                RunSignal(
                    SignalCode.ASSUMPTION_INVALIDATED,
                    value="split:security,pricing",
                    semantic_replan=SemanticReplanDirective(
                        SemanticReplanOperation.SPLIT,
                        capability_ids=("security", "pricing"),
                    ),
                ),
                request.roster,
            )
        )
        self.assertIsNone(patch)


if __name__ == "__main__":
    unittest.main()
