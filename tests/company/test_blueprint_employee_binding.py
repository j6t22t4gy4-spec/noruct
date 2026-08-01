from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from dynamic_firm.company import (
    AuthoritySnapshotIdentity,
    EmployeeBoundaryCandidate,
    EmployeeSubstitutionDisposition,
    GraphBlueprint,
    GraphBlueprintExecutionReplica,
    GraphBlueprintTask,
    GraphMutationPolicy,
    GraphUserConstraints,
    WorkOrderBudgetSnapshot,
    bind_blueprint,
    bind_blueprint_execution,
    normalize_work_order,
    plan_employee_substitution,
)
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    ExecutionReplicaAggregation,
    ExecutionReplicaStrategy,
    JobLimits,
    JobStatus,
)
from dynamic_firm.kernel.mutation import content_digest, frozen_snapshot_digest
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.runtime.employee_capability import material_profile_difference
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ContextBundle,
    EmployeeCapabilityProfile,
    EmployeeRunRequest,
    EmployeeSessionRetention,
    RunLimits,
    ToolEffect,
    ToolGrant,
    Usage,
    VersionedContent,
)


PROVIDER_DIGEST = "1" * 64
TOOL_CONTRACT_DIGEST = "2" * 64
COORDINATION_DIGEST = "3" * 64


