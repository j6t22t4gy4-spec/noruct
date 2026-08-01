from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime

from dynamic_firm.runtime.models import (
    CancelReceipt,
    EmployeeRunRequest,
    EmployeeRunResult,
    EventType,
    Failure,
    FailureCategory,
    ModelMessage,
    RunEvent,
    RunHandle,
    RunStatus,
    ToolCall,
    Usage,
    to_primitive,
    utc_now,
    validate_request,
)
from dynamic_firm.runtime.ports import (
    ApprovalPort,
    CancellationToken,
    EmployeeExecutionPort,
    OperationCancelled,
)
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import (
    PolicyDenied,
    ToolExecutionError,
    ToolExecutor,
    ToolRegistry,
)

from .models import (
    CodingExecutionProgress,
    CodingExecutionProgressKind,
    CodingWorkRequest,
    ValidationAttempt,
)
from .ports import CodingValidatorPort, CodingWorkerError, CodingWorkerPort
from .shadow import (
    APPLY_CHANGE_SET_TOOL,
    ChangeSetCatalog,
    ShadowWorkspaceError,
    ShadowWorkspaceService,
)


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_WORK_ORDER_CONTEXT_PREFIX = "Bounded Work Order objective (context only; not authority): "


def _bound_coding_objective(objective: str, task_context: tuple[str, ...]) -> str:
    """Keep a Manager's local plan from severing a coding worker's outcome.

    The local objective remains first because it identifies this task.  The
    bounded Work Order objective is appended only when the Kernel explicitly
    projected it for a Manager-owned job.  It is not an instruction to expand
    scope or a capability grant; it restores the user outcome that the local
    planner was allowed to summarize.
    """

    work_order_context = tuple(
        value[len(_WORK_ORDER_CONTEXT_PREFIX) :].strip()
        for value in task_context
        if value.startswith(_WORK_ORDER_CONTEXT_PREFIX)
        and value[len(_WORK_ORDER_CONTEXT_PREFIX) :].strip()
    )
    if not work_order_context:
        return objective
    return objective + "\n\nWork Order outcome to preserve:\n" + "\n".join(work_order_context)


