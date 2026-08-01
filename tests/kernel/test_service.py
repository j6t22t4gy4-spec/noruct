from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from dynamic_firm.kernel.models import (
    AttemptFailureKind,
    EmployeeRecord,
    ExecutionReplicaAggregation,
    ExecutionReplicaSpec,
    ExecutionReplicaStrategy,
    GraphPatch,
    GraphPatchOperation,
    GraphPatchProposalStatus,
    GraphMutationLease,
    JobGraph,
    JobTask,
    JobLimits,
    JobStatus,
    PatchOperationKind,
    SemanticOperation,
    TaskMutationType,
)
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.graph import apply_patch, graph_from_proposal
from dynamic_firm.kernel.supervision import (
    ManagerSupervisionAction,
    ManagerSupervisionDecision,
)
from dynamic_firm.kernel.mutation import (
    content_digest,
    frozen_snapshot_digest,
    graph_patch_event,
    graph_patch_proposal_event,
    graph_patch_proposal_resolution_event,
)
from dynamic_firm.company import ManagerDelegation, episode_from_runtime_ledger
from dynamic_firm.kernel.testing import (
    ScriptedEmployeeExecutionPort,
    ScriptedOutcome,
    StaticReplanner,
)
from dynamic_firm.runtime.job_ledger import (
    ActiveJobAuditStatus,
    ActiveJobInspector,
    SQLiteActiveJobLedger,
)
from dynamic_firm.runtime.company_coordination import CompanyCoordinationError
from dynamic_firm.runtime.models import (
    ApprovalDecision,
    ActionPolicy,
    Failure,
    FailureCategory,
    RunStatus,
    RunSignal,
    RunHandle,
    SignalCode,
    ToolEffect,
    ToolGrant,
    Usage,
    VersionedContent,
)
from dynamic_firm.runtime.store import RunStore
from tests.kernel.helpers import company_request, task


class FirmKernelTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_supervision_is_advisory_and_direct_jobs_skip_it(self) -> None:
        class Supervisor:
            def __init__(self) -> None:
                self.contexts = []

            async def assess(self, context):  # type: ignore[no-untyped-def]
                self.contexts.append(context)
                return ManagerSupervisionDecision(
                    action=ManagerSupervisionAction.CONTINUE,
                    rationale="The current valid graph remains sufficient.",
                )

        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            ),
            manager_employee_id="manager",
            manager_assignment_digest="a" * 64,
            manager_session_key="manager:manager:test",
            work_order_id="work-order-manager-supervision",
            work_order_digest="b" * 64,
            manager_employee=EmployeeRecord(
                "manager",
                "Executive Manager",
                ("company_management",),
            ),
            company_work_mode="TEAM_JOB",
        )
        delegation = ManagerDelegation.from_proposal_payload(
            assignment_digest=request.manager_assignment_digest,
            manager_employee_id=request.manager_employee_id,
            work_order_id=request.work_order_id,
            work_order_digest=request.work_order_digest,
            proposal=request.plan_proposal,
        )
        request = replace(
            request,
            manager_delegation_payload=delegation.canonical_payload(),
            manager_delegation_digest=delegation.content_digest,
        )
        supervisor = Supervisor()
        store = RunStore()
        result = await FirmKernel(
            employee_execution=ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("done")}
            ),
            manager_supervisor=supervisor,
            active_job_ledger=SQLiteActiveJobLedger(store),
        ).run(request)
        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(len(supervisor.contexts), 1)
        self.assertEqual(supervisor.contexts[0].priority, "FINAL_INTEGRATION")
        supervision = store.get_job_supervision_events(request.job_id)
        self.assertEqual(len(supervision), 1)
        self.assertEqual(supervision[0]["action"], "CONTINUE")
        store.close()

        direct = replace(
            request,
            company_work_mode="DIRECT",
            manager_delegation_payload={},
            manager_delegation_digest="",
        )
        direct_supervisor = Supervisor()
        await FirmKernel(
            employee_execution=ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("done")}
            ),
            manager_supervisor=direct_supervisor,
        ).run(direct)
        self.assertEqual(direct_supervisor.contexts, [])

    async def test_value_gated_same_employee_replicas_run_in_parallel_and_remain_isolated(self) -> None:
        def replica(task_id: str, replica_id: str, scope: str) -> JobTask:
            return replace(
                task(task_id),
                execution_replica=ExecutionReplicaSpec(
                    group_id="wide_analysis",
                    replica_id=replica_id,
                    strategy=ExecutionReplicaStrategy.PARTITION,
                    scope=scope,
                    aggregation_task_id="final",
                    aggregation=ExecutionReplicaAggregation.JOIN,
                    marginal_value_reason=(
                        "Two disjoint source ranges improve bounded coverage and wall time."
                    ),
                ),
            )

        runner = ScriptedEmployeeExecutionPort(
            {
                "range_a": ScriptedOutcome("A", delay_seconds=0.02),
                "range_b": ScriptedOutcome("B", delay_seconds=0.02),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        assignments = []
        request = company_request(
            (
                replica("range_a", "a", "sources 1-50"),
                replica("range_b", "b", "sources 51-100"),
                task("final", depends_on=("range_a", "range_b")),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("analyst-a", "Analyst A", ("analysis",)),
                EmployeeRecord("analyst-b", "Analyst B", ("analysis",)),
            ),
            limits=JobLimits(max_concurrency=2, max_wall_time_ms=5_000),
        )

        with TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runtime.db")
            result = await FirmKernel(
                employee_execution=runner,
                assignment_sink=assignments.append,
                active_job_ledger=SQLiteActiveJobLedger(store),
            ).run(request)
            inspection = ActiveJobInspector(store).inspect(request.job_id)
            store.close()

        replica_requests = runner.requests[:2]
        self.assertEqual(
            [item.employee.employee_id for item in replica_requests],
            ["analyst-a", "analyst-a"],
        )
        self.assertEqual(runner.maximum_parallelism, 2)
        self.assertTrue(
            all(item.session_retention.value == "RUN_ONLY" for item in replica_requests)
        )
        self.assertEqual(result.metrics.unique_employee_count, 1)
        self.assertEqual(result.metrics.execution_replica_count, 2)
        self.assertEqual(result.metrics.replica_group_count, 1)
        self.assertEqual(result.company_work_mode, "TEAM_JOB")
        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.TERMINAL)
        self.assertTrue(inspection.replay_matches)
        self.assertEqual(
            {item["replica_group_id"] for item in inspection.attempts},
            {"", "wide_analysis"},
        )
        self.assertEqual(
            inspection.execution_replica_groups,
            (
                {
                    "group_id": "wide_analysis",
                    "strategy": "PARTITION",
                    "aggregation_task_id": "final",
                    "aggregation": "JOIN",
                    "marginal_value_reason": (
                        "Two disjoint source ranges improve bounded coverage and wall time."
                    ),
                    "member_task_ids": ("range_a", "range_b"),
                },
            ),
        )
        self.assertEqual(inspection.job_limits["max_concurrency"], 2)
        replica_events = [event for event in assignments if event.replica_group_id]
        self.assertEqual(len(replica_events), 2)
        self.assertTrue(
            all(
                event.selection_reason == "VALUE_GATED_EXECUTION_REPLICA"
                for event in replica_events
            )
        )
        self.assertEqual(
            {event.execution_instance_id for event in replica_events},
            {
                "fixture-job:wide_analysis:a:attempt-1",
                "fixture-job:wide_analysis:b:attempt-1",
            },
        )

    async def test_identity_only_employee_clones_do_not_manufacture_parallelism(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "analysis_a": ScriptedOutcome("A", delay_seconds=0.02),
                "analysis_b": ScriptedOutcome("B", delay_seconds=0.02),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        assignments = []
        request = company_request(
            (
                task("analysis_a"),
                task("analysis_b"),
                task(
                    "final",
                    depends_on=("analysis_a", "analysis_b"),
                    capabilities=("integration",),
                ),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("analyst-a", "Analyst A", ("analysis",)),
                EmployeeRecord("analyst-b", "Analyst B", ("analysis",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )

        result = await FirmKernel(
            employee_execution=runner,
            assignment_sink=assignments.append,
        ).run(request)

        analysis_requests = [
            item for item in runner.requests if item.task.task_id.startswith("analysis_")
        ]
        self.assertEqual(
            [item.employee.employee_id for item in analysis_requests],
            ["analyst-a", "analyst-a"],
        )
        self.assertEqual(result.metrics.maximum_parallelism, 1)
        self.assertNotIn("analyst-b", [item.employee.employee_id for item in runner.requests])
        self.assertTrue(all(event.capability_profile_digest for event in assignments))
        self.assertEqual(
            next(event for event in assignments if event.task_id == "analysis_b").selection_reason,
            "HOMOGENEOUS_CLONE_COLLAPSED",
        )
        self.assertEqual(
            len(
                {
                    event.capability_material_digest
                    for event in assignments
                    if event.task_id.startswith("analysis_")
                }
            ),
            1,
        )

    async def test_independent_review_rejects_an_identity_only_clone(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "review": ScriptedOutcome("Reviewed"),
                "final": ScriptedOutcome("Must not run"),
            }
        )
        request = company_request(
            (
                task("review", capabilities=("review",)),
                task("final", depends_on=("review",), capabilities=("review",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("reviewer-a", "Reviewer A", ("review",)),
                EmployeeRecord("reviewer-b", "Reviewer B", ("review",)),
            ),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.STALLED)
        self.assertEqual(len(runner.requests), 1)
        self.assertIn("materially independent", result.failure_reason)

    async def test_read_only_final_integration_runs_as_persistent_manager(self) -> None:
        manager = EmployeeRecord(
            "manager",
            "Executive Manager",
            ("company_management",),
            model_profile="manager-model",
        )
        request = replace(
            company_request(
                (
                    task("research", capabilities=("analysis",)),
                    task(
                        "final",
                        depends_on=("research",),
                        capabilities=("integration",),
                    ),
                ),
                final_task_id="final",
                roster=(
                    EmployeeRecord("analyst", "Analyst", ("analysis",)),
                    EmployeeRecord("integrator", "Integrator", ("integration",)),
                ),
            ),
            requested_effect="READ",
            company_work_mode="TEAM_JOB",
            coordination_policy="PLAN_FIRST",
            work_order_id="work-order-manager-final",
            work_order_digest="b" * 64,
            manager_employee_id=manager.employee_id,
            manager_assignment_digest="a" * 64,
            manager_session_key="manager:manager:session-1",
            manager_employee=manager,
            action_policy=ActionPolicy(
                tool_grants=(
                    ToolGrant("read", (ToolEffect.READ,)),
                    ToolGrant(
                        "write",
                        (ToolEffect.WRITE,),
                        requires_approval=True,
                    ),
                    ToolGrant(
                        "run",
                        (ToolEffect.EXECUTE,),
                        requires_approval=True,
                    ),
                ),
                filesystem_policy="WORKSPACE_WRITE",
                sandbox_profile="host-workspace-approved",
            ),
        )
        delegation = ManagerDelegation.from_proposal_payload(
            assignment_digest=request.manager_assignment_digest,
            manager_employee_id=manager.employee_id,
            work_order_id=request.work_order_id,
            work_order_digest=request.work_order_digest,
            proposal=request.plan_proposal,
        )
        request = replace(
            request,
            manager_delegation_payload=delegation.canonical_payload(),
            manager_delegation_digest=delegation.content_digest,
        )
        assignments = []
        runner = ScriptedEmployeeExecutionPort(
            {
                "research": ScriptedOutcome(
                    "Cited specialist evidence",
                    acceptance_evidence=("evidence:1",),
                ),
                ("final", "manager"): ScriptedOutcome("Manager integrated report"),
            }
        )

        result = await FirmKernel(
            employee_execution=runner,
            assignment_sink=assignments.append,
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.summary, "Manager integrated report")
        self.assertEqual(result.manager_employee_id, manager.employee_id)
        self.assertEqual(tuple(item.employee.employee_id for item in runner.requests), ("analyst", "manager"))
        manager_request = runner.requests[-1]
        self.assertEqual(manager_request.session_key, "manager:manager:session-1")
        self.assertEqual(
            tuple(grant.tool_name for grant in manager_request.action_policy.tool_grants),
            ("read",),
        )
        self.assertEqual(manager_request.action_policy.filesystem_policy, "READ_ONLY")
        self.assertTrue(
            any("Cited specialist evidence" in item.content for item in manager_request.context.task_dependencies)
        )
        self.assertEqual(assignments[-1].selection_reason, "PERSISTENT_MANAGER_FINAL_INTEGRATION")

        tampered_payload = dict(request.manager_delegation_payload)
        tampered_payload["authority_granted"] = True
        with self.assertRaisesRegex(ValueError, "payload digest"):
            FirmKernel._validate_request(
                replace(request, manager_delegation_payload=tampered_payload)
            )

        tampered_tasks = list(request.manager_delegation_payload["tasks"])
        tampered_task = dict(tampered_tasks[0])
        tampered_task["objective"] = "Replace the frozen specialist objective."
        tampered_tasks[0] = tampered_task
        tampered_contract = dict(request.manager_delegation_payload)
        tampered_contract["tasks"] = tuple(tampered_tasks)
        with self.assertRaisesRegex(ValueError, "does not match"):
            FirmKernel._validate_request(
                replace(
                    request,
                    manager_delegation_payload=tampered_contract,
                    manager_delegation_digest=content_digest(tampered_contract),
                )
            )

    async def test_compiler_provenance_and_usage_are_owned_by_kernel_result(self) -> None:
        compiler_usage = Usage(
            model_calls=1,
            input_tokens=120,
            cached_input_tokens=20,
            output_tokens=30,
            cost_usd=0.25,
        )
        employee_usage = Usage(
            model_calls=2,
            tool_calls=1,
            input_tokens=80,
            output_tokens=40,
            cost_usd=0.40,
        )
        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            ),
            planning_mode="DYNAMIC",
            planning_reason="VALID_DYNAMIC",
            compiler_usage=compiler_usage,
            compiler_provider_request_id="compiler-request-42",
            work_order_id="work-order-42",
            work_order_digest="d" * 64,
            work_order_authority_digest="e" * 64,
            firm_admission_digest="f" * 64,
        )

        result = await FirmKernel(
            employee_execution=ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("done", usage=employee_usage)}
            )
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.planning_mode, "DYNAMIC")
        self.assertEqual(result.planning_reason, "VALID_DYNAMIC")
        self.assertEqual(result.compiler_usage, compiler_usage)
        self.assertEqual(
            result.compiler_provider_request_id,
            "compiler-request-42",
        )
        self.assertEqual(result.work_order_id, "work-order-42")
        self.assertEqual(result.work_order_digest, "d" * 64)
        self.assertEqual(result.work_order_authority_digest, "e" * 64)
        self.assertEqual(result.firm_admission_digest, "f" * 64)
        self.assertEqual(
            result.metrics.usage,
            compiler_usage.plus(employee_usage),
        )

    async def test_compiler_that_consumes_model_budget_terminalizes_before_dispatch(
        self,
    ) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {"final": ScriptedOutcome("must not execute")}
        )
        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
                limits=JobLimits(
                    max_total_model_calls=1,
                    max_total_cost_usd=2.0,
                    max_wall_time_ms=5_000,
                ),
            ),
            planning_mode="SOLO_FALLBACK",
            planning_reason="COMPILER_BUDGET_EXHAUSTED",
            compiler_usage=Usage(model_calls=1, cost_usd=0.10),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertEqual(runner.requests, [])
        self.assertEqual(result.metrics.usage, request.compiler_usage)
        self.assertIn("compiler consumed", result.failure_reason.lower())

    async def test_compiler_wall_timeout_never_starts_an_employee(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {"final": ScriptedOutcome("must not execute")}
        )
        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            ),
            planning_mode="SOLO_FALLBACK",
            planning_reason="COMPILER_WALL_TIME_EXHAUSTED",
            compiler_usage=Usage(model_calls=1, cost_usd=0.10),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertEqual(runner.requests, [])
        self.assertEqual(result.metrics.usage, request.compiler_usage)
        self.assertIn("compiler consumed", result.failure_reason.lower())

    async def test_front_door_wall_timeout_never_starts_managed_employee(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {"final": ScriptedOutcome("must not execute")}
        )
        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            ),
            planning_mode="SOLO",
            planning_reason="JOB_WALL_TIME_EXHAUSTED_BEFORE_DISPATCH",
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertEqual(runner.requests, [])
        self.assertIn("before Employee dispatch", result.failure_reason)

    async def test_terminal_cancellation_is_durably_replayable_for_parallel_attempts(
        self,
    ) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "analysis-a": ScriptedOutcome("Late A", delay_seconds=0.2),
                "analysis-b": ScriptedOutcome("Late B", delay_seconds=0.2),
                "final": ScriptedOutcome("Should not start"),
            }
        )
        request = company_request(
            (
                task("analysis-a", capabilities=("analysis-a",)),
                task("analysis-b", capabilities=("analysis-b",)),
                task(
                    "final",
                    depends_on=("analysis-a", "analysis-b"),
                    capabilities=("integration",),
                ),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("analyst-a", "Analyst A", ("analysis-a",)),
                EmployeeRecord("analyst-b", "Analyst B", ("analysis-b",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
            limits=JobLimits(max_concurrency=2, max_wall_time_ms=15),
        )

        with TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runtime.db")
            result = await FirmKernel(
                employee_execution=runner,
                active_job_ledger=SQLiteActiveJobLedger(store),
            ).run(request)
            inspection = ActiveJobInspector(store).inspect(request.job_id)
            store.close()

        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertEqual(len(result.attempt_records), 2)
        self.assertTrue(
            all(item.failure_kind == AttemptFailureKind.CANCELLED for item in result.attempt_records)
        )
        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.TERMINAL)
        self.assertTrue(inspection.replay_matches)
        self.assertEqual(inspection.attempt_count, 2)

    async def test_hanging_cancel_port_cannot_hold_kernel_terminalization(self) -> None:
        class HangingCancelPort:
            async def start(self, request):  # type: ignore[no-untyped-def]
                return RunHandle("hanging-run", request.request_id)

            async def collect(self, handle):  # type: ignore[no-untyped-def]
                await asyncio.Event().wait()

            async def cancel(self, handle, reason):  # type: ignore[no-untyped-def]
                await asyncio.Event().wait()

        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            limits=JobLimits(max_wall_time_ms=5),
        )
        started = asyncio.get_running_loop().time()

        result = await FirmKernel(employee_execution=HangingCancelPort()).run(request)

        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertLess(elapsed, 1.0)

    async def test_plan_only_success_retries_once_with_fixed_safe_instruction(self) -> None:
        raw_plan = "I will inspect CUSTOMER_SECRET_123 next and then implement the fix."
        runner = ScriptedEmployeeExecutionPort(
            {
                "final": (
                    ScriptedOutcome(raw_plan),
                    ScriptedOutcome("The repository defect is in the dispatch boundary."),
                )
            }
        )
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(runner.started_order, ["final", "final"])
        self.assertEqual(result.metrics.task_mutation_count, 1)
        self.assertEqual(
            result.mutation_events[0].failure_kind,
            AttemptFailureKind.RECOVERABLE_LIVENESS,
        )
        instructions = runner.requests[1].context.ephemeral_instructions
        self.assertEqual(len(instructions), 1)
        self.assertIn("Take the first safe concrete action now", instructions[0])
        self.assertNotIn("CUSTOMER_SECRET_123", instructions[0])

    async def test_plan_only_retry_exhaustion_never_creates_third_attempt(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "final": (
                    ScriptedOutcome("I will inspect the repository next."),
                    ScriptedOutcome("Let me check the files first."),
                )
            }
        )
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(runner.started_order, ["final", "final"])
        self.assertEqual(len(result.attempt_records), 2)
        self.assertEqual(len(result.mutation_events), 1)

    async def test_plan_only_write_capable_run_is_not_automatically_retried(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {"final": ScriptedOutcome("I will update the production file next.")}
        )
        base = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("writer", "Writer", ("analysis",)),),
        )
        request = replace(
            base,
            action_policy=ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        "write_workspace_file",
                        (ToolEffect.WRITE,),
                        requires_approval=True,
                    ),
                ),
                filesystem_policy="WORKSPACE_WRITE",
                sandbox_profile="host-workspace-approved",
            ),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(result.mutation_events, ())

    async def test_declared_approval_blocker_fails_without_automatic_retry(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "final": ScriptedOutcome(
                    "I cannot proceed because I need user approval and credentials."
                )
            }
        )
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(result.mutation_events, ())
        self.assertEqual(
            result.attempt_records[0].failure_kind,
            AttemptFailureKind.POLICY_DENIED,
        )

    async def test_recoverable_read_failure_retries_once_before_downstream(self) -> None:
        transient = Failure(
            code="MODEL_RATE_LIMIT",
            category=FailureCategory.MODEL,
            message_safe="The model endpoint was temporarily rate limited.",
            retryable=True,
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                "analysis": (
                    ScriptedOutcome(
                        "Transient failure",
                        status=RunStatus.FAILED,
                        failure=transient,
                    ),
                    ScriptedOutcome("Analysis recovered"),
                ),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        emitted = []
        request = replace(
            company_request(
                (
                    task("analysis"),
                    task("final", depends_on=("analysis",), capabilities=("integration",)),
                ),
                final_task_id="final",
                roster=(
                    EmployeeRecord("analyst", "Analyst", ("analysis",)),
                    EmployeeRecord("integrator", "Integrator", ("integration",)),
                ),
            ),
            company_revision=3,
            roster_revision=5,
            playbook_revision=7,
        )

        result = await FirmKernel(
            employee_execution=runner,
            mutation_sink=emitted.append,
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(runner.started_order, ["analysis", "analysis", "final"])
        analysis_requests = [
            item for item in runner.requests if item.task.task_id == "analysis"
        ]
        self.assertEqual([item.task.attempt for item in analysis_requests], [1, 2])
        self.assertEqual(
            [item.employee.employee_id for item in analysis_requests],
            ["analyst", "analyst"],
        )
        self.assertEqual(result.metrics.task_mutation_count, 1)
        self.assertEqual(len(result.mutation_events), 1)
        event = result.mutation_events[0]
        self.assertEqual(event.mutation_type, TaskMutationType.RETRY)
        self.assertEqual(event.failure_kind, AttemptFailureKind.RECOVERABLE_MODEL)
        self.assertEqual(event.from_employee_id, event.to_employee_id)
        self.assertEqual(event.downstream_task_ids, ("final",))
        self.assertEqual(event.mutation_budget_before, 2)
        self.assertEqual(event.mutation_budget_after, 1)
        self.assertEqual(emitted, [event])
        analysis_attempts = [
            item for item in result.attempt_records if item.task_id == "analysis"
        ]
        self.assertEqual(len(analysis_attempts), 2)
        self.assertEqual(analysis_attempts[1].source_attempt_id, analysis_attempts[0].attempt_id)
        self.assertEqual(
            {item.frozen_snapshot_hash for item in result.attempt_records},
            {event.frozen_snapshot_hash},
        )
        self.assertEqual(result.final_graph_version, 1)

    async def test_explicit_assignee_mismatch_reroutes_to_frozen_existing_employee(self) -> None:
        mismatch = Failure(
            code="ASSIGNEE_CAPABILITY_MISMATCH",
            category=FailureCategory.INPUT,
            message_safe="This assignment needs another eligible analyst.",
        )
        signal = RunSignal(
            SignalCode.ASSIGNEE_MISMATCH,
            value="analysis",
            evidence=("typed:assignment-mismatch",),
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                ("analysis", "analyst-a"): ScriptedOutcome(
                    "Assignment mismatch",
                    status=RunStatus.FAILED,
                    signals=(signal,),
                    failure=mismatch,
                ),
                ("analysis", "analyst-b"): ScriptedOutcome("Reassigned analysis"),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        request = company_request(
            (
                task("analysis"),
                task("final", depends_on=("analysis",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("analyst-a", "Analyst A", ("analysis",)),
                EmployeeRecord("analyst-b", "Analyst B", ("analysis",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        analysis_requests = [
            item for item in runner.requests if item.task.task_id == "analysis"
        ]
        self.assertEqual(
            [item.employee.employee_id for item in analysis_requests],
            ["analyst-a", "analyst-b"],
        )
        self.assertEqual(len(result.mutation_events), 1)
        event = result.mutation_events[0]
        self.assertEqual(event.mutation_type, TaskMutationType.REROUTE)
        self.assertEqual(event.from_employee_id, "analyst-a")
        self.assertEqual(event.to_employee_id, "analyst-b")
        self.assertEqual(event.matched_capabilities, ("analysis",))
        self.assertEqual(runner.started_order[-1], "final")

    async def test_retry_exhaustion_never_creates_a_third_attempt(self) -> None:
        transient = Failure(
            code="TOOL_READ_TRANSIENT",
            category=FailureCategory.TOOL,
            message_safe="Read tool temporarily unavailable.",
            retryable=True,
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                "analysis": (
                    ScriptedOutcome("Failed once", status=RunStatus.FAILED, failure=transient),
                    ScriptedOutcome("Failed twice", status=RunStatus.FAILED, failure=transient),
                ),
                "final": ScriptedOutcome("Must not run"),
            }
        )
        request = company_request(
            (
                task("analysis"),
                task("final", depends_on=("analysis",)),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(runner.started_order, ["analysis", "analysis"])
        self.assertEqual(len(result.attempt_records), 2)
        self.assertEqual(len(result.mutation_events), 1)

    async def test_job_mutation_budget_blocks_a_second_mutation_kind(self) -> None:
        mismatch = Failure(
            "ASSIGNEE_CAPABILITY_MISMATCH",
            FailureCategory.INPUT,
            "Another employee is required.",
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                ("final", "analyst-a"): (
                    ScriptedOutcome(
                        "Transient",
                        status=RunStatus.FAILED,
                        failure=Failure(
                            "MODEL_TRANSIENT",
                            FailureCategory.MODEL,
                            "Temporary model failure.",
                            retryable=True,
                        ),
                    ),
                    ScriptedOutcome(
                        "Mismatch",
                        status=RunStatus.FAILED,
                        failure=mismatch,
                        signals=(RunSignal(SignalCode.ASSIGNEE_MISMATCH, "analysis"),),
                    ),
                ),
                ("final", "analyst-b"): ScriptedOutcome("Must not run"),
            }
        )
        base = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(
                EmployeeRecord("analyst-a", "Analyst A", ("analysis",)),
                EmployeeRecord("analyst-b", "Analyst B", ("analysis",)),
            ),
        )
        request = replace(
            base,
            job_limits=replace(base.job_limits, max_task_mutations=1),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(
            [item.employee.employee_id for item in runner.requests],
            ["analyst-a", "analyst-a"],
        )
        self.assertEqual(
            [item.mutation_type for item in result.mutation_events],
            [TaskMutationType.RETRY],
        )

    async def test_policy_safety_internal_and_write_authority_are_not_retried(self) -> None:
        cases = (
            Failure(
                "TOOL_APPROVAL_DENIED",
                FailureCategory.POLICY,
                "The user rejected the requested action.",
                retryable=True,
            ),
            Failure(
                "SAFETY_VIOLATION",
                FailureCategory.TOOL,
                "The action violated the safety boundary.",
                retryable=True,
            ),
            Failure(
                "RUNTIME_BOUNDARY_FAILED",
                FailureCategory.INTERNAL,
                "The runtime boundary failed.",
                retryable=True,
            ),
        )
        expected = (
            AttemptFailureKind.APPROVAL_REJECTED,
            AttemptFailureKind.SAFETY_VIOLATION,
            AttemptFailureKind.INTERNAL_ERROR,
        )
        for failure, kind in zip(cases, expected, strict=True):
            with self.subTest(kind=kind):
                runner = ScriptedEmployeeExecutionPort(
                    {
                        "final": ScriptedOutcome(
                            "Rejected",
                            status=RunStatus.FAILED,
                            failure=failure,
                        )
                    }
                )
                request = company_request(
                    (task("final"),),
                    final_task_id="final",
                    roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
                )
                result = await FirmKernel(employee_execution=runner).run(request)
                self.assertEqual(len(runner.requests), 1)
                self.assertEqual(result.mutation_events, ())
                self.assertEqual(result.attempt_records[0].failure_kind, kind)

        unknown_runner = ScriptedEmployeeExecutionPort(
            {
                "final": ScriptedOutcome(
                    "Unknown failure",
                    status=RunStatus.FAILED,
                    synthesize_failure=False,
                )
            }
        )
        unknown_request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        unknown_result = await FirmKernel(employee_execution=unknown_runner).run(
            unknown_request
        )
        self.assertEqual(unknown_result.mutation_events, ())
        self.assertEqual(
            unknown_result.attempt_records[0].failure_kind,
            AttemptFailureKind.UNKNOWN,
        )

        write_runner = ScriptedEmployeeExecutionPort(
            {
                "final": ScriptedOutcome(
                    "Transient but mutation-capable",
                    status=RunStatus.FAILED,
                    failure=Failure(
                        "MODEL_RATE_LIMIT",
                        FailureCategory.MODEL,
                        "Temporary model failure.",
                        retryable=True,
                    ),
                )
            }
        )
        write_base = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("writer", "Writer", ("analysis",)),),
        )
        write_request = replace(
            write_base,
            action_policy=ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        "write_workspace_file",
                        (ToolEffect.WRITE,),
                        requires_approval=True,
                    ),
                ),
                filesystem_policy="WORKSPACE_WRITE",
                sandbox_profile="host-workspace-approved",
            ),
        )
        write_result = await FirmKernel(employee_execution=write_runner).run(write_request)
        self.assertEqual(len(write_runner.requests), 1)
        self.assertEqual(write_result.mutation_events, ())

    async def test_reroute_is_bounded_once_and_never_cycles_assignees(self) -> None:
        signal = RunSignal(SignalCode.ASSIGNEE_MISMATCH, "analysis")
        mismatch = Failure(
            "ASSIGNEE_CAPABILITY_MISMATCH",
            FailureCategory.INPUT,
            "Another assignee is required.",
        )
        failed = ScriptedOutcome(
            "Mismatch",
            status=RunStatus.FAILED,
            signals=(signal,),
            failure=mismatch,
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                ("final", "analyst-a"): failed,
                ("final", "analyst-b"): failed,
                ("final", "analyst-c"): ScriptedOutcome("Must not run"),
            }
        )
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=tuple(
                EmployeeRecord(f"analyst-{suffix}", f"Analyst {suffix}", ("analysis",))
                for suffix in ("a", "b", "c")
            ),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(
            [item.employee.employee_id for item in runner.requests],
            ["analyst-a", "analyst-b"],
        )
        self.assertEqual(len(result.mutation_events), 1)

    async def test_same_fixture_replays_identical_mutation_identity(self) -> None:
        async def run_once():
            runner = ScriptedEmployeeExecutionPort(
                {
                    "final": (
                        ScriptedOutcome(
                            "Transient",
                            status=RunStatus.FAILED,
                            failure=Failure(
                                "MODEL_TRANSIENT",
                                FailureCategory.MODEL,
                                "Temporary model failure.",
                                retryable=True,
                            ),
                        ),
                        ScriptedOutcome("Recovered"),
                    )
                }
            )
            request = replace(
                company_request(
                    (task("final"),),
                    final_task_id="final",
                    roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
                ),
                company_revision=4,
                roster_revision=6,
                playbook_revision=8,
            )
            return await FirmKernel(employee_execution=runner).run(request)

        first = await run_once()
        second = await run_once()

        self.assertEqual(first.status, JobStatus.SUCCEEDED)
        self.assertEqual(
            [(item.event_id, item.content_hash) for item in first.mutation_events],
            [(item.event_id, item.content_hash) for item in second.mutation_events],
        )
        self.assertEqual(
            [item.attempt_id for item in first.attempt_records],
            [item.attempt_id for item in second.attempt_records],
        )

    async def test_employee_skill_snapshot_is_bound_only_to_matching_employee(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "analysis": ScriptedOutcome("Analysis"),
                "final": ScriptedOutcome("Done"),
            }
        )
        skill = VersionedContent(
            "employee-skill:analyst:targeted-validation:context",
            "2",
            "bounded procedure",
            "skill-hash",
        )
        base = company_request(
            (
                task("analysis", capabilities=("analysis",)),
                task("final", depends_on=("analysis",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("analyst", "Analyst", ("analysis",)),
                EmployeeRecord("engineer", "Engineer", ("integration",)),
            ),
        )
        request = replace(
            base,
            employee_skill_snapshots={"analyst": (skill,), "engineer": ()},
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        analysis_request = next(
            item for item in runner.requests if item.task.task_id == "analysis"
        )
        final_request = next(
            item for item in runner.requests if item.task.task_id == "final"
        )
        self.assertEqual(analysis_request.employee.skills, (skill,))
        self.assertEqual(final_request.employee.skills, ())

    async def test_temporary_specialist_uses_only_frozen_job_local_external_skills(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "specialist": ScriptedOutcome("Specialist artifact"),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        external_skill = VersionedContent(
            "external-skill:job-local-compliance",
            "sha256:temporaryskill",
            "Check the required policy evidence before reporting completion.",
            "temporary-skill-hash",
        )
        persistent_skill = VersionedContent(
            "employee-skill:generalist:private-procedure:context",
            "1",
            "This must never cross into a temporary employee.",
            "persistent-skill-hash",
        )
        base = company_request(
            (
                task("specialist", capabilities=("compliance_review",)),
                task("final", depends_on=("specialist",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("generalist", "Generalist", ("integration",)),),
        )
        request = replace(
            base,
            employee_skill_snapshots={"generalist": (persistent_skill,)},
            job_local_skill_snapshots=(external_skill,),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        specialist_request = next(
            item for item in runner.requests if item.task.task_id == "specialist"
        )
        self.assertTrue(specialist_request.employee.temporary)
        self.assertEqual(specialist_request.employee.skills, (external_skill,))
        self.assertEqual(specialist_request.session_retention.value, "RUN_ONLY")
        self.assertEqual(
            specialist_request.employee.capability_profile.skill_revision_refs,
            ("external-skill:job-local-compliance@sha256:temporaryskill#temporary-skill-hash",),
        )

    async def test_job_local_specialist_rejects_persistent_employee_procedure(self) -> None:
        invalid = VersionedContent(
            "employee-skill:generalist:private-procedure:context",
            "1",
            "Private procedure",
            "private-skill-hash",
        )
        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("generalist", "Generalist", ("analysis",)),),
            ),
            job_local_skill_snapshots=(invalid,),
        )

        with self.assertRaisesRegex(ValueError, "Job-local specialist skills"):
            await FirmKernel(
                employee_execution=ScriptedEmployeeExecutionPort(
                    {"final": ScriptedOutcome("unused")}
                )
            ).run(request)

    async def test_employee_skill_snapshot_is_frozen_but_selected_per_task_objective(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "inspect-api": ScriptedOutcome("API inspected"),
                "inspect-ui": ScriptedOutcome("UI inspected"),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        api_skill = VersionedContent(
            "employee-skill:analyst:api-validation:context",
            "3",
            "Validate API request and response contracts before completion.",
            "api-skill-hash",
        )
        ui_skill = VersionedContent(
            "employee-skill:analyst:ui-review:context",
            "4",
            "Review UI layout accessibility and visual interaction before completion.",
            "ui-skill-hash",
        )
        base = company_request(
            (
                replace(
                    task("inspect-api", capabilities=("analysis",)),
                    objective="Inspect API response contract",
                ),
                replace(
                    task("inspect-ui", capabilities=("analysis",)),
                    objective="Inspect UI accessibility layout",
                ),
                task("final", depends_on=("inspect-api", "inspect-ui"), capabilities=("analysis",)),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )
        request = replace(
            base,
            employee_skill_snapshots={"analyst": (api_skill, ui_skill)},
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        requests = {item.task.task_id: item for item in runner.requests}
        self.assertEqual(requests["inspect-api"].employee.skills, (api_skill,))
        self.assertEqual(requests["inspect-ui"].employee.skills, (ui_skill,))
        self.assertLessEqual(len(requests["final"].employee.skills), 1)
        self.assertTrue(
            all(
                item.content_id in {api_skill.content_id, ui_skill.content_id}
                for request_item in requests.values()
                for item in request_item.employee.skills
            )
        )

    async def test_solo_uses_one_employee_and_no_patch(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "final": ScriptedOutcome(
                    "Solo result",
                    acceptance_evidence=("solo:evidence",),
                    usage=Usage(model_calls=1, cost_usd=0.01),
                )
            }
        )
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.summary, "Solo result")
        self.assertEqual(result.metrics.unique_employee_count, 1)
        self.assertEqual(result.metrics.temporary_role_count, 0)
        self.assertEqual(result.metrics.maximum_parallelism, 1)
        self.assertEqual(result.metrics.graph_patch_count, 0)

    async def test_independent_tasks_overlap_and_integration_waits(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "analysis-a": ScriptedOutcome("A", delay_seconds=0.03),
                "analysis-b": ScriptedOutcome("B", delay_seconds=0.03),
                "final": ScriptedOutcome("Integrated", delay_seconds=0.0),
            }
        )
        request = company_request(
            (
                task("analysis-a"),
                task("analysis-b"),
                task("final", depends_on=("analysis-a", "analysis-b"), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("employee-a", "Senior Analyst", ("analysis", "integration")),
                EmployeeRecord("employee-b", "Analyst", ("analysis",)),
            ),
        )

        assignments = []
        result = await FirmKernel(
            employee_execution=runner,
            assignment_sink=assignments.append,
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.metrics.maximum_parallelism, 2)
        self.assertEqual(runner.maximum_parallelism, 2)
        self.assertEqual(set(runner.started_order[:2]), {"analysis-a", "analysis-b"})
        self.assertEqual(runner.started_order[-1], "final")
        self.assertEqual(
            [item.task_id for item in assignments],
            ["analysis-a", "analysis-b", "final"],
        )
        self.assertEqual([item.active_task_count for item in assignments[:2]], [1, 2])
        self.assertTrue(all(not item.employee_temporary for item in assignments))
        self.assertTrue(assignments[-1].final_task)
        final_request = next(item for item in runner.requests if item.task.task_id == "final")
        self.assertEqual(
            {item.content_id for item in final_request.context.task_dependencies},
            {"task-result:analysis-a", "task-result:analysis-b"},
        )
        dependency_payloads = {
            item.content_id: json.loads(item.content)
            for item in final_request.context.task_dependencies
        }
        self.assertEqual(
            dependency_payloads["task-result:analysis-a"]["status"],
            "SUCCEEDED",
        )
        self.assertEqual(
            dependency_payloads["task-result:analysis-a"]["summary"],
            "A",
        )
        self.assertEqual(
            dependency_payloads["task-result:analysis-b"]["partial"],
            False,
        )

    async def test_graph_employee_constraints_control_staffing_and_are_retained(self) -> None:
        runner = ScriptedEmployeeExecutionPort({"final": ScriptedOutcome("done")})
        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(
                    EmployeeRecord("analyst-a", "Analyst", ("analysis",)),
                    EmployeeRecord("analyst-b", "Analyst", ("analysis",)),
                ),
            ),
            graph_constraints_digest="a" * 64,
            graph_pinned_employee_ids=("analyst-b",),
            graph_excluded_employee_ids=("analyst-a",),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(runner.requests[0].employee.employee_id, "analyst-b")
        self.assertEqual(result.graph_pinned_employee_ids, ("analyst-b",))
        self.assertEqual(result.graph_excluded_employee_ids, ("analyst-a",))

    async def test_graph_concurrency_constraint_never_exceeds_user_ceiling(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "analysis-a": ScriptedOutcome("A", delay_seconds=0.02),
                "analysis-b": ScriptedOutcome("B", delay_seconds=0.02),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        request = replace(
            company_request(
                (
                    task("analysis-a"),
                    task("analysis-b"),
                    task(
                        "final",
                        depends_on=("analysis-a", "analysis-b"),
                        capabilities=("integration",),
                    ),
                ),
                final_task_id="final",
                roster=(
                    EmployeeRecord("employee-a", "Analyst", ("analysis", "integration")),
                    EmployeeRecord("employee-b", "Analyst", ("analysis",)),
                ),
            ),
            graph_constraints_digest="b" * 64,
            graph_max_concurrency=1,
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.metrics.maximum_parallelism, 1)
        self.assertEqual(runner.maximum_parallelism, 1)

    async def test_graph_independent_review_requires_an_explicit_review_edge(self) -> None:
        request = replace(
            company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            ),
            graph_constraints_digest="c" * 64,
            graph_require_independent_review=True,
        )

        with self.assertRaisesRegex(ValueError, "explicit review dependency"):
            await FirmKernel(
                employee_execution=ScriptedEmployeeExecutionPort({})
            ).run(request)

    async def test_dependency_handoff_is_typed_and_bounded(self) -> None:
        large = "근거" * 4_000
        runner = ScriptedEmployeeExecutionPort(
            {
                "analysis": ScriptedOutcome(
                    large,
                    acceptance_evidence=(large, large),
                    unresolved_issues=(large,),
                    output_artifact_refs=(large,),
                ),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        request = company_request(
            (
                task("analysis"),
                task("final", depends_on=("analysis",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("employee", "Engineer", ("analysis", "integration")),),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        final_request = next(item for item in runner.requests if item.task.task_id == "final")
        dependency = final_request.context.task_dependencies[0]
        self.assertLessEqual(len(dependency.content.encode("utf-8")), 2_048)
        payload = json.loads(dependency.content)
        self.assertEqual(payload["schema_version"], "noruct.task-dependency.v1")
        self.assertEqual(payload["task_id"], "analysis")
        self.assertEqual(payload["status"], "SUCCEEDED")
        self.assertIn("acceptance_evidence", payload)
        self.assertIn("unresolved_issues", payload)
        self.assertIn("output_artifact_refs", payload)
        self.assertIs(payload["partial"], False)

    async def test_only_final_task_receives_mutation_authority(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "analysis": ScriptedOutcome("Analysis"),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        base = company_request(
            (
                task("analysis"),
                task("final", depends_on=("analysis",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("employee", "Engineer", ("analysis", "integration")),),
        )
        request = replace(
            base,
            action_policy=ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        "read_workspace_file",
                        (ToolEffect.READ,),
                        max_calls=4,
                        requires_approval=True,
                    ),
                    ToolGrant(
                        "write_workspace_file",
                        (ToolEffect.WRITE,),
                        max_calls=4,
                        requires_approval=True,
                    ),
                ),
                filesystem_policy="WORKSPACE_WRITE",
                sandbox_profile="host-workspace-approved",
            ),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        analysis_request = next(item for item in runner.requests if item.task.task_id == "analysis")
        final_request = next(item for item in runner.requests if item.task.task_id == "final")
        self.assertEqual(
            [grant.tool_name for grant in analysis_request.action_policy.tool_grants],
            ["read_workspace_file"],
        )
        self.assertEqual(analysis_request.action_policy.filesystem_policy, "READ_ONLY")
        self.assertTrue(
            analysis_request.action_policy.tool_grants[0].requires_approval
        )
        self.assertEqual(
            {grant.tool_name for grant in final_request.action_policy.tool_grants},
            {"read_workspace_file", "write_workspace_file"},
        )

    async def test_review_constraint_report_is_structurally_read_only(self) -> None:
        runner = ScriptedEmployeeExecutionPort({"final": ScriptedOutcome("Review unavailable")})
        base = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(EmployeeRecord("employee", "Reporter", ("analysis",)),),
        )
        request = replace(
            base,
            plan_proposal=replace(
                base.plan_proposal,
                proposal_id="review-constraint-company-request-1",
            ),
            action_policy=ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        "write_workspace_file",
                        (ToolEffect.WRITE,),
                        max_calls=1,
                        requires_approval=False,
                    ),
                ),
                filesystem_policy="WORKSPACE_WRITE",
            ),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(runner.requests[0].action_policy, ActionPolicy())

    async def test_temporary_employee_session_is_run_only(self) -> None:
        runner = ScriptedEmployeeExecutionPort({"final": ScriptedOutcome("Temporary result")})
        request = company_request(
            (task("final"),),
            final_task_id="final",
            roster=(
                EmployeeRecord(
                    "persistent-coordinator",
                    "Coordinator",
                    ("general_reasoning",),
                ),
                EmployeeRecord(
                    "temporary-specialist",
                    "Temporary specialist",
                    ("analysis",),
                    temporary=True,
                ),
            ),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(
            runner.requests[0].session_retention.value,
            "RUN_ONLY",
        )

    async def test_non_final_research_keeps_bounded_external_read_without_action_authority(
        self,
    ) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "research": ScriptedOutcome("External evidence"),
                "final": ScriptedOutcome("Integrated"),
            }
        )
        base = company_request(
            (
                task("research"),
                task("final", depends_on=("research",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("employee", "Researcher", ("analysis", "integration")),),
        )
        request = replace(
            base,
            action_policy=ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        "web_search",
                        (ToolEffect.NETWORK,),
                        resource_patterns=("external-read:web-search",),
                        max_calls=2,
                        requires_approval=True,
                    ),
                    ToolGrant(
                        "run_workspace_command",
                        (ToolEffect.EXECUTE,),
                        resource_patterns=("workspace:noruct-workspace:command:*",),
                        requires_approval=True,
                    ),
                ),
                network_policy="EXTERNAL_READ_ONLY",
                filesystem_policy="WORKSPACE_WRITE",
                sandbox_profile="host-workspace-approved",
            ),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        research_request = next(
            item for item in runner.requests if item.task.task_id == "research"
        )
        self.assertEqual(
            [grant.tool_name for grant in research_request.action_policy.tool_grants],
            ["web_search"],
        )
        self.assertEqual(
            research_request.action_policy.network_policy,
            "EXTERNAL_READ_ONLY",
        )
        self.assertEqual(
            research_request.action_policy.filesystem_policy,
            "READ_ONLY",
        )
        self.assertEqual(research_request.action_policy.sandbox_profile, "none")
        self.assertTrue(
            research_request.action_policy.tool_grants[0].requires_approval
        )

    async def test_independent_reviewer_cannot_be_the_final_writer(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "review": ScriptedOutcome("Independent review"),
                "final": ScriptedOutcome("Integrated result"),
            }
        )
        request = company_request(
            (
                task("review", capabilities=("independent_review",)),
                task(
                    "final",
                    depends_on=("review",),
                    capabilities=("integration",),
                ),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord(
                    "broad-generalist",
                    "Broad Generalist",
                    ("independent_review", "integration"),
                ),
            ),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        assignments = {
            item.task.task_id: item.employee.employee_id for item in runner.requests
        }
        self.assertEqual(assignments["review"], "broad-generalist")
        self.assertNotEqual(assignments["review"], assignments["final"])
        self.assertTrue(assignments["final"].startswith("temp-fixture-job-"))
        self.assertEqual(result.metrics.unique_employee_count, 2)
        self.assertEqual(result.metrics.temporary_role_count, 1)

    async def test_capability_signal_inserts_task_and_creates_one_temporary_specialist(self) -> None:
        signal = RunSignal(
            SignalCode.CAPABILITY_MISSING,
            value="compliance_review",
            evidence=("scout:gap",),
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                "scout": ScriptedOutcome("Gap found", signals=(signal,)),
                "compliance": ScriptedOutcome("Compliance checked"),
                "final": ScriptedOutcome("Replanned result"),
            }
        )
        patch = GraphPatch(
            patch_id="insert-compliance",
            base_graph_version=1,
            trigger_task_id="scout",
            semantic_operation=SemanticOperation.INSERT,
            rationale="A typed capability gap was returned.",
            expected_gain="Validate compliance before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task(
                        "compliance",
                        depends_on=("scout",),
                        capabilities=("compliance_review",),
                    ),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )
        replanner = StaticReplanner({"scout": patch})
        request = company_request(
            (
                task("scout", capabilities=("discovery",)),
                task("final", depends_on=("scout",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord(
                    "generalist",
                    "Generalist",
                    ("discovery", "integration"),
                ),
            ),
        )

        result = await FirmKernel(employee_execution=runner, replanner=replanner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.final_graph_version, 2)
        self.assertEqual(result.metrics.graph_patch_count, 1)
        self.assertEqual(result.metrics.temporary_role_count, 1)
        self.assertEqual(result.metrics.unique_employee_count, 2)
        self.assertEqual(runner.started_order, ["scout", "compliance", "final"])
        compliance_request = runner.requests[1]
        self.assertTrue(compliance_request.employee.employee_id.startswith("temp-fixture-job-1"))
        self.assertTrue(compliance_request.employee.temporary)
        self.assertEqual(compliance_request.employee.capabilities, ("compliance_review",))
        self.assertEqual(len(replanner.contexts), 1)
        self.assertEqual(len(result.graph_patch_events), 1)
        lease = result.graph_patch_events[0].mutation_lease
        self.assertGreaterEqual(lease.model_calls, 1)
        self.assertGreaterEqual(lease.tool_calls, 1)
        self.assertGreaterEqual(lease.cost_usd, 0.0)
        self.assertEqual(
            result.graph_patch_events[0].expected_impact.value,
            "CAPABILITY_COVERAGE",
        )
        self.assertEqual(
            result.graph_patch_events[0].validation_receipt.value,
            "KERNEL_GRAPH_AND_LEASE_VALIDATED",
        )

    async def test_locked_or_unapproved_proposed_blueprints_never_rewrite_the_job_graph(self) -> None:
        signal = RunSignal(
            SignalCode.CAPABILITY_MISSING,
            value="compliance_review",
            evidence=("scout:gap",),
        )
        patch = GraphPatch(
            patch_id="would-insert-compliance",
            base_graph_version=1,
            trigger_task_id="scout",
            semantic_operation=SemanticOperation.INSERT,
            rationale="A typed capability gap was returned.",
            expected_gain="Validate compliance before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task(
                        "compliance",
                        depends_on=("scout",),
                        capabilities=("compliance_review",),
                    ),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )
        for policy in ("LOCKED", "PROPOSE"):
            with self.subTest(policy=policy):
                runner = ScriptedEmployeeExecutionPort(
                    {
                        "scout": ScriptedOutcome("Gap found", signals=(signal,)),
                        "final": ScriptedOutcome("Integrated result"),
                    }
                )
                replanner = StaticReplanner({"scout": patch})
                request = replace(
                    company_request(
                        (
                            task("scout", capabilities=("discovery",)),
                            task(
                                "final",
                                depends_on=("scout",),
                                capabilities=("integration",),
                            ),
                        ),
                        final_task_id="final",
                        roster=(
                            EmployeeRecord(
                                "generalist",
                                "Generalist",
                                ("discovery", "integration"),
                            ),
                        ),
                    ),
                    graph_mutation_policy=policy,
                )

                result = await FirmKernel(
                    employee_execution=runner,
                    replanner=replanner,
                ).run(request)

                self.assertEqual(result.status, JobStatus.SUCCEEDED)
                self.assertEqual(result.final_graph_version, 1)
                self.assertEqual(result.metrics.graph_patch_count, 0)
                self.assertEqual(len(replanner.contexts), 0 if policy == "LOCKED" else 1)
                self.assertEqual(
                    tuple(event.status.value for event in result.graph_patch_proposal_events),
                    () if policy == "LOCKED" else ("UNAVAILABLE",),
                )
                self.assertEqual(runner.started_order, ["scout", "final"])

    async def test_proposed_graph_patch_requires_one_explicit_approval_before_apply(self) -> None:
        class Approval:
            def __init__(self, decision: ApprovalDecision) -> None:
                self.decision = decision
                self.requests = []

            async def request(self, request, cancellation):  # type: ignore[no-untyped-def]
                cancellation.raise_if_cancelled()
                self.requests.append(request)
                return self.decision

        signal = RunSignal(
            SignalCode.CAPABILITY_MISSING,
            value="compliance_review",
            evidence=("scout:gap",),
        )
        patch = GraphPatch(
            patch_id="approval-gated-compliance",
            base_graph_version=1,
            trigger_task_id="scout",
            semantic_operation=SemanticOperation.INSERT,
            rationale="A typed capability gap needs a check.",
            expected_gain="Validate compliance before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task(
                        "compliance",
                        depends_on=("scout",),
                        capabilities=("compliance_review",),
                    ),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )

        for decision, expected_version, expected_status in (
            (ApprovalDecision.ALLOW_ONCE, 2, "APPROVED"),
            (ApprovalDecision.DENY, 1, "REJECTED"),
            (ApprovalDecision.ALLOW_SESSION, 1, "UNAVAILABLE"),
        ):
            with self.subTest(decision=decision):
                runner = ScriptedEmployeeExecutionPort(
                    {
                        "scout": ScriptedOutcome("Gap found", signals=(signal,)),
                        "compliance": ScriptedOutcome("Compliance checked"),
                        "final": ScriptedOutcome("Integrated result"),
                    }
                )
                approval = Approval(decision)
                request = replace(
                    company_request(
                        (
                            task("scout", capabilities=("discovery",)),
                            task("final", depends_on=("scout",), capabilities=("integration",)),
                        ),
                        final_task_id="final",
                        roster=(
                            EmployeeRecord(
                                "generalist",
                                "Generalist",
                                ("discovery", "integration"),
                            ),
                        ),
                    ),
                    graph_mutation_policy="PROPOSE",
                )
                store = RunStore()
                try:
                    result = await FirmKernel(
                        employee_execution=runner,
                        replanner=StaticReplanner({"scout": patch}),
                        approval_port=approval,
                        active_job_ledger=SQLiteActiveJobLedger(store),
                    ).run(request)
                    inspection = ActiveJobInspector(store).inspect(request.job_id)
                finally:
                    store.close()

                self.assertEqual(result.status, JobStatus.SUCCEEDED)
                self.assertEqual(result.final_graph_version, expected_version)
                self.assertEqual(result.metrics.graph_patch_count, expected_version - 1)
                self.assertEqual(len(approval.requests), 1)
                self.assertEqual(approval.requests[0].tool_name, "propose_graph_patch")
                self.assertFalse(approval.requests[0].allow_session)
                self.assertEqual(
                    result.graph_patch_proposal_events[0].status.value,
                    expected_status,
                )
                self.assertEqual(
                    result.graph_patch_proposal_events[0].content_hash,
                    content_digest(
                        replace(result.graph_patch_proposal_events[0], content_hash="")
                    ),
                )
                self.assertEqual(
                    len(result.graph_patch_events),
                    1 if decision is ApprovalDecision.ALLOW_ONCE else 0,
                )
                self.assertEqual(
                    inspection.graph_proposal_decisions[0]["status"],
                    expected_status,
                )
                self.assertGreater(
                    inspection.graph_proposal_decisions[0]["ledger_sequence"],
                    0,
                )
                episode = episode_from_runtime_ledger(
                    result,
                    (),
                    execution_profile="READ_ONLY",
                )
                self.assertEqual(
                    episode.graph_proposal_approved_count,
                    1 if expected_status == "APPROVED" else 0,
                )
                self.assertEqual(
                    episode.graph_proposal_rejected_count,
                    1 if expected_status == "REJECTED" else 0,
                )
                self.assertEqual(
                    episode.graph_proposal_unavailable_count,
                    1 if expected_status == "UNAVAILABLE" else 0,
                )

    async def test_proposed_graph_patch_without_live_modal_pauses_durably(self) -> None:
        signal = RunSignal(
            SignalCode.CAPABILITY_MISSING,
            value="compliance_review",
            evidence=("scout:gap",),
        )
        patch = GraphPatch(
            patch_id="durable-approval-compliance",
            base_graph_version=1,
            trigger_task_id="scout",
            semantic_operation=SemanticOperation.INSERT,
            rationale="A typed capability gap needs a check.",
            expected_gain="Validate compliance before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task(
                        "compliance",
                        depends_on=("scout",),
                        capabilities=("compliance_review",),
                    ),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )
        request = replace(
            company_request(
                (
                    task("scout", capabilities=("discovery",)),
                    task("final", depends_on=("scout",), capabilities=("integration",)),
                ),
                final_task_id="final",
                roster=(
                    EmployeeRecord(
                        "generalist",
                        "Generalist",
                        ("discovery", "integration"),
                    ),
                ),
            ),
            graph_mutation_policy="PROPOSE",
        )
        store = RunStore()
        try:
            runner = ScriptedEmployeeExecutionPort(
                {
                    "scout": ScriptedOutcome("Gap found", signals=(signal,)),
                    "final": ScriptedOutcome("Integrated result"),
                }
            )
            result = await FirmKernel(
                employee_execution=runner,
                replanner=StaticReplanner({"scout": patch}),
                active_job_ledger=SQLiteActiveJobLedger(store),
            ).run(request)
            rows = store.get_job_ledger_rows(request.job_id)
            lifecycle = store.get_job_lifecycle(request.job_id)
            inspection = ActiveJobInspector(store).inspect(request.job_id)
        finally:
            store.close()

        self.assertEqual(result.status, JobStatus.STALLED)
        self.assertEqual(runner.started_order, ["scout"])
        self.assertEqual(result.metrics.graph_patch_count, 0)
        self.assertEqual(
            tuple(event.status.value for event in result.graph_patch_proposal_events),
            ("PENDING",),
        )
        assert lifecycle is not None and rows is not None
        self.assertEqual(lifecycle["state"], "PAUSED")
        self.assertIsNone(rows["terminal"])
        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.INTERRUPTED)
        self.assertEqual(inspection.graph_proposal_decisions[0]["status"], "PENDING")

    async def test_approved_graph_proposal_resumes_the_same_job_once(self) -> None:
        signal = RunSignal(
            SignalCode.CAPABILITY_MISSING,
            value="compliance_review",
            evidence=("scout:gap",),
        )
        patch = GraphPatch(
            patch_id="resume-approved-compliance",
            base_graph_version=1,
            trigger_task_id="scout",
            semantic_operation=SemanticOperation.INSERT,
            rationale="A typed capability gap needs a check.",
            expected_gain="Validate compliance before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task(
                        "compliance",
                        depends_on=("scout",),
                        capabilities=("compliance_review",),
                    ),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )
        request = replace(
            company_request(
                (
                    task("scout", capabilities=("discovery",)),
                    task("final", depends_on=("scout",), capabilities=("integration",)),
                ),
                final_task_id="final",
                roster=(
                    EmployeeRecord(
                        "generalist",
                        "Generalist",
                        ("discovery", "integration", "compliance_review"),
                    ),
                ),
            ),
            graph_mutation_policy="PROPOSE",
        )
        store = RunStore()
        try:
            ledger = SQLiteActiveJobLedger(store)
            paused = await FirmKernel(
                employee_execution=ScriptedEmployeeExecutionPort(
                    {"scout": ScriptedOutcome("Gap found", signals=(signal,))}
                ),
                replanner=StaticReplanner({"scout": patch}),
                active_job_ledger=ledger,
            ).run(request)
            pending = paused.graph_patch_proposal_events[0]
            self.assertEqual(pending.status, GraphPatchProposalStatus.PENDING)
            before_graph = JobGraph(
                version=1,
                tasks=paused.final_tasks,
                final_task_id=request.plan_proposal.final_task_id,
            )
            approved = graph_patch_proposal_event(
                patch=pending.patch,
                before=before_graph,
                after=apply_patch(
                    before_graph,
                    pending.patch,
                    max_tasks=request.job_limits.max_tasks,
                ),
                proposed_lease=pending.proposed_lease,
                status=GraphPatchProposalStatus.APPROVED,
            )
            self.assertEqual(pending.proposal_id, approved.proposal_id)
            ledger.resolve_graph_proposal(request.job_id, approved)
            resumed_runner = ScriptedEmployeeExecutionPort(
                {
                    "compliance": ScriptedOutcome("Compliance checked"),
                    "final": ScriptedOutcome("Integrated result"),
                }
            )
            result = await FirmKernel(
                employee_execution=resumed_runner,
                active_job_ledger=ledger,
            ).continue_approved_graph_proposal(request, approved)
            inspection = ActiveJobInspector(store).inspect(request.job_id)
        finally:
            store.close()

        self.assertEqual(paused.status, JobStatus.STALLED)
        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.final_graph_version, 2)
        self.assertEqual(result.metrics.graph_patch_count, 1)
        self.assertEqual(resumed_runner.started_order, ["compliance", "final"])
        self.assertEqual(
            tuple(event.status.value for event in result.graph_patch_proposal_events),
            ("PENDING", "APPROVED"),
        )
        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.TERMINAL)
        self.assertEqual(inspection.graph_patch_count, 1)

    async def test_remote_graph_decision_outage_leaves_the_local_candidate_pending(self) -> None:
        """A remote decision failure cannot create a local Graph continuation."""

        class UnavailableCoordination:
            def resolve_graph_proposal_continuation(self, **value):  # type: ignore[no-untyped-def]
                del value
                raise CompanyCoordinationError("fixture transport outage")

        signal = RunSignal(
            SignalCode.CAPABILITY_MISSING,
            value="compliance_review",
            evidence=("scout:gap",),
        )
        patch = GraphPatch(
            patch_id="remote-decision-outage",
            base_graph_version=1,
            trigger_task_id="scout",
            semantic_operation=SemanticOperation.INSERT,
            rationale="A typed capability gap needs a check.",
            expected_gain="Validate compliance before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task("compliance", depends_on=("scout",), capabilities=("compliance_review",)),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )
        request = replace(
            company_request(
                (
                    task("scout", capabilities=("discovery",)),
                    task("final", depends_on=("scout",), capabilities=("integration",)),
                ),
                final_task_id="final",
                roster=(EmployeeRecord("generalist", "Generalist", ("discovery", "integration", "compliance_review")),),
            ),
            graph_mutation_policy="PROPOSE",
        )
        store = RunStore()
        try:
            paused = await FirmKernel(
                employee_execution=ScriptedEmployeeExecutionPort(
                    {"scout": ScriptedOutcome("Gap found", signals=(signal,))}
                ),
                replanner=StaticReplanner({"scout": patch}),
                active_job_ledger=SQLiteActiveJobLedger(store),
            ).run(request)
            pending = paused.graph_patch_proposal_events[0]
            approved = graph_patch_proposal_resolution_event(
                pending,
                status=GraphPatchProposalStatus.APPROVED,
            )
            remote_ledger = SQLiteActiveJobLedger(
                store,
                company_coordination=UnavailableCoordination(),  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(RuntimeError, "Remote Graph proposal decision is unavailable"):
                remote_ledger.resolve_graph_proposal(request.job_id, approved)
            rows = store.get_job_ledger_rows(request.job_id)
            lifecycle = store.get_job_lifecycle(request.job_id)
        finally:
            store.close()

        assert rows is not None and lifecycle is not None
        self.assertEqual([row["status"] for row in rows["graph_proposals"]], ["PENDING"])
        self.assertEqual(lifecycle["state"], "PAUSED")

    async def test_rejected_graph_proposal_resumes_the_unchanged_same_job_once(self) -> None:
        signal = RunSignal(
            SignalCode.CAPABILITY_MISSING,
            value="compliance_review",
            evidence=("scout:gap",),
        )
        patch = GraphPatch(
            patch_id="resume-rejected-compliance",
            base_graph_version=1,
            trigger_task_id="scout",
            semantic_operation=SemanticOperation.INSERT,
            rationale="A typed capability gap needs a check.",
            expected_gain="Validate compliance before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task(
                        "compliance",
                        depends_on=("scout",),
                        capabilities=("compliance_review",),
                    ),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )
        request = replace(
            company_request(
                (
                    task("scout", capabilities=("discovery",)),
                    task("final", depends_on=("scout",), capabilities=("integration",)),
                ),
                final_task_id="final",
                roster=(
                    EmployeeRecord(
                        "generalist",
                        "Generalist",
                        ("discovery", "integration", "compliance_review"),
                    ),
                ),
            ),
            graph_mutation_policy="PROPOSE",
        )
        store = RunStore()
        try:
            ledger = SQLiteActiveJobLedger(store)
            paused = await FirmKernel(
                employee_execution=ScriptedEmployeeExecutionPort(
                    {"scout": ScriptedOutcome("Gap found", signals=(signal,))}
                ),
                replanner=StaticReplanner({"scout": patch}),
                active_job_ledger=ledger,
            ).run(request)
            pending = paused.graph_patch_proposal_events[0]
            rejected = graph_patch_proposal_resolution_event(
                pending,
                status=GraphPatchProposalStatus.REJECTED,
            )
            ledger.resolve_graph_proposal(request.job_id, rejected)
            resumed_runner = ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("Integrated original graph")}
            )
            result = await FirmKernel(
                employee_execution=resumed_runner,
                active_job_ledger=ledger,
            ).continue_rejected_graph_proposal(request, rejected)
            inspection = ActiveJobInspector(store).inspect(request.job_id)
            lifecycle = store.get_job_lifecycle(request.job_id)
        finally:
            store.close()

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.final_graph_version, 1)
        self.assertEqual(result.metrics.graph_patch_count, 0)
        self.assertEqual(resumed_runner.started_order, ["final"])
        self.assertEqual(
            tuple(event.status.value for event in result.graph_patch_proposal_events),
            ("PENDING", "REJECTED"),
        )
        self.assertEqual(inspection.errors, ())
        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.TERMINAL)
        self.assertEqual(inspection.graph_patch_count, 0)
        assert lifecycle is not None
        self.assertEqual(lifecycle["state"], "TERMINAL")

    async def test_approved_graph_proposal_replays_prior_graph_revisions(self) -> None:
        request = company_request(
            (
                task("scout", capabilities=("discovery",)),
                task("final", depends_on=("scout",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord(
                    "generalist",
                    "Generalist",
                    ("discovery", "precheck", "compliance_review", "integration"),
                ),
            ),
        )
        initial = graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks)
        prior_patch = GraphPatch(
            patch_id="prior-precheck",
            base_graph_version=1,
            trigger_task_id="scout",
            semantic_operation=SemanticOperation.INSERT,
            rationale="Add a deterministic precheck.",
            expected_gain="Precheck before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task("precheck", depends_on=("scout",), capabilities=("precheck",)),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="precheck",
                ),
            ),
        )
        revised = apply_patch(initial, prior_patch, max_tasks=request.job_limits.max_tasks)
        pending_patch = GraphPatch(
            patch_id="approved-compliance-after-prior",
            base_graph_version=2,
            trigger_task_id="precheck",
            semantic_operation=SemanticOperation.INSERT,
            rationale="Add the approved compliance check.",
            expected_gain="Compliance before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task(
                        "compliance",
                        depends_on=("precheck",),
                        capabilities=("compliance_review",),
                    ),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )
        approved_graph = apply_patch(revised, pending_patch, max_tasks=request.job_limits.max_tasks)
        prior_event = graph_patch_event(
            sequence=1,
            patch=prior_patch,
            before=initial,
            after=revised,
        )
        pending = graph_patch_proposal_event(
            patch=pending_patch,
            before=revised,
            after=approved_graph,
            proposed_lease=GraphMutationLease(),
            status=GraphPatchProposalStatus.PENDING,
        )
        approved = graph_patch_proposal_event(
            patch=pending_patch,
            before=revised,
            after=approved_graph,
            proposed_lease=pending.proposed_lease,
            status=GraphPatchProposalStatus.APPROVED,
        )
        store = RunStore()
        try:
            ledger = SQLiteActiveJobLedger(store)
            ledger.start_job(request, initial, frozen_snapshot_digest(request))
            ledger.append_graph_patch(request.job_id, prior_event)
            ledger.append_graph_proposal(request.job_id, pending)
            ledger.hold_graph_proposal(request.job_id, pending)
            ledger.resolve_graph_proposal(request.job_id, approved)
            runner = ScriptedEmployeeExecutionPort(
                {
                    "scout": ScriptedOutcome("scouted"),
                    "precheck": ScriptedOutcome("checked"),
                    "compliance": ScriptedOutcome("compliant"),
                    "final": ScriptedOutcome("integrated"),
                }
            )
            result = await FirmKernel(
                employee_execution=runner,
                active_job_ledger=ledger,
            ).continue_approved_graph_proposal(request, approved)
            inspection = ActiveJobInspector(store).inspect(request.job_id)
        finally:
            store.close()

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.final_graph_version, 3)
        self.assertEqual(result.metrics.graph_patch_count, 2)
        self.assertEqual(runner.started_order, ["scout", "precheck", "compliance", "final"])
        self.assertEqual(inspection.graph_patch_count, 2)
        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.TERMINAL)

    async def test_topology_patch_is_declined_when_added_work_cannot_lease_job_budget(self) -> None:
        signal = RunSignal(
            SignalCode.CAPABILITY_MISSING,
            value="compliance_review",
            evidence=("scout:gap",),
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                "scout": ScriptedOutcome(
                    "Gap found",
                    signals=(signal,),
                    usage=Usage(model_calls=1, tool_calls=1, cost_usd=0.1),
                ),
                "final": ScriptedOutcome("Must not get a topology addition"),
            }
        )
        patch = GraphPatch(
            patch_id="budget-rejected-insert",
            base_graph_version=1,
            trigger_task_id="scout",
            semantic_operation=SemanticOperation.INSERT,
            rationale="A typed gap would normally add a bounded check.",
            expected_gain="Test pre-reservation refusal.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task(
                        "compliance",
                        depends_on=("scout",),
                        capabilities=("compliance_review",),
                    ),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )
        base = company_request(
            (
                task("scout", capabilities=("discovery",)),
                task("final", depends_on=("scout",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("generalist", "Generalist", ("discovery", "integration")),),
        )
        request = replace(
            base,
            job_limits=replace(
                base.job_limits,
                max_total_model_calls=1,
                max_total_tool_calls=2,
                max_total_cost_usd=1.0,
            ),
        )

        result = await FirmKernel(
            employee_execution=runner,
            replanner=StaticReplanner({"scout": patch}),
        ).run(request)

        self.assertEqual(result.metrics.graph_patch_count, 0)
        self.assertEqual(result.graph_patch_events, ())
        self.assertNotIn("compliance", runner.started_order)

    async def test_assumption_signal_can_apply_a_bounded_split_and_run_the_new_ready_work(self) -> None:
        signal = RunSignal(
            SignalCode.ASSUMPTION_INVALIDATED,
            value="two independent checks are required",
            evidence=("discovery:two-lanes",),
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                "trigger": ScriptedOutcome("Discovery completed", signals=(signal,)),
                "security": ScriptedOutcome("Security evidence"),
                "pricing": ScriptedOutcome("Pricing evidence"),
                "final": ScriptedOutcome("Integrated result"),
            }
        )
        patch = GraphPatch(
            patch_id="split-after-discovery",
            base_graph_version=1,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.SPLIT,
            rationale="The successful discovery invalidated the one-lane evidence assumption.",
            expected_gain="Run the two independent checks before final integration.",
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
        replanner = StaticReplanner({"trigger": patch})
        request = company_request(
            (
                task("trigger", capabilities=("discovery",)),
                task("final", depends_on=("trigger",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("scout", "Scout", ("discovery",)),
                EmployeeRecord("security", "Security", ("security",)),
                EmployeeRecord("pricing", "Pricing", ("pricing",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )

        applied_patches = []
        with TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runtime.db")
            result = await FirmKernel(
                employee_execution=runner,
                replanner=replanner,
                graph_patch_sink=applied_patches.append,
                active_job_ledger=SQLiteActiveJobLedger(store),
            ).run(request)
            inspection = ActiveJobInspector(store).inspect(request.job_id)
            store.close()

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.final_graph_version, 2)
        self.assertEqual(result.metrics.graph_patch_count, 1)
        self.assertEqual(result.metrics.organization_admission_count, 0)
        self.assertEqual(len(result.graph_patch_events), 1)
        self.assertEqual(applied_patches, list(result.graph_patch_events))
        self.assertEqual(inspection.graph_patch_count, 1)
        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.TERMINAL)
        self.assertTrue(inspection.replay_matches)
        self.assertEqual(runner.started_order, ["trigger", "pricing", "security", "final"])
        self.assertEqual(replanner.contexts[0].signal.code, SignalCode.ASSUMPTION_INVALIDATED)

    async def test_terminal_validation_failure_becomes_one_typed_replan_trigger(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "draft": ScriptedOutcome(
                    "The local completion repair was exhausted.",
                    status=RunStatus.FAILED,
                    failure=Failure(
                        "COMPLETION_VALIDATION_FAILED",
                        FailureCategory.INPUT,
                        "Completion contract remained unsatisfied.",
                    ),
                ),
                "recovery": ScriptedOutcome("Validated replacement report"),
            }
        )
        patch = GraphPatch(
            patch_id="validated-replacement",
            base_graph_version=1,
            trigger_task_id="draft",
            semantic_operation=SemanticOperation.INSERT,
            rationale="The bounded local completion repair was exhausted.",
            expected_gain="Run one separately validated replacement task.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task("recovery", capabilities=("validation",)),
                ),
                GraphPatchOperation(
                    PatchOperationKind.SET_FINAL_TASK,
                    task_id="recovery",
                ),
                GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id="draft"),
                GraphPatchOperation(PatchOperationKind.CANCEL_TASK, task_id="final"),
            ),
        )
        replanner = StaticReplanner({"draft": patch})
        request = company_request(
            (
                task("draft", capabilities=("analysis",)),
                task("final", depends_on=("draft",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("analyst", "Analyst", ("analysis",)),
                EmployeeRecord("validator", "Validator", ("validation",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )

        result = await FirmKernel(
            employee_execution=runner,
            replanner=replanner,
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.final_graph_version, 2)
        self.assertEqual(runner.started_order, ["draft", "recovery"])
        self.assertEqual(replanner.contexts[0].signal.code, SignalCode.VALIDATION_FAILED)
        self.assertEqual(replanner.contexts[0].signal.value, "COMPLETION_VALIDATION_FAILED")

    async def test_constraint_signal_can_merge_unstarted_siblings_without_executing_them(self) -> None:
        signal = RunSignal(
            SignalCode.CONSTRAINT_CHANGED,
            value="the two checks must share one evidence collection pass",
            evidence=("constraint:single-source",),
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                "trigger": ScriptedOutcome("Discovery completed", signals=(signal,)),
                "combined": ScriptedOutcome("Combined evidence"),
                "final": ScriptedOutcome("Integrated result"),
            }
        )
        patch = GraphPatch(
            patch_id="merge-after-constraint",
            base_graph_version=1,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.MERGE,
            rationale="Two pending sibling checks now share one bounded evidence source.",
            expected_gain="Avoid duplicate work while retaining both acceptance checks.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=JobTask(
                        task_id="combined",
                        objective="Collect combined alpha and beta evidence",
                        depends_on=("trigger",),
                        required_capabilities=("alpha", "beta"),
                        acceptance_criteria=("Evidence for alpha", "Evidence for beta"),
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
        replanner = StaticReplanner({"trigger": patch})
        request = company_request(
            (
                task("trigger", capabilities=("discovery",)),
                task("alpha", depends_on=("trigger",), capabilities=("alpha",)),
                task("beta", depends_on=("trigger",), capabilities=("beta",)),
                task("final", depends_on=("alpha", "beta"), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("scout", "Scout", ("discovery",)),
                EmployeeRecord("combined", "Combined", ("alpha", "beta")),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )

        with TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runtime.db")
            result = await FirmKernel(
                employee_execution=runner,
                replanner=replanner,
                active_job_ledger=SQLiteActiveJobLedger(store),
            ).run(request)
            inspection = ActiveJobInspector(store).inspect(request.job_id)
            store.close()

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(runner.started_order, ["trigger", "combined", "final"])
        self.assertEqual(
            {task.task_id: task.status.value for task in result.final_tasks}["alpha"],
            "CANCELLED",
        )
        self.assertEqual(inspection.graph_patch_count, 1)
        self.assertTrue(inspection.replay_matches)

    async def test_replanner_exception_is_nonfatal_to_the_current_valid_graph(self) -> None:
        signal = RunSignal(
            SignalCode.ASSUMPTION_INVALIDATED,
            value="A replanner may inspect this typed signal.",
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                "trigger": ScriptedOutcome("Evidence", signals=(signal,)),
                "final": ScriptedOutcome("Integrated result"),
            }
        )

        class ExplodingReplanner:
            async def propose(self, context):
                del context
                raise RuntimeError("proposal backend failed")

        request = company_request(
            (
                task("trigger", capabilities=("discovery",)),
                task(
                    "final",
                    depends_on=("trigger",),
                    capabilities=("integration",),
                ),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("scout", "Scout", ("discovery",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )

        result = await FirmKernel(
            employee_execution=runner,
            replanner=ExplodingReplanner(),
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.final_graph_version, 1)
        self.assertEqual(result.metrics.graph_patch_count, 0)
        self.assertEqual(runner.started_order, ["trigger", "final"])

    async def test_invalid_graph_patch_is_rejected_without_stopping_final_work(self) -> None:
        signal = RunSignal(
            SignalCode.ASSUMPTION_INVALIDATED,
            value="A stale patch must not replace current execution.",
        )
        runner = ScriptedEmployeeExecutionPort(
            {
                "trigger": ScriptedOutcome("Evidence", signals=(signal,)),
                "final": ScriptedOutcome("Integrated result"),
            }
        )
        stale_patch = GraphPatch(
            patch_id="stale-proposal",
            base_graph_version=99,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.INSERT,
            rationale="This proposal was created against stale state.",
            expected_gain="None; it must be rejected.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task(
                        "stale_task",
                        depends_on=("trigger",),
                        capabilities=("stale",),
                    ),
                ),
            ),
        )
        request = company_request(
            (
                task("trigger", capabilities=("discovery",)),
                task(
                    "final",
                    depends_on=("trigger",),
                    capabilities=("integration",),
                ),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("scout", "Scout", ("discovery",)),
                EmployeeRecord("integrator", "Integrator", ("integration",)),
            ),
        )

        result = await FirmKernel(
            employee_execution=runner,
            replanner=StaticReplanner({"trigger": stale_patch}),
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.final_graph_version, 1)
        self.assertEqual(result.metrics.graph_patch_count, 0)
        self.assertEqual(result.graph_patch_events, ())
        self.assertEqual(runner.started_order, ["trigger", "final"])

    async def test_one_employee_never_runs_two_ready_tasks_at_once(self) -> None:
        runner = ScriptedEmployeeExecutionPort(
            {
                "a": ScriptedOutcome("A", delay_seconds=0.01),
                "b": ScriptedOutcome("B", delay_seconds=0.01),
                "final": ScriptedOutcome("Done"),
            }
        )
        request = company_request(
            (
                task("a"),
                task("b"),
                task("final", depends_on=("a", "b")),
            ),
            final_task_id="final",
            roster=(EmployeeRecord("only", "Generalist", ("analysis",)),),
        )

        result = await FirmKernel(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(runner.maximum_parallelism, 1)
        self.assertEqual(result.metrics.temporary_role_count, 0)

    async def test_second_patch_is_declined_by_job_replan_limit_without_failing_work(self) -> None:
        first_signal = RunSignal(SignalCode.CAPABILITY_MISSING, "capability-two")
        second_signal = RunSignal(SignalCode.CAPABILITY_MISSING, "capability-three")
        runner = ScriptedEmployeeExecutionPort(
            {
                "first": ScriptedOutcome("First gap", signals=(first_signal,)),
                "second": ScriptedOutcome("Second gap", signals=(second_signal,)),
                "third": ScriptedOutcome("Third"),
                "final": ScriptedOutcome("Final"),
            }
        )
        first_patch = GraphPatch(
            patch_id="first-patch",
            base_graph_version=1,
            trigger_task_id="first",
            semantic_operation=SemanticOperation.INSERT,
            rationale="first gap",
            expected_gain="first specialist",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task("second", depends_on=("first",), capabilities=("capability-two",)),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="second",
                ),
            ),
        )
        second_patch = GraphPatch(
            patch_id="second-patch",
            base_graph_version=2,
            trigger_task_id="second",
            semantic_operation=SemanticOperation.INSERT,
            rationale="second gap",
            expected_gain="second specialist",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=task("third", depends_on=("second",), capabilities=("capability-three",)),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="third",
                ),
            ),
        )
        replanner = StaticReplanner({"first": first_patch, "second": second_patch})
        base = company_request(
            (
                task("first", capabilities=("discovery",)),
                task("final", depends_on=("first",), capabilities=("integration",)),
            ),
            final_task_id="final",
            roster=(
                EmployeeRecord("generalist", "Generalist", ("discovery", "integration")),
            ),
        )
        request = replace(
            base,
            job_limits=replace(base.job_limits, max_graph_patches=1, max_temporary_roles=3),
        )

        applied_patches = []
        result = await FirmKernel(
            employee_execution=runner,
            replanner=replanner,
            graph_patch_sink=applied_patches.append,
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.final_graph_version, 2)
        self.assertEqual(result.metrics.graph_patch_count, 1)
        self.assertEqual([item.patch.patch_id for item in applied_patches], ["first-patch"])
        self.assertEqual(result.failure_reason, "")


if __name__ == "__main__":
    unittest.main()
