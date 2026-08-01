"""Employee Runtime execution lifecycle composed through an injected service owner.

The owner retains RunStore, provider, worker, approval, and result authority.
This module coordinates one request lifecycle through those already-owned ports.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import hashlib
import json
import time
import uuid
from typing import Any, Mapping

from dynamic_firm.company.model_invocation_receipt import (
    InvocationTerminalStatus,
    ModelInvocationReceipt,
    ReceiptAvailability,
)
from dynamic_firm.company.frozen_task_route_provider import FrozenTaskRouteProvider
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
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
    RunStatus,
    SignalCode,
    ToolCall,
    ToolSchema,
    Usage,
    to_primitive,
)
from dynamic_firm.runtime.ports import (
    CancellationToken,
    ModelProviderError,
    OperationCancelled,
    StreamingModelProviderPort,
    observe_model_fanout,
)
from dynamic_firm.runtime.store import EmployeeSessionUpdate
from dynamic_firm.runtime.tools import (
    PolicyDenied,
    ToolExecutionError,
    capability_projection,
)

from .protocol import FoundationProtocolError
from .runtime import (
    FoundationDependencyUnavailable,
    NoructEmployeeRuntimeError,
    _BudgetReached,
    _EMPTY_RESPONSE_ERROR,
    _EmptyResponseExhausted,
    _FOUNDATION_EMPTY_RESPONSE_TERMINAL_COUNT,
    _InvalidModelOutput,
    _PolicyRejected,
    _UnexpectedProviderFailure,
)
from .runtime_support import (
    _model_visible_tool_schemas,
    _safe_namespace,
    _scrub_memory_context_response,
)


def _receipt_digest(value: object) -> str:
    """Hash a value without retaining its potentially sensitive contents."""

    raw = json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _frozen_dispatch_epoch(service: Any) -> str:
    """Return one opaque epoch stable for this local service process only."""

    epoch = getattr(service, "_frozen_dispatch_epoch", None)
    if isinstance(epoch, str) and epoch:
        return epoch
    epoch = f"epoch-{uuid.uuid4().hex}"
    setattr(service, "_frozen_dispatch_epoch", epoch)
    return epoch


def _record_frozen_model_invocation(
    service: Any,
    handle: RunHandle,
    binding: object,
    model_request: ModelRequest,
    started: float,
    *,
    invocation_id: str | None,
    fanout_parent_id: str | None = None,
    terminal_status: InvocationTerminalStatus,
    response: ModelResponse | None = None,
    safe_error_code: str | None = None,
) -> None:
    """Append one content-free receipt for one physical frozen provider call."""

    if binding is None:
        return
    # The value has already been parsed and verified by RunStore.  Keeping the
    # local type boundary narrow avoids making an ordinary provider adapter a
    # route authority.
    route_binding_digest = binding.digest
    if terminal_status is InvocationTerminalStatus.SUCCEEDED:
        if response is None:
            raise ValueError("successful invocation receipt requires a response")
        usage_availability = ReceiptAvailability.AVAILABLE
        usage_units = (
            response.usage.input_tokens
            + response.usage.cached_input_tokens
            + response.usage.output_tokens
        )
        # ModelResponse has a numeric cost default but no observation state;
        # treating that default as an observed zero would collapse unknown and
        # zero.  A typed provider availability signal is required before cost
        # can be claimed as observed.
        cost_availability = ReceiptAvailability.UNAVAILABLE
        cost_usd = None
        output_digest = _receipt_digest(
            {
                "content": response.content,
                "tool_calls": response.tool_calls,
                "completion": response.completion,
            }
        )
    else:
        usage_availability = ReceiptAvailability.UNAVAILABLE
        usage_units = None
        cost_availability = ReceiptAvailability.UNAVAILABLE
        cost_usd = None
        output_digest = None
    if invocation_id is None:
        raise ValueError("frozen invocation receipt requires a dispatch reservation")
    receipt = ModelInvocationReceipt(
        invocation_id=invocation_id,
        route_binding_digest=route_binding_digest,
        context_projection_digest=_receipt_digest(
            {
                "messages": model_request.messages,
                "tools": model_request.tools,
                "model_profile": model_request.model_profile,
            }
        ),
        attempt_id=binding.attempt_id,
        fanout_parent_id=fanout_parent_id,
        terminal_status=terminal_status,
        output_digest=output_digest,
        usage_availability=usage_availability,
        usage_units=usage_units,
        cost_availability=cost_availability,
        cost_usd=cost_usd,
        latency_ms=(time.monotonic() - started) * 1000,
        safe_error_code=safe_error_code,
    )
    service.store.finalize_model_invocation_receipt(handle.run_id, receipt)

async def execute_runtime(
    service: Any,
    request: EmployeeRunRequest,
    handle: RunHandle,
    cancellation: CancellationToken,
    *,
    resume: bool = False,
) -> EmployeeRunResult:
    # Frozen runs cross their durable RUNNING boundary together with their
    # non-expiring local dispatcher ownership.  This occurs before prompt or
    # worker state can be persisted; legacy runs retain their original start
    # lifecycle.
    frozen_binding = service.store.get_frozen_route_binding(handle.run_id)
    if frozen_binding is None:
        status = service.store.begin_run(handle.run_id)
    else:
        status = service.store.begin_frozen_run_with_dispatch_lease(
            handle.run_id,
            dispatch_epoch=_frozen_dispatch_epoch(service),
        )
    if status == RunStatus.CANCELLING or cancellation.cancelled:
        return service._cancelled(request, handle, cancellation.reason)
    if status.terminal:
        result = service.store.get_result(handle.run_id)
        if result is not None:
            return result
        raise NoructEmployeeRuntimeError("terminal employee run has no result")
    if resume and status != RunStatus.WAITING_APPROVAL:
        raise NoructEmployeeRuntimeError(
            "only a durable approval wait can resume an employee run"
        )
    snapshot = service.prompt_builder.build(request)
    tool_schemas = service.registry.schemas_for_policy(request.action_policy)
    if not resume:
        service.store.record_prompt(
            handle.run_id,
            snapshot.prompt_hash,
            snapshot.context_hash,
            knowledge_projection=snapshot.knowledge_projection,
            capability_projection=capability_projection(
                request.action_policy,
                tool_schemas,
                employee_profile=request.employee.capability_profile,
            ),
        )
        service.store.append_message(
            handle.run_id,
            ModelMessage("system", snapshot.system_prompt),
        )
        service.store.append_message(
            handle.run_id,
            ModelMessage(
                "user",
                snapshot.audit_user_message or snapshot.user_message,
            ),
        )
    session_key, worker = service._worker_for(request)
    service._run_workers[handle.run_id] = worker
    started = time.monotonic()
    completions: dict[int, CompletionEnvelope] = {}
    tool_counts: dict[str, int] = {}
    consecutive_empty_responses = 0
    empty_response_limit = min(
        request.limits.max_consecutive_errors,
        _FOUNDATION_EMPTY_RESPONSE_TERMINAL_COUNT,
    )

    async with worker.lock:
        cancellation.raise_if_cancelled()
        persisted = (
            service.store.load_employee_session(
                session_key,
                request.employee.employee_id,
            )
            if request.session_retention == EmployeeSessionRetention.PERSIST
            else None
        )
        history = list(persisted.messages) if persisted is not None else []
        expected_revision = persisted.revision if persisted is not None else 0
        if resume:
            await service._resume_approval_batch(
                request,
                handle,
                cancellation,
                started,
            )
            history.extend(service._project_resume_history(handle.run_id))
            user_message = (
                snapshot.user_message
                + "\nContinue the same task from the approved tool result and finish it."
                if request.session_retention == EmployeeSessionRetention.RUN_ONLY
                else "Continue the same task from the approved tool result and finish it."
            )
            for action in service.store.list_tool_actions(handle.run_id):
                name = str(action["tool_name"])
                tool_counts[name] = tool_counts.get(name, 0) + 1
        else:
            user_message = snapshot.user_message
        projected_history = service.context_compactor.compact_session_history(
            history,
            max_messages=request.limits.max_context_messages,
            max_chars=request.limits.max_context_chars,
            keep_recent_messages=request.limits.context_keep_recent_messages,
        )
        if projected_history.compacted:
            service.store.append_event(
                handle.run_id,
                EventType.CONTEXT_COMPACTED,
                {
                "call_index": 1,
                    "scope": "employee_session_projection",
                    "revision": service.context_compactor.revision,
                    "removed_message_count": (
                        projected_history.removed_message_count
                    ),
                    "source_sha256": projected_history.source_hash,
                    "chars_before": projected_history.chars_before,
                    "chars_after": projected_history.chars_after,
                    "canonical_message_count": len(history),
                    "projected_message_count": len(projected_history.messages),
                    "employee_session_revision": expected_revision,
                },
            )
            history = list(projected_history.messages)
        model_profile = service._execution_model_profile(request, handle)
        await worker.send(
            "execute",
            handle.run_id,
            {
                "conversation_history": history,
                "initial_model_call_index": service.store.get_usage(
                    handle.run_id
                ).model_calls,
                "max_model_calls": max(
                    1,
                    request.limits.max_model_calls
                    - service.store.get_usage(handle.run_id).model_calls,
                ),
                "model_profile": model_profile,
                "local_planning_enabled": "planning" in request.employee.capabilities,
                "company_context": {
                    "employee_id": request.employee.employee_id,
                    "task_id": request.task.task_id,
                    "workspace_id": request.context.workspace_id,
                    "company_policy_excerpt": request.context.company_policy_excerpt,
                },
                "session_id": _safe_namespace(session_key),
                "system_message": snapshot.system_prompt,
                "task_id": request.task.task_id,
                "tools": [to_primitive(schema) for schema in tool_schemas],
                "user_message": user_message,
            },
        )
        while True:
            cancellation.raise_if_cancelled()
            frame = await worker.receive()
            if frame.run_id != handle.run_id:
                raise FoundationProtocolError("worker returned a cross-run frame")
            if frame.type == "text_delta":
                text = str(frame.payload.get("text") or "")
                if text:
                    service.store.append_event(
                        handle.run_id,
                        EventType.MODEL_TEXT_DELTA,
                        {"text": text},
                    )
                continue
            if frame.type == "model_request":
                if consecutive_empty_responses:
                    service.store.append_event(
                        handle.run_id,
                        EventType.MODEL_RECOVERY_REQUESTED,
                        {
                            "reason": "EMPTY_RESPONSE",
                            "attempt": consecutive_empty_responses,
                            "max_consecutive_errors": empty_response_limit,
                        },
                    )
                response = await service._model_call(
                    request,
                    handle,
                    cancellation,
                    frame.payload,
                    _model_visible_tool_schemas(frame.payload, tool_schemas),
                )
                call_index = int(frame.payload.get("call_index") or 0)
                if response.completion is not None:
                    completions[call_index] = response.completion
                if service._is_empty_response(response):
                    consecutive_empty_responses += 1
                    if (
                        consecutive_empty_responses
                        >= request.limits.max_consecutive_errors
                        and consecutive_empty_responses
                        < _FOUNDATION_EMPTY_RESPONSE_TERMINAL_COUNT
                    ):
                        raise _EmptyResponseExhausted
                    if (
                        service.store.get_usage(handle.run_id).model_calls
                        >= request.limits.max_model_calls
                        and consecutive_empty_responses
                        < _FOUNDATION_EMPTY_RESPONSE_TERMINAL_COUNT
                    ):
                        raise _BudgetReached("max_model_calls")
                else:
                    consecutive_empty_responses = 0
                await worker.send(
                    "provider_response",
                    handle.run_id,
                    {
                        "content": response.content
                        or (response.completion.summary if response.completion else ""),
                        "finish_reason": response.finish_reason,
                        "tool_calls": [to_primitive(call) for call in response.tool_calls],
                        "usage": to_primitive(response.usage),
                    },
                )
                continue
            if frame.type == "tool_intent":
                call = ToolCall(
                    str(frame.payload.get("call_id") or ""),
                    str(frame.payload.get("name") or ""),
                    frame.payload.get("arguments")
                    if isinstance(frame.payload.get("arguments"), dict)
                    else {},
                )
                if service.store.get_usage(handle.run_id).tool_calls >= request.limits.max_tool_calls:
                    raise _BudgetReached("max_tool_calls")
                prior = tool_counts.get(call.name, 0)
                try:
                    result = await service.tool_executor.execute(
                        run_id=handle.run_id,
                        model_call_index=int(frame.payload.get("call_index") or 0),
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
                tool_counts[call.name] = prior + 1
                service.store.append_message(
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
                await worker.send(
                    "tool_result",
                    handle.run_id,
                    {
                        "action_id": result.action_id,
                        "content": result.content,
                        "error_code": result.error_code,
                        "ok": result.ok,
                    },
                )
                continue
            if frame.type == "terminal":
                if frame.payload.get("interrupted"):
                    raise OperationCancelled(
                        str(frame.payload.get("reason") or cancellation.reason or "cancelled")
                    )
                raw_history = frame.payload.get("messages")
                if not isinstance(raw_history, list) or not all(
                    isinstance(item, dict) for item in raw_history
                ):
                    raise FoundationProtocolError(
                        "Employee worker terminal frame omitted structured session history"
                    )
                if frame.payload.get("turn_exit_reason") == "empty_response_exhausted":
                    raise _EmptyResponseExhausted
                final_text = str(frame.payload.get("final_response") or "").strip()
                completion = (
                    completions[max(completions)]
                    if completions
                    else CompletionEnvelope(summary=final_text)
                )
                if not completion.summary.strip():
                    raise NoructEmployeeRuntimeError("Employee runtime returned an empty completion")
                if service.completion_validator is not None:
                    validation = service.completion_validator.validate(
                        request,
                        completion,
                    )
                    if type(validation.passed) is not bool:
                        raise NoructEmployeeRuntimeError(
                            "Completion validator returned an invalid result"
                        )
                    service.store.append_event(
                        handle.run_id,
                        EventType.VALIDATION_RECORDED,
                        {
                            "validation_kind": "completion",
                            "attempt": 1,
                            "passed": validation.passed,
                            "failed_checks": validation.failed_checks,
                            # The foundation worker has already reached a
                            # terminal frame.  It therefore cannot make a
                            # hidden post-terminal repair attempt.
                            "repair_remaining": 0,
                        },
                    )
                    if not validation.passed:
                        return service._failed(
                            request,
                            handle,
                            Failure(
                                "COMPLETION_VALIDATION_FAILED",
                                FailureCategory.MODEL,
                                "Employee completion did not satisfy the task contract.",
                                retryable=False,
                                origin="noruct-authority",
                            ),
                        )
                if any(
                    signal.code == SignalCode.ASSIGNEE_MISMATCH
                    for signal in completion.signals
                ):
                    return service._assignee_mismatch(request, handle, completion)
                employee_session = (
                    EmployeeSessionUpdate(
                        namespace_hash=session_key,
                        employee_id=request.employee.employee_id,
                        expected_revision=expected_revision,
                        messages=raw_history,
                        max_messages=request.limits.max_context_messages,
                        max_chars=request.limits.max_context_chars,
                    )
                    if request.session_retention == EmployeeSessionRetention.PERSIST
                    else None
                )
                return service._succeeded(
                    request,
                    handle,
                    completion,
                    employee_session=employee_session,
                )
            if frame.type == "worker_error":
                error_type = str(frame.payload.get("error_type") or "error")
                if error_type in {"ImportError", "ModuleNotFoundError"}:
                    raise FoundationDependencyUnavailable(
                        "Employee worker dependency import failed; repair the "
                        "Noruct installation in the selected runtime Python"
                    )
                raise NoructEmployeeRuntimeError(f"worker {error_type}")
            raise FoundationProtocolError(f"unexpected worker frame: {frame.type}")

async def call_runtime_model(
    service: Any,
    request: EmployeeRunRequest,
    handle: RunHandle,
    cancellation: CancellationToken,
    payload: Mapping[str, Any],
    tool_schemas: tuple[ToolSchema, ...],
) -> ModelResponse:
    usage = service.store.get_usage(handle.run_id)
    budget_reason = service._usage_budget_reason(request, usage, before_model=True)
    if budget_reason:
        raise _BudgetReached(budget_reason)
    call_index = usage.model_calls + 1
    model_profile = service._execution_model_profile(request, handle)
    # A frozen run needs its durable dispatcher lease before it can even
    # project a physical-call start.  Legacy mode retains its established
    # observational timing.
    frozen_binding = service.store.get_frozen_route_binding(handle.run_id)
    if frozen_binding is None:
        service.store.append_event(
            handle.run_id,
            EventType.MODEL_CALL_STARTED,
            {"call_index": call_index, "model_profile": model_profile},
        )
    messages = []
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            raise FoundationProtocolError("model request message must be an object")
        content = item.get("content")
        if str(item.get("role") or "") == "assistant":
            raw_calls = item.get("tool_calls")
            if isinstance(raw_calls, list) and raw_calls:
                calls: list[dict[str, Any]] = []
                for raw_call in raw_calls:
                    if not isinstance(raw_call, dict):
                        continue
                    call_id = raw_call.get("id")
                    function = raw_call.get("function")
                    if not isinstance(call_id, str) or not isinstance(function, dict):
                        continue
                    name = function.get("name")
                    encoded_arguments = function.get("arguments")
                    if not isinstance(name, str) or not isinstance(encoded_arguments, str):
                        continue
                    try:
                        arguments = json.loads(encoded_arguments)
                    except json.JSONDecodeError as exc:
                        raise FoundationProtocolError(
                            "employee session tool arguments are invalid JSON"
                        ) from exc
                    if not isinstance(arguments, dict):
                        raise FoundationProtocolError(
                            "employee session tool arguments must be an object"
                        )
                    calls.append(
                        {
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments,
                        }
                    )
                if calls:
                    content = {
                        "content": content,
                        "tool_calls": calls,
                        "completion": None,
                    }
        messages.append(
            ModelMessage(
                str(item.get("role") or ""),
                content,
                str(item.get("tool_call_id"))
                if item.get("tool_call_id") is not None
                else None,
            )
        )
    economy = service.cost_efficiency_projector.project(
        tuple(messages),
        mode=request.limits.cost_efficiency_mode,
    )
    if economy.applied:
        service.store.append_event(
            handle.run_id,
            EventType.CONTEXT_ECONOMY_PROJECTED,
            {
                "call_index": call_index,
                "revision": service.cost_efficiency_projector.revision,
                "projected_message_count": economy.projected_message_count,
                "chars_before": economy.chars_before,
                "chars_after": economy.chars_after,
                "mode": request.limits.cost_efficiency_mode.value,
            },
        )
    model_request = ModelRequest(
        economy.messages,
        tool_schemas,
        model_profile,
        handle.run_id,
        call_index,
    )
    # Receipt emission is deliberately gated by the durable binding, rather
    # than by a provider capability or request-provided model profile.  Legacy
    # execution remains observationally unchanged.
    invocation_id: str | None = None
    if frozen_binding is not None:
        # Receipt attribution is valid only for the dedicated frozen-provider
        # path and for the exact model identity admitted with this run.  Do
        # this before reservation or provider dispatch so a shadow/default
        # path cannot leave misleading durable evidence.
        if (
            not isinstance(service._frozen_provider, FrozenTaskRouteProvider)
            or service.provider is not service._frozen_provider
        ):
            raise ValueError("frozen receipt dispatch requires foundation frozen-provider mode")
        if model_request.model_profile != frozen_binding.requested_model_id:
            raise ValueError("frozen receipt model profile does not match binding")
        dispatch_epoch = _frozen_dispatch_epoch(service)
        service.store.acquire_model_invocation_dispatch_lease(
            handle.run_id,
            dispatch_epoch=dispatch_epoch,
        )
        service.store.append_event(
            handle.run_id,
            EventType.MODEL_CALL_STARTED,
            {"call_index": call_index, "model_profile": model_profile},
        )
        invocation_id = service.store.reserve_model_invocation_dispatch(
            handle.run_id,
            dispatch_epoch=dispatch_epoch,
            route_binding_digest=frozen_binding.digest,
            context_projection_digest=_receipt_digest(
                {
                    "messages": model_request.messages,
                    "tools": model_request.tools,
                    "model_profile": model_request.model_profile,
                }
            ),
            attempt_id=frozen_binding.attempt_id,
        )
    invocation_started = time.monotonic()
    stream = {"chunks": 0, "chars": 0, "emitted": -1}

    def progress(value: ModelStreamProgress) -> None:
        stream["chunks"] = value.chunk_count
        stream["chars"] = value.received_chars
        if not value.finished and stream["emitted"] >= 0 and value.received_chars - stream["emitted"] < 512:
            return
        stream["emitted"] = value.received_chars
        service.store.append_event(
            handle.run_id,
            EventType.MODEL_STREAM_PROGRESS,
            {
                "call_index": call_index,
                "chunk_count": value.chunk_count,
                "received_chars": value.received_chars,
                "finished": value.finished,
            },
        )

    def record_fanout_child(
        phase: object,
        _label: object,
        child_request: object,
        **details: object,
    ) -> str | None:
        if frozen_binding is None or invocation_id is None:
            return None
        if not isinstance(child_request, ModelRequest):
            raise ValueError("fan-out observer requires a ModelRequest")
        dispatch_epoch = _frozen_dispatch_epoch(service)
        if phase == "START":
            return service.store.reserve_model_invocation_dispatch(
                handle.run_id,
                dispatch_epoch=dispatch_epoch,
                route_binding_digest=frozen_binding.digest,
                context_projection_digest=_receipt_digest(
                    {
                        "messages": child_request.messages,
                        "tools": child_request.tools,
                        "model_profile": child_request.model_profile,
                    }
                ),
                attempt_id=frozen_binding.attempt_id,
            )
        if phase != "TERMINAL":
            raise ValueError("fan-out observer phase is invalid")
        child_id = details.get("invocation_id")
        started = details.get("started")
        terminal_status = details.get("terminal_status")
        response = details.get("response")
        safe_error_code = details.get("safe_error_code")
        if not isinstance(child_id, str) or not isinstance(started, (int, float)):
            raise ValueError("fan-out terminal identity is invalid")
        _record_frozen_model_invocation(
            service,
            handle,
            frozen_binding,
            child_request,
            float(started),
            invocation_id=child_id,
            fanout_parent_id=invocation_id,
            terminal_status=InvocationTerminalStatus(terminal_status),
            response=response if isinstance(response, ModelResponse) else None,
            safe_error_code=safe_error_code if isinstance(safe_error_code, str) else None,
        )
        return None

    try:
        observer_context = (
            observe_model_fanout(record_fanout_child)
            if frozen_binding is not None
            else nullcontext()
        )
        with observer_context:
            response = await (
                service.provider.complete_stream(model_request, cancellation, progress)
                if isinstance(service.provider, StreamingModelProviderPort)
                else service.provider.complete(model_request, cancellation)
            )
    except OperationCancelled:
        _record_frozen_model_invocation(
            service,
            handle,
            frozen_binding,
            model_request,
            invocation_started,
            invocation_id=invocation_id,
            terminal_status=InvocationTerminalStatus.INDETERMINATE,
            safe_error_code="RUN_CANCELLED",
        )
        provider_request_id = service._consume_cancelled_provider_request_id(
            handle.run_id
        )
        if provider_request_id is not None:
            service.store.append_event(
                handle.run_id,
                EventType.MODEL_CALL_CANCELLED,
                {
                    "call_index": call_index,
                    "provider_request_id": provider_request_id,
                },
            )
        worker = service._run_workers.get(handle.run_id)
        if worker is not None:
            await worker.send(
                "provider_error",
                handle.run_id,
                {"code": "RUN_CANCELLED", "message": cancellation.reason},
            )
        raise
    except ModelProviderError as exc:
        _record_frozen_model_invocation(
            service,
            handle,
            frozen_binding,
            model_request,
            invocation_started,
            invocation_id=invocation_id,
            terminal_status=InvocationTerminalStatus.FAILED,
            safe_error_code=exc.code,
        )
        raise
    except asyncio.CancelledError:
        _record_frozen_model_invocation(
            service,
            handle,
            frozen_binding,
            model_request,
            invocation_started,
            invocation_id=invocation_id,
            # Task cancellation has no provider terminal observation.  Do
            # not overstate it as a provider-confirmed cancellation.
            terminal_status=InvocationTerminalStatus.INDETERMINATE,
            safe_error_code=("RUN_CANCELLED" if cancellation.cancelled else "CALL_INTERRUPTED"),
        )
        raise
    except Exception as exc:
        _record_frozen_model_invocation(
            service,
            handle,
            frozen_binding,
            model_request,
            invocation_started,
            invocation_id=invocation_id,
            terminal_status=InvocationTerminalStatus.INDETERMINATE,
            safe_error_code="PROVIDER_INDETERMINATE",
        )
        raise _UnexpectedProviderFailure(type(exc).__name__) from exc
    response = _scrub_memory_context_response(response)
    response_error = service._model_response_error(
        response,
        request.limits.max_result_bytes,
    )
    # The physical invocation is complete at this boundary.  Record it before
    # downstream transcript, usage, or completion handling can fail.
    _record_frozen_model_invocation(
        service,
        handle,
        frozen_binding,
        model_request,
        invocation_started,
        invocation_id=invocation_id,
        terminal_status=InvocationTerminalStatus.SUCCEEDED,
        response=response,
    )
    # Once a response exists, its physical call is already durably evidenced.
    # A subsequently observed cancellation remains a run outcome, not a
    # retroactive statement about the completed provider dispatch.
    cancellation.raise_if_cancelled()
    delta = Usage(
        model_calls=1,
        input_tokens=response.usage.input_tokens,
        cached_input_tokens=response.usage.cached_input_tokens,
        output_tokens=response.usage.output_tokens,
        cost_usd=response.usage.cost_usd,
    )
    new_usage = usage.plus(delta)
    service.store.append_event(
        handle.run_id,
        EventType.MODEL_CALL_COMPLETED,
        {
            "call_index": call_index,
            "provider_request_id": response.provider_request_id,
            "finish_reason": response.finish_reason,
            "response_kind": (
                "completion"
                if response.completion
                else "tool_calls"
                if response.tool_calls
                else "empty"
                if service._is_empty_response(response)
                else "text"
            ),
            "tool_call_count": len(response.tool_calls),
        },
        usage_delta=delta,
        new_usage=new_usage,
    )
    service.store.append_message(
        handle.run_id,
        ModelMessage(
            "assistant",
            {
                "content": response.content,
                "tool_calls": [to_primitive(call) for call in response.tool_calls],
                "completion": to_primitive(response.completion)
                if response.completion
                else None,
            },
        ),
    )
    budget_reason = service._usage_budget_reason(request, new_usage, before_model=False)
    if budget_reason:
        raise _BudgetReached(budget_reason)
    if response_error and response_error != _EMPTY_RESPONSE_ERROR:
        raise _InvalidModelOutput(response_error)
    return response
