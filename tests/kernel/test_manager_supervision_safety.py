from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields, replace
from types import SimpleNamespace

from dynamic_firm.company import ManagerDelegation
from dynamic_firm.kernel.models import EmployeeRecord, JobStatus
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.supervision import (
    ManagerSupervisionAction,
    ManagerSupervisionContext,
    ManagerSupervisionDecision,
)
from dynamic_firm.kernel.testing import (
    ScriptedEmployeeExecutionPort,
    ScriptedOutcome,
)
from dynamic_firm.runtime.job_ledger import SQLiteActiveJobLedger
from dynamic_firm.runtime.models import RunSignal, SignalCode
from dynamic_firm.runtime.store import RunStore
from tests.kernel.helpers import company_request, task


def _managed_request(*tasks, final_task_id: str):  # type: ignore[no-untyped-def]
    request = replace(
        company_request(
            tuple(tasks),
            final_task_id=final_task_id,
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        ),
        manager_employee_id="manager",
        manager_assignment_digest="a" * 64,
        manager_session_key="manager:manager:safety-test",
        work_order_id="work-order-manager-supervision-safety",
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
    return replace(
        request,
        manager_delegation_payload=delegation.canonical_payload(),
        manager_delegation_digest=delegation.content_digest,
    )


class ManagerSupervisionSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_context_static_surface_excludes_every_manager_authority(self) -> None:
        expected_fields = (
            "job_id",
            "graph_version",
            "task_id",
            "priority",
            "remaining_wall_time_ms",
            "required_capabilities",
            "capability_shortage",
            "conflicting_outcome",
            "result_status",
            "unresolved_issue_count",
        )
        self.assertEqual(
            tuple(field.name for field in fields(ManagerSupervisionContext)),
            expected_fields,
        )
        prohibited_authority_names = {
            "permission",
            "action_policy",
            "approval",
            "budget_lease",
            "budget_authority",
            "ledger",
            "artifact",
            "activation",
            "company_state",
            "credential",
            "secret",
            "token",
        }
        self.assertTrue(
            prohibited_authority_names.isdisjoint(expected_fields)
        )
        context = ManagerSupervisionContext(
            job_id="job-static-boundary",
            graph_version=1,
            task_id="final",
            priority="FINAL_INTEGRATION",
            remaining_wall_time_ms=1_000,
            required_capabilities=("analysis",),
            capability_shortage=(),
            conflicting_outcome=False,
            result_status="SUCCEEDED",
            unresolved_issue_count=0,
        )
        self.assertFalse(hasattr(context, "__dict__"))
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            context.approval = "ALLOW"  # type: ignore[attr-defined,misc]

    async def test_fault_invalid_output_and_disallowed_signal_all_fail_safe(self) -> None:
        class Supervisor:
            def __init__(self, mode: str) -> None:
                self.mode = mode
                self.calls = 0

            async def assess(self, context):  # type: ignore[no-untyped-def]
                del context
                self.calls += 1
                if self.mode == "exception":
                    raise RuntimeError("fixture supervisor failure")
                if self.mode == "invalid-output":
                    return object()
                return SimpleNamespace(
                    action=ManagerSupervisionAction.SIGNAL,
                    rationale="Attempt an unpermitted assignee signal.",
                    signal=RunSignal(
                        SignalCode.ASSIGNEE_MISMATCH,
                        value="manager-cannot-select-an-assignee",
                    ),
                )

        for mode in ("exception", "invalid-output", "disallowed-signal"):
            with self.subTest(mode=mode):
                supervisor = Supervisor(mode)
                store = RunStore()
                request = _managed_request(task("final"), final_task_id="final")
                result = await FirmKernel(
                    employee_execution=ScriptedEmployeeExecutionPort(
                        {"final": ScriptedOutcome("completed result")}
                    ),
                    manager_supervisor=supervisor,  # type: ignore[arg-type]
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
                supervision = store.get_job_supervision_events(request.job_id)
                store.close()

                self.assertEqual(supervisor.calls, 1)
                self.assertEqual(result.status, JobStatus.SUCCEEDED)
                self.assertEqual(result.summary, "completed result")
                self.assertEqual(result.metrics.graph_patch_count, 0)
                self.assertEqual(result.metrics.task_mutation_count, 0)
                self.assertEqual(result.task_results[0].signals, ())
                self.assertEqual(supervision, ())

        with self.assertRaisesRegex(ValueError, "not permitted"):
            ManagerSupervisionDecision(
                action=ManagerSupervisionAction.SIGNAL,
                rationale="Disallowed signal must fail construction.",
                signal=RunSignal(SignalCode.ASSIGNEE_MISMATCH),
            )

    async def test_each_completed_result_gets_at_most_one_supervision_call(self) -> None:
        class CountingSupervisor:
            def __init__(self) -> None:
                self.task_ids: list[str] = []

            async def assess(self, context):  # type: ignore[no-untyped-def]
                self.task_ids.append(context.task_id)
                return ManagerSupervisionDecision(
                    action=ManagerSupervisionAction.CONTINUE,
                    rationale="One bounded observation is sufficient.",
                )

        supervisor = CountingSupervisor()
        store = RunStore()
        request = _managed_request(
            task("research"),
            task("final", depends_on=("research",)),
            final_task_id="final",
        )
        result = await FirmKernel(
            employee_execution=ScriptedEmployeeExecutionPort(
                {
                    "research": ScriptedOutcome("research complete"),
                    "final": ScriptedOutcome("integration complete"),
                }
            ),
            manager_supervisor=supervisor,
            active_job_ledger=SQLiteActiveJobLedger(store),
        ).run(request)
        supervision = store.get_job_supervision_events(request.job_id)
        store.close()

        completed_task_ids = tuple(item.task_id for item in result.task_results)
        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(set(supervisor.task_ids), set(completed_task_ids))
        self.assertEqual(len(supervisor.task_ids), len(completed_task_ids))
        self.assertEqual(len(supervisor.task_ids), len(set(supervisor.task_ids)))
        self.assertEqual(len(supervision), len(result.task_results))
        self.assertEqual(
            {item["task_id"] for item in supervision},
            set(completed_task_ids),
        )


if __name__ == "__main__":
    unittest.main()
