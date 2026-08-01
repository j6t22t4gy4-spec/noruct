"""Approval resume and durable-history projection for Employee Runtime."""

from __future__ import annotations

import json
import time
from typing import Any

from dynamic_firm.runtime.models import EmployeeRunRequest, ModelMessage, RunHandle, ToolCall
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import PolicyDenied, ToolExecutionError

from .runtime import _BudgetReached, _PolicyRejected, NoructEmployeeRuntimeError


async def resume_approval_batch(
    service: Any,
    request: EmployeeRunRequest,
    handle: RunHandle,
    cancellation: CancellationToken,
    started: float,
) -> None:
    """Resume only the durable tool-call checkpoint that awaited approval."""

    assistant = next(
        (
            message
            for message in reversed(service.store.list_messages(handle.run_id))
            if message.role == "assistant"
            and isinstance(message.content, dict)
            and message.content.get("tool_calls")
        ),
        None,
    )
    if assistant is None:
        raise NoructEmployeeRuntimeError(
            "durable approval has no matching tool-call checkpoint"
        )
    raw_calls = assistant.content.get("tool_calls")
    if not isinstance(raw_calls, list):
        raw_calls = list(raw_calls) if isinstance(raw_calls, tuple) else []
    model_call_index = service.store.get_usage(handle.run_id).model_calls
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
            raise NoructEmployeeRuntimeError(
                "durable approval tool-call checkpoint is invalid"
            )
        action_id = service.tool_executor.action_id(
            handle.run_id,
            model_call_index,
            call.call_id,
        )
        usage = service.store.get_usage(handle.run_id)
        if (
            usage.tool_calls >= request.limits.max_tool_calls
            and service.store.get_tool_result(action_id) is None
        ):
            raise _BudgetReached("max_tool_calls")
        prior = service.store.tool_call_count_before(
            handle.run_id,
            model_call_index,
            call.name,
        ) + current_name_counts.get(call.name, 0)
        try:
            result = await service.tool_executor.execute(
                run_id=handle.run_id,
                model_call_index=model_call_index,
                call=call,
                policy=request.action_policy,
                cancellation=cancellation,
                prior_tool_calls=prior,
                max_result_bytes=request.limits.max_result_bytes,
                max_tool_output_bytes=request.limits.max_tool_output_bytes,
                current_usage=service.store.get_usage(handle.run_id),
                remaining_wall_ms=max(
                    1,
                    int(
                        request.limits.max_wall_time_ms
                        - (time.monotonic() - started) * 1000
                    ),
                ),
            )
        except PolicyDenied as exc:
            raise _PolicyRejected(str(exc)) from exc
        except ToolExecutionError as exc:
            raise NoructEmployeeRuntimeError(str(exc)) from exc
        service.store.append_tool_message_once(
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


def project_resume_history(service: Any, run_id: str) -> list[dict[str, Any]]:
    """Project only durable user/assistant/tool history for a resumed worker."""

    actions = {
        str(action["tool_call_id"]): str(action["tool_name"])
        for action in service.store.list_tool_actions(run_id)
    }
    projected: list[dict[str, Any]] = []
    for message in service.store.list_messages(run_id):
        if message.role == "system":
            continue
        if message.role == "user":
            projected.append(
                {
                    "role": "user",
                    "content": (
                        message.content
                        if isinstance(message.content, str)
                        else json.dumps(
                            message.content,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    ),
                }
            )
            continue
        if message.role == "assistant" and isinstance(message.content, dict):
            item: dict[str, Any] = {
                "role": "assistant",
                "content": str(message.content.get("content") or ""),
            }
            raw_calls = message.content.get("tool_calls") or []
            if isinstance(raw_calls, (list, tuple)) and raw_calls:
                item["tool_calls"] = [
                    {
                        "id": str(raw.get("call_id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(raw.get("name") or ""),
                            "arguments": json.dumps(
                                raw.get("arguments")
                                if isinstance(raw.get("arguments"), dict)
                                else {},
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for raw in raw_calls
                    if isinstance(raw, dict)
                ]
            projected.append(item)
            continue
        if message.role == "tool" and isinstance(message.content, dict):
            projected.append(
                {
                    "role": "tool",
                    "name": actions.get(message.tool_call_id or "", ""),
                    "tool_call_id": message.tool_call_id or "",
                    "content": str(message.content.get("content") or ""),
                }
            )
    return projected