def _work_order(*, action_policy_digest: str):
    authority = AuthoritySnapshotIdentity(
        company_id="company-local",
        company_revision=3,
        roster_revision=7,
        playbook_revision=2,
        action_policy_digest=action_policy_digest,
    )
    order = normalize_work_order(
        "Analyze two release surfaces and implement the bounded release note.",
        work_order_id="heterogeneous-release",
        authority_snapshot=authority,
        budget_snapshot=WorkOrderBudgetSnapshot(
            max_model_calls=12,
            max_tool_calls=12,
            max_cost_usd=4.0,
            max_wall_time_ms=10_000,
        ),
        requested_outcome="A verified release note.",
        requested_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    return authority, order


def _blueprint() -> GraphBlueprint:
    replica_common = {
        "group_id": "release-analysis",
        "strategy": ExecutionReplicaStrategy.PARTITION,
        "aggregation_task_id": "write_release",
        "aggregation": ExecutionReplicaAggregation.JOIN,
        "marginal_value_reason_template": (
            "The runtime and delivery surfaces are disjoint for {{objective}}."
        ),
    }
    return GraphBlueprint(
        blueprint_id="heterogeneous-release",
        version=1,
        objective_class="release",
        execution_profiles=("workspace_change",),
        parameters=("objective", "requested_outcome"),
        tasks=(
            GraphBlueprintTask(
                task_id="analyze_runtime",
                objective_template="Analyze runtime risk for {{objective}}",
                depends_on=(),
                required_capabilities=("analysis",),
                acceptance_templates=("Runtime evidence",),
                execution_replica=GraphBlueprintExecutionReplica(
                    replica_id="runtime",
                    scope_template="runtime boundary for {{objective}}",
                    **replica_common,
                ),
            ),
            GraphBlueprintTask(
                task_id="analyze_delivery",
                objective_template="Analyze delivery risk for {{objective}}",
                depends_on=(),
                required_capabilities=("analysis",),
                acceptance_templates=("Delivery evidence",),
                execution_replica=GraphBlueprintExecutionReplica(
                    replica_id="delivery",
                    scope_template="delivery boundary for {{objective}}",
                    **replica_common,
                ),
            ),
            GraphBlueprintTask(
                task_id="write_release",
                objective_template="Implement {{requested_outcome}} for {{objective}}",
                depends_on=("analyze_runtime", "analyze_delivery"),
                required_capabilities=("implementation",),
                acceptance_templates=("One verified release note",),
            ),
        ),
        final_task_id="write_release",
    )


def _fixture_request():
    policy = ActionPolicy(
        tool_grants=(
            ToolGrant(
                "read_workspace_file",
                (ToolEffect.READ,),
                ("workspace:repo:*",),
                max_calls=4,
            ),
            ToolGrant(
                "write_workspace_file",
                (ToolEffect.WRITE,),
                ("workspace:repo:*",),
                max_calls=2,
                requires_approval=True,
            ),
        ),
        approval_grants=("workspace-write",),
        filesystem_policy="WORKSPACE_WRITE",
        sandbox_profile="workspace-write",
    )
    authority, order = _work_order(action_policy_digest=content_digest(policy))
    constraints = GraphUserConstraints(
        pinned_employee_ids=("analyst", "writer"),
        max_concurrency=2,
        mutation_policy=GraphMutationPolicy.LOCKED,
    )
    job_limits = JobLimits(
        max_tasks=4,
        max_concurrency=2,
        max_total_model_calls=12,
        max_total_tool_calls=12,
        max_total_cost_usd=4.0,
        max_wall_time_ms=10_000,
    )
    binding = bind_blueprint(
        _blueprint(),
        work_order=order,
        constraints=constraints,
        limits=job_limits,
    )
    shared_analysis_skill = VersionedContent(
        "external-skill:bounded-release-analysis",
        "sha256:fixture-analysis-v1",
        "Inspect only the assigned release surface and cite concrete evidence.",
    )
    writer_skill = VersionedContent(
        "employee-skill:writer:release-note",
        "4",
        "Integrate verified dependency evidence into the release note.",
    )
    writer_memory = VersionedContent(
        "employee-memory:writer:release-style",
        "5",
        "Release notes use a concise risk and mitigation structure.",
    )
    request = CompanyRunRequest(
        request_id="request-heterogeneous-release",
        job_id="job-heterogeneous-release",
        goal=order.objective,
        plan_proposal=binding.proposal,
        roster=(
            EmployeeRecord(
                "analyst",
                "Release Analyst",
                ("analysis",),
                model_profile="provider-a/model-analysis",
            ),
            EmployeeRecord(
                "writer",
                "Release Writer",
                ("implementation",),
                model_profile="provider-b/model-writing",
            ),
        ),
        employee_skill_snapshots={
            "analyst": (shared_analysis_skill,),
            "writer": (writer_skill,),
        },
        context_snapshot=ContextBundle(selected_memory=(writer_memory,)),
        runtime_limits=RunLimits(
            max_wall_time_ms=8_000,
            max_model_calls=6,
            max_tool_calls=6,
            max_cost_usd=2.0,
        ),
        action_policy=policy,
        job_limits=job_limits,
        company_revision=authority.company_revision,
        roster_revision=authority.roster_revision,
        playbook_revision=authority.playbook_revision,
        session_key="company:heterogeneous-release",
        planning_mode="BLUEPRINT",
        planning_reason="PINNED_BLUEPRINT",
        work_order_id=order.work_order_id,
        work_order_digest=order.content_digest,
        work_order_authority_digest=authority.identity_digest,
        runtime_provider_binding_digest=PROVIDER_DIGEST,
        runtime_tool_contract_digest=TOOL_CONTRACT_DIGEST,
        runtime_company_coordination_digest=COORDINATION_DIGEST,
        company_work_mode="TEAM_JOB",
        coordination_policy="PLAN_FIRST",
        requested_effect="WORKSPACE_CHANGE",
        operating_reason="MULTI_CAPABILITY",
        graph_blueprint_id=binding.blueprint_ref.blueprint_id,
        graph_blueprint_version=binding.blueprint_ref.version,
        graph_blueprint_digest=binding.blueprint_ref.content_digest,
        graph_mutation_policy=binding.constraints.mutation_policy.value,
        graph_constraints_digest=content_digest(binding.constraints),
        graph_pinned_employee_ids=binding.constraints.pinned_employee_ids,
        graph_excluded_employee_ids=binding.constraints.excluded_employee_ids,
        graph_require_independent_review=(
            binding.constraints.require_independent_review
        ),
        graph_max_concurrency=binding.constraints.max_concurrency,
        graph_max_cost_usd=binding.constraints.max_cost_usd,
        graph_max_wall_time_ms=binding.constraints.max_wall_time_ms,
    )
    return binding, request, writer_memory


async def _execute_fixture():
    binding, request, writer_memory = _fixture_request()
    runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_runtime": ScriptedOutcome(
                "Runtime evidence",
                acceptance_evidence=("runtime boundary inspected",),
                usage=Usage(model_calls=1),
            ),
            "analyze_delivery": ScriptedOutcome(
                "Delivery evidence",
                acceptance_evidence=("delivery boundary inspected",),
                usage=Usage(model_calls=1),
            ),
            "write_release": ScriptedOutcome(
                "Verified release note",
                acceptance_evidence=("dependency evidence integrated",),
                usage=Usage(model_calls=1),
            ),
        }
    )
    result = await FirmKernel(employee_execution=runner).run(request)
    return binding, request, writer_memory, runner, result


