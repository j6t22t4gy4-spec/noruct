from __future__ import annotations

import tempfile
import unittest
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from dynamic_firm.company import (
    AuthoritySnapshotIdentity,
    EvidenceSource,
    OrganizationEpisode,
    PortfolioExecutionService,
    PortfolioPolicy,
    PortfolioRecoveryDecision,
    PortfolioReuseDecision,
    WorkOrderBudgetSnapshot,
    WorkOrderPortfolioStore,
    WorkflowTaskTemplate,
    normalize_work_order,
)
from dynamic_firm.kernel.models import JobMetrics, JobResult, JobStatus
from dynamic_firm.runtime.models import Usage
from dynamic_firm.runtime.company_budget import CompanyCostBudgetPolicy, SQLiteCompanyBudgetAuthority
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.evaluation.manager_value_campaign import (
    ManagerValueArmOutcome,
    ManagerValueCampaignReport,
)
from dynamic_firm.kernel.models import EmployeeRecord, JobLimits
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from tests.kernel.helpers import company_request, task


def _order(identifier: str):
    return normalize_work_order(
        f"Portfolio objective {identifier}",
        work_order_id=f"portfolio-order-{identifier}",
        authority_snapshot=AuthoritySnapshotIdentity(
            company_id="company-portfolio",
            company_revision=3,
            roster_revision=5,
            playbook_revision=7,
            action_policy_digest="portfolio-policy",
        ),
        budget_snapshot=WorkOrderBudgetSnapshot(12, 12, 4.0, 30_000),
        requested_at=datetime(2026, 7, 30, tzinfo=UTC),
    )


def _episode(identifier: str, *, quality: float = 1.0, baseline: float = 0.8):
    return OrganizationEpisode.create(
        job_id=f"evidence-{identifier}",
        source=EvidenceSource.REAL_JOB,
        task_family="portfolio-evidence",
        context_fingerprint="a" * 64,
        execution_profile="READ_ONLY",
        planning_mode="DYNAMIC",
        plan_template=(
            WorkflowTaskTemplate("analysis", ("analysis",)),
            WorkflowTaskTemplate("final", ("implementation",), ("analysis",), True),
        ),
        success=True,
        quality_score=quality,
        baseline_quality_score=baseline,
        model_calls=2,
        baseline_model_calls=4,
        employee_count=2,
        maximum_parallelism=2,
        writer_count=1,
        approvals_requested=0,
        approvals_granted=0,
        preapproval_mutations=0,
        validation_attempts=(True,),
        ledger_digest=("a" if identifier == "one" else "b") * 64,
        manager_employee_id="manager-1",
        manager_assignment_digest="c" * 64,
        manager_delegation_digest="d" * 64,
        manager_supervision_count=1,
        temporary_role_count=1,
    )


def _result(job_id: str, *, status: JobStatus = JobStatus.SUCCEEDED) -> JobResult:
    return JobResult(
        job_id=job_id,
        request_id=f"request-{job_id}",
        status=status,
        summary="terminal",
        acceptance_evidence=(),
        unresolved_issues=(),
        task_results=(),
        final_graph_version=0,
        final_tasks=(),
        metrics=JobMetrics(
            unique_employee_count=1,
            temporary_role_count=0,
            maximum_parallelism=1,
            graph_patch_count=0,
            usage=Usage(model_calls=3, tool_calls=2, cost_usd=1.25),
        ),
    )


