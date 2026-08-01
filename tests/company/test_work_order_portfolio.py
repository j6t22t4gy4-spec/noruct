from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from dynamic_firm.company.frontdoor import (
    AuthoritySnapshotIdentity,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
)
from dynamic_firm.company.work_order_portfolio import (
    PortfolioLeaseStatus,
    PortfolioPolicy,
    PortfolioStatus,
    WorkOrderPortfolioStore,
    read_work_order_read_only,
)
from dynamic_firm.company.work_order_portfolio_models import PortfolioReestimateChoice
from dynamic_firm.kernel.mutation import frozen_snapshot_digest
from tests.kernel.helpers import company_request, task
from dynamic_firm.kernel.models import EmployeeRecord, GraphMutationLease


def _order(identifier: str, *, cost: float = 4.0):
    return normalize_work_order(
        f"Research {identifier}",
        work_order_id=f"work-order-{identifier}",
        authority_snapshot=AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=3,
            roster_revision=5,
            playbook_revision=7,
            action_policy_digest="read-only-policy",
        ),
        budget_snapshot=WorkOrderBudgetSnapshot(16, 32, cost, 30_000),
        requested_at=datetime(2026, 7, 28, tzinfo=UTC),
    )


class WorkOrderPortfolioTests(unittest.TestCase):
    def test_reestimate_is_append_only_and_never_changes_the_bound_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "work-orders.db")
            order = _order("reestimate")
            store.submit(order, priority=90)
            store.reconcile(PortfolioPolicy(max_active_jobs=1))
            store.bind_job(order.work_order_id, job_id="job-reestimate")
            notice = store.report_reestimate(
                order.work_order_id,
                proposed_reserved_cost_usd=7.5,
                reason="PROVIDER_SCOPE_CHANGE",
            )
            self.assertEqual(notice.job_id, "job-reestimate")
            self.assertEqual(notice.prior_reserved_cost_usd, 4.0)
            self.assertEqual(notice.proposed_reserved_cost_usd, 7.5)
            self.assertIsNone(notice.choice)
            self.assertEqual(store.operator_projection()[0]["job_id"], "job-reestimate")
            decided = store.decide_reestimate(
                notice.reestimate_id,
                choice=PortfolioReestimateChoice.REDUCE,
                reason="OPERATOR_REPLACEMENT_REQUIRED",
                confirmed=True,
            )
            self.assertEqual(decided.choice, PortfolioReestimateChoice.REDUCE)
            self.assertEqual(store.get_reestimate(notice.reestimate_id), decided)
            with self.assertRaisesRegex(ValueError, "immutable user decision"):
                store.decide_reestimate(
                    notice.reestimate_id,
                    choice=PortfolioReestimateChoice.CANCEL,
                    reason="OPERATOR_CANCEL_REQUIRED",
                    confirmed=True,
                )
            self.assertEqual(store.operator_projection()[0]["status"], "ADMITTED")
            store.close()

    def test_portfolio_policy_is_durable_but_does_not_reconcile_or_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "work-orders.db"
            store = WorkOrderPortfolioStore(path)
            policy = PortfolioPolicy(
                max_active_jobs=2,
                max_reserved_cost_usd=7.5,
                max_incremental_model_calls=3,
                max_incremental_tool_calls=4,
                max_incremental_cost_usd=1.25,
            )
            self.assertEqual(store.save_portfolio_policy(policy), policy)
            self.assertEqual(store.operator_projection(), ())
            store.close()
            reopened = WorkOrderPortfolioStore(path)
            self.assertEqual(reopened.portfolio_policy(), policy)
            self.assertEqual(reopened.operator_projection(), ())
            reopened.close()

    def test_canonical_order_round_trips_outside_active_job_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "work-orders.db")
            original = _order("round-trip")
            store.retain_work_order(original)
            restored = store.work_order(original.work_order_id)
            self.assertEqual(restored, original)
            store.close()

    def test_read_only_work_order_lookup_never_initializes_missing_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "work-orders.db"
            self.assertIsNone(read_work_order_read_only(path, "work-order-missing"))
            self.assertFalse(path.exists())
            store = WorkOrderPortfolioStore(path)
            original = _order("read-only")
            store.retain_work_order(original)
            store.close()
            self.assertEqual(
                read_work_order_read_only(path, original.work_order_id), original
            )

    def test_priority_budget_admission_is_deterministic_and_does_not_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "work-orders.db")
            low = _order("low")
            high = _order("high")
            rejected = _order("rejected", cost=9.0)
            store.submit(low, priority=10)
            store.submit(high, priority=90)
            store.submit(rejected, priority=100)
            entries = store.reconcile(
                PortfolioPolicy(max_active_jobs=1, max_reserved_cost_usd=5.0)
            )
            by_id = {entry.work_order_id: entry for entry in entries}
            self.assertEqual(by_id[high.work_order_id].status, PortfolioStatus.ADMITTED)
            self.assertEqual(by_id[low.work_order_id].status, PortfolioStatus.DEFERRED)
            self.assertEqual(by_id[rejected.work_order_id].status, PortfolioStatus.REJECTED)
            self.assertIsNone(by_id[high.work_order_id].job_id)
            projection = store.operator_projection()
            self.assertNotIn(high.objective, str(projection))
            store.close()

    def test_job_binding_and_close_release_portfolio_capacity_on_next_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "work-orders.db")
            first = _order("first")
            second = _order("second")
            store.submit(first, priority=90)
            store.submit(second, priority=10)
            policy = PortfolioPolicy(max_active_jobs=1, max_reserved_cost_usd=8.0)
            store.reconcile(policy)
            bound = store.bind_job(first.work_order_id, job_id="job-first")
            self.assertEqual(bound.job_id, "job-first")
            store.close_job("job-first", reason="JOB_SUCCEEDED")
            entries = {entry.work_order_id: entry for entry in store.reconcile(policy)}
            self.assertEqual(entries[second.work_order_id].status, PortfolioStatus.ADMITTED)
            store.close()

    def test_incremental_lease_is_bound_to_an_admitted_job_and_releases_only_when_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "work-orders.db")
            first = _order("lease-first")
            second = _order("lease-second")
            store.submit(first, priority=90)
            store.submit(second, priority=10)
            policy = PortfolioPolicy(
                max_active_jobs=2,
                max_incremental_model_calls=3,
                max_incremental_tool_calls=2,
                max_incremental_cost_usd=0.5,
            )
            store.reconcile(policy)
            store.bind_job(first.work_order_id, job_id="job-first")
            store.bind_job(second.work_order_id, job_id="job-second")
            lease = GraphMutationLease(model_calls=2, tool_calls=1, cost_usd=0.3)
            first_reservation = store.reserve_incremental_lease(
                first.work_order_id,
                job_id="job-first",
                lease_id="mutation-first",
                mutation_lease=lease,
                policy=policy,
            )
            self.assertEqual(first_reservation.status, PortfolioLeaseStatus.RESERVED)
            self.assertEqual(
                store.reserve_incremental_lease(
                    first.work_order_id,
                    job_id="job-first",
                    lease_id="mutation-first",
                    mutation_lease=lease,
                    policy=policy,
                ),
                first_reservation,
            )
            with self.assertRaisesRegex(ValueError, "exceeds configured capacity"):
                store.reserve_incremental_lease(
                    second.work_order_id,
                    job_id="job-second",
                    lease_id="mutation-second",
                    mutation_lease=lease,
                    policy=policy,
                )
            released = store.resolve_incremental_lease(
                "mutation-first",
                status=PortfolioLeaseStatus.RELEASED,
                reason="KERNEL_CONFIRMED_UNUSED",
            )
            self.assertEqual(released.status, PortfolioLeaseStatus.RELEASED)
            second_reservation = store.reserve_incremental_lease(
                second.work_order_id,
                job_id="job-second",
                lease_id="mutation-second",
                mutation_lease=lease,
                policy=policy,
            )
            projection = store.incremental_lease_projection()
            self.assertEqual(projection["reserved"]["model_calls"], 2)
            self.assertNotIn(second.objective, str(projection))
            self.assertEqual(second_reservation.job_id, "job-second")
            store.close()

    def test_local_continuation_request_round_trips_without_using_active_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkOrderPortfolioStore(Path(directory) / "work-orders.db")
            order = _order("continuation")
            store.retain_work_order(order)
            request = replace(
                company_request(
                    (task("analysis"), task("final", depends_on=("analysis",))),
                    final_task_id="final",
                    roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
                ),
                request_id="request-continuation",
                job_id="job-continuation",
                work_order_id=order.work_order_id,
                work_order_digest=order.content_digest,
                work_order_authority_digest=order.authority_snapshot.identity_digest,
                firm_admission_digest="f" * 64,
                requested_effect="READ",
            )
            store.retain_continuation_request(request)
            restored = store.continuation_request(request.job_id)
            self.assertEqual(restored, request)
            self.assertEqual(
                frozen_snapshot_digest(restored), frozen_snapshot_digest(request)
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                store.retain_continuation_request(replace(request, goal="altered"))
            store.close()