def _profile_as(
    source: EmployeeCapabilityProfile,
    employee_id: str,
    *,
    model_profile: str | None = None,
    tool_names: tuple[str, ...] | None = None,
    tool_grant_digest: str | None = None,
    permission_digest: str | None = None,
) -> EmployeeCapabilityProfile:
    return EmployeeCapabilityProfile.create(
        employee_id=employee_id,
        roster_revision=source.roster_revision + 1,
        model_profile=model_profile or source.model_profile,
        capability_ids=source.capability_ids,
        skill_revision_refs=source.skill_revision_refs,
        tool_names=tool_names or source.tool_names,
        tool_grant_digest=tool_grant_digest or source.tool_grant_digest,
        permission_effects=source.permission_effects,
        permission_digest=permission_digest or source.permission_digest,
        knowledge_scopes=source.knowledge_scopes,
        memory_namespace=f"employee:{employee_id}",
        memory_revision_refs=source.memory_revision_refs,
        session_policy=source.session_policy,
        validator_ids=source.validator_ids,
        evaluation_revision=source.evaluation_revision,
    )


def _candidate(
    profile: EmployeeCapabilityProfile,
    *,
    action_policy_digest: str,
    provider_digest: str = PROVIDER_DIGEST,
) -> EmployeeBoundaryCandidate:
    return EmployeeBoundaryCandidate(
        profile=profile,
        runtime_provider_binding_digest=provider_digest,
        runtime_tool_contract_digest=TOOL_CONTRACT_DIGEST,
        runtime_action_policy_digest=action_policy_digest,
        runtime_company_coordination_digest=COORDINATION_DIGEST,
    )


class BlueprintEmployeeBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_heterogeneous_blueprint_execution_binds_actual_employee_boundaries(
        self,
    ) -> None:
        binding, request, _, runner, result = await _execute_fixture()

        execution = bind_blueprint_execution(
            binding,
            request=request,
            runtime_requests=tuple(runner.requests),
            attempt_records=result.attempt_records,
        )
        by_task = {item.task.task_id: item for item in runner.requests}
        analyst = by_task["analyze_runtime"].employee.capability_profile
        writer = by_task["write_release"].employee.capability_profile
        assert analyst is not None
        assert writer is not None

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.metrics.unique_employee_count, 2)
        self.assertEqual(
            {
                task_id: item.employee.employee_id
                for task_id, item in by_task.items()
            },
            {
                "analyze_delivery": "analyst",
                "analyze_runtime": "analyst",
                "write_release": "writer",
            },
        )
        self.assertEqual(execution.request_snapshot_digest, frozen_snapshot_digest(request))
        self.assertEqual(execution.work_order_digest, request.work_order_digest)
        self.assertEqual(
            tuple(pin.task_id for pin in execution.employee_pins),
            ("analyze_delivery", "analyze_runtime", "write_release"),
        )
        self.assertTrue(execution.request_execution_envelope_digest)
        self.assertTrue(all(pin.run_limits_digest for pin in execution.employee_pins))
        self.assertEqual(
            by_task["analyze_runtime"].session_retention,
            EmployeeSessionRetention.RUN_ONLY,
        )
        self.assertEqual(
            by_task["write_release"].session_retention,
            EmployeeSessionRetention.PERSIST,
        )
        self.assertEqual(analyst.tool_names, ("read_workspace_file",))
        self.assertEqual(
            writer.tool_names,
            ("read_workspace_file", "write_workspace_file"),
        )
        self.assertEqual(analyst.memory_revision_refs, ())
        self.assertEqual(len(writer.memory_revision_refs), 1)
        self.assertTrue(
            {
                "model_profile",
                "skill_revision_refs",
                "tool_grant_digest",
                "permission_digest",
                "memory_revision_refs",
                "session_policy",
            }.issubset(set(material_profile_difference(analyst, writer)))
        )

    async def test_binding_rejects_permission_runtime_and_knowledge_boundary_drift(
        self,
    ) -> None:
        binding, request, writer_memory, runner, result = await _execute_fixture()
        captured = tuple(runner.requests)
        analyst_index = next(
            index
            for index, item in enumerate(captured)
            if item.task.task_id == "analyze_runtime"
        )

        def with_analyst(changed: EmployeeRunRequest):
            values = list(captured)
            values[analyst_index] = changed
            return tuple(values)

        analyst = captured[analyst_index]
        cases = (
            (
                "ActionPolicy",
                replace(analyst, action_policy=request.action_policy),
            ),
            (
                "runtime limits",
                replace(
                    analyst,
                    limits=replace(
                        analyst.limits,
                        max_tool_calls=request.job_limits.max_total_tool_calls + 1,
                    ),
                ),
            ),
            (
                "Knowledge or memory",
                replace(
                    analyst,
                    context=replace(
                        analyst.context,
                        selected_memory=(writer_memory,),
                    ),
                ),
            ),
            (
                "Skill selection",
                replace(
                    analyst,
                    employee=replace(
                        analyst.employee,
                        skills=request.employee_skill_snapshots["writer"],
                    ),
                ),
            ),
        )
        for expected, changed in cases:
            with self.subTest(boundary=expected):
                with self.assertRaisesRegex(ValueError, expected):
                    bind_blueprint_execution(
                        binding,
                        request=request,
                        runtime_requests=with_analyst(changed),
                        attempt_records=result.attempt_records,
                    )

        with self.assertRaisesRegex(ValueError, "frozen successful dispatch"):
            bind_blueprint_execution(
                binding,
                request=replace(request, runtime_provider_binding_digest="4" * 64),
                runtime_requests=captured,
                attempt_records=result.attempt_records,
            )
        with self.assertRaisesRegex(ValueError, "missing Kernel attempt evidence"):
            bind_blueprint_execution(
                binding,
                request=request,
                runtime_requests=captured,
                attempt_records=(),
            )
        with self.assertRaisesRegex(ValueError, "content hash is invalid"):
            bind_blueprint_execution(
                binding,
                request=request,
                runtime_requests=captured,
                attempt_records=(
                    replace(result.attempt_records[0], content_hash="0" * 64),
                    *result.attempt_records[1:],
                ),
            )

    async def test_substitution_is_data_only_exact_degraded_or_safe_refusal(
        self,
    ) -> None:
        binding, request, _, runner, result = await _execute_fixture()
        execution = bind_blueprint_execution(
            binding,
            request=request,
            runtime_requests=tuple(runner.requests),
            attempt_records=result.attempt_records,
        )
        source_request = next(
            item for item in runner.requests if item.task.task_id == "analyze_runtime"
        )
        source = source_request.employee.capability_profile
        assert source is not None
        analyst_policy_digest = content_digest(source_request.action_policy)

        available = plan_employee_substitution(
            execution,
            task_id="analyze_runtime",
            candidates=(
                _candidate(
                    source,
                    action_policy_digest=analyst_policy_digest,
                ),
            ),
        )
        self.assertEqual(
            available.disposition,
            EmployeeSubstitutionDisposition.PIN_AVAILABLE,
        )
        self.assertEqual(available.selected_employee_id, "analyst")
        self.assertFalse(available.execution_authority)

        exact_profile = _profile_as(source, "analyst-substitute")
        exact = plan_employee_substitution(
            execution,
            task_id="analyze_runtime",
            candidates=(
                _candidate(
                    exact_profile,
                    action_policy_digest=analyst_policy_digest,
                ),
            ),
        )
        self.assertEqual(
            exact.disposition,
            EmployeeSubstitutionDisposition.EXACT_COMPATIBLE_SUBSTITUTE,
        )
        self.assertEqual(exact.selected_employee_id, "analyst-substitute")
        self.assertFalse(exact.requires_user_choice)
        self.assertTrue(exact.requires_new_frozen_request)
        self.assertFalse(exact.execution_authority)
        exact.verify()

        provider_only = _profile_as(source, "analyst-provider-moved")
        changed_surface = _profile_as(
            source,
            "analyst-degraded",
            model_profile="provider-c/model-analysis",
            tool_names=("read_workspace_file", "search_workspace"),
            tool_grant_digest="8" * 64,
            permission_digest="9" * 64,
        )
        degraded = plan_employee_substitution(
            execution,
            task_id="analyze_runtime",
            candidates=(
                _candidate(
                    changed_surface,
                    action_policy_digest=analyst_policy_digest,
                    provider_digest="4" * 64,
                ),
                _candidate(
                    provider_only,
                    action_policy_digest=analyst_policy_digest,
                    provider_digest="4" * 64,
                ),
            ),
        )
        self.assertEqual(
            degraded.disposition,
            EmployeeSubstitutionDisposition.DEGRADED_USER_CHOICE,
        )
        self.assertIsNone(degraded.selected_employee_id)
        self.assertTrue(degraded.requires_user_choice)
        differences = {
            item.employee_id: set(item.difference_dimensions)
            for item in degraded.choices
        }
        self.assertEqual(
            differences["analyst-provider-moved"],
            {"runtime_provider_binding"},
        )
        self.assertTrue(
            {
                "model_profile",
                "tool_grant_digest",
                "permission_digest",
                "runtime_provider_binding",
            }.issubset(differences["analyst-degraded"])
        )
        self.assertFalse(degraded.execution_authority)
        degraded.verify()

        writer_request = next(
            item for item in runner.requests if item.task.task_id == "write_release"
        )
        writer_source = writer_request.employee.capability_profile
        assert writer_source is not None
        writer_policy_digest = content_digest(writer_request.action_policy)
        private_state_degraded = plan_employee_substitution(
            execution,
            task_id="write_release",
            candidates=(
                _candidate(
                    _profile_as(writer_source, "writer-substitute"),
                    action_policy_digest=writer_policy_digest,
                ),
            ),
        )
        self.assertEqual(
            private_state_degraded.disposition,
            EmployeeSubstitutionDisposition.DEGRADED_USER_CHOICE,
        )
        self.assertIn(
            "memory_namespace",
            private_state_degraded.choices[0].difference_dimensions,
        )

        refusal = plan_employee_substitution(
            execution,
            task_id="analyze_runtime",
            candidates=(),
        )
        self.assertEqual(
            refusal.disposition,
            EmployeeSubstitutionDisposition.SAFE_REFUSAL,
        )
        self.assertIsNone(refusal.selected_employee_id)
        self.assertEqual(refusal.choices, ())
        self.assertFalse(refusal.execution_authority)
        refusal.verify()


if __name__ == "__main__":
    unittest.main()
