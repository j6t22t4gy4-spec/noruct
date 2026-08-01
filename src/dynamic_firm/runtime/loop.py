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
from .loop_session import EmployeeAgentLoopSessionMixin
from .loop_approval import EmployeeAgentLoopApprovalMixin
from .loop_outcome import EmployeeAgentLoopOutcomeMixin


class EmployeeAgentLoop(
    EmployeeAgentLoopSessionMixin,
    EmployeeAgentLoopApprovalMixin,
    EmployeeAgentLoopOutcomeMixin,
):
    _VALIDATION_CHECK_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

    def __init__(
        self,
        *,
        store: RunStore,
        provider: ModelProviderPort,
        registry: ToolRegistry,
        approval_port: ApprovalPort | None = None,
        prompt_builder: PromptBuilder | None = None,
        completion_validator: CompletionValidatorPort | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.registry = registry
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.completion_validator = completion_validator
        self.tool_executor = ToolExecutor(registry, store, approval_port=approval_port)
        self.tool_batch_planner = PermissionPreservingToolBatchPlanner()
        self.context_compactor = BoundedContextCompactor()
        self.cost_efficiency_projector = CostEfficiencyProjector()

    async def run(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        cancellation: CancellationToken,
        *,
        resume: bool = False,
    ) -> EmployeeRunResult:
        started_monotonic = time.monotonic()
        usage = self.store.get_usage(handle.run_id)
        consecutive_errors = 0
        completion_repairs = 0
        completion_validation_attempts = 0
        tool_counts: dict[str, int] = {}
        session_key = self._session_key(request)
        session_history: tuple[ModelMessage, ...] = ()
        expected_session_revision = 0
        transient_user_message: str | None = None
        session_lease_claimed = False
        try:
            status = self.store.begin_run(handle.run_id)
            if status == RunStatus.CANCELLING or cancellation.cancelled:
                return self._cancelled(request, handle, usage, cancellation.reason)
            if status.terminal:
                result = self.store.get_result(handle.run_id)
                if result:
                    return result
                raise RuntimeError("Terminal run has no result")

            if request.session_retention == EmployeeSessionRetention.PERSIST:
                session_lease_claimed = self.store.acquire_employee_session_lease(
                    namespace_hash=session_key,
                    employee_id=request.employee.employee_id,
                    run_id=handle.run_id,
                )
                if not session_lease_claimed:
                    return self._failed(
                        request,
                        handle,
                        usage,
                        Failure(
                            "EMPLOYEE_SESSION_BUSY",
                            FailureCategory.INTERNAL,
                            "Another live run owns this Employee conversation state; retry after it reaches a terminal state.",
                            retryable=False,
                            origin="noruct-session-lease",
                        ),
                    )

            # Both selectable Employee Runtime profiles deliberately share
            # this first-party session projection.  The native profile used
            # to retain only the current run ledger, which meant a safe
            # noruct -> legacy rollback silently lost a conversation.  Keep
            # provider-facing history outside the immutable per-run ledger;
            # it is atomically committed only with a successful run below.
            persisted_session = (
                self.store.load_employee_session(
                    session_key,
                    request.employee.employee_id,
                )
                if request.session_retention == EmployeeSessionRetention.PERSIST
                else None
            )
            if persisted_session is not None:
                session_history = self._session_history_to_model_messages(
                    persisted_session.messages
                )
                expected_session_revision = persisted_session.revision

            tool_schemas = self.registry.schemas_for_policy(request.action_policy)
            if resume:
                if status != RunStatus.WAITING_APPROVAL:
                    raise RuntimeError("Only a durable approval wait can resume an employee run")
                resumed = await self._resume_approval_batch(
                    request,
                    handle,
                    cancellation,
                    started_monotonic,
                )
                if isinstance(resumed, EmployeeRunResult):
                    return resumed
                consecutive_errors = 1 if resumed else 0
                usage = self.store.get_usage(handle.run_id)
                for action in self.store.list_tool_actions(handle.run_id):
                    name = str(action["tool_name"])
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                if request.session_retention == EmployeeSessionRetention.RUN_ONLY:
                    transient_user_message = self.prompt_builder.build(request).user_message
            else:
                snapshot = self.prompt_builder.build(request)
                transient_user_message = (
                    snapshot.user_message
                    if request.session_retention == EmployeeSessionRetention.RUN_ONLY
                    else None
                )
                self.store.record_prompt(
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
                self.store.append_message(handle.run_id, ModelMessage("system", snapshot.system_prompt))
                self.store.append_message(
                    handle.run_id,
                    ModelMessage(
                        "user",
                        snapshot.audit_user_message or snapshot.user_message,
                    ),
                )

            while True:
                cancellation.raise_if_cancelled()
                budget_reason = self._budget_reason(request, usage, started_monotonic, before_model=True)
                if budget_reason:
                    return self._budget_exhausted(request, handle, usage, budget_reason)

                # Composite providers may fan one logical Employee turn out to
                # several physical model calls.  Admit the worst-case closure
                # before dispatch rather than discovering after the fact that
                # fallback or advisor fan-out exceeded the frozen Run limit.
                model_call_ceiling = self._model_call_ceiling(self.provider)
                if usage.model_calls + model_call_ceiling > request.limits.max_model_calls:
                    return self._budget_exhausted(
                        request,
                        handle,
                        usage,
                        "max_model_calls",
                    )

                call_index = usage.model_calls + 1
                self.store.append_event(
                    handle.run_id,
                    EventType.MODEL_CALL_STARTED,
                    {"call_index": call_index, "model_profile": request.employee.model_profile},
                )
                canonical_messages = self._canonical_messages(
                    handle.run_id,
                    session_history,
                    transient_user_message=transient_user_message,
                )
                compacted = self.context_compactor.compact(
                    canonical_messages,
                    max_messages=request.limits.max_context_messages,
                    max_chars=request.limits.max_context_chars,
                    keep_recent_messages=request.limits.context_keep_recent_messages,
                )
                if compacted.compacted:
                    self.store.append_event(
                        handle.run_id,
                        EventType.CONTEXT_COMPACTED,
                        {
                            "call_index": call_index,
                            "revision": self.context_compactor.revision,
                            "removed_message_count": compacted.removed_message_count,
                            "source_sha256": compacted.source_hash,
                            "chars_before": compacted.chars_before,
                            "chars_after": compacted.chars_after,
                            "canonical_message_count": len(canonical_messages),
                            "projected_message_count": len(compacted.messages),
                        },
                    )
                economy = self.cost_efficiency_projector.project(
                    compacted.messages,
                    mode=request.limits.cost_efficiency_mode,
                )
                if economy.applied:
                    self.store.append_event(
                        handle.run_id,
                        EventType.CONTEXT_ECONOMY_PROJECTED,
                        {
                            "call_index": call_index,
                            "revision": self.cost_efficiency_projector.revision,
                            "projected_message_count": economy.projected_message_count,
                            "chars_before": economy.chars_before,
                            "chars_after": economy.chars_after,
                            "mode": request.limits.cost_efficiency_mode.value,
                        },
                    )
                model_request = ModelRequest(
                    messages=economy.messages,
                    tools=tool_schemas,
                    model_profile=request.employee.model_profile,
                    run_id=handle.run_id,
                    call_index=call_index,
                )
                remaining_ms = (
                    request.limits.max_wall_time_ms
                    - (time.monotonic() - started_monotonic) * 1000
                )
                if remaining_ms <= 0:
                    return self._budget_exhausted(
                        request,
                        handle,
                        usage,
                        "max_wall_time_ms",
                    )
                remaining_seconds = remaining_ms / 1000
                try:
                    stream_state = {"chunks": 0, "chars": 0, "emitted_chars": -1}

                    def record_stream(progress: ModelStreamProgress) -> None:
                        stream_state["chunks"] = progress.chunk_count
                        stream_state["chars"] = progress.received_chars
                        should_emit = (
                            progress.finished
                            or stream_state["emitted_chars"] < 0
                            or progress.received_chars - stream_state["emitted_chars"] >= 512
                        )
                        if not should_emit:
                            return
                        stream_state["emitted_chars"] = progress.received_chars
                        self.store.append_event(
                            handle.run_id,
                            EventType.MODEL_STREAM_PROGRESS,
                            {
                                "call_index": call_index,
                                "chunk_count": progress.chunk_count,
                                "received_chars": progress.received_chars,
                                "finished": progress.finished,
                            },
                        )

                    completion = (
                        self.provider.complete_stream(
                            model_request,
                            cancellation,
                            record_stream,
                        )
                        if isinstance(self.provider, StreamingModelProviderPort)
                        else self.provider.complete(model_request, cancellation)
                    )
                    response = await asyncio.wait_for(
                        completion,
                        timeout=remaining_seconds,
                    )
                    # Thread-backed transports enqueue progress on this loop.
                    # Drain those callbacks before a run can become terminal.
                    await asyncio.sleep(0)
                except TimeoutError:
                    # The provider did not return trustworthy usage.  Preserve
                    # the physical-call ceiling so the Employee/Job audit does
                    # not report an impossible zero-call timeout.  Company
                    # budget terminalization separately treats this timeout as
                    # indeterminate cost and forfeits its reservation.
                    usage = usage.plus(Usage(model_calls=model_call_ceiling))
                    return self._budget_exhausted(request, handle, usage, "max_wall_time_ms")
                except OperationCancelled:
                    raise
                except ModelProviderError as exc:
                    delta = replace(
                        exc.usage,
                        model_calls=max(1, exc.usage.model_calls),
                        tool_calls=0,
                    )
                    usage = usage.plus(delta)
                    return self._failed(
                        request,
                        handle,
                        usage,
                        Failure(
                            exc.code,
                            FailureCategory.TIMEOUT
                            if exc.code in {"MODEL_TIMEOUT", "MODEL_STALE"}
                            else FailureCategory.MODEL,
                            exc.message_safe,
                            retryable=exc.retryable,
                            origin="model-provider",
                        ),
                    )
                except Exception as exc:
                    # An unexpected provider boundary failure cannot prove how
                    # much of a composite call already ran.  Charge the frozen
                    # physical ceiling and let Company settlement fail closed.
                    usage = usage.plus(Usage(model_calls=model_call_ceiling))
                    return self._failed(
                        request,
                        handle,
                        usage,
                        Failure(
                            "MODEL_PROVIDER_ERROR",
                            FailureCategory.MODEL,
                            f"Model provider failed: {type(exc).__name__}",
                            retryable=True,
                        ),
                    )
                cancellation.raise_if_cancelled()
                delta = replace(
                    response.usage,
                    model_calls=max(1, response.usage.model_calls),
                    tool_calls=0,
                )
                usage = usage.plus(delta)
                self.store.append_event(
                    handle.run_id,
                    EventType.MODEL_CALL_COMPLETED,
                    {
                        "call_index": call_index,
                        "provider_request_id": response.provider_request_id,
                        "finish_reason": response.finish_reason,
                        "response_kind": "completion" if response.completion else "tool_calls",
                        "tool_call_count": len(response.tool_calls),
                    },
                    usage_delta=delta,
                    new_usage=usage,
                )
                self.store.append_message(
                    handle.run_id,
                    ModelMessage(
                        "assistant",
                        {
                            "content": response.content,
                            "tool_calls": [to_primitive(call) for call in response.tool_calls],
                            "completion": to_primitive(response.completion) if response.completion else None,
                        },
                    ),
                )

                budget_reason = self._budget_reason(request, usage, started_monotonic, before_model=False)
                if budget_reason:
                    return self._budget_exhausted(request, handle, usage, budget_reason)

                response_error = self._response_error(response, request.limits.max_result_bytes)
                if response_error:
                    consecutive_errors += 1
                    if consecutive_errors >= request.limits.max_consecutive_errors:
                        return self._failed(
                            request,
                            handle,
                            usage,
                            Failure(
                                "MODEL_OUTPUT_INVALID",
                                FailureCategory.MODEL,
                                response_error,
                                retryable=True,
                            ),
                        )
                    self.store.append_message(
                        handle.run_id,
                        ModelMessage(
                            "user",
                            {
                                "runtime_error": "MODEL_OUTPUT_INVALID",
                                "message": response_error,
                                "remaining_repairs": request.limits.max_consecutive_errors - consecutive_errors,
                            },
                        ),
                    )
                    continue

                if response.completion:
                    if self.completion_validator is not None:
                        completion_validation_attempts += 1
                        try:
                            validation = self.completion_validator.validate(
                                request,
                                response.completion,
                            )
                            validation_error = self._completion_validation_error(
                                validation
                            )
                        except Exception as exc:
                            return self._failed(
                                request,
                                handle,
                                usage,
                                Failure(
                                    "COMPLETION_VALIDATOR_ERROR",
                                    FailureCategory.INTERNAL,
                                    (
                                        "Completion validator failed: "
                                        f"{type(exc).__name__}"
                                    ),
                                    retryable=False,
                                ),
                            )
                        if validation_error is not None:
                            return self._failed(
                                request,
                                handle,
                                usage,
                                Failure(
                                    "COMPLETION_VALIDATOR_INVALID",
                                    FailureCategory.INTERNAL,
                                    validation_error,
                                    retryable=False,
                                ),
                            )
                        self.store.append_event(
                            handle.run_id,
                            EventType.VALIDATION_RECORDED,
                            {
                                "validation_kind": "completion",
                                "attempt": completion_validation_attempts,
                                "passed": validation.passed,
                                "failed_checks": validation.failed_checks,
                                "repair_remaining": (
                                    0 if validation.passed else 1 - completion_repairs
                                ),
                            },
                        )
                        if not validation.passed:
                            if completion_repairs >= 1:
                                return self._failed(
                                    request,
                                    handle,
                                    usage,
                                    Failure(
                                        "COMPLETION_VALIDATION_FAILED",
                                        FailureCategory.INPUT,
                                        (
                                            "Employee completion did not satisfy "
                                            "the first-party output contract."
                                        ),
                                        retryable=False,
                                    ),
                                )
                            completion_repairs += 1
                            self.store.append_message(
                                handle.run_id,
                                ModelMessage(
                                    "user",
                                    {
                                        "runtime_error": (
                                            "COMPLETION_VALIDATION_FAILED"
                                        ),
                                        "failed_checks": validation.failed_checks,
                                        "message": validation.semantic_expectation,
                                        "remaining_repairs": 0,
                                    },
                                ),
                            )
                            continue
                    if any(
                        signal.code == SignalCode.ASSIGNEE_MISMATCH
                        for signal in response.completion.signals
                    ):
                        return self._assignee_mismatch(
                            request,
                            handle,
                            usage,
                            response.completion,
                        )
                    employee_session = (
                        EmployeeSessionUpdate(
                            namespace_hash=session_key,
                            employee_id=request.employee.employee_id,
                            expected_revision=expected_session_revision,
                            messages=self._session_messages_for_success(
                                session_history,
                                handle.run_id,
                            ),
                            max_messages=request.limits.max_context_messages,
                            max_chars=request.limits.max_context_chars,
                        )
                        if request.session_retention == EmployeeSessionRetention.PERSIST
                        else None
                    )
                    return self._succeeded(
                        request,
                        handle,
                        usage,
                        response.completion,
                        employee_session=employee_session,
                    )

                usage = self.store.get_usage(handle.run_id)
                output_bytes = self.store.get_tool_output_bytes(handle.run_id)
                if usage.tool_calls >= request.limits.max_tool_calls:
                    return self._budget_exhausted(request, handle, usage, "max_tool_calls")
                if output_bytes >= request.limits.max_tool_output_bytes:
                    return self._budget_exhausted(
                        request, handle, usage, "max_tool_output_bytes"
                    )

                batch_plan = self.tool_batch_planner.plan(
                    response.tool_calls,
                    registry=self.registry,
                    policy=request.action_policy,
                    prior_tool_counts=tool_counts,
                )
                if usage.tool_calls + len(response.tool_calls) > request.limits.max_tool_calls:
                    batch_plan = replace(
                        batch_plan,
                        mode=ToolBatchMode.SEQUENTIAL,
                        reason="batch_exceeds_tool_budget",
                    )
                remaining_output = request.limits.max_tool_output_bytes - output_bytes
                if batch_plan.mode == ToolBatchMode.PARALLEL:
                    declared_output = sum(
                        min(
                            self.registry.get(call.name).output_limit_bytes,
                            request.limits.max_result_bytes,
                        )
                        for call in response.tool_calls
                    )
                    if declared_output > remaining_output:
                        batch_plan = replace(
                            batch_plan,
                            mode=ToolBatchMode.SEQUENTIAL,
                            reason="output_budget_requires_sequential",
                        )
                if len(response.tool_calls) > 1:
                    self.store.append_event(
                        handle.run_id,
                        EventType.TOOL_BATCH_PLANNED,
                        {
                            "call_index": call_index,
                            "call_count": len(response.tool_calls),
                            "mode": batch_plan.mode.value,
                            "reason": batch_plan.reason,
                        },
                    )

                parallel = batch_plan.mode == ToolBatchMode.PARALLEL
                staged_counts = dict(tool_counts)

                async def execute_tool(call):
                    prior_count = staged_counts.get(call.name, 0)
                    staged_counts[call.name] = prior_count + 1
                    return await self.tool_executor.execute(
                        run_id=handle.run_id,
                        model_call_index=call_index,
                        call=call,
                        policy=request.action_policy,
                        cancellation=cancellation,
                        prior_tool_calls=prior_count,
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
                        reserved_output_limit_bytes=None,
                    )

                if parallel:
                    outcomes = await asyncio.gather(
                        *(execute_tool(call) for call in response.tool_calls),
                        return_exceptions=True,
                    )
                else:
                    outcomes = []
                    for call in response.tool_calls:
                        cancellation.raise_if_cancelled()
                        usage = self.store.get_usage(handle.run_id)
                        if usage.tool_calls >= request.limits.max_tool_calls:
                            return self._budget_exhausted(
                                request, handle, usage, "max_tool_calls"
                            )
                        try:
                            outcomes.append(await execute_tool(call))
                        except (OperationCancelled, PolicyDenied, ToolExecutionError) as exc:
                            outcomes.append(exc)

                batch_failed = False
                for call, outcome in zip(response.tool_calls, outcomes, strict=True):
                    if isinstance(outcome, OperationCancelled):
                        raise outcome
                    if isinstance(outcome, PolicyDenied):
                        return self._failed(
                            request,
                            handle,
                            self.store.get_usage(handle.run_id),
                            Failure(
                                "ACTION_POLICY_DENIED",
                                FailureCategory.POLICY,
                                str(outcome),
                                retryable=False,
                            ),
                        )
                    if isinstance(outcome, ToolExecutionError):
                        return self._failed(
                            request,
                            handle,
                            self.store.get_usage(handle.run_id),
                            Failure(
                                "ACTION_INDETERMINATE",
                                FailureCategory.TOOL,
                                str(outcome),
                                retryable=False,
                            ),
                        )
                    if isinstance(outcome, BaseException):
                        raise outcome
                    result = outcome
                    tool_counts[call.name] = tool_counts.get(call.name, 0) + 1
                    self.store.append_message(
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
                    batch_failed = batch_failed or not result.ok
                usage = self.store.get_usage(handle.run_id)

                if batch_failed:
                    consecutive_errors += 1
                    if consecutive_errors >= request.limits.max_consecutive_errors:
                        return self._failed(
                            request,
                            handle,
                            usage,
                            Failure(
                                "TOOL_ERRORS_EXHAUSTED",
                                FailureCategory.TOOL,
                                "Repeated tool validation or execution errors exhausted the repair budget.",
                                retryable=True,
                            ),
                        )
                else:
                    consecutive_errors = 0
        except OperationCancelled:
            return self._cancelled(request, handle, self.store.get_usage(handle.run_id), cancellation.reason)
        except asyncio.CancelledError:
            cancellation.cancel("Runtime task cancelled")
            return self._cancelled(request, handle, self.store.get_usage(handle.run_id), cancellation.reason)
        except EmployeeSessionConflict:
            return self._failed(
                request,
                handle,
                self.store.get_usage(handle.run_id),
                Failure(
                    "EMPLOYEE_SESSION_CONFLICT",
                    FailureCategory.INTERNAL,
                    "Employee conversation state changed during this run; retry is safe.",
                    retryable=True,
                    origin="noruct-state",
                ),
            )
        except Exception as exc:
            return self._failed(
                request,
                handle,
                self.store.get_usage(handle.run_id),
                Failure(
                    "INTERNAL_RUNTIME_ERROR",
                    FailureCategory.INTERNAL,
                    f"Native runtime failed: {type(exc).__name__}",
                    retryable=True,
                ),
            )
        finally:
            if session_lease_claimed:
                self.store.release_employee_session_lease(
                    namespace_hash=session_key,
                    employee_id=request.employee.employee_id,
                    run_id=handle.run_id,
                )
