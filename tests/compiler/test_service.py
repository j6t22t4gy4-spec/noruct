from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace

from dynamic_firm.compiler import (
    CompilerExecutionProfile,
    CompilerReason,
    CompilerRequest,
    ManagerOutcomeSummary,
    ManagerPlanningBrief,
    ManagerPlanningSkill,
    DynamicWorkflowCompiler,
    PlanningOwner,
    PlanningMode,
    WorkflowPrior,
    WorkflowPriorTask,
    direct_conversation_decision,
    plan_json_schema,
    repository_review_paths,
    solo_first_decision,
)
from dynamic_firm.runtime.models import StructuredOutputResponse, Usage
from dynamic_firm.runtime.ports import ModelProviderError
from dynamic_firm.kernel.models import (
    ExecutionReplicaPreference,
    ExecutionReplicaStrategy,
)
from tests.compiler.test_parser import plan, task


class StructuredProvider:
    def __init__(
        self,
        response,
        *,
        structured_model_call_ceiling: int = 1,
        model_call_ceiling: int = 1,
    ) -> None:
        self.response = response
        self.requests = []
        self.structured_model_call_ceiling = structured_model_call_ceiling
        self.model_call_ceiling = model_call_ceiling

    async def complete_structured(self, request, cancellation):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class HangingStructuredProvider:
    structured_model_call_ceiling = 1

    def __init__(self) -> None:
        self.requests = []
        self.cancelled = False

    async def complete_structured(self, request, cancellation):
        self.requests.append(request)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def request(
    *,
    max_model_calls: int = 8,
    max_wall_time_ms: int = 30_000,
    execution_profile: CompilerExecutionProfile = CompilerExecutionProfile.READ_ONLY,
) -> CompilerRequest:
    return CompilerRequest(
        request_id="compiler-request",
        goal="Inspect the repository",
        workspace_manifest=("calculator.py", "test_calculator.py"),
        available_capabilities=("repository_analysis", "evidence_synthesis"),
        model_profile="contract-model",
        execution_profile=execution_profile,
        max_total_model_calls=max_model_calls,
        max_wall_time_ms=max_wall_time_ms,
    )


class CompilerServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_schema_uses_conservative_structured_output_subset(self) -> None:
        schema = plan_json_schema(max_tasks=3)
        unsupported = {
            "pattern",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "uniqueItems",
        }

        def visit(value) -> None:
            if isinstance(value, dict):
                self.assertTrue(unsupported.isdisjoint(value))
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(schema)
        task_schema = schema["properties"]["tasks"]["items"]
        self.assertIn("execution_replica", task_schema["required"])
        self.assertEqual(
            task_schema["properties"]["execution_replica"]["type"],
            ["object", "null"],
        )

    async def test_direct_conversation_skips_compiler_and_workspace_evidence(self) -> None:
        decision = direct_conversation_decision(request())

        self.assertEqual(decision.mode, PlanningMode.DIRECT)
        self.assertEqual(decision.reason, CompilerReason.DIRECT_USER_MESSAGE)
        self.assertEqual(decision.usage.model_calls, 0)
        self.assertEqual(decision.proposal.final_task_id, "respond_to_user")
        self.assertEqual(decision.proposal.tasks[0].required_capabilities, ("conversation",))

    async def test_solo_first_uses_zero_provider_calls_and_active_capability_match(self) -> None:
        compiler_request = replace(
            request(),
            goal="Use the persistent security reviewer",
            available_capabilities=(
                "repository_analysis",
                "security_review",
            ),
        )

        decision = solo_first_decision(compiler_request)

        self.assertEqual(decision.mode, PlanningMode.SOLO)
        self.assertEqual(decision.reason, CompilerReason.SOLO_FIRST_ATTEMPT)
        self.assertEqual(decision.usage.model_calls, 0)
        self.assertEqual(
            decision.proposal.tasks[0].required_capabilities,
            ("security_review",),
        )

    async def test_explicit_repository_files_create_a_bounded_solo_review_brief(self) -> None:
        compiler_request = replace(
            request(),
            goal=(
                "Compare docs/README.md with "
                "docs/20-architecture/noruct-employee-foundation-strategy.md and summarize blockers."
            ),
        )

        decision = solo_first_decision(compiler_request)
        criteria = decision.proposal.tasks[0].acceptance_criteria

        self.assertEqual(
            repository_review_paths(compiler_request.goal),
            (
                "docs/README.md",
                "docs/20-architecture/noruct-employee-foundation-strategy.md",
            ),
        )
        self.assertIn("Evidence scope is limited", criteria[0])
        self.assertIn("docs/README.md", criteria[0])
        self.assertIn("read_workspace_file", criteria[1])
        self.assertIn("do not call list_workspace_files", criteria[1])
        self.assertIn("do not retry", criteria[2])
        self.assertEqual(decision.usage.model_calls, 0)

    async def test_sentence_terminated_explicit_files_do_not_silently_narrow_scope(self) -> None:
        goal = (
            "Compare docs/50-mvp/phase-h2-78-path-bounded-workload-suite.md and "
            "docs/50-mvp/current-system-audit-and-roadmap.md. Explain the constraint."
        )

        self.assertEqual(
            repository_review_paths(goal),
            (
                "docs/50-mvp/phase-h2-78-path-bounded-workload-suite.md",
                "docs/50-mvp/current-system-audit-and-roadmap.md",
            ),
        )
        criteria = solo_first_decision(replace(request(), goal=goal)).proposal.tasks[0].acceptance_criteria
        self.assertIn("docs/50-mvp/current-system-audit-and-roadmap.md", criteria[0])

    async def test_ambiguous_or_oversized_path_sets_do_not_create_a_partial_scope(self) -> None:
        self.assertEqual(repository_review_paths("Review the repository policy."), ())
        self.assertEqual(
            repository_review_paths(
                "Review a/one.py b/two.py c/three.py d/four.py e/five.py"
            ),
            (),
        )

    async def test_valid_solo_is_a_normal_compiler_success(self) -> None:
        provider = StructuredProvider(
            StructuredOutputResponse(
                plan("SOLO", [task("analyze")], "analyze"),
                usage=Usage(input_tokens=10, output_tokens=5),
                provider_request_id="plan-1",
            )
        )
        decision = await DynamicWorkflowCompiler(provider).compile(request())

        self.assertEqual(decision.mode, PlanningMode.SOLO)
        self.assertEqual(decision.reason, CompilerReason.VALID_SOLO)
        self.assertEqual(decision.usage.model_calls, 1)
        self.assertEqual(decision.provider_request_id, "plan-1")
        self.assertEqual(
            provider.requests[0].json_schema["properties"]["mode"]["enum"],
            ["SOLO", "GRAPH"],
        )
        self.assertIn(
            "same persistent Employee profile",
            str(provider.requests[0].messages[0].content),
        )

    async def test_manager_owned_planning_is_one_existing_structured_call(self) -> None:
        provider = StructuredProvider(
            StructuredOutputResponse(
                plan("SOLO", [task("analyze")], "analyze"),
                usage=Usage(model_calls=1),
                provider_request_id="manager-plan-1",
            )
        )
        owner = PlanningOwner(
            employee_id="employee-manager",
            role="Executive Manager",
            assignment_digest="a" * 64,
            session_key="manager:employee-manager:session-a",
        )
        brief = ManagerPlanningBrief(
            company_revision=3,
            company_purpose="Build a durable AI company",
            work_order_constraints=("Keep effects approval gated.",),
            skills=(
                ManagerPlanningSkill(
                    skill_key="staffing",
                    revision="2",
                    purpose="Choose the smallest evidence-backed staffing shape.",
                    content_hash="b" * 64,
                ),
            ),
            outcome_summary=ManagerOutcomeSummary(
                context_fingerprint="context-a",
                observed_count=3,
                succeeded_count=2,
                safety_passed_count=2,
                effect_passed_count=1,
            ),
        )

        decision = await DynamicWorkflowCompiler(provider).compile(
            replace(request(), planning_owner=owner, manager_planning_brief=brief)
        )

        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(decision.planning_owner_id, owner.employee_id)
        self.assertEqual(decision.planning_owner_assignment_digest, owner.assignment_digest)
        self.assertIn("persistent Executive Manager", str(provider.requests[0].messages[0].content))
        payload = json.loads(str(provider.requests[0].messages[1].content))
        self.assertEqual(payload["planning_owner"]["employee_id"], owner.employee_id)
        self.assertEqual(payload["planning_owner"]["assignment_digest"], owner.assignment_digest)
        self.assertEqual(payload["manager_planning_brief"]["company_revision"], 3)
        self.assertEqual(
            payload["manager_planning_brief"]["skills"][0]["skill_key"],
            "staffing",
        )
        self.assertIsNone(payload["manager_planning_brief"]["knowledge_brief"])
        self.assertEqual(
            decision.manager_planning_brief_digest,
            brief.content_digest,
        )

    async def test_manager_owned_safe_fallback_preserves_planning_provenance(self) -> None:
        owner = PlanningOwner(
            employee_id="employee-manager",
            role="Executive Manager",
            assignment_digest="a" * 64,
            session_key="manager:employee-manager:session-fallback",
        )
        brief = ManagerPlanningBrief(
            company_revision=3,
            company_purpose="Build a durable AI company",
            work_order_constraints=("Keep effects approval gated.",),
            skills=(),
            outcome_summary=ManagerOutcomeSummary("context-a", 0, 0, 0, 0),
        )
        decision = await DynamicWorkflowCompiler(
            StructuredProvider(
                ModelProviderError("MODEL_TIMEOUT", "timed out", retryable=True)
            )
        ).compile(replace(request(), planning_owner=owner, manager_planning_brief=brief))

        self.assertEqual(decision.mode, PlanningMode.SOLO_FALLBACK)
        self.assertEqual(decision.planning_owner_id, owner.employee_id)
        self.assertEqual(
            decision.planning_owner_assignment_digest, owner.assignment_digest
        )
        self.assertEqual(decision.manager_planning_brief_digest, brief.content_digest)

    async def test_manager_planning_brief_requires_a_typed_owner(self) -> None:
        brief = ManagerPlanningBrief(
            company_revision=1,
            company_purpose="Company",
            work_order_constraints=(),
            skills=(),
            outcome_summary=ManagerOutcomeSummary("", 0, 0, 0, 0),
        )

        with self.assertRaisesRegex(ValueError, "requires a planning owner"):
            await DynamicWorkflowCompiler(None).compile(
                replace(request(), manager_planning_brief=brief)
            )

    async def test_performance_first_replica_policy_is_explicit_and_bounded(self) -> None:
        provider = StructuredProvider(
            StructuredOutputResponse(
                plan("SOLO", [task("analyze")], "analyze"),
                usage=Usage(model_calls=1),
            )
        )
        compiler_request = replace(
            request(),
            execution_replica_preference=(
                ExecutionReplicaPreference.PERFORMANCE_FIRST
            ),
            suggested_execution_replica_strategy=(
                ExecutionReplicaStrategy.PARTITION
            ),
        )

        decision = await DynamicWorkflowCompiler(provider).compile(compiler_request)

        self.assertEqual(decision.mode, PlanningMode.SOLO)
        system_prompt = str(provider.requests[0].messages[0].content)
        payload = json.loads(str(provider.requests[0].messages[1].content))
        self.assertIn("technically possible is not a sufficient reason", system_prompt)
        self.assertIn("hard ceilings", system_prompt)
        self.assertEqual(
            payload["execution_replica_preference"],
            "PERFORMANCE_FIRST",
        )
        self.assertEqual(
            payload["suggested_execution_replica_strategy"],
            "PARTITION",
        )
        self.assertEqual(payload["limits"]["max_total_model_calls"], 8)

    async def test_disabled_replica_policy_is_preserved_in_prompt_and_payload(self) -> None:
        provider = StructuredProvider(
            StructuredOutputResponse(plan("SOLO", [task("analyze")], "analyze"))
        )

        await DynamicWorkflowCompiler(provider).compile(
            replace(
                request(),
                execution_replica_preference=ExecutionReplicaPreference.DISABLED,
            )
        )

        system_prompt = str(provider.requests[0].messages[0].content)
        payload = json.loads(str(provider.requests[0].messages[1].content))
        self.assertIn("Do not emit execution_replica", system_prompt)
        self.assertEqual(payload["execution_replica_preference"], "DISABLED")
        self.assertIsNone(payload["suggested_execution_replica_strategy"])

    async def test_disabled_replica_policy_cannot_carry_a_strategy_hint(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot suggest"):
            await DynamicWorkflowCompiler(object()).compile(
                replace(
                    request(),
                    execution_replica_preference=(
                        ExecutionReplicaPreference.DISABLED
                    ),
                    suggested_execution_replica_strategy=(
                        ExecutionReplicaStrategy.DIAGNOSTIC
                    ),
                )
            )

    async def test_invalid_proposal_and_provider_failure_use_explained_solo_fallback(self) -> None:
        cycle = plan(
            "GRAPH",
            [task("a", depends_on=("b",)), task("b", depends_on=("a",))],
            "b",
        )
        rejected = await DynamicWorkflowCompiler(
            StructuredProvider(StructuredOutputResponse(cycle))
        ).compile(request())
        failed = await DynamicWorkflowCompiler(
            StructuredProvider(
                ModelProviderError("MODEL_TIMEOUT", "timed out", retryable=True)
            )
        ).compile(request())

        self.assertEqual(rejected.mode, PlanningMode.SOLO_FALLBACK)
        self.assertEqual(rejected.reason, CompilerReason.COMPILER_PROPOSAL_REJECTED)
        self.assertEqual(failed.reason, CompilerReason.COMPILER_PROVIDER_FAILURE)
        self.assertEqual(failed.usage.model_calls, 1)
        self.assertEqual(rejected.proposal.final_task_id, "analyze_goal")

    async def test_unexpected_composite_failure_charges_physical_ceiling(self) -> None:
        decision = await DynamicWorkflowCompiler(
            StructuredProvider(
                RuntimeError("unexpected provider failure"),
                structured_model_call_ceiling=3,
            )
        ).compile(request(max_model_calls=4))

        self.assertEqual(decision.reason, CompilerReason.COMPILER_PROVIDER_FAILURE)
        self.assertEqual(decision.usage.model_calls, 3)

    async def test_one_call_budget_skips_compiler_and_preserves_employee_call(self) -> None:
        provider = StructuredProvider(
            StructuredOutputResponse(plan("SOLO", [task("analyze")], "analyze"))
        )
        decision = await DynamicWorkflowCompiler(provider).compile(request(max_model_calls=1))

        self.assertEqual(decision.reason, CompilerReason.COMPILER_SKIPPED_BUDGET)
        self.assertEqual(decision.usage.model_calls, 0)
        self.assertEqual(provider.requests, [])

    async def test_composite_provider_ceiling_is_reserved_before_planning(self) -> None:
        blocked = StructuredProvider(
            StructuredOutputResponse(plan("SOLO", [task("analyze")], "analyze")),
            structured_model_call_ceiling=3,
        )
        skipped = await DynamicWorkflowCompiler(blocked).compile(
            request(max_model_calls=3)
        )

        admitted = StructuredProvider(
            StructuredOutputResponse(plan("SOLO", [task("analyze")], "analyze")),
            structured_model_call_ceiling=3,
        )
        accepted = await DynamicWorkflowCompiler(admitted).compile(
            request(max_model_calls=4)
        )

        self.assertEqual(skipped.reason, CompilerReason.COMPILER_SKIPPED_BUDGET)
        self.assertEqual(blocked.requests, [])
        self.assertEqual(accepted.reason, CompilerReason.VALID_SOLO)
        self.assertEqual(len(admitted.requests), 1)

    async def test_compiler_reserves_composite_employee_closure_too(self) -> None:
        provider = StructuredProvider(
            StructuredOutputResponse(plan("SOLO", [task("analyze")], "analyze")),
            structured_model_call_ceiling=2,
            model_call_ceiling=2,
        )

        decision = await DynamicWorkflowCompiler(provider).compile(
            request(max_model_calls=3)
        )

        self.assertEqual(decision.reason, CompilerReason.COMPILER_SKIPPED_BUDGET)
        self.assertEqual(provider.requests, [])

    async def test_compiler_timeout_preserves_one_employee_call(self) -> None:
        provider = HangingStructuredProvider()

        decision = await DynamicWorkflowCompiler(provider).compile(
            request(max_model_calls=2, max_wall_time_ms=5)
        )

        self.assertEqual(
            decision.reason,
            CompilerReason.COMPILER_WALL_TIME_EXHAUSTED,
        )
        self.assertEqual(decision.usage.model_calls, 1)
        self.assertEqual(decision.mode, PlanningMode.SOLO_FALLBACK)
        self.assertTrue(provider.cancelled)

    async def test_duplicate_nonfinal_work_is_rejected_as_redundant_team(self) -> None:
        first = task("first")
        duplicate = task("duplicate")
        duplicate["objective"] = first["objective"]
        duplicate["acceptance_criteria"] = first["acceptance_criteria"]
        graph = plan(
            "GRAPH",
            [
                first,
                duplicate,
                task(
                    "final",
                    depends_on=("first", "duplicate"),
                    capability="evidence_synthesis",
                ),
            ],
            "final",
        )

        decision = await DynamicWorkflowCompiler(
            StructuredProvider(StructuredOutputResponse(graph))
        ).compile(request())

        self.assertEqual(
            decision.reason,
            CompilerReason.COMPILER_PROPOSAL_REJECTED,
        )
        self.assertEqual(decision.mode, PlanningMode.SOLO_FALLBACK)

    async def test_provider_without_structured_surface_is_safe_fallback(self) -> None:
        decision = await DynamicWorkflowCompiler(object()).compile(request())
        self.assertEqual(decision.reason, CompilerReason.COMPILER_UNAVAILABLE)

    async def test_plan_requiring_more_employee_calls_than_job_budget_is_rejected(self) -> None:
        graph = plan(
            "GRAPH",
            [
                task("inspect", capability="code_analysis"),
                task("final", depends_on=("inspect",), capability="evidence_synthesis"),
            ],
            "final",
        )
        provider = StructuredProvider(StructuredOutputResponse(graph))
        decision = await DynamicWorkflowCompiler(provider).compile(request(max_model_calls=2))

        self.assertEqual(decision.mode, PlanningMode.SOLO_FALLBACK)
        self.assertEqual(decision.reason, CompilerReason.COMPILER_PROPOSAL_REJECTED)
        self.assertEqual(decision.usage.model_calls, 1)

    async def test_shadow_coding_profile_requires_implementation_only_on_final_task(self) -> None:
        valid = plan(
            "GRAPH",
            [
                task("inspect", capability="repository_analysis"),
                task(
                    "implement_change",
                    depends_on=("inspect",),
                    capability="implementation",
                ),
            ],
            "implement_change",
        )
        provider = StructuredProvider(StructuredOutputResponse(valid))
        decision = await DynamicWorkflowCompiler(provider).compile(
            request(execution_profile=CompilerExecutionProfile.SHADOW_CODING)
        )

        self.assertEqual(decision.mode, PlanningMode.DYNAMIC)
        self.assertIn("Exactly the final task", provider.requests[0].messages[0].content)
        self.assertIn('"execution_profile":"SHADOW_CODING"', provider.requests[0].messages[1].content)

        invalid = plan(
            "GRAPH",
            [
                task("edit_early", capability="implementation"),
                task(
                    "final",
                    depends_on=("edit_early",),
                    capability="evidence_synthesis",
                ),
            ],
            "final",
        )
        rejected = await DynamicWorkflowCompiler(
            StructuredProvider(StructuredOutputResponse(invalid))
        ).compile(request(execution_profile=CompilerExecutionProfile.SHADOW_CODING))

        self.assertEqual(rejected.reason, CompilerReason.COMPILER_PROPOSAL_REJECTED)
        self.assertEqual(rejected.proposal.final_task_id, "implement_change")
        self.assertEqual(
            rejected.proposal.tasks[0].required_capabilities,
            ("implementation",),
        )

    async def test_host_direct_profile_uses_implementation_without_shadow_language(self) -> None:
        valid = plan(
            "SOLO",
            [task("implement_change", capability="implementation")],
            "implement_change",
        )
        provider = StructuredProvider(StructuredOutputResponse(valid))
        decision = await DynamicWorkflowCompiler(provider).compile(
            request(execution_profile=CompilerExecutionProfile.HOST_DIRECT)
        )

        self.assertEqual(decision.mode, PlanningMode.SOLO)
        prompt = str(provider.requests[0].messages[0].content)
        self.assertIn("approved host-direct workspace tools", prompt)
        self.assertNotIn("disposable shadow", prompt)
        self.assertEqual(
            decision.proposal.tasks[0].required_capabilities,
            ("implementation",),
        )

        fallback = await DynamicWorkflowCompiler(object()).compile(
            request(execution_profile=CompilerExecutionProfile.HOST_DIRECT)
        )
        self.assertIn("approved workspace operation", fallback.proposal.tasks[0].acceptance_criteria[0])
        self.assertNotIn("shadow", fallback.proposal.tasks[0].acceptance_criteria[0])
        self.assertIn("do not list the workspace root", fallback.proposal.tasks[0].acceptance_criteria[1])

    async def test_host_action_profile_does_not_force_implementation(self) -> None:
        compiler_request = replace(
            request(execution_profile=CompilerExecutionProfile.HOST_ACTION),
            goal="Run the bounded test command",
            available_capabilities=(
                "repository_analysis",
                "general_reasoning",
                "implementation",
            ),
        )
        valid = plan(
            "SOLO",
            [task("perform_action", capability="general_reasoning")],
            "perform_action",
        )
        provider = StructuredProvider(StructuredOutputResponse(valid))

        decision = await DynamicWorkflowCompiler(provider).compile(compiler_request)
        fallback = await DynamicWorkflowCompiler(object()).compile(compiler_request)
        solo = solo_first_decision(compiler_request)

        self.assertEqual(decision.mode, PlanningMode.SOLO)
        prompt = str(provider.requests[0].messages[0].content)
        self.assertIn("approval-gated action lane, not a coding intent", prompt)
        self.assertNotIn(
            "implementation",
            decision.proposal.tasks[0].required_capabilities,
        )
        self.assertFalse(CompilerExecutionProfile.HOST_ACTION.requires_implementation)
        self.assertFalse(CompilerExecutionProfile.HOST_ACTION.allows_workspace_mutation)
        self.assertTrue(CompilerExecutionProfile.HOST_ACTION.permits_host_actions)
        for bounded in (fallback, solo):
            self.assertEqual(bounded.proposal.final_task_id, "perform_action")
            self.assertEqual(
                bounded.proposal.tasks[0].required_capabilities,
                ("general_reasoning",),
            )
            self.assertIn(
                "do not infer a code change",
                bounded.proposal.tasks[0].acceptance_criteria[0],
            )

    async def test_dynamic_prompt_places_optional_reviewer_before_single_final_writer(self) -> None:
        reviewed = plan(
            "GRAPH",
            [
                task("inspect", capability="repository_analysis"),
                task(
                    "review_evidence",
                    depends_on=("inspect",),
                    capability="evidence_synthesis",
                ),
                task(
                    "final",
                    depends_on=("review_evidence",),
                    capability="evidence_synthesis",
                ),
            ],
            "final",
        )
        provider = StructuredProvider(StructuredOutputResponse(reviewed))

        decision = await DynamicWorkflowCompiler(provider).compile(request())

        prompt = str(provider.requests[0].messages[0].content)
        self.assertIn("reviewer task immediately before", prompt)
        self.assertIn("must not be final_task_id", prompt)
        self.assertIn("remains the only final writer", prompt)
        self.assertEqual(decision.proposal.final_task_id, "final")
        reviewer = next(
            task
            for task in decision.proposal.tasks
            if task.task_id == "review_evidence"
        )
        final = next(
            task for task in decision.proposal.tasks if task.task_id == "final"
        )
        self.assertEqual(reviewer.depends_on, ("inspect",))
        self.assertEqual(final.depends_on, ("review_evidence",))

    async def test_required_independent_review_rejects_provider_solo(self) -> None:
        compiler_request = replace(
            request(),
            requires_independent_review=True,
        )
        provider = StructuredProvider(
            StructuredOutputResponse(
                plan("SOLO", [task("analyze")], "analyze"),
                usage=Usage(model_calls=1),
                provider_request_id="review-solo",
            )
        )

        decision = await DynamicWorkflowCompiler(provider).compile(compiler_request)

        self.assertEqual(
            decision.reason,
            CompilerReason.COMPILER_REQUIRED_REVIEW_MISSING,
        )
        self.assertEqual(decision.mode, PlanningMode.DYNAMIC)
        self.assertEqual(len(decision.proposal.tasks), 2)
        self.assertEqual(decision.proposal.final_task_id, "integrate_goal")
        final = next(
            task
            for task in decision.proposal.tasks
            if task.task_id == decision.proposal.final_task_id
        )
        self.assertEqual(final.depends_on, ("independent_review",))
        self.assertIn(
            "requires_independent_review=true",
            provider.requests[0].messages[0].content,
        )
        self.assertIn(
            '"requires_independent_review":true',
            provider.requests[0].messages[1].content,
        )

    async def test_required_independent_review_accepts_direct_pre_final_boundary(self) -> None:
        reviewed = plan(
            "GRAPH",
            [
                task("review", capability="independent_review"),
                task(
                    "final",
                    depends_on=("review",),
                    capability="evidence_synthesis",
                ),
            ],
            "final",
        )
        decision = await DynamicWorkflowCompiler(
            StructuredProvider(StructuredOutputResponse(reviewed))
        ).compile(replace(request(), requires_independent_review=True))

        self.assertEqual(decision.reason, CompilerReason.VALID_DYNAMIC)
        self.assertEqual(decision.proposal.final_task_id, "final")

    async def test_required_review_without_capacity_refuses_effect_explicitly(self) -> None:
        compiler_request = replace(
            request(
                max_model_calls=2,
                execution_profile=CompilerExecutionProfile.HOST_DIRECT,
            ),
            requires_independent_review=True,
            max_tasks=1,
        )
        provider = StructuredProvider(
            StructuredOutputResponse(
                plan(
                    "SOLO",
                    [task("implement_change", capability="implementation")],
                    "implement_change",
                ),
                usage=Usage(model_calls=1),
            )
        )

        decision = await DynamicWorkflowCompiler(provider).compile(compiler_request)

        self.assertEqual(
            decision.reason,
            CompilerReason.COMPILER_REQUIRED_REVIEW_MISSING,
        )
        self.assertEqual(decision.mode, PlanningMode.SOLO_FALLBACK)
        self.assertEqual(
            decision.proposal.final_task_id,
            "report_review_constraint",
        )
        self.assertNotIn(
            "implementation",
            decision.proposal.tasks[0].required_capabilities,
        )
        self.assertIn("refused explicitly", decision.rationale)

    async def test_host_action_graph_requires_final_effect_owner(self) -> None:
        compiler_request = replace(
            request(execution_profile=CompilerExecutionProfile.HOST_ACTION),
            available_capabilities=(
                "repository_analysis",
                "general_reasoning",
                "evidence_synthesis",
            ),
            required_final_action_capability="general_reasoning",
        )
        valid = plan(
            "GRAPH",
            [
                task("inspect", capability="repository_analysis"),
                task(
                    "perform_action",
                    depends_on=("inspect",),
                    capability="general_reasoning",
                ),
            ],
            "perform_action",
        )
        accepted_provider = StructuredProvider(StructuredOutputResponse(valid))
        accepted = await DynamicWorkflowCompiler(accepted_provider).compile(
            compiler_request
        )

        action_first = plan(
            "GRAPH",
            [
                task("perform_early", capability="general_reasoning"),
                task(
                    "summarize",
                    depends_on=("perform_early",),
                    capability="evidence_synthesis",
                ),
            ],
            "summarize",
        )
        rejected = await DynamicWorkflowCompiler(
            StructuredProvider(StructuredOutputResponse(action_first))
        ).compile(compiler_request)

        self.assertEqual(accepted.reason, CompilerReason.VALID_DYNAMIC)
        self.assertIn(
            '"required_final_action_capability":"general_reasoning"',
            accepted_provider.requests[0].messages[1].content,
        )
        self.assertEqual(
            rejected.reason,
            CompilerReason.COMPILER_PROPOSAL_REJECTED,
        )
        self.assertEqual(rejected.mode, PlanningMode.SOLO_FALLBACK)
        self.assertEqual(rejected.proposal.final_task_id, "perform_action")
        self.assertEqual(
            rejected.proposal.tasks[0].required_capabilities,
            ("general_reasoning",),
        )

    async def test_compiler_preserves_multi_call_usage_and_reserves_employee_budget(self) -> None:
        solo_provider = StructuredProvider(
            StructuredOutputResponse(
                plan("SOLO", [task("analyze")], "analyze"),
                usage=Usage(model_calls=3, input_tokens=30, output_tokens=10),
            )
        )
        accepted = await DynamicWorkflowCompiler(solo_provider).compile(
            request(max_model_calls=4)
        )

        exhausted = await DynamicWorkflowCompiler(
            StructuredProvider(
                StructuredOutputResponse(
                    plan("SOLO", [task("analyze")], "analyze"),
                    usage=Usage(model_calls=3),
                )
            )
        ).compile(request(max_model_calls=3))

        graph = plan(
            "GRAPH",
            [
                task("inspect", capability="repository_analysis"),
                task(
                    "final",
                    depends_on=("inspect",),
                    capability="evidence_synthesis",
                ),
            ],
            "final",
        )
        bounded = await DynamicWorkflowCompiler(
            StructuredProvider(
                StructuredOutputResponse(graph, usage=Usage(model_calls=3))
            )
        ).compile(request(max_model_calls=4))

        self.assertEqual(accepted.reason, CompilerReason.VALID_SOLO)
        self.assertEqual(accepted.usage.model_calls, 3)
        self.assertEqual(
            exhausted.reason,
            CompilerReason.COMPILER_BUDGET_EXHAUSTED,
        )
        self.assertEqual(exhausted.usage.model_calls, 3)
        self.assertEqual(
            exhausted.proposal.final_task_id,
            "report_budget_exhausted",
        )
        self.assertIn("no fallback employee call", exhausted.rationale)
        self.assertEqual(
            bounded.reason,
            CompilerReason.COMPILER_PROPOSAL_REJECTED,
        )
        self.assertEqual(bounded.mode, PlanningMode.SOLO_FALLBACK)
        self.assertEqual(len(bounded.proposal.tasks), 1)

    async def test_verified_workflow_prior_is_bounded_advisory_input_only(self) -> None:
        provider = StructuredProvider(
            StructuredOutputResponse(plan("SOLO", [task("analyze")], "analyze"))
        )
        prior = WorkflowPrior(
            pattern_id="workflow-proven",
            task_family="repository-analysis",
            context_fingerprint="python-small",
            execution_profile=CompilerExecutionProfile.READ_ONLY,
            rationale="Two successful episodes improved quality.",
            tasks=(
                WorkflowPriorTask(
                    task_key="analyze",
                    required_capabilities=("repository_analysis",),
                    final=True,
                ),
            ),
            evidence_count=2,
        )

        decision = await DynamicWorkflowCompiler(provider).compile(
            replace(
                request(),
                workflow_context_fingerprint="python-small",
                workflow_priors=(prior,),
            )
        )

        self.assertEqual(decision.mode, PlanningMode.SOLO)
        self.assertIn("advisory company experience", provider.requests[0].messages[0].content)
        self.assertIn('"verified_workflow_priors":[{', provider.requests[0].messages[1].content)
        self.assertIn('"pattern_id":"workflow-proven"', provider.requests[0].messages[1].content)
        self.assertIn('"workflow_context_fingerprint":"python-small"', provider.requests[0].messages[1].content)
        self.assertEqual(decision.exposed_workflow_prior_ids, ("workflow-proven",))
        self.assertEqual(decision.aligned_workflow_prior_ids, ("workflow-proven",))

        with self.assertRaisesRegex(ValueError, "context does not match"):
            await DynamicWorkflowCompiler(provider).compile(
                replace(
                    request(),
                    workflow_context_fingerprint="different",
                    workflow_priors=(prior,),
                )
            )

    async def test_prior_exposure_is_distinct_from_validated_shape_alignment(self) -> None:
        prior = WorkflowPrior(
            pattern_id="workflow-graph",
            task_family="repository-analysis",
            context_fingerprint="python-small",
            execution_profile=CompilerExecutionProfile.READ_ONLY,
            rationale="Repeated graph.",
            tasks=(
                WorkflowPriorTask("inspect", ("repository_analysis",)),
                WorkflowPriorTask(
                    "synthesize",
                    ("evidence_synthesis",),
                    depends_on=("inspect",),
                    final=True,
                ),
            ),
            evidence_count=3,
        )
        compiler_request = replace(
            request(),
            workflow_context_fingerprint="python-small",
            workflow_priors=(prior,),
        )
        renamed_graph = plan(
            "GRAPH",
            [
                task("read_repo", capability="repository_analysis"),
                task(
                    "write_answer",
                    depends_on=("read_repo",),
                    capability="evidence_synthesis",
                ),
            ],
            "write_answer",
        )
        aligned = await DynamicWorkflowCompiler(
            StructuredProvider(StructuredOutputResponse(renamed_graph))
        ).compile(compiler_request)
        solo = await DynamicWorkflowCompiler(
            StructuredProvider(
                StructuredOutputResponse(plan("SOLO", [task("analyze")], "analyze"))
            )
        ).compile(compiler_request)
        failed = await DynamicWorkflowCompiler(
            StructuredProvider(ModelProviderError("MODEL_TIMEOUT", "timeout", retryable=True))
        ).compile(compiler_request)

        self.assertEqual(aligned.exposed_workflow_prior_ids, ("workflow-graph",))
        self.assertEqual(aligned.aligned_workflow_prior_ids, ("workflow-graph",))
        self.assertEqual(solo.exposed_workflow_prior_ids, ("workflow-graph",))
        self.assertEqual(solo.aligned_workflow_prior_ids, ())
        self.assertEqual(failed.exposed_workflow_prior_ids, ("workflow-graph",))
        self.assertEqual(failed.aligned_workflow_prior_ids, ())

        cyclic = replace(
            prior,
            tasks=(
                WorkflowPriorTask(
                    "a", ("repository_analysis",), depends_on=("b",), final=True
                ),
                WorkflowPriorTask(
                    "b", ("evidence_synthesis",), depends_on=("a",)
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "contains a cycle"):
            await DynamicWorkflowCompiler(StructuredProvider(object())).compile(
                replace(compiler_request, workflow_priors=(cyclic,))
            )


if __name__ == "__main__":
    unittest.main()
