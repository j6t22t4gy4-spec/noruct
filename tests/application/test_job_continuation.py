from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from dynamic_firm.application.job_continuation import ReceiptBoundContinuationService
from dynamic_firm.company.frontdoor import (
    AuthoritySnapshotIdentity,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
)
from dynamic_firm.company.work_order_portfolio import WorkOrderPortfolioStore
from dynamic_firm.kernel.models import EmployeeRecord, JobLimits
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.runtime.job_ledger import ActiveJobInspector, SQLiteActiveJobLedger
from dynamic_firm.runtime.company_budget import CompanyCostBudgetPolicy, SQLiteCompanyBudgetAuthority
from dynamic_firm.runtime.models import EventType, ToolCall, ToolEffect, ToolResult, Usage
from dynamic_firm.runtime.store import RunStore
from tests.kernel.helpers import company_request, task
from tests.runtime.helpers import make_request


class ReceiptBoundContinuationServiceTests(unittest.IsolatedAsyncioTestCase):

    async def test_effectful_prefix_requires_replacement_without_replaying_receipt(self) -> None:
        authority = AuthoritySnapshotIdentity(
            company_id="company-local", company_revision=3, roster_revision=5,
            playbook_revision=7, action_policy_digest="effect-policy-fixture",
        )
        work_order = normalize_work_order(
            "Continue only unstarted work",
            work_order_id="work-order-effect-recovery",
            authority_snapshot=authority,
            budget_snapshot=WorkOrderBudgetSnapshot(32, 32, 10.0, 10_000),
            requested_at=datetime(2026, 7, 30, tzinfo=UTC),
        )
        request = replace(
            company_request(
                (task("effect"), task("final", depends_on=("effect",))),
                final_task_id="final",
                roster=(
                    EmployeeRecord("operator", "Operator", ("analysis",)),
                    EmployeeRecord("integrator", "Integrator", ("analysis",)),
                ),
                limits=JobLimits(max_total_cost_usd=2.0, max_wall_time_ms=5_000),
            ),
            request_id="request-effect-recovery", job_id="job-effect-recovery",
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            work_order_authority_digest=authority.identity_digest,
            firm_admission_digest="e" * 64,
            requested_effect="WORKSPACE_CHANGE", graph_mutation_policy="LOCKED",
        )

        class AbortAfterEffectReceiptLedger(SQLiteActiveJobLedger):
            def append_dependency_result_receipt(self, job_id, record, result) -> None:  # type: ignore[no-untyped-def]
                super().append_dependency_result_receipt(job_id, record, result)
                if record.task_id == "effect":
                    raise RuntimeError("fixture interruption after effect receipt")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_store = RunStore(root / "runtime.db")
            budget = SQLiteCompanyBudgetAuthority(
                runtime_store, CompanyCostBudgetPolicy(max_total_cost_usd=5.0)
            )
            work_orders = WorkOrderPortfolioStore(root / "work-orders.db")
            work_orders.retain_work_order(work_order)
            work_orders.retain_continuation_request(request)
            with self.assertRaisesRegex(RuntimeError, "after effect receipt"):
                await FirmKernel(
                    employee_execution=ScriptedEmployeeExecutionPort(
                        {"effect": ScriptedOutcome("effect complete", usage=Usage(model_calls=1, cost_usd=0.25))}
                    ),
                    active_job_ledger=AbortAfterEffectReceiptLedger(runtime_store),
                    company_budget_authority=budget,
                ).run(request)
            receipt = runtime_store.list_job_dependency_result_receipts(request.job_id)[0]
            runtime_request = make_request(request_id="runtime-effect-receipt")
            runtime_request = replace(
                runtime_request,
                task=replace(
                    runtime_request.task,
                    job_id=request.job_id,
                    task_id="effect",
                ),
            )
            handle, _ = runtime_store.create_run(runtime_request)
            runtime_store.begin_run(handle.run_id)
            runtime_store.terminalize(
                replace(
                    receipt["result"],
                    run_id=handle.run_id,
                    request_id=runtime_request.request_id,
                    employee_id=runtime_request.employee.employee_id,
                ),
                EventType.RUN_SUCCEEDED,
                {},
            )
            runtime_run_id = handle.run_id
            action_id = "effect-action-receipt"
            call = ToolCall("effect-call", "write_fixture", {"path": "ignored"})
            runtime_store.record_tool_intent(
                runtime_run_id, action_id, 1, call,
                hashlib.sha256(b'{"path":"ignored"}').hexdigest(), "workspace:fixture",
                effect=ToolEffect.WRITE, idempotency_mode="CALL_KEY",
            )
            runtime_store.mark_tool_terminal(
                action_id,
                ToolResult("effect-call", "write_fixture", True, "done", action_id),
            )
            advice = ActiveJobInspector(runtime_store).recovery_advice(request.job_id)
            self.assertEqual(
                advice.recovery_state,
                "INTERRUPTED_EFFECTFUL_REPLACEMENT_REQUIRED",
            )
            self.assertIsNotNone(advice.effect_recovery)
            assert advice.effect_recovery is not None
            self.assertEqual(
                advice.effect_recovery.disposition,
                "REPLACEMENT_WORK_ORDER_REQUIRED",
            )
            outcome = ReceiptBoundContinuationService(
                work_orders=work_orders,
                inspector=ActiveJobInspector(runtime_store),
                continue_partial=lambda _request, _session: (_ for _ in ()).throw(AssertionError("must not dispatch")),
                company_budget_authority=budget,
            ).reconcile_effectful_interrupted_job(request.job_id)
            self.assertEqual(outcome.recovery.disposition, "REPLACEMENT_WORK_ORDER_REQUIRED")
            self.assertEqual(outcome.recovery.completed_task_ids, ("effect",))
            self.assertEqual(outcome.recovery.pending_task_ids, ("final",))
            assert outcome.budget_terminal is not None
            self.assertAlmostEqual(outcome.budget_terminal.actual_cost_usd, 0.25)
            work_orders.close()
            runtime_store.close()
    async def test_rehydrates_only_user_local_request_and_receipt_prefix(self) -> None:
        authority = AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=3,
            roster_revision=5,
            playbook_revision=7,
            action_policy_digest="read-only-policy-fixture",
        )
        work_order = normalize_work_order(
            "Continue the read-only fixture",
            work_order_id="work-order-continued",
            authority_snapshot=authority,
            budget_snapshot=WorkOrderBudgetSnapshot(32, 32, 10.0, 10_000),
            requested_at=datetime(2026, 7, 29, tzinfo=UTC),
        )
        request = replace(
            company_request(
                (task("analysis"), task("final", depends_on=("analysis",))),
                final_task_id="final",
                roster=(
                    EmployeeRecord("analyst", "Analyst", ("analysis",)),
                    EmployeeRecord("integrator", "Integrator", ("analysis",)),
                ),
            ),
            request_id="request-continued",
            job_id="job-continued",
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            work_order_authority_digest=authority.identity_digest,
            firm_admission_digest="f" * 64,
            requested_effect="READ",
            graph_mutation_policy="LOCKED",
            session_key="original-session",
        )

        class AbortBeforeFinalLedger(SQLiteActiveJobLedger):
            def append_attempt(self, job_id, record) -> None:  # type: ignore[no-untyped-def]
                if record.task_id == "final":
                    raise RuntimeError("fixture interruption before final append")
                super().append_attempt(job_id, record)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_store = RunStore(root / "runtime.db")
            work_orders = WorkOrderPortfolioStore(root / "work-orders.db")
            work_orders.retain_work_order(work_order)
            work_orders.retain_continuation_request(request)
            with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
                await FirmKernel(
                    employee_execution=ScriptedEmployeeExecutionPort(
                        {
                            "analysis": ScriptedOutcome("analysis complete"),
                            "final": ScriptedOutcome("not persisted"),
                        }
                    ),
                    active_job_ledger=AbortBeforeFinalLedger(runtime_store),
                ).run(request)

            captured = ScriptedEmployeeExecutionPort(
                {"final": ScriptedOutcome("integrated from persisted receipt")}
            )

            async def continue_partial(restored, pending_execution_session_key):  # type: ignore[no-untyped-def]
                self.assertEqual(restored, request)
                self.assertNotEqual(pending_execution_session_key, request.session_key)
                self.assertTrue(pending_execution_session_key.startswith("partial-continuation:"))
                return await FirmKernel(
                    employee_execution=captured,
                    active_job_ledger=SQLiteActiveJobLedger(runtime_store),
                ).continue_partial_read_only_job(
                    restored,
                    pending_execution_session_key=pending_execution_session_key,
                )

            outcome = await ReceiptBoundContinuationService(
                work_orders=work_orders,
                inspector=ActiveJobInspector(runtime_store),
                continue_partial=continue_partial,
            ).resume_partial_read_only_job(request.job_id)
            self.assertEqual(outcome.admission.completed_task_ids, ("analysis",))
            self.assertEqual(outcome.result.status.value, "SUCCEEDED")
            self.assertEqual(
                {result.task_id: result.summary for result in outcome.result.task_results}["analysis"],
                "analysis complete",
            )
            self.assertEqual([item.task.task_id for item in captured.requests], ["final"])
            self.assertNotEqual(captured.requests[0].session_key, request.session_key)
            self.assertTrue(captured.requests[0].session_key.startswith("partial-continuation:"))
            work_orders.close()
            runtime_store.close()