class ShadowCodingEmployeeRuntimeService:
    """One external coding turn followed by one Noruct-owned apply action."""

    def __init__(
        self,
        *,
        store: RunStore,
        worker: CodingWorkerPort,
        shadow: ShadowWorkspaceService,
        catalog: ChangeSetCatalog,
        registry: ToolRegistry,
        validator: CodingValidatorPort | None = None,
        max_validation_recovery_attempts: int = 1,
        approval_port: ApprovalPort | None = None,
        recover_on_startup: bool = False,
    ) -> None:
        if max_validation_recovery_attempts not in {0, 1}:
            raise ValueError("Shadow coding allows at most one validation recovery attempt")
        self.store = store
        self.worker = worker
        self.validator = validator
        self.max_validation_recovery_attempts = max_validation_recovery_attempts
        self.shadow = shadow
        self.catalog = catalog
        self.tool_executor = ToolExecutor(registry, store, approval_port=approval_port)
        self._tasks: dict[str, asyncio.Task[EmployeeRunResult]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self._accepting = True
        self.recovered_results = store.recover_interrupted_runs() if recover_on_startup else []
        store.subscribe(self._on_event)

    def _on_event(self, event: RunEvent) -> None:
        signal = self._signals.get(event.run_id)
        if signal:
            signal.set()

    def _validate_handle(self, handle: RunHandle) -> dict:
        row = self.store.get_run(handle.run_id)
        if not row or row["request_id"] != handle.request_id:
            raise KeyError(f"Unknown run handle: {handle.run_id}")
        return row

    async def start(self, request: EmployeeRunRequest) -> RunHandle:
        if not self._accepting:
            raise RuntimeError("Runtime service is shutting down")
        validate_request(request)
        workspace_id = request.context.workspace_id
        if workspace_id is None or workspace_id not in self.catalog.workspaces:
            raise ValueError("Shadow coding run requires a registered workspace_id")
        handle, created = self.store.create_run(request)
        if not created:
            return handle
        token = CancellationToken()
        self._tokens[handle.run_id] = token
        self._signals.setdefault(handle.run_id, asyncio.Event())
        task = asyncio.create_task(
            self._run(request, handle, token),
            name=f"shadow-coding-run:{handle.run_id}",
        )
        self._tasks[handle.run_id] = task
        return handle

    async def observe(self, handle: RunHandle, after_seq: int = 0) -> AsyncIterator[RunEvent]:
        self._validate_handle(handle)
        cursor = after_seq
        signal = self._signals.setdefault(handle.run_id, asyncio.Event())
        while True:
            events = self.store.list_events(handle.run_id, cursor)
            for event in events:
                cursor = event.seq
                yield event
            status = self.store.get_status(handle.run_id)
            if status.terminal and cursor >= self.store.get_last_seq(handle.run_id):
                return
            signal.clear()
            if self.store.list_events(handle.run_id, cursor):
                continue
            await signal.wait()

    async def cancel(self, handle: RunHandle, reason: str) -> CancelReceipt:
        self._validate_handle(handle)
        clean_reason = reason.strip() or "Cancelled by caller"
        accepted, status = self.store.request_cancel(handle.run_id, clean_reason)
        token = self._tokens.get(handle.run_id)
        if accepted and token:
            token.cancel(clean_reason)
        return CancelReceipt(handle.run_id, accepted, status, clean_reason)

    async def collect(self, handle: RunHandle) -> EmployeeRunResult:
        self._validate_handle(handle)
        result = self.store.get_result(handle.run_id)
        if result:
            return result
        task = self._tasks.get(handle.run_id)
        if task:
            return await asyncio.shield(task)
        async for _ in self.observe(handle, self.store.get_last_seq(handle.run_id)):
            pass
        result = self.store.get_result(handle.run_id)
        if not result:
            raise RuntimeError(f"Run reached terminal state without a result: {handle.run_id}")
        return result

    async def close(
        self,
        reason: str = "Runtime service shutdown",
        grace_seconds: float = 1.0,
    ) -> None:
        self._accepting = False
        active = [
            RunHandle(run_id, self.store.get_run(run_id)["request_id"])
            for run_id, task in self._tasks.items()
            if not task.done() and self.store.get_run(run_id)
        ]
        for handle in active:
            await self.cancel(handle, reason)
        pending = [task for task in self._tasks.values() if not task.done()]
        if pending:
            _, still_pending = await asyncio.wait(pending, timeout=grace_seconds)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)

    async def _run(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        cancellation: CancellationToken,
    ) -> EmployeeRunResult:
        started_monotonic = time.monotonic()
        try:
            status = self.store.begin_run(handle.run_id)
            if status == RunStatus.CANCELLING or cancellation.cancelled:
                return self._cancelled(request, handle, cancellation.reason)
            if status.terminal:
                existing = self.store.get_result(handle.run_id)
                if existing:
                    return existing
                raise RuntimeError("Terminal run has no result")

            workspace_id = request.context.workspace_id
            assert workspace_id is not None
            dependencies = tuple(item.content for item in request.context.task_dependencies)
            # Only the Kernel-created Work Order projection is passed to an
            # external coding worker.  Other ephemeral instructions can carry
            # runtime-local liveness or orchestration details and must remain
            # inside the parent runtime.  The prefix gives this bridge a
            # stable, auditable contract rather than forwarding an unbounded
            # context bundle to a disposable worker.
            task_context = tuple(
                instruction
                for instruction in request.context.ephemeral_instructions
                if instruction.startswith(_WORK_ORDER_CONTEXT_PREFIX)
            )
            bound_objective = _bound_coding_objective(
                request.task.objective,
                task_context,
            )
            work_request = CodingWorkRequest(
                task_id=request.task.task_id,
                objective=bound_objective,
                acceptance_criteria=request.task.acceptance_criteria,
                dependency_context=dependencies,
                workspace=self.catalog.workspaces[workspace_id],
                model_profile=request.employee.model_profile,
                max_wall_time_ms=request.limits.max_wall_time_ms,
                required_capabilities=request.task.required_capabilities,
                task_context=task_context,
            )
            contract = {
                "runtime": "noruct-shadow-coding-v1",
                "employee": request.employee,
                "task": request.task,
                "context": request.context,
                "rule": "External worker may mutate only a disposable shadow workspace.",
            }
            prompt_hash = _canonical_hash(contract)
            context_hash = _canonical_hash(request.context)
            self.store.record_prompt(handle.run_id, prompt_hash, context_hash)
            self.store.append_message(
                handle.run_id,
                ModelMessage(
                    "system",
                    "Noruct owns policy, state, validation, approval, and real-workspace apply.",
                ),
            )
            self.store.append_message(
                handle.run_id,
                ModelMessage(
                    "user",
                    {
                        "objective": bound_objective,
                        "acceptance_criteria": list(request.task.acceptance_criteria),
                    },
                ),
            )
            usage = Usage()
            validation_sequence = 0

            def record_progress(progress: CodingExecutionProgress) -> None:
                nonlocal usage, validation_sequence
                if progress.kind == CodingExecutionProgressKind.WORKER_STARTED:
                    self.store.append_event(
                        handle.run_id,
                        EventType.MODEL_CALL_STARTED,
                        {
                            "call_index": progress.call_index,
                            "model_profile": request.employee.model_profile,
                            "recovery": progress.call_index > 1,
                        },
                    )
                    return
                if progress.kind == CodingExecutionProgressKind.WORKER_COMPLETED:
                    result = progress.worker_result
                    if result is None:
                        raise RuntimeError("Coding progress omitted the worker result")
                    delta = Usage(
                        model_calls=1,
                        input_tokens=result.usage.input_tokens,
                        cached_input_tokens=result.usage.cached_input_tokens,
                        output_tokens=result.usage.output_tokens,
                        cost_usd=result.usage.cost_usd,
                    )
                    usage = usage.plus(delta)
                    self.store.append_event(
                        handle.run_id,
                        EventType.MODEL_CALL_COMPLETED,
                        {
                            "call_index": progress.call_index,
                            "provider_request_id": result.provider_request_id,
                            "finish_reason": "stop",
                            "response_kind": "shadow_candidate",
                            "recovery": progress.call_index > 1,
                        },
                        usage_delta=delta,
                        new_usage=usage,
                    )
                    return
                if progress.kind == CodingExecutionProgressKind.VALIDATION_RECORDED:
                    attempt = progress.validation_attempt
                    if attempt is None:
                        raise RuntimeError("Coding progress omitted the validation attempt")
                    validation_sequence += 1
                    self.store.append_event(
                        handle.run_id,
                        EventType.VALIDATION_RECORDED,
                        {
                            "attempt": validation_sequence,
                            "worker_call_index": progress.call_index,
                            "name": attempt.name,
                            "passed": attempt.passed,
                            "detail": attempt.detail,
                            "candidate_changed_paths": list(
                                progress.candidate_changed_paths
                            ),
                        },
                    )

            remaining_seconds = max(
                0.001,
                (request.limits.max_wall_time_ms - (time.monotonic() - started_monotonic) * 1000)
                / 1000,
            )
            try:
                outcome = await asyncio.wait_for(
                    self.shadow.execute(
                        source_root=self.catalog.workspaces[workspace_id],
                        workspace_id=workspace_id,
                        request=work_request,
                        worker=self.worker,
                        cancellation=cancellation,
                        validator=self.validator,
                        validation_recovery=(
                            self.validator is not None
                            and self.max_validation_recovery_attempts == 1
                        ),
                        max_worker_calls=min(
                            1 + self.max_validation_recovery_attempts,
                            request.limits.max_model_calls,
                        ),
                        retry_admission_reason=lambda candidate_usage: (
                            self._retry_usage_budget_reason(request, candidate_usage)
                        ),
                        progress=record_progress,
                    ),
                    timeout=remaining_seconds,
                )
            except TimeoutError:
                return self._failed(
                    request,
                    handle,
                    Failure(
                        "CODING_WORKER_TIMEOUT",
                        FailureCategory.TIMEOUT,
                        "The external coding worker exceeded the run time limit.",
                        retryable=True,
                        origin="external-coding-worker",
                    ),
                )

            self.store.append_message(
                handle.run_id,
                ModelMessage(
                    "assistant",
                    {
                        "summary": outcome.worker_result.summary,
                        "change_set_id": (
                            outcome.change_set.change_set_id if outcome.change_set else None
                        ),
                    },
                ),
            )

            attempts = outcome.worker_result.validation_attempts
            if (
                len(attempts) > 8
                or any(
                    not isinstance(attempt, ValidationAttempt)
                    or not isinstance(attempt.name, str)
                    or not attempt.name.strip()
                    or len(attempt.name) > 128
                    or type(attempt.passed) is not bool
                    or not isinstance(attempt.detail, str)
                    or len(attempt.detail) > 1_000
                    for attempt in attempts
                )
            ):
                return self._failed(
                    request,
                    handle,
                    Failure(
                        "CODING_VALIDATION_INVALID",
                        FailureCategory.MODEL,
                        "The external coding worker returned an invalid validation record.",
                        retryable=True,
                        origin="external-coding-worker",
                    ),
                )
            if outcome.recovery_budget_exhausted:
                return self._budget_exhausted(
                    request,
                    handle,
                    outcome.recovery_budget_reason or "validation_recovery_budget",
                )
            if attempts and not attempts[-1].passed:
                return self._failed(
                    request,
                    handle,
                    Failure(
                        "CODING_VALIDATION_FAILED",
                        FailureCategory.MODEL,
                        "The shadow change did not pass its final bounded validation.",
                        retryable=True,
                        origin="shadow-coding-runtime",
                    ),
                )

            result_bytes = len(
                json.dumps(
                    to_primitive(outcome.worker_result),
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            )
            if not outcome.worker_result.summary.strip() or result_bytes > request.limits.max_result_bytes:
                return self._failed(
                    request,
                    handle,
                    Failure(
                        "CODING_RESULT_INVALID",
                        FailureCategory.MODEL,
                        "The external coding worker returned an invalid or oversized result.",
                        retryable=True,
                        origin="external-coding-worker",
                    ),
                )
            budget_reason = self._usage_budget_reason(request, usage)
            if budget_reason:
                return self._budget_exhausted(request, handle, budget_reason)

            if outcome.change_set is None:
                return self._succeeded(request, handle, outcome.worker_result, usage, ())

            self.catalog.add(outcome.change_set)
            call = ToolCall(
                call_id=f"apply-{outcome.change_set.change_set_id}",
                name=APPLY_CHANGE_SET_TOOL,
                arguments={
                    "workspace_id": workspace_id,
                    "change_set_id": outcome.change_set.change_set_id,
                },
            )
            try:
                tool_result = await self.tool_executor.execute(
                    run_id=handle.run_id,
                    model_call_index=1,
                    call=call,
                    policy=request.action_policy,
                    cancellation=cancellation,
                    prior_tool_calls=0,
                    max_result_bytes=request.limits.max_result_bytes,
                    max_tool_output_bytes=request.limits.max_tool_output_bytes,
                    current_usage=usage,
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
                    Failure(
                        "ACTION_INDETERMINATE",
                        FailureCategory.TOOL,
                        str(exc),
                        retryable=False,
                    ),
                )
            if not tool_result.ok:
                category = (
                    FailureCategory.POLICY
                    if tool_result.error_code
                    in {"APPROVAL_DENIED", "APPROVAL_UNAVAILABLE", "POLICY_DENIED"}
                    else FailureCategory.TOOL
                )
                return self._failed(
                    request,
                    handle,
                    Failure(
                        tool_result.error_code or "CHANGE_SET_APPLY_FAILED",
                        category,
                        tool_result.content,
                        retryable=False,
                    ),
                )
            usage = self.store.get_usage(handle.run_id)
            evidence = tuple(f"Applied shadow change: {item.path}" for item in outcome.change_set.files)
            verification_evidence, verification_issues = await self._verify_applied_change(
                request=request,
                handle=handle,
                cancellation=cancellation,
                started_monotonic=started_monotonic,
                change_set_id=outcome.change_set.change_set_id,
                commands=outcome.worker_result.verification_commands,
                prior_tool_calls=usage.tool_calls,
            )
            if verification_issues:
                outcome = replace(
                    outcome,
                    worker_result=replace(
                        outcome.worker_result,
                        unresolved_issues=(
                            *outcome.worker_result.unresolved_issues,
                            *verification_issues,
                        ),
                    ),
                )
            usage = self.store.get_usage(handle.run_id)
            return self._succeeded(
                request,
                handle,
                outcome.worker_result,
                usage,
                evidence + verification_evidence,
                artifact_ref=f"change-set:{outcome.change_set.change_set_id}",
            )
        except OperationCancelled:
            return self._cancelled(request, handle, cancellation.reason)
        except asyncio.CancelledError:
            cancellation.cancel("Runtime task cancelled")
            return self._cancelled(request, handle, cancellation.reason)
        except CodingWorkerError as exc:
            return self._failed(
                request,
                handle,
                Failure(
                    exc.code,
                    FailureCategory.MODEL,
                    exc.message_safe,
                    retryable=exc.retryable,
                    origin="external-coding-worker",
                ),
            )
        except ShadowWorkspaceError as exc:
            return self._failed(
                request,
                handle,
                Failure(
                    exc.code,
                    FailureCategory.POLICY,
                    exc.message_safe,
                    retryable=exc.retryable,
                    origin="shadow-workspace",
                ),
            )
        except Exception as exc:
            return self._failed(
                request,
                handle,
                Failure(
                    "INTERNAL_RUNTIME_ERROR",
                    FailureCategory.INTERNAL,
                    f"Shadow coding runtime failed: {type(exc).__name__}",
                    retryable=True,
                ),
            )

    async def _verify_applied_change(
        self,
        *,
        request: EmployeeRunRequest,
        handle: RunHandle,
        cancellation: CancellationToken,
        started_monotonic: float,
        change_set_id: str,
        commands: tuple[str, ...],
        prior_tool_calls: int,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Replay bounded worker suggestions only through the parent tool boundary.

        A completed apply is durable and must not be reclassified as failed by
        an optional post-apply check.  Each command therefore remains an
        independently approved, auditable action and reports an unresolved
        result rather than undoing the committed change set.
        """
        evidence: list[str] = []
        unresolved: list[str] = []
        for index, command in enumerate(commands[:3], start=1):
            if self.store.get_usage(handle.run_id).tool_calls >= request.limits.max_tool_calls:
                unresolved.append(
                    "Post-apply verification was not run because the tool-call budget was reached."
                )
                break
            call = ToolCall(
                call_id=f"verify-{change_set_id}-{index}",
                name="run_workspace_command",
                arguments={
                    "workspace_id": request.context.workspace_id,
                    "command": command,
                },
            )
            try:
                result = await self.tool_executor.execute(
                    run_id=handle.run_id,
                    model_call_index=1 + index,
                    call=call,
                    policy=request.action_policy,
                    cancellation=cancellation,
                    prior_tool_calls=prior_tool_calls + index - 1,
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
            except (PolicyDenied, ToolExecutionError) as exc:
                unresolved.append(
                    f"Post-apply verification was not run: {str(exc)[:240]}"
                )
                continue
            if result.ok:
                evidence.append(f"Verified applied change with approved command: {command}")
            else:
                unresolved.append(
                    "Post-apply verification did not complete: "
                    f"{result.error_code or result.content}"
                )
        return tuple(evidence), tuple(unresolved)

    def _base_result(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        status: RunStatus,
        usage: Usage,
        *,
        summary: str,
        artifacts: tuple[str, ...] = (),
        evidence: tuple[str, ...] = (),
        unresolved: tuple[str, ...] = (),
        observations: tuple[str, ...] = (),
        followups: tuple[str, ...] = (),
        partial: bool,
        failure: Failure | None = None,
    ) -> EmployeeRunResult:
        row = self.store.get_run(handle.run_id) or {}
        started_at = datetime.fromisoformat(row["started_at"]) if row.get("started_at") else None
        return EmployeeRunResult(
            run_id=handle.run_id,
            request_id=handle.request_id,
            job_id=request.task.job_id,
            task_id=request.task.task_id,
            employee_id=request.employee.employee_id,
            status=status,
            summary=summary,
            output_artifact_refs=artifacts,
            acceptance_evidence=evidence,
            unresolved_issues=unresolved,
            observations=observations,
            suggested_followups=followups,
            signals=(),
            partial_result=partial,
            usage=usage,
            last_event_seq=0,
            started_at=started_at,
            finished_at=utc_now(),
            failure=failure,
        )

    def _succeeded(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        worker_result,
        usage: Usage,
        evidence: tuple[str, ...],
        *,
        artifact_ref: str | None = None,
    ) -> EmployeeRunResult:
        result = self._base_result(
            request,
            handle,
            RunStatus.SUCCEEDED,
            usage,
            summary=worker_result.summary,
            artifacts=(artifact_ref,) if artifact_ref else (),
            evidence=tuple(worker_result.acceptance_evidence) + evidence,
            unresolved=worker_result.unresolved_issues,
            observations=worker_result.observations,
            followups=worker_result.suggested_followups,
            partial=False,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_SUCCEEDED,
            {"summary_bytes": len(worker_result.summary.encode("utf-8"))},
        )

    def _failed(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        failure: Failure,
    ) -> EmployeeRunResult:
        existing = self.store.get_result(handle.run_id)
        if existing:
            return existing
        result = self._base_result(
            request,
            handle,
            RunStatus.FAILED,
            self.store.get_usage(handle.run_id),
            summary=failure.message_safe,
            unresolved=("Kernel decision required.",),
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_FAILED,
            {"failure_code": failure.code, "category": failure.category.value},
        )

    @staticmethod
    def _usage_budget_reason(request: EmployeeRunRequest, usage: Usage) -> str | None:
        if usage.model_calls > request.limits.max_model_calls:
            return "max_model_calls"
        if usage.input_tokens > request.limits.max_input_tokens:
            return "max_input_tokens"
        if usage.output_tokens > request.limits.max_output_tokens:
            return "max_output_tokens"
        if usage.cost_usd > request.limits.max_cost_usd:
            return "max_cost_usd"
        return None

    @staticmethod
    def _retry_usage_budget_reason(
        request: EmployeeRunRequest,
        usage: Usage,
    ) -> str | None:
        if usage.model_calls >= request.limits.max_model_calls:
            return "max_model_calls"
        if usage.input_tokens >= request.limits.max_input_tokens:
            return "max_input_tokens"
        if usage.output_tokens >= request.limits.max_output_tokens:
            return "max_output_tokens"
        if usage.cost_usd >= request.limits.max_cost_usd:
            return "max_cost_usd"
        return None

    def _budget_exhausted(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        reason: str,
    ) -> EmployeeRunResult:
        failure = Failure(
            "RUN_BUDGET_EXHAUSTED",
            FailureCategory.MODEL,
            f"Run limit reached: {reason}",
            retryable=True,
            origin="shadow-coding-runtime",
        )
        result = self._base_result(
            request,
            handle,
            RunStatus.BUDGET_EXHAUSTED,
            self.store.get_usage(handle.run_id),
            summary=failure.message_safe,
            unresolved=("A smaller task or a new budget reservation is required.",),
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_BUDGET_EXHAUSTED,
            {"limit": reason},
        )

    def _cancelled(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        reason: str,
    ) -> EmployeeRunResult:
        existing = self.store.get_result(handle.run_id)
        if existing:
            return existing
        failure = Failure(
            "RUN_CANCELLED",
            FailureCategory.CANCEL,
            reason or "Run cancelled",
            retryable=True,
        )
        result = self._base_result(
            request,
            handle,
            RunStatus.CANCELLED,
            self.store.get_usage(handle.run_id),
            summary=failure.message_safe,
            unresolved=("No unapplied shadow change set was committed.",),
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_CANCELLED,
            {"reason": failure.message_safe},
        )


from .routed_execution import RoutedEmployeeExecutionService
