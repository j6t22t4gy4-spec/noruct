from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from dynamic_firm.application.job_cli import run_job_command
from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.mutation import frozen_snapshot_digest
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.runtime.job_ledger import SQLiteActiveJobLedger
from dynamic_firm.runtime.interruption import EffectInterruptionReason
from dynamic_firm.runtime.models import IdempotencyMode, ToolCall, ToolEffect
from dynamic_firm.runtime.store import RunStore
from tests.kernel.helpers import company_request, task
from tests.runtime.helpers import make_request


class JobEffectResolutionCliTests(unittest.TestCase):
    def test_local_job_listing_does_not_require_an_enabled_remote_credential(self) -> None:
        """An optional coordination outage must not hide local operator state."""

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            store = RunStore(state_path)
            store.close()
            output = io.StringIO()
            code = run_job_command(
                Namespace(job_command="list", limit=10, json=True),
                state_path=state_path,
                settings={
                    "company_coordination": {
                        "enabled": True,
                        "endpoint": "https://coordination.invalid",
                        "company_scope_digest": "a" * 64,
                        "device_id": "device-local-test",
                        "token_env": "NORUCT_TEST_MISSING_COORDINATION_TOKEN",
                    }
                },
                output=output,
            )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), [])

    def test_confirmed_no_effect_appends_evidence_and_releases_resource(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            store = RunStore(state_path)
            request = make_request(request_id="effect-cli-request")
            handle, created = store.create_run(request)
            self.assertTrue(created)
            store.begin_run(handle.run_id)
            call = ToolCall("call-effect-cli", "workspace_write", {"value": "redacted"})
            action_id = "action-effect-cli"
            resource_key = "workspace:effect-cli"
            store.record_tool_intent(
                handle.run_id,
                action_id,
                1,
                call,
                hashlib.sha256(b"arguments").hexdigest(),
                resource_key,
                effect=ToolEffect.WRITE,
                idempotency_mode=IdempotencyMode.NONE.value,
            )
            self.assertTrue(
                store.acquire_effect_resource_lease(
                    action_id=action_id,
                    run_id=handle.run_id,
                    effect=ToolEffect.WRITE,
                    resource_key=resource_key,
                )
            )
            store.mark_tool_started(action_id)
            store.mark_tool_effect_indeterminate(
                action_id,
                cause=EffectInterruptionReason.PROCESS_OR_MACHINE_LOSS,
            )
            store.recover_interrupted_runs()
            store.close()

            evidence_digest = hashlib.sha256(b"operator evidence").hexdigest()
            output = io.StringIO()
            code = run_job_command(
                Namespace(
                    job_command="effect-resolve",
                    job_id=request.task.job_id,
                    action_id=action_id,
                    outcome="confirmed-no-effect",
                    evidence_digest=evidence_digest,
                    operator_id="operator-test",
                    reason="external audit proved no effect",
                    confirm=True,
                    json=True,
                ),
                state_path=state_path,
                settings={},
                output=output,
            )

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["outcome"], "CONFIRMED_NO_EFFECT")
            self.assertEqual(payload["evidence_digest"], evidence_digest)
            self.assertTrue(payload["resource_released"])
            reopened = RunStore(state_path)
            try:
                case = reopened.list_job_effect_recovery_cases(request.task.job_id)[0]
            finally:
                reopened.close()
            self.assertEqual(case["case_status"], "RESOLVED")
            self.assertFalse(case["lease_held"])

    def test_resolution_requires_confirmation_before_store_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            empty_store = RunStore(state_path)
            empty_store.close()
            with self.assertRaisesRegex(ValueError, "requires --confirm"):
                run_job_command(
                    Namespace(
                        job_command="effect-resolve",
                        job_id="job-does-not-matter",
                        action_id="action-does-not-matter",
                        outcome="seal-unknown",
                        evidence_digest=None,
                        operator_id="operator-test",
                        reason="no trustworthy evidence",
                        confirm=False,
                        json=True,
                    ),
                    state_path=state_path,
                    settings={},
                    output=io.StringIO(),
                )

    def test_execution_summary_is_read_only_and_honest_about_missing_terminal_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            request = company_request(
                (task("final"),),
                final_task_id="final",
                roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
            )
            graph = graph_from_proposal(
                request.plan_proposal, max_tasks=request.job_limits.max_tasks
            )
            store = RunStore(state_path)
            SQLiteActiveJobLedger(store).start_job(
                request, graph, frozen_snapshot_digest(request)
            )
            store.close()
            output = io.StringIO()
            code = run_job_command(
                Namespace(job_command="summary", job_id=request.job_id, json=True),
                state_path=state_path,
                settings={},
                output=output,
            )

        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["schema_version"], "noruct.execution-summary.v1")
        self.assertEqual(summary["result"]["terminal_status"], "NOT_RECORDED")
        self.assertEqual(summary["verification"][1]["status"], "NOT_RUN")
        self.assertEqual(summary["result"]["outcome_claim"], "NO_REAL_WORLD_OUTCOME_CLAIM")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
