from __future__ import annotations

import io
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.company import CompanyStateStore
from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import EmployeeRecord, JobLimits
from dynamic_firm.kernel.mutation import frozen_snapshot_digest
from dynamic_firm.runtime.company_budget import (
    CompanyCostBudgetPolicy,
    SQLiteCompanyBudgetAuthority,
)
from dynamic_firm.runtime.job_ledger import SQLiteActiveJobLedger
from dynamic_firm.runtime.interruption import EffectInterruptionReason
from dynamic_firm.runtime.models import (
    ContextBundle,
    IdempotencyMode,
    ToolCall,
    ToolEffect,
    to_primitive,
)
from dynamic_firm.runtime.operator_attention import (
    CompanyAttentionInspector,
    CompanyAttentionKind,
    normalize_attention_job_limit,
)
from dynamic_firm.runtime.store import RunStore
from tests.kernel.helpers import company_request, task
from tests.runtime.test_approval_lifecycle import _request, _stage_waiting_approval


class CompanyOperatorAttentionTests(unittest.TestCase):
    def _interrupted_request(self):
        return replace(
            company_request(
                (task("analysis"),),
                final_task_id="analysis",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
                limits=JobLimits(max_total_cost_usd=0.5, max_wall_time_ms=5_000),
            ),
            job_id="attention-interrupted-job",
            request_id="attention-interrupted-request",
            company_revision=3,
        )

    def test_interrupted_job_owns_its_pending_approval_and_projection_is_private(self) -> None:
        redaction_marker = "ATTENTION-SECRET-SENTINEL"
        request = self._interrupted_request()
        store = RunStore()
        SQLiteActiveJobLedger(store).start_job(
            request,
            graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks),
            frozen_snapshot_digest(request),
        )
        employee_request = _request("attention-waiting-approval")
        employee_request = replace(
            employee_request,
            task=replace(
                employee_request.task,
                job_id=request.job_id,
                task_id="analysis",
            ),
            context=replace(
                employee_request.context,
                company_policy_excerpt=redaction_marker,
            ),
        )
        _stage_waiting_approval(store, employee_request)
        before = store.active_job_table_payloads(request.job_id)

        attention = CompanyAttentionInspector(store, CompanyCostBudgetPolicy()).inspect()
        rendered = json.dumps(to_primitive(attention), ensure_ascii=False)

        self.assertEqual(attention.interrupted_job_count, 1)
        self.assertEqual(attention.pending_approval_count, 1)
        self.assertEqual(attention.suppressed_pending_approval_count, 1)
        self.assertEqual(len(attention.items), 1)
        self.assertEqual(attention.items[0].kind, CompanyAttentionKind.INTERRUPTED_JOB)
        self.assertFalse(attention.state_changed)
        self.assertFalse(attention.automatic_resolution)
        self.assertNotIn(redaction_marker, rendered)
        self.assertNotIn("workspace:repo", rendered)
        self.assertEqual(before, store.active_job_table_payloads(request.job_id))
        store.close()

    def test_budget_incident_and_cli_attention_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            with CompanyStateStore(path) as company:
                company.set_company_cost_budget_policy(
                    {"max_total_cost_usd": 1.0, "window_kind": "lifetime"},
                    actor="operator:test",
                )

            request = replace(
                self._interrupted_request(),
                job_id="attention-budget-job",
                request_id="attention-budget-request",
                job_limits=JobLimits(max_total_cost_usd=1.1, max_wall_time_ms=5_000),
            )
            store = RunStore(path)
            admission = SQLiteCompanyBudgetAuthority(
                store, CompanyCostBudgetPolicy(max_total_cost_usd=1.0)
            ).admit_job(request)
            self.assertFalse(admission.allowed)
            self.assertIsNotNone(admission.incident)
            before_budget = store.company_budget_status(
                CompanyCostBudgetPolicy(max_total_cost_usd=1.0)
            )
            store.close()
            with CompanyStateStore(path) as company:
                before_company = (
                    company.company().revision,
                    company.company_cost_budget_policy(),
                    company.list_company_policy_events(),
                )

            output = io.StringIO()
            errors = io.StringIO()
            self.assertEqual(
                main(
                    ["company", "attention", "--state", str(path), "--json"],
                    stdout=output,
                    stderr=errors,
                ),
                EXIT_OK,
                errors.getvalue(),
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["open_budget_incident_count"], 1)
            self.assertEqual(payload["items"][0]["kind"], "BUDGET_INCIDENT")
            self.assertFalse(payload["automatic_resolution"])
            self.assertFalse(payload["state_changed"])
            after_store = RunStore(path)
            self.assertEqual(
                before_budget,
                after_store.company_budget_status(
                    CompanyCostBudgetPolicy(max_total_cost_usd=1.0)
                ),
            )
            after_store.close()
            with CompanyStateStore(path) as company:
                self.assertEqual(
                    before_company,
                    (
                        company.company().revision,
                        company.company_cost_budget_policy(),
                        company.list_company_policy_events(),
                    ),
                )

    def test_interrupted_unknown_effect_replaces_generic_job_prompt_without_mutation(self) -> None:
        request = self._interrupted_request()
        store = RunStore()
        SQLiteActiveJobLedger(store).start_job(
            request,
            graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks),
            frozen_snapshot_digest(request),
        )
        employee_request = _request("attention-unknown-effect")
        employee_request = replace(
            employee_request,
            task=replace(
                employee_request.task,
                job_id=request.job_id,
                task_id="analysis",
            ),
        )
        handle, created = store.create_run(employee_request)
        self.assertTrue(created)
        store.begin_run(handle.run_id)
        action_id = "attention-unknown-effect-action"
        store.record_tool_intent(
            handle.run_id,
            action_id,
            1,
            ToolCall("attention-unknown-effect-call", "workspace_write", {}),
            hashlib.sha256(b"arguments").hexdigest(),
            "workspace:attention-effect",
            effect=ToolEffect.WRITE,
            idempotency_mode=IdempotencyMode.NONE.value,
        )
        self.assertTrue(
            store.acquire_effect_resource_lease(
                action_id=action_id,
                run_id=handle.run_id,
                effect=ToolEffect.WRITE,
                resource_key="workspace:attention-effect",
            )
        )
        store.mark_tool_started(action_id)
        store.mark_tool_effect_indeterminate(
            action_id,
            cause=EffectInterruptionReason.PROCESS_OR_MACHINE_LOSS,
        )
        before = store.active_job_table_payloads(request.job_id)

        attention = CompanyAttentionInspector(store, CompanyCostBudgetPolicy()).inspect()

        self.assertEqual(attention.interrupted_job_count, 1)
        self.assertEqual(attention.blocking_effect_recovery_count, 1)
        self.assertEqual(len(attention.items), 1)
        self.assertEqual(attention.items[0].kind, CompanyAttentionKind.EFFECT_RECOVERY)
        self.assertEqual(attention.items[0].subject_id, action_id)
        self.assertIn("job effect-resolve", attention.items[0].recommended_action)
        self.assertEqual(before, store.active_job_table_payloads(request.job_id))
        store.close()

    def test_attention_limit_is_explicit_and_bounded(self) -> None:
        self.assertEqual(normalize_attention_job_limit(1), 1)
        self.assertEqual(normalize_attention_job_limit(500), 500)
        with self.assertRaisesRegex(ValueError, "between 1 and 500"):
            normalize_attention_job_limit(0)
        with self.assertRaisesRegex(ValueError, "integer"):
            normalize_attention_job_limit(True)


if __name__ == "__main__":
    unittest.main()
