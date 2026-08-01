from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.kernel.mutation import frozen_snapshot_digest
from dynamic_firm.runtime.job_ledger import SQLiteActiveJobLedger
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResumeState,
    EventType,
    ModelMessage,
    ModelResponse,
    RunStatus,
    ToolCall,
    ToolEffect,
    ToolGrant,
    ToolRisk,
    Usage,
)
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import ApprovalConflict, RunStore
from dynamic_firm.runtime.tools import ToolExecutor, ToolRegistry, WorkspaceTools
from tests.runtime.helpers import completion, make_request
from tests.kernel.helpers import company_request, task


class _AllowApproval:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        return ApprovalDecision.ALLOW_ONCE


def _request(request_id: str):
    base = make_request(request_id=request_id, workspace_id="repo")
    return replace(
        base,
        action_policy=ActionPolicy(
            tool_grants=(
                ToolGrant(
                    tool_name="write_workspace_file",
                    allowed_effects=(ToolEffect.WRITE,),
                    resource_patterns=("workspace:repo:*",),
                    max_calls=2,
                    requires_approval=True,
                ),
            ),
            filesystem_policy="WORKSPACE_WRITE",
        ),
    )


def _call() -> ToolCall:
    return ToolCall(
        "write-approval-1",
        "write_workspace_file",
        {
            "workspace_id": "repo",
            "path": "src/app.py",
            "content": "value = 1\n",
        },
    )


def _approval(handle, request, action_id: str) -> ApprovalRequest:
    return ApprovalRequest(
        action_id=action_id,
        run_id=handle.run_id,
        job_id=request.task.job_id,
        task_id=request.task.task_id,
        employee_id=request.employee.employee_id,
        tool_name="write_workspace_file",
        effect=ToolEffect.WRITE,
        risk=ToolRisk.MEDIUM,
        resource_key="workspace:repo:src/app.py",
        preview="Write src/app.py (10 bytes)",
        allow_session=True,
    )


