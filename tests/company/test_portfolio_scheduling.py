from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from dynamic_firm.company import (
    AuthoritySnapshotIdentity,
    PortfolioLifecycleState,
    PortfolioPolicy,
    WorkOrderBudgetSnapshot,
    WorkOrderPortfolioStore,
    normalize_work_order,
)
from dynamic_firm.company.portfolio_scheduling import (
    PortfolioScheduleRecord,
    plan_portfolio_admission,
)
from dynamic_firm.company.work_order_portfolio_models import PortfolioStatus
from dynamic_firm.kernel.models import JobMetrics, JobResult, JobStatus
from dynamic_firm.runtime.models import Usage


def _order(identifier: str):
    return normalize_work_order(
        f"Schedule {identifier}",
        work_order_id=f"work-order-{identifier}",
        authority_snapshot=AuthoritySnapshotIdentity(
            company_id="company-scheduler",
            company_revision=1,
            roster_revision=1,
            playbook_revision=1,
            action_policy_digest="scheduler-policy",
        ),
        budget_snapshot=WorkOrderBudgetSnapshot(8, 8, 2.0, 30_000),
        requested_at=datetime(2026, 7, 31, tzinfo=UTC),
    )


def _result(job_id: str) -> JobResult:
    return JobResult(
        job_id=job_id,
        request_id=f"request-{job_id}",
        status=JobStatus.SUCCEEDED,
        summary="done",
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
            usage=Usage(model_calls=1, tool_calls=0, cost_usd=0.1),
        ),
    )


def _record(
    identifier: str,
    *,
    priority: int,
    dependencies: tuple[str, ...] = (),
    defer_count: int = 0,
) -> PortfolioScheduleRecord:
    return PortfolioScheduleRecord(
        work_order_id=identifier,
        priority=priority,
        reserved_cost_usd=0.0,
        admission_status=PortfolioStatus.QUEUED,
        created_at="2026-07-31T00:00:00+00:00",
        dependency_work_order_ids=dependencies,
        deadline_at=None,
        required_capabilities=(),
        lifecycle_state=PortfolioLifecycleState.QUEUED,
        defer_count=defer_count,
    )


