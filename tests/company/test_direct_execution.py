from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.company.direct import DirectCompanyExecutor
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobLimits,
    JobStatus,
    JobTask,
    PlanProposal,
)
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.runtime.company_budget import (
    CompanyBudgetAdmission,
    CompanyBudgetForfeit,
    CompanyBudgetLease,
    CompanyBudgetSettlement,
    CompanyCostBudgetPolicy,
    SQLiteCompanyBudgetAuthority,
)
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ContextBundle,
    RunLimits,
    RunHandle,
    RunStatus,
    ToolEffect,
    ToolGrant,
    Usage,
    VersionedContent,
)
from dynamic_firm.runtime.store import RunStore


def direct_request(
    *,
    task: JobTask | None = None,
    roster: tuple[EmployeeRecord, ...] | None = None,
) -> CompanyRunRequest:
    assignment = task or JobTask(
        task_id="respond_to_user",
        objective="Explain the result directly.",
        depends_on=(),
        required_capabilities=("conversation",),
        acceptance_criteria=("Answer the user's question.",),
    )
    employees = roster or (
        EmployeeRecord(
            "employee-generalist",
            "Company Generalist",
            ("conversation", "general_reasoning"),
            model_profile="fixture",
        ),
    )
    policy = ActionPolicy(
        tool_grants=(
            ToolGrant(
                tool_name="read_workspace_file",
                allowed_effects=(ToolEffect.READ,),
                max_calls=2,
            ),
        ),
        filesystem_policy="READ_ONLY",
    )
    return CompanyRunRequest(
        request_id="request-direct-1",
        job_id="job-direct-1",
        goal=assignment.objective,
        plan_proposal=PlanProposal(
            proposal_id="direct-request-direct-1",
            goal=assignment.objective,
            tasks=(assignment,),
            final_task_id=assignment.task_id,
        ),
        roster=employees,
        employee_skill_snapshots={
            "employee-generalist": (
                VersionedContent(
                    "employee-skill:employee-generalist:answer",
                    "3",
                    "Answer in the user's language.",
                ),
            ),
        },
        context_snapshot=ContextBundle(
            company_policy_excerpt="Purpose: help the operator.",
            selected_memory=(
                VersionedContent(
                    "employee-memory:employee-generalist:preference",
                    "2",
                    "The operator prefers concise answers.",
                ),
            ),
        ),
        runtime_limits=RunLimits(
            max_model_calls=4,
            max_tool_calls=5,
            max_cost_usd=2.0,
            max_wall_time_ms=2_000,
        ),
        action_policy=policy,
        job_limits=JobLimits(
            max_total_model_calls=3,
            max_total_tool_calls=4,
            max_total_cost_usd=1.5,
            max_wall_time_ms=1_500,
        ),
        company_revision=4,
        roster_revision=7,
        playbook_revision=2,
        session_key="company-session-1",
        company_work_mode="DIRECT",
        coordination_policy="DIRECT",
        requested_effect="READ",
        operating_reason="DIRECT_RESPONSE",
        work_order_id="work-order-direct-1",
        work_order_digest="a" * 64,
        work_order_authority_digest="b" * 64,
        firm_admission_digest="f" * 64,
    )


class _RecordingBudgetAuthority:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.admitted: list[CompanyRunRequest] = []
        self.settled = []
        self.forfeited = []
        self.lease = CompanyBudgetLease(
            job_id="job-direct-1",
            request_id="request-direct-1",
            company_revision=4,
            window_start="2026-07-01T00:00:00+00:00",
            window_end="2026-08-01T00:00:00+00:00",
            reserved_cost_usd=1.5,
        )

    def admit_job(self, request: CompanyRunRequest) -> CompanyBudgetAdmission:
        self.admitted.append(request)
        return CompanyBudgetAdmission(
            allowed=self.allowed,
            lease=self.lease if self.allowed else None,
            incident=None,
            reason="" if self.allowed else "Direct run exceeds the Company budget.",
        )

    def settle_job(self, lease, result) -> CompanyBudgetSettlement:  # type: ignore[no-untyped-def]
        self.settled.append((lease, result))
        return CompanyBudgetSettlement(
            lease=lease,
            actual_cost_usd=result.metrics.usage.cost_usd,
            incident=None,
        )

    def forfeit_job(self, lease, *, reason: str) -> CompanyBudgetForfeit:  # type: ignore[no-untyped-def]
        self.forfeited.append((lease, reason))
        return CompanyBudgetForfeit(
            lease=lease,
            charged_cost_usd=lease.reserved_cost_usd,
            reason=reason,
            forfeited_at="2026-07-25T00:00:00+00:00",
            incident=None,
        )


class DirectCompanyExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_run_uses_one_persistent_employee_without_a_graph(self) -> None:
        temporary = EmployeeRecord(
            "temp-specialist",
            "Temporary Specialist",
            ("conversation",),
            temporary=True,
            model_profile="fixture",
        )
        persistent = EmployeeRecord(
            "employee-generalist",
            "Company Generalist",
            ("conversation", "general_reasoning"),
            model_profile="fixture",
        )
        request = direct_request(roster=(temporary, persistent))
        runner = ScriptedEmployeeExecutionPort(
            {
                ("respond_to_user", "employee-generalist"): ScriptedOutcome(
                    "The Company answered directly.",
                    acceptance_evidence=("direct answer",),
                    usage=Usage(model_calls=1, tool_calls=1, cost_usd=0.1),
                )
            }
        )
        assignments = []
        budget = _RecordingBudgetAuthority()

        result = await DirectCompanyExecutor(
            employee_execution=runner,
            assignment_sink=assignments.append,
            company_budget_authority=budget,
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.summary, "The Company answered directly.")
        self.assertEqual(result.company_work_mode, "DIRECT")
        self.assertEqual(result.final_graph_version, 0)
        self.assertEqual(result.final_tasks, ())
        self.assertEqual(result.final_task_id, "")
        self.assertEqual(result.work_order_id, "work-order-direct-1")
        self.assertEqual(result.work_order_digest, "a" * 64)
        self.assertEqual(result.work_order_authority_digest, "b" * 64)
        self.assertEqual(result.firm_admission_digest, "f" * 64)
        self.assertEqual(result.metrics.unique_employee_count, 1)
        self.assertEqual(result.metrics.temporary_role_count, 0)
        self.assertEqual(result.metrics.maximum_parallelism, 1)
        self.assertEqual(result.metrics.graph_patch_count, 0)
        self.assertEqual(result.metrics.task_mutation_count, 0)
        self.assertEqual(result.attempt_records, ())
        self.assertEqual(result.mutation_events, ())
        self.assertEqual(result.graph_patch_events, ())

        self.assertEqual(len(runner.requests), 1)
        employee_request = runner.requests[0]
        self.assertEqual(employee_request.employee.employee_id, "employee-generalist")
        self.assertFalse(employee_request.employee.temporary)
        self.assertEqual(employee_request.task.job_graph_version, 1)
        self.assertEqual(employee_request.session_key, "company-session-1")
        self.assertEqual(employee_request.action_policy, request.action_policy)
        self.assertEqual(
            employee_request.context.company_policy_excerpt,
            "Purpose: help the operator.",
        )
        self.assertEqual(len(employee_request.employee.skills), 1)
        self.assertEqual(len(employee_request.context.selected_memory), 1)
        self.assertEqual(employee_request.limits.max_model_calls, 3)
        self.assertEqual(employee_request.limits.max_tool_calls, 4)
        self.assertEqual(employee_request.limits.max_cost_usd, 1.5)
        self.assertEqual(employee_request.limits.max_wall_time_ms, 1_500)

        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].graph_version, 0)
        self.assertFalse(assignments[0].employee_temporary)
        self.assertEqual(
            assignments[0].selection_reason,
            "DIRECT_PERSISTENT_CAPABILITY_MATCH",
        )
        self.assertEqual(budget.admitted, [request])
        self.assertEqual(len(budget.settled), 1)
        self.assertIs(budget.settled[0][1], result)
        self.assertEqual(budget.forfeited, [])

    async def test_direct_run_uses_best_existing_employee_and_never_creates_a_role(self) -> None:
        task = JobTask(
            task_id="answer",
            objective="Answer a specialist question.",
            depends_on=(),
            required_capabilities=("missing_specialty",),
            acceptance_criteria=("Return a bounded answer or capability gap.",),
        )
        roster = (
            EmployeeRecord("employee-analyst", "Analyst", ("analysis",), model_profile="fixture"),
            EmployeeRecord(
                "employee-generalist",
                "Generalist",
                ("general_reasoning",),
                model_profile="fixture",
            ),
        )
        request = direct_request(task=task, roster=roster)
        runner = ScriptedEmployeeExecutionPort(
            {
                ("answer", "employee-generalist"): ScriptedOutcome(
                    "The existing employee reported the bounded limitation.",
                )
            }
        )
        assignments = []

        result = await DirectCompanyExecutor(
            employee_execution=runner,
            assignment_sink=assignments.append,
        ).run(request)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(runner.requests[0].employee.employee_id, "employee-generalist")
        self.assertEqual(assignments[0].selection_reason, "DIRECT_PERSISTENT_BEST_FIT")
        self.assertEqual(result.metrics.temporary_role_count, 0)

    async def test_direct_liveness_failure_is_not_retried_or_replanned(self) -> None:
        request = direct_request()
        runner = ScriptedEmployeeExecutionPort(
            {
                "respond_to_user": ScriptedOutcome(
                    "I will inspect the source next and then provide the answer."
                )
            }
        )

        result = await DirectCompanyExecutor(employee_execution=runner).run(request)

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(result.metrics.task_mutation_count, 0)
        self.assertEqual(result.metrics.graph_patch_count, 0)
        self.assertEqual(result.final_graph_version, 0)

    async def test_budget_denial_does_not_start_an_employee_or_create_graph_state(self) -> None:
        request = direct_request()
        runner = ScriptedEmployeeExecutionPort(
            {"respond_to_user": ScriptedOutcome("must not run")}
        )
        budget = _RecordingBudgetAuthority(allowed=False)

        result = await DirectCompanyExecutor(
            employee_execution=runner,
            company_budget_authority=budget,
        ).run(request)

        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertEqual(runner.requests, [])
        self.assertEqual(result.task_results, ())
        self.assertEqual(result.final_graph_version, 0)
        self.assertEqual(result.final_tasks, ())
        self.assertEqual(result.metrics.maximum_parallelism, 0)
        self.assertEqual(len(budget.admitted), 1)
        self.assertEqual(budget.settled, [])
        self.assertEqual(budget.forfeited, [])

    async def test_expired_front_door_wall_time_never_starts_direct_employee(self) -> None:
        request = replace(
            direct_request(),
            planning_reason="JOB_WALL_TIME_EXHAUSTED_BEFORE_DISPATCH",
        )
        runner = ScriptedEmployeeExecutionPort(
            {"respond_to_user": ScriptedOutcome("must not run")}
        )
        budget = _RecordingBudgetAuthority()

        result = await DirectCompanyExecutor(
            employee_execution=runner,
            company_budget_authority=budget,
        ).run(request)

        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertEqual(runner.requests, [])
        self.assertEqual(budget.admitted, [])
        self.assertEqual(budget.settled, [])
        self.assertEqual(budget.forfeited, [])

    async def test_wall_timeout_forfeits_indeterminate_direct_usage(self) -> None:
        request = direct_request()
        request = replace(
            request,
            runtime_limits=replace(request.runtime_limits, max_wall_time_ms=5),
            job_limits=replace(request.job_limits, max_wall_time_ms=5),
        )
        runner = ScriptedEmployeeExecutionPort(
            {"respond_to_user": ScriptedOutcome("too late", delay_seconds=30.0)}
        )
        budget = _RecordingBudgetAuthority()

        result = await DirectCompanyExecutor(
            employee_execution=runner,
            company_budget_authority=budget,
        ).run(request)

        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertEqual(budget.settled, [])
        self.assertEqual(
            budget.forfeited,
            [(budget.lease, "DIRECT_RUN_USAGE_UNCERTAIN")],
        )

    async def test_hanging_direct_cancel_port_is_bounded(self) -> None:
        class HangingCancelPort:
            async def start(self, request):  # type: ignore[no-untyped-def]
                return RunHandle("direct-hanging-run", request.request_id)

            async def collect(self, handle):  # type: ignore[no-untyped-def]
                await asyncio.Event().wait()

            async def cancel(self, handle, reason):  # type: ignore[no-untyped-def]
                await asyncio.Event().wait()

        request = direct_request()
        request = replace(
            request,
            runtime_limits=replace(request.runtime_limits, max_wall_time_ms=5),
            job_limits=replace(request.job_limits, max_wall_time_ms=5),
        )
        budget = _RecordingBudgetAuthority()
        started = asyncio.get_running_loop().time()

        result = await DirectCompanyExecutor(
            employee_execution=HangingCancelPort(),
            company_budget_authority=budget,
        ).run(request)

        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertLess(asyncio.get_running_loop().time() - started, 1.0)
        self.assertEqual(
            budget.forfeited,
            [(budget.lease, "DIRECT_RUN_USAGE_UNCERTAIN")],
        )

    async def test_cancellation_forfeits_the_company_budget_lease(self) -> None:
        request = direct_request()
        runner = ScriptedEmployeeExecutionPort(
            {
                "respond_to_user": ScriptedOutcome(
                    "must be cancelled",
                    delay_seconds=30.0,
                )
            }
        )
        budget = _RecordingBudgetAuthority()
        execution = asyncio.create_task(
            DirectCompanyExecutor(
                employee_execution=runner,
                company_budget_authority=budget,
            ).run(request)
        )
        while not runner.requests:
            await asyncio.sleep(0)

        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution

        self.assertEqual(budget.settled, [])
        self.assertEqual(
            budget.forfeited,
            [(budget.lease, "DIRECT_RUN_CANCELLED")],
        )

    async def test_post_run_exception_forfeits_the_company_budget_lease(self) -> None:
        request = direct_request()
        runner = ScriptedEmployeeExecutionPort(
            {"respond_to_user": ScriptedOutcome("completed before projection failed")}
        )
        budget = _RecordingBudgetAuthority()

        with patch(
            "dynamic_firm.company.direct.enforce_employee_completion_liveness",
            side_effect=RuntimeError("projection failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "projection failed"):
                await DirectCompanyExecutor(
                    employee_execution=runner,
                    company_budget_authority=budget,
                ).run(request)

        self.assertEqual(budget.settled, [])
        self.assertEqual(
            budget.forfeited,
            [(budget.lease, "DIRECT_RUN_ABORTED")],
        )

    async def test_sqlite_lease_is_not_active_after_direct_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runtime.db")
            budget = SQLiteCompanyBudgetAuthority(
                store,
                CompanyCostBudgetPolicy(max_total_cost_usd=5.0),
            )
            runner = ScriptedEmployeeExecutionPort(
                {
                    "respond_to_user": ScriptedOutcome(
                        "must be cancelled",
                        delay_seconds=30.0,
                    )
                }
            )
            execution = asyncio.create_task(
                DirectCompanyExecutor(
                    employee_execution=runner,
                    company_budget_authority=budget,
                ).run(direct_request())
            )
            while not runner.requests:
                await asyncio.sleep(0)

            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await execution

            status = budget.status()
            self.assertAlmostEqual(status["observed_cost_usd"], 1.5)
            self.assertAlmostEqual(status["reserved_cost_usd"], 0.0)
            self.assertFalse(status["paused"])
            store.close()

    async def test_direct_executor_rejects_multi_task_compatibility_proposal(self) -> None:
        request = direct_request()
        second = JobTask(
            task_id="second",
            objective="Second task",
            depends_on=(),
            required_capabilities=("conversation",),
            acceptance_criteria=("Second answer",),
        )
        request = replace(
            request,
            plan_proposal=PlanProposal(
                proposal_id=request.plan_proposal.proposal_id,
                goal=request.goal,
                tasks=(*request.plan_proposal.tasks, second),
                final_task_id="second",
            ),
        )
        runner = ScriptedEmployeeExecutionPort({})

        with self.assertRaisesRegex(ValueError, "exactly one assignment"):
            await DirectCompanyExecutor(employee_execution=runner).run(request)

        self.assertEqual(runner.requests, [])


if __name__ == "__main__":
    unittest.main()