class PortfolioExecutionTests(unittest.TestCase):
    def test_qualified_manager_and_heterogeneous_context_allow_only_next_bound_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "portfolio.sqlite3")
            high = _order("high")
            low = _order("low")
            store.submit(low, priority=10)
            store.submit(high, priority=90)
            service = PortfolioExecutionService(store)

            plan = service.next_dispatch_plan(
                policy=PortfolioPolicy(max_active_jobs=1, max_reserved_cost_usd=4.0),
                episodes=(_episode("one"), _episode("two", quality=0.95)),
                context_fingerprint="a" * 64,
                manager_employee_id="manager-1",
                automatic_blueprint_requested=True,
            )

            self.assertEqual(plan.entry.work_order_id, high.work_order_id)
            self.assertEqual(plan.reuse_decision, PortfolioReuseDecision.OBSERVE_ONLY)
            self.assertIn("manager_campaign_report_missing", plan.reasons)
            bound = service.bind_dispatched_job(plan, job_id="job-high")
            self.assertEqual(bound.job_id, "job-high")
            store.close()

    def test_complete_16_slot_manager_report_is_required_for_automatic_reuse(self) -> None:
        def arm(name: str, *, quality: float, calls: float) -> ManagerValueArmOutcome:
            return ManagerValueArmOutcome(name, 4, 0.0, 0.0, quality, quality, calls, 10.0, 0.0, 0.0, None, "MODEL_CALL_PROXY")
        report = ManagerValueCampaignReport(
            schema_version="fixture", benchmark_id="fixture", content_hash="f" * 64,
            created_at="2026-07-30T00:00:00+00:00", qualified=True,
            outcomes=(
                arm("SINGLE_EMPLOYEE", quality=0.7, calls=3.0),
                arm("HOMOGENEOUS_GRAPH", quality=0.75, calls=3.0),
                arm("HETEROGENEOUS_GRAPH", quality=0.8, calls=3.0),
                arm("MANAGER_LED_FIRM", quality=0.9, calls=2.0),
            ),
            manager_incremental_quality_vs_heterogeneous=0.1,
            manager_incremental_model_calls_vs_heterogeneous=-1.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "portfolio.sqlite3")
            order = _order("qualified")
            store.submit(order)
            plan = PortfolioExecutionService(store).next_dispatch_plan(
                policy=PortfolioPolicy(),
                episodes=(_episode("one"), _episode("two", quality=0.95)),
                context_fingerprint="a" * 64,
                manager_employee_id="manager-1",
                automatic_blueprint_requested=True,
                manager_campaign_report=report,
            )
            self.assertEqual(plan.reuse_decision, PortfolioReuseDecision.AUTOMATIC_REUSE_ALLOWED)
            store.close()

    def test_terminal_result_settles_actual_usage_and_forfeits_unclaimed_incremental_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "portfolio.sqlite3")
            order = _order("settlement")
            store.submit(order)
            store.reconcile(PortfolioPolicy())
            store.bind_job(order.work_order_id, job_id="job-settlement")
            settlement = PortfolioExecutionService(store).record_terminal_result(
                _result("job-settlement")
            )

            self.assertEqual(settlement.status.value, "SETTLED")
            self.assertEqual(settlement.actual_cost_usd, 1.25)
            self.assertEqual(
                store.settle_job_result(_result("job-settlement")), settlement
            )
            projection = store.settlement_projection()
            self.assertEqual(projection[0]["actual_model_calls"], 3)
            self.assertEqual(store.operator_projection()[0]["status"], "CLOSED")
            store.close()

    def test_effectful_or_attempted_work_requires_replacement_while_proven_read_prefix_can_resume(self) -> None:
        safe = PortfolioExecutionService.recovery_plan(
            job_id="job-read",
            requested_effect="READ",
            has_receipt_proven_read_only_prefix=True,
            has_in_flight_or_attempted_pending_work=False,
            has_graph_revision=False,
            has_effect_receipt=False,
        )
        unsafe = PortfolioExecutionService.recovery_plan(
            job_id="job-write",
            requested_effect="WORKSPACE_CHANGE",
            has_receipt_proven_read_only_prefix=True,
            has_in_flight_or_attempted_pending_work=False,
            has_graph_revision=False,
            has_effect_receipt=True,
        )

        self.assertEqual(safe.decision, PortfolioRecoveryDecision.RECEIPT_BOUND_READ_ONLY)
        self.assertEqual(
            unsafe.decision, PortfolioRecoveryDecision.REPLACEMENT_WORK_ORDER_REQUIRED
        )
        self.assertIn("effectful_or_effect_receipt_requires_explicit_replacement", unsafe.reasons)


class PortfolioBatchExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_front_door_drain_binds_canonical_orders_before_concurrent_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "portfolio.sqlite3")
            for identifier, priority in (("one", 10), ("two", 90), ("three", 50)):
                store.submit(_order(identifier), priority=priority)
            seen: list[tuple[str, str]] = []

            async def dispatch(order, job_id):  # type: ignore[no-untyped-def]
                seen.append((order.work_order_id, job_id))
                return _result(job_id)

            outcome = await PortfolioExecutionService(store).execute_work_orders_until_idle(
                policy=PortfolioPolicy(max_active_jobs=2, max_reserved_cost_usd=8.0),
                job_id_for=lambda order: f"job-{order.work_order_id}",
                dispatch=dispatch,
            )

            self.assertEqual(outcome.waves, 2)
            self.assertEqual(len(outcome.dispatched_job_ids), 3)
            self.assertEqual(outcome.dispatched_job_ids, outcome.settled_job_ids)
            self.assertEqual(seen[0][0], "portfolio-order-two")
            self.assertTrue(all(item["status"] == "CLOSED" for item in store.operator_projection()))
            store.close()

    async def test_front_door_budget_denial_returns_the_order_to_deferred_without_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "portfolio.sqlite3")
            order = _order("budget-denied")
            store.submit(order)

            async def dispatch(_order, job_id):  # type: ignore[no-untyped-def]
                return _result(job_id, status=JobStatus.BUDGET_EXHAUSTED)

            outcome = await PortfolioExecutionService(store).execute_work_orders_until_idle(
                policy=PortfolioPolicy(),
                job_id_for=lambda _order: "job-budget-denied",
                dispatch=dispatch,
            )

            self.assertEqual(outcome.settled_job_ids, ())
            self.assertEqual(outcome.deferred_work_order_ids, (order.work_order_id,))
            entry = store.operator_projection()[0]
            self.assertEqual(entry["status"], "DEFERRED")
            self.assertIsNone(entry["job_id"])
            self.assertEqual(store.settlement_projection(), ())
            store.close()

    async def test_runs_priority_waves_concurrently_and_closes_actual_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "portfolio.sqlite3")
            for identifier, priority in (("one", 10), ("two", 90), ("three", 50)):
                store.submit(_order(identifier), priority=priority)
            active = 0
            maximum = 0

            def prepare(order):  # type: ignore[no-untyped-def]
                nonlocal active
                base = company_request(
                    (task("final"),), final_task_id="final",
                    roster=(EmployeeRecord("worker", "Worker", ("analysis",)),),
                )
                return replace(
                    base,
                    job_id=f"job-{order.work_order_id}",
                    request_id=f"request-{order.work_order_id}",
                    work_order_id=order.work_order_id,
                    work_order_digest=order.content_digest,
                    work_order_authority_digest=order.authority_snapshot.identity_digest,
                )

            async def dispatch(request):  # type: ignore[no-untyped-def]
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0)
                active -= 1
                return _result(request.job_id)

            outcome = await PortfolioExecutionService(store).execute_until_idle(
                policy=PortfolioPolicy(max_active_jobs=2, max_reserved_cost_usd=8.0),
                prepare_request=prepare,
                dispatch=dispatch,
            )
            self.assertEqual(outcome.waves, 2)
            self.assertEqual(len(outcome.dispatched_job_ids), 3)
            self.assertEqual(outcome.blocked_job_ids, ())
            self.assertEqual(maximum, 2)
            self.assertTrue(all(item["status"] == "CLOSED" for item in store.operator_projection()))
            store.close()

    async def test_company_budget_admission_defers_without_dispatching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkOrderPortfolioStore(root / "portfolio.sqlite3")
            order = _order("budget-deferred")
            store.submit(order)
            runtime_store = RunStore(root / "runtime.sqlite3")
            budget = SQLiteCompanyBudgetAuthority(
                runtime_store, CompanyCostBudgetPolicy(max_total_cost_usd=1.0)
            )
            dispatched = False

            def prepare(order):  # type: ignore[no-untyped-def]
                base = company_request(
                    (task("final"),), final_task_id="final",
                    roster=(EmployeeRecord("worker", "Worker", ("analysis",)),),
                )
                return replace(
                    base, job_id="job-budget-deferred", request_id="request-budget-deferred",
                    work_order_id=order.work_order_id, work_order_digest=order.content_digest,
                    work_order_authority_digest=order.authority_snapshot.identity_digest,
                )

            async def dispatch(request):  # type: ignore[no-untyped-def]
                del request
                nonlocal dispatched
                dispatched = True
                return _result("unreachable")

            outcome = await PortfolioExecutionService(store).execute_until_idle(
                policy=PortfolioPolicy(), prepare_request=prepare, dispatch=dispatch,
                company_budget_authority=budget,
            )
            self.assertFalse(dispatched)
            self.assertEqual(outcome.dispatched_job_ids, ())
            self.assertEqual(outcome.deferred_work_order_ids, (order.work_order_id,))
            self.assertEqual(store.operator_projection()[0]["status"], "DEFERRED")
            runtime_store.close()
            store.close()

    async def test_batch_uses_kernel_company_budget_terminal_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkOrderPortfolioStore(root / "portfolio.sqlite3")
            order = _order("budget-settled")
            store.submit(order)
            runtime_store = RunStore(root / "runtime.sqlite3")
            budget = SQLiteCompanyBudgetAuthority(
                runtime_store, CompanyCostBudgetPolicy(max_total_cost_usd=2.0)
            )

            def prepare(order):  # type: ignore[no-untyped-def]
                base = company_request(
                    (task("final"),), final_task_id="final",
                    roster=(EmployeeRecord("worker", "Worker", ("analysis",)),),
                    limits=JobLimits(max_total_cost_usd=0.5, max_wall_time_ms=5_000),
                )
                return replace(
                    base, job_id="job-budget-settled", request_id="request-budget-settled",
                    work_order_id=order.work_order_id, work_order_digest=order.content_digest,
                    work_order_authority_digest=order.authority_snapshot.identity_digest,
                )

            async def dispatch(request):  # type: ignore[no-untyped-def]
                return await FirmKernel(
                    employee_execution=ScriptedEmployeeExecutionPort(
                        {"final": ScriptedOutcome("done", usage=Usage(model_calls=1, cost_usd=0.25))}
                    ),
                    company_budget_authority=budget,
                ).run(request)

            outcome = await PortfolioExecutionService(store).execute_until_idle(
                policy=PortfolioPolicy(), prepare_request=prepare, dispatch=dispatch,
                company_budget_authority=budget,
            )
            self.assertEqual(outcome.settled_job_ids, ("job-budget-settled",))
            self.assertAlmostEqual(budget.status()["observed_cost_usd"], 0.25)
            self.assertAlmostEqual(budget.status()["reserved_cost_usd"], 0.0)
            runtime_store.close()
            store.close()