def _stage_waiting_approval(store: RunStore, request):
    handle, _ = store.create_run(request)
    store.begin_run(handle.run_id)
    store.append_message(handle.run_id, ModelMessage("system", "system"))
    store.append_message(handle.run_id, ModelMessage("user", "write the file"))
    call = _call()
    store.append_event(
        handle.run_id,
        EventType.MODEL_CALL_COMPLETED,
        {"call_index": 1, "response_kind": "tool_calls", "tool_call_count": 1},
        usage_delta=Usage(model_calls=1),
        new_usage=Usage(model_calls=1),
    )
    store.append_message(
        handle.run_id,
        ModelMessage(
            "assistant",
            {"content": "", "tool_calls": [call], "completion": None},
        ),
    )
    arguments_json = json.dumps(
        call.arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    action_id = ToolExecutor.action_id(handle.run_id, 1, call.call_id)
    store.record_tool_intent(
        handle.run_id,
        action_id,
        1,
        call,
        hashlib.sha256(arguments_json.encode("utf-8")).hexdigest(),
        "workspace:repo:src/app.py",
        usage_delta=Usage(tool_calls=1),
        new_usage=Usage(model_calls=1, tool_calls=1),
    )
    store.record_approval_request(_approval(handle, request, action_id))
    return handle, call, action_id


class ApprovalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_firm_job_does_not_resume_waiting_approval_outside_kernel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.db"
            employee_request = _request("approval-interrupted-firm-job")
            firm_request = company_request(
                (task("task-1", capabilities=("repository_analysis",)),),
                final_task_id="task-1",
                roster=(
                    EmployeeRecord(
                        "employee-researcher",
                        "Repository Analyst",
                        ("repository_analysis",),
                    ),
                ),
            )
            firm_request = replace(
                firm_request,
                request_id="firm-approval-interrupted",
                job_id=employee_request.task.job_id,
                plan_proposal=replace(
                    firm_request.plan_proposal,
                    proposal_id="proposal-approval-interrupted",
                ),
            )
            first_store = RunStore(database)
            SQLiteActiveJobLedger(first_store).start_job(
                firm_request,
                graph_from_proposal(
                    firm_request.plan_proposal,
                    max_tasks=firm_request.job_limits.max_tasks,
                ),
                frozen_snapshot_digest(firm_request),
            )
            handle, _, action_id = _stage_waiting_approval(
                first_store,
                employee_request,
            )
            first_store.close()

            reopened = RunStore(database)
            recovered = reopened.recover_interrupted_runs(
                preserve_waiting_approvals=True,
            )

            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].run_id, handle.run_id)
            self.assertEqual(recovered[0].failure.code, "PROCESS_INTERRUPTED")
            self.assertEqual(reopened.get_status(handle.run_id), RunStatus.FAILED)
            with self.assertRaises(ApprovalConflict):
                reopened.resolve_approval(action_id, ApprovalDecision.ALLOW_ONCE)
            reopened.close()

    async def test_resolution_is_idempotent_and_conflicting_retry_fails_closed(self) -> None:
        store = RunStore()
        request = _request("approval-resolution-cas")
        handle, _, action_id = _stage_waiting_approval(store, request)

        first = store.resolve_approval(
            action_id,
            ApprovalDecision.ALLOW_ONCE,
            decided_by="user-1",
        )
        repeated = store.resolve_approval(
            action_id,
            ApprovalDecision.ALLOW_ONCE,
            decided_by="user-1",
        )
        with self.assertRaises(ApprovalConflict):
            store.resolve_approval(
                action_id,
                ApprovalDecision.DENY,
                decided_by="user-2",
            )

        self.assertTrue(first.applied)
        self.assertFalse(repeated.applied)
        self.assertEqual(first.approval.resume_state, ApprovalResumeState.READY)
        self.assertEqual(store.get_status(handle.run_id), RunStatus.WAITING_APPROVAL)
        events = store.list_events(handle.run_id)
        self.assertEqual(sum(e.type == EventType.APPROVAL_REQUIRED for e in events), 1)
        self.assertEqual(sum(e.type == EventType.APPROVAL_RESOLVED for e in events), 1)
        store.close()

    async def test_reopened_runtime_resumes_exact_tool_call_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            database = Path(directory) / "runtime.db"
            request = _request("approval-restart-resume")

            first_store = RunStore(database)
            handle, _, action_id = _stage_waiting_approval(first_store, request)
            first_store.close()

            second_store = RunStore(database)
            registry = ToolRegistry()
            for definition in WorkspaceTools({"repo": root}).definitions():
                registry.register(definition)
            provider = ScriptedModelProvider(
                [ModelResponse(completion=completion("The approved write completed"))]
            )
            approval_port = _AllowApproval()
            service = NativeEmployeeRuntimeService(
                store=second_store,
                provider=provider,
                registry=registry,
                approval_port=approval_port,
            )

            self.assertEqual(service.recovered_results, [])
            resumed_handle = await service.start(request)
            result = await service.collect(resumed_handle)

            self.assertEqual(resumed_handle, handle)
            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual((root / "src" / "app.py").read_text(), "value = 1\n")
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(len(approval_port.requests), 1)
            approval = second_store.get_approval(action_id)
            self.assertIsNotNone(approval)
            self.assertEqual(approval.resume_state, ApprovalResumeState.COMPLETED)
            events = second_store.list_events(handle.run_id)
            self.assertEqual(
                sum(e.type == EventType.APPROVAL_RESUME_CLAIMED for e in events),
                1,
            )
            self.assertEqual(
                sum(e.type == EventType.APPROVAL_RESUME_COMPLETED for e in events),
                1,
            )

            replay = await ToolExecutor(registry, second_store).execute(
                run_id=handle.run_id,
                model_call_index=1,
                call=_call(),
                policy=request.action_policy,
                cancellation=CancellationToken(),
                prior_tool_calls=1,
                max_result_bytes=request.limits.max_result_bytes,
                max_tool_output_bytes=request.limits.max_tool_output_bytes,
                current_usage=second_store.get_usage(handle.run_id),
                remaining_wall_ms=1_000,
            )
            self.assertTrue(replay.replayed)
            self.assertEqual(
                sum(
                    e.type == EventType.APPROVAL_RESUME_COMPLETED
                    for e in second_store.list_events(handle.run_id)
                ),
                1,
            )
            await service.close()
            second_store.close()


if __name__ == "__main__":
    unittest.main()