class PortfolioSchedulingTests(unittest.TestCase):
    def test_every_lifecycle_state_replays_after_database_reopen(self) -> None:
        target_states = (
            PortfolioLifecycleState.QUEUED,
            PortfolioLifecycleState.RUNNING,
            PortfolioLifecycleState.PAUSED,
            PortfolioLifecycleState.BLOCKED,
            PortfolioLifecycleState.CANCELLED,
            PortfolioLifecycleState.TERMINAL,
        )
        for target in target_states:
            with self.subTest(target=target.value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "portfolio.sqlite3"
                order = _order(target.value.lower())
                with WorkOrderPortfolioStore(path) as store:
                    if target is PortfolioLifecycleState.BLOCKED:
                        store.submit(order, required_capabilities=("missing",))
                        store.reconcile(PortfolioPolicy())
                    else:
                        store.submit(order)
                    if target in {
                        PortfolioLifecycleState.RUNNING,
                        PortfolioLifecycleState.PAUSED,
                        PortfolioLifecycleState.CANCELLED,
                        PortfolioLifecycleState.TERMINAL,
                    }:
                        store.reconcile(PortfolioPolicy())
                        job_id = f"job-{target.value.lower()}"
                        store.bind_job(order.work_order_id, job_id=job_id)
                        if target is PortfolioLifecycleState.PAUSED:
                            store.pause_job(job_id, reason="OPERATOR_PAUSE")
                        elif target is PortfolioLifecycleState.CANCELLED:
                            store.cancel_job(
                                job_id,
                                reason="RUNTIME_CANCELLED_CONFIRMED",
                                terminal_confirmed=True,
                            )
                        elif target is PortfolioLifecycleState.TERMINAL:
                            store.settle_job_result(_result(job_id))
                    self.assertEqual(
                        store.replay_lifecycle(order.work_order_id)[0], target
                    )
                with WorkOrderPortfolioStore(path) as reopened:
                    self.assertEqual(
                        reopened.replay_lifecycle(order.work_order_id)[0], target
                    )

    def test_dependency_priority_inheritance_and_restart_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.sqlite3"
            prerequisite = _order("prerequisite")
            dependent = _order("dependent")
            independent = _order("independent")
            with WorkOrderPortfolioStore(path) as store:
                store.submit(
                    prerequisite,
                    priority=1,
                    required_capabilities=("gpu",),
                )
                store.submit(
                    dependent,
                    priority=100,
                    dependency_work_order_ids=(prerequisite.work_order_id,),
                    deadline_at=datetime(2099, 1, 1, tzinfo=UTC),
                    required_capabilities=("gpu",),
                )
                store.submit(independent, priority=50)
                entries = store.reconcile(
                    PortfolioPolicy(
                        max_active_jobs=1,
                        capability_slots=(("gpu", 1),),
                    )
                )
                by_id = {entry.work_order_id: entry for entry in entries}
                self.assertEqual(
                    by_id[prerequisite.work_order_id].status,
                    PortfolioStatus.ADMITTED,
                )
                self.assertEqual(
                    by_id[dependent.work_order_id].reason,
                    f"PORTFOLIO_DEPENDENCY_WAIT:{prerequisite.work_order_id}",
                )
                projection = {
                    item["work_order_id"]: item for item in store.operator_projection()
                }
                self.assertEqual(
                    projection[prerequisite.work_order_id]["inherited_priority"],
                    100,
                )
                store.bind_job(prerequisite.work_order_id, job_id="job-prerequisite")
                store.settle_job_result(_result("job-prerequisite"))

            with WorkOrderPortfolioStore(path) as reopened:
                self.assertEqual(
                    reopened.replay_lifecycle(prerequisite.work_order_id)[0],
                    PortfolioLifecycleState.TERMINAL,
                )
                entries = reopened.reconcile(
                    PortfolioPolicy(
                        max_active_jobs=1,
                        capability_slots=(("gpu", 1),),
                    )
                )
                by_id = {entry.work_order_id: entry for entry in entries}
                self.assertEqual(
                    by_id[dependent.work_order_id].status,
                    PortfolioStatus.ADMITTED,
                )

    def test_scarce_capability_partial_admission_releases_after_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.sqlite3"
            high, low = _order("gpu-high"), _order("gpu-low")
            policy = PortfolioPolicy(
                max_active_jobs=2,
                capability_slots=(("gpu", 1),),
            )
            with WorkOrderPortfolioStore(path) as store:
                store.submit(high, priority=90, required_capabilities=("gpu",))
                store.submit(low, priority=10, required_capabilities=("gpu",))
                entries = {item.work_order_id: item for item in store.reconcile(policy)}
                self.assertEqual(entries[high.work_order_id].status, PortfolioStatus.ADMITTED)
                self.assertEqual(entries[low.work_order_id].status, PortfolioStatus.DEFERRED)
                self.assertEqual(
                    entries[low.work_order_id].reason,
                    "PORTFOLIO_CAPABILITY_DEFERRED:gpu",
                )
                store.bind_job(high.work_order_id, job_id="job-gpu-high")
                store.settle_job_result(_result("job-gpu-high"))
                entries = {item.work_order_id: item for item in store.reconcile(policy)}
                self.assertEqual(entries[low.work_order_id].status, PortfolioStatus.ADMITTED)

    def test_missing_capability_and_missed_deadline_block_without_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with WorkOrderPortfolioStore(Path(directory) / "portfolio.sqlite3") as store:
                capability = _order("missing-capability")
                expired = _order("expired")
                store.submit(capability, required_capabilities=("gpu",))
                store.submit(
                    expired,
                    deadline_at=datetime(2000, 1, 1, tzinfo=UTC),
                )
                entries = {
                    item.work_order_id: item
                    for item in store.reconcile(PortfolioPolicy(max_active_jobs=2))
                }
                self.assertEqual(
                    entries[capability.work_order_id].reason,
                    "PORTFOLIO_CAPABILITY_UNAVAILABLE:gpu",
                )
                self.assertEqual(
                    entries[expired.work_order_id].reason,
                    "PORTFOLIO_DEADLINE_MISSED",
                )
                self.assertTrue(
                    all(
                        item["lifecycle_state"] == "BLOCKED"
                        for item in store.operator_projection()
                    )
                )

    def test_starvation_age_and_dependency_cycle_are_deterministic(self) -> None:
        low = _record("low", priority=10, defer_count=100)
        high = _record("high", priority=90)
        decisions = plan_portfolio_admission(
            (high, low),
            PortfolioPolicy(max_active_jobs=1),
            now=datetime(2026, 7, 31, tzinfo=UTC),
        )
        admitted = [
            item.work_order_id
            for item in decisions
            if item.admission_status is PortfolioStatus.ADMITTED
        ]
        self.assertEqual(admitted, ["low"])
        with self.assertRaisesRegex(ValueError, "cycles"):
            plan_portfolio_admission(
                (
                    _record("a", priority=1, dependencies=("b",)),
                    _record("b", priority=1, dependencies=("a",)),
                ),
                PortfolioPolicy(),
                now=datetime(2026, 7, 31, tzinfo=UTC),
            )

    def test_cost_is_a_hard_ceiling_not_a_scheduling_objective(self) -> None:
        expensive_high_priority = replace(
            _record("expensive", priority=90),
            reserved_cost_usd=10.0,
        )
        cheap_low_priority = replace(
            _record("cheap", priority=10),
            reserved_cost_usd=0.01,
        )
        decisions = plan_portfolio_admission(
            (cheap_low_priority, expensive_high_priority),
            PortfolioPolicy(max_active_jobs=1),
            now=datetime(2026, 7, 31, tzinfo=UTC),
        )
        admitted = tuple(
            item.work_order_id
            for item in decisions
            if item.admission_status is PortfolioStatus.ADMITTED
        )
        self.assertEqual(admitted, ("expensive",))

        denied = plan_portfolio_admission(
            (expensive_high_priority,),
            PortfolioPolicy(max_active_jobs=1, max_reserved_cost_usd=5.0),
            now=datetime(2026, 7, 31, tzinfo=UTC),
        )
        self.assertEqual(denied[0].admission_status, PortfolioStatus.REJECTED)
        self.assertEqual(
            denied[0].admission_reason, "PORTFOLIO_RESERVE_EXCEEDS_POLICY"
        )

    def test_pause_resume_and_concurrent_terminal_cancel_are_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.sqlite3"
            order = _order("race")
            store = WorkOrderPortfolioStore(path)
            store.submit(order)
            store.reconcile(PortfolioPolicy())
            store.bind_job(order.work_order_id, job_id="job-race")
            store.pause_job("job-race", reason="OPERATOR_PAUSE")
            self.assertEqual(
                store.replay_lifecycle(order.work_order_id)[0],
                PortfolioLifecycleState.PAUSED,
            )
            store.resume_job("job-race", reason="OPERATOR_RESUME")
            with self.assertRaisesRegex(ValueError, "terminal runtime confirmation"):
                store.cancel_job(
                    "job-race", reason="CANCELLED", terminal_confirmed=False
                )

            gate = threading.Barrier(2)

            def settle() -> str:
                gate.wait()
                try:
                    store.settle_job_result(_result("job-race"))
                    return "settled"
                except ValueError:
                    return "lost"

            def cancel() -> str:
                gate.wait()
                try:
                    store.cancel_job(
                        "job-race",
                        reason="RUNTIME_CANCELLED_CONFIRMED",
                        terminal_confirmed=True,
                    )
                    return "cancelled"
                except ValueError:
                    return "lost"

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = (pool.submit(settle), pool.submit(cancel))
                outcomes = {future.result() for future in futures}
            self.assertIn("lost", outcomes)
            self.assertEqual(len(outcomes), 2)
            final_state, _ = store.replay_lifecycle(order.work_order_id)
            self.assertIn(
                final_state,
                {PortfolioLifecycleState.CANCELLED, PortfolioLifecycleState.TERMINAL},
            )
            store.close()
            with WorkOrderPortfolioStore(path) as reopened:
                self.assertEqual(
                    reopened.replay_lifecycle(order.work_order_id)[0], final_state
                )


if __name__ == "__main__":
    unittest.main()
