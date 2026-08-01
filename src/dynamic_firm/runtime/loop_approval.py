from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import replace
from datetime import datetime

from .models import (
    CompletionEnvelope,
    CompletionValidation,
    EmployeeRunRequest,
    EmployeeRunResult,
    EmployeeSessionRetention,
    EventType,
    Failure,
    FailureCategory,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamProgress,
    RunHandle,
    RunSignal,
    RunStatus,
    SignalCode,
    ToolCall,
    Usage,
    to_primitive,
    utc_now,
)
from .context_compaction import BoundedContextCompactor
from .cost_efficiency import CostEfficiencyProjector
from .ports import (
    ApprovalPort,
    CancellationToken,
    CompletionValidatorPort,
    ModelProviderError,
    ModelProviderPort,
    OperationCancelled,
    StreamingModelProviderPort,
)
from .prompt import PromptBuilder
from .store import (
    EmployeeSessionConflict,
    EmployeeSessionUpdate,
    RunStore,
    employee_session_namespace,
)
from .tool_batch import PermissionPreservingToolBatchPlanner, ToolBatchMode
from .tools import (
    PolicyDenied,
    ToolExecutionError,
    ToolExecutor,
    ToolRegistry,
    capability_projection,
)



class EmployeeAgentLoopApprovalMixin:
    """Resume a durable approval batch without recreating its model request."""
    async def _resume_approval_batch(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        cancellation: CancellationToken,
        started_monotonic: float,
    ) -> bool | EmployeeRunResult:
        messages = self.store.list_messages(handle.run_id)
        assistant = next(
            (
                message
                for message in reversed(messages)
                if message.role == "assistant"
                and isinstance(message.content, dict)
                and message.content.get("tool_calls")
            ),
            None,
        )
        if assistant is None:
            return self._failed(
                request,
                handle,
                self.store.get_usage(handle.run_id),
                Failure(
                    "APPROVAL_RESUME_CONTEXT_MISSING",
                    FailureCategory.INTERNAL,
                    "The durable approval has no matching tool-call checkpoint.",
                    retryable=False,
                ),
            )
        raw_calls = assistant.content.get("tool_calls")
        if not isinstance(raw_calls, list):
            raw_calls = list(raw_calls) if isinstance(raw_calls, tuple) else []
        model_call_index = self.store.get_usage(handle.run_id).model_calls
        batch_failed = False
        current_name_counts: dict[str, int] = {}
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            call = ToolCall(
                str(raw.get("call_id") or ""),
                str(raw.get("name") or ""),
                raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {},
            )
            if not call.call_id or not call.name:
                return self._failed(
                    request,
                    handle,
                    self.store.get_usage(handle.run_id),
                    Failure(
                        "APPROVAL_RESUME_CONTEXT_INVALID",
                        FailureCategory.INTERNAL,
                        "The durable approval tool-call checkpoint is invalid.",
                        retryable=False,
                    ),
                )
            cancellation.raise_if_cancelled()
            usage = self.store.get_usage(handle.run_id)
            action_id = self.tool_executor.action_id(
                handle.run_id,
                model_call_index,
                call.call_id,
            )
            if (
                usage.tool_calls >= request.limits.max_tool_calls
                and self.store.get_tool_result(action_id) is None
            ):
                return self._budget_exhausted(
                    request,
                    handle,
                    usage,
                    "max_tool_calls",
                )
            prior = self.store.tool_call_count_before(
                handle.run_id,
                model_call_index,
                call.name,
            ) + current_name_counts.get(call.name, 0)
            try:
                result = await self.tool_executor.execute(
                    run_id=handle.run_id,
                    model_call_index=model_call_index,
                    call=call,
                    policy=request.action_policy,
                    cancellation=cancellation,
                    prior_tool_calls=prior,
                    max_result_bytes=request.limits.max_result_bytes,
                    max_tool_output_bytes=request.limits.max_tool_output_bytes,
                    current_usage=self.store.get_usage(handle.run_id),
                    remaining_wall_ms=max(
                        1,
                        int(
                            request.limits.max_wall_time_ms
                            - (time.monotonic() - started_monotonic) * 1000
                        ),
                    ),
                )
            except PolicyDenied as exc:
                return self._failed(
                    request,
                    handle,
                    self.store.get_usage(handle.run_id),
                    Failure(
                        "ACTION_POLICY_DENIED",
                        FailureCategory.POLICY,
                        str(exc),
                        retryable=False,
                    ),
                )
            except ToolExecutionError as exc:
                return self._failed(
                    request,
                    handle,
                    self.store.get_usage(handle.run_id),
                    Failure(
                        "ACTION_INDETERMINATE",
                        FailureCategory.TOOL,
                        str(exc),
                        retryable=False,
                    ),
                )
            self.store.append_tool_message_once(
                handle.run_id,
                ModelMessage(
                    "tool",
                    {
                        "ok": result.ok,
                        "content": result.content,
                        "error_code": result.error_code,
                        "action_id": result.action_id,
                    },
                    call.call_id,
                ),
            )
            current_name_counts[call.name] = current_name_counts.get(call.name, 0) + 1
            batch_failed = batch_failed or not result.ok
        return batch_failed

