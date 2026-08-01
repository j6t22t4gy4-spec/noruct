"""Noruct Employee Runtime backed by a private audited foundation.

The isolated child runs the exact-pinned foundation loop. This parent
is the only model, tool, approval, cancellation, event, and result authority.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.runtime.models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResolutionReceipt,
    CancelReceipt,
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
    RunEvent,
    RunHandle,
    RunStatus,
    SignalCode,
    ToolCall,
    ToolResult,
    Usage,
    to_primitive,
    utc_now,
    validate_request,
)
from dynamic_firm.runtime.context_compaction import BoundedContextCompactor
from dynamic_firm.runtime.cost_efficiency import CostEfficiencyProjector
from dynamic_firm.runtime.ports import (
    ApprovalPort,
    CancellationToken,
    CompletionValidatorPort,
    ModelProviderError,
    ModelProviderPort,
    OperationCancelled,
    StreamingModelProviderPort,
)
from dynamic_firm.runtime.prompt import PromptBuilder
from dynamic_firm.runtime.store import (
    EmployeeSessionConflict,
    EmployeeSessionUpdate,
    RunStore,
    employee_session_namespace,
)
from dynamic_firm.runtime.store_model_invocation_receipt import (
    FrozenDispatcherLeaseConflict,
)
from dynamic_firm.runtime.tools import (
    PolicyDenied,
    ToolExecutionError,
    ToolExecutor,
    ToolRegistry,
    capability_projection,
)
from dynamic_firm.runtime.company_coordination import RemoteCompanyCoordinationClient
from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.frozen_route_admission import FrozenRouteAdmission
from dynamic_firm.company.frozen_task_route_provider import FrozenTaskRouteProvider
from dynamic_firm.company.route_provider_registry import FrozenRouteProviderRegistry

from .source import FoundationSourceError, verify_foundation_source
from .protocol import (
    MAX_FRAME_BYTES,
    FoundationFrame,
    FoundationProtocolError,
    FrameSequence,
    decode_frame,
    encode_frame,
)
from .runtime_support import (
    _default_worker_python,
    _employee_runtime_core_root,
    _model_visible_tool_schemas,
    _package_root,
    _project_worker_code,
    _safe_namespace,
    _scrub_memory_context_response,
)


class NoructEmployeeRuntimeError(RuntimeError):
    """Safe first-party failure around the private execution worker."""


class FoundationDependencyUnavailable(NoructEmployeeRuntimeError):
    """The explicitly selected worker Python cannot import the pinned stack."""


class _UnexpectedProviderFailure(RuntimeError):
    """A provider raised outside the public ModelProviderError contract."""


class _InvalidModelOutput(RuntimeError):
    """The provider returned an invalid or oversized first-party response."""


class _EmptyResponseExhausted(RuntimeError):
    """Foundation recovery or the stricter first-party error bound was exhausted."""


_EMPTY_RESPONSE_ERROR = "provider returned an empty response"
_FOUNDATION_EMPTY_RESPONSE_TERMINAL_COUNT = 4
from .worker_transport import _WorkerProcess  # noqa: E402


class NoructEmployeeRuntimeService:
    """Execute first-party employee requests through the Noruct runtime.

    This is the default employee foundation. It does not install dependencies,
    inherit provider credentials into the worker, or make worker state
    canonical; Noruct's parent contracts remain the authority boundary.
    """

    def __init__(
        self,
        *,
        store: RunStore,
        provider: ModelProviderPort,
        registry: ToolRegistry,
        python_executable: str | os.PathLike[str] | None = None,
        approval_port: ApprovalPort | None = None,
        company_coordination: RemoteCompanyCoordinationClient | None = None,
        prompt_builder: PromptBuilder | None = None,
        completion_validator: CompletionValidatorPort | None = None,
        context_compactor: BoundedContextCompactor | None = None,
        frozen_route_binding_resolver: Callable[
            [EmployeeRunRequest], ExecutionRouteBinding
        ] | None = None,
        frozen_route_admission_resolver: Callable[
            [EmployeeRunRequest], FrozenRouteAdmission
        ] | None = None,
        frozen_route_registry: FrozenRouteProviderRegistry | None = None,
        worker_root: str | os.PathLike[str] | None = None,
        recover_on_startup: bool = True,
    ) -> None:
        try:
            verify_foundation_source()
        except FoundationSourceError as exc:
            raise NoructEmployeeRuntimeError(str(exc)) from exc
        # A foundation route is executable only after its selected-route
        # evidence has been durably admitted.  A binding resolver is optional
        # compatibility cross-check evidence; it must never open a
        # binding-only dispatch mode.
        if frozen_route_binding_resolver is not None and frozen_route_admission_resolver is None:
            raise ValueError(
                "frozen route binding resolver requires a frozen route admission resolver"
            )
        if (frozen_route_admission_resolver is not None) != (
            frozen_route_registry is not None
        ):
            raise ValueError(
                "frozen route admission resolver and registry must be supplied together"
            )
        self.store = store
        self.provider: ModelProviderPort = provider
        self._frozen_route_binding_resolver = frozen_route_binding_resolver
        self._frozen_route_admission_resolver = frozen_route_admission_resolver
        self._frozen_provider: FrozenTaskRouteProvider | None = None
        if frozen_route_registry is not None:
            self._frozen_provider = FrozenTaskRouteProvider(
                store.resolve_frozen_route_binding,
                frozen_route_registry,
                resolve_admission=store.resolve_frozen_route_admission,
            )
            self.provider = self._frozen_provider
        self.registry = registry
        self.python_executable = os.fspath(python_executable or _default_worker_python())
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.completion_validator = completion_validator
        self.context_compactor = context_compactor or BoundedContextCompactor()
        self.cost_efficiency_projector = CostEfficiencyProjector()
        self.tool_executor = ToolExecutor(
            registry,
            store,
            approval_port=approval_port,
            company_coordination=company_coordination,
        )
        self._tasks: dict[str, asyncio.Task[EmployeeRunResult]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self._run_workers: dict[str, _WorkerProcess] = {}
        self._workers: dict[str, _WorkerProcess] = {}
        self._accepting = True
        self._owned_root = None
        if worker_root is None:
            self._owned_root = tempfile.TemporaryDirectory(prefix="noruct-employee-runtime-")
            self.worker_root = Path(self._owned_root.name)
        else:
            self.worker_root = Path(worker_root)
            self.worker_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.recovered_results = (
            store.recover_interrupted_runs(preserve_waiting_approvals=True)
            if recover_on_startup
            else []
        )
        store.subscribe(self._on_event)

    def _on_event(self, event: RunEvent) -> None:
        signal = self._signals.get(event.run_id)
        if signal:
            signal.set()

    def _validate_handle(self, handle: RunHandle) -> dict[str, Any]:
        row = self.store.get_run(handle.run_id)
        if not row or row["request_id"] != handle.request_id:
            raise KeyError(f"Unknown run handle: {handle.run_id}")
        return row

    def _session_key(self, request: EmployeeRunRequest) -> str:
        continuity = request.session_key.strip() or request.request_id
        return employee_session_namespace(request.employee.employee_id, continuity)

    def _worker_for(self, request: EmployeeRunRequest) -> tuple[str, _WorkerProcess]:
        key = self._session_key(request)
        worker = self._workers.get(key)
        if worker is None:
            namespace = _safe_namespace(key)
            worker = _WorkerProcess(
                python_executable=self.python_executable,
                home=self.worker_root / namespace,
            )
            self._workers[key] = worker
        return key, worker

    def _frozen_binding_for(
        self, request: EmployeeRunRequest
    ) -> ExecutionRouteBinding | None:
        if self._frozen_route_binding_resolver is None:
            return None
        binding = self._frozen_route_binding_resolver(request)
        if not isinstance(binding, ExecutionRouteBinding):
            raise TypeError("frozen route resolver must return an ExecutionRouteBinding")
        return binding

    def _frozen_admission_for(
        self, request: EmployeeRunRequest
    ) -> FrozenRouteAdmission | None:
        if self._frozen_route_admission_resolver is None:
            return None
        admission = self._frozen_route_admission_resolver(request)
        if not isinstance(admission, FrozenRouteAdmission):
            raise TypeError(
                "frozen route admission resolver must return a FrozenRouteAdmission"
            )
        return admission

    def _frozen_route_for(
        self, request: EmployeeRunRequest
    ) -> tuple[ExecutionRouteBinding | None, FrozenRouteAdmission | None]:
        binding = self._frozen_binding_for(request)
        admission = self._frozen_admission_for(request)
        if admission is None:
            return binding, None
        if binding is not None and binding != admission.binding:
            raise ValueError(
                "frozen route admission binding must match frozen route binding"
            )
        return admission.binding, admission

    def _execution_model_profile(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
    ) -> str:
        """Return the sole model identity permitted for this physical run.

        Frozen foundation dispatch deliberately reads the verified durable
        admission again at execution time.  The request's employee profile is
        compatibility/default-mode data, never authority for a frozen run.
        """

        if self._frozen_provider is None:
            return request.employee.model_profile
        admission = self.store.get_frozen_route_admission(handle.run_id)
        if admission is None:
            raise ValueError("foundation frozen run has no durable route admission")
        return admission.binding.requested_model_id

    async def start(self, request: EmployeeRunRequest) -> RunHandle:
        if not self._accepting:
            raise RuntimeError("Runtime service is shutting down")
        validate_request(request)
        # Resolve an already-selected immutable route before durable creation
        # and before either the private worker or a provider can be reached.
        binding, admission = self._frozen_route_for(request)
        handle, created = self.store.create_run(
            request,
            frozen_route_binding=binding,
            frozen_route_admission=admission,
        )
        if not created:
            if (
                self.store.get_status(handle.run_id) == RunStatus.WAITING_APPROVAL
                and handle.run_id not in self._tasks
            ):
                token = CancellationToken()
                self._tokens[handle.run_id] = token
                self._signals.setdefault(handle.run_id, asyncio.Event())
                self._tasks[handle.run_id] = asyncio.create_task(
                    self._guarded_run(request, handle, token, resume=True),
                    name=f"noruct-employee-run-resume:{handle.run_id}",
                )
            return handle
        token = CancellationToken()
        self._tokens[handle.run_id] = token
        self._signals.setdefault(handle.run_id, asyncio.Event())
        task = asyncio.create_task(
            self._guarded_run(request, handle, token),
            name=f"noruct-employee-run:{handle.run_id}",
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
        worker = self._run_workers.get(handle.run_id)
        if accepted and worker:
            try:
                await worker.interrupt(handle.run_id, clean_reason)
            except (BrokenPipeError, ConnectionResetError, NoructEmployeeRuntimeError):
                pass
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

    async def list_pending_approvals(
        self,
        run_id: str | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        return tuple(self.store.list_pending_approvals(run_id))

    async def resolve_approval(
        self,
        action_id: str,
        decision: ApprovalDecision,
        *,
        decided_by: str = "interactive-user",
    ) -> ApprovalResolutionReceipt:
        return self.store.resolve_approval(
            action_id,
            decision,
            decided_by=decided_by,
        )

    async def _guarded_run(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        cancellation: CancellationToken,
        *,
        resume: bool = False,
    ) -> EmployeeRunResult:
        lease_conflict = False
        try:
            return await asyncio.wait_for(
                self._execute(request, handle, cancellation, resume=resume),
                timeout=max(0.001, request.limits.max_wall_time_ms / 1000),
            )
        except TimeoutError:
            return self._budget(request, handle, "max_wall_time_ms")
        except OperationCancelled:
            return self._cancelled(request, handle, cancellation.reason)
        except asyncio.CancelledError:
            cancellation.cancel("Runtime task cancelled")
            return self._cancelled(request, handle, cancellation.reason)
        except ModelProviderError as exc:
            return self._failed(
                request,
                handle,
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
        except _UnexpectedProviderFailure:
            return self._failed(
                request,
                handle,
                Failure(
                    "MODEL_PROVIDER_ERROR",
                    FailureCategory.MODEL,
                    "Model provider failed outside its declared error contract.",
                    retryable=True,
                    origin="model-provider",
                ),
            )
        except _InvalidModelOutput as exc:
            return self._failed(
                request,
                handle,
                Failure(
                    "MODEL_OUTPUT_INVALID",
                    FailureCategory.MODEL,
                    str(exc),
                    retryable=True,
                    origin="model-provider",
                ),
            )
        except _EmptyResponseExhausted:
            return self._failed(
                request,
                handle,
                Failure(
                    "MODEL_EMPTY_RESPONSE_EXHAUSTED",
                    FailureCategory.MODEL,
                    "Model returned no usable reply within the recovery limit.",
                    retryable=True,
                    origin="model-provider",
                ),
            )
        except _BudgetReached as exc:
            return self._budget(request, handle, exc.reason)
        except _PolicyRejected as exc:
            return self._failed(
                request,
                handle,
                Failure(
                    "ACTION_POLICY_DENIED",
                    FailureCategory.POLICY,
                    str(exc),
                    retryable=False,
                    origin="noruct-authority",
                ),
            )
        except FrozenDispatcherLeaseConflict:
            # A foreign or missing frozen dispatcher lease is not evidence
            # that this service may terminalize, cancel, or release another
            # dispatcher-owned run.  Preserve the durable lifecycle exactly.
            lease_conflict = True
            raise
        except EmployeeSessionConflict:
            return self._failed(
                request,
                handle,
                Failure(
                    "EMPLOYEE_SESSION_CONFLICT",
                    FailureCategory.INTERNAL,
                    "Employee conversation state changed during this run; retry is safe.",
                    retryable=True,
                    origin="noruct-state",
                ),
            )
        except FoundationDependencyUnavailable:
            return self._failed(
                request,
                handle,
                Failure(
                    "FOUNDATION_DEPENDENCY_UNAVAILABLE",
                    FailureCategory.INTERNAL,
                    "The selected Noruct runtime Python is missing required PyYAML==6.0.3; "
                    "repair the Noruct installation in that environment.",
                    retryable=False,
                    origin="employee-foundation",
                ),
            )
        except (FoundationProtocolError, NoructEmployeeRuntimeError) as exc:
            return self._failed(
                request,
                handle,
                Failure(
                    "FOUNDATION_PROTOCOL_FAILURE",
                    FailureCategory.INTERNAL,
                    f"Employee foundation channel failed: {type(exc).__name__}",
                    retryable=True,
                    origin="employee-foundation",
                ),
            )
        except Exception as exc:
            return self._failed(
                request,
                handle,
                Failure(
                    "FOUNDATION_RUNTIME_FAILURE",
                    FailureCategory.INTERNAL,
                    f"Employee foundation runtime failed: {type(exc).__name__}",
                    retryable=True,
                    origin="employee-foundation",
                ),
            )
        finally:
            worker = self._run_workers.pop(handle.run_id, None)
            try:
                terminal_status = self.store.get_status(handle.run_id)
            except KeyError:
                terminal_status = RunStatus.FAILED
            if worker is not None and terminal_status != RunStatus.SUCCEEDED:
                await worker.close()
                for key, candidate in tuple(self._workers.items()):
                    if candidate is worker:
                        self._workers.pop(key, None)
            dispatch_epoch = getattr(self, "_frozen_dispatch_epoch", None)
            if (
                not lease_conflict
                and isinstance(dispatch_epoch, str)
                and dispatch_epoch
            ):
                # Store-side checks retain the lease when this run is not
                # terminal or any physical call remains indeterminate.  This
                # service cannot infer foreign-process abandonment.
                self.store.release_model_invocation_dispatch_lease(
                    handle.run_id,
                    dispatch_epoch=dispatch_epoch,
                )

    async def _execute(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        cancellation: CancellationToken,
        *,
        resume: bool = False,
    ) -> EmployeeRunResult:
        return await execute_runtime(self, request, handle, cancellation, resume=resume)

    async def _resume_approval_batch(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        cancellation: CancellationToken,
        started: float,
    ) -> None:
        await resume_approval_batch(self, request, handle, cancellation, started)

    def _project_resume_history(self, run_id: str) -> list[dict[str, Any]]:
        return project_resume_history(self, run_id)

    async def _model_call(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        cancellation: CancellationToken,
        payload: Mapping[str, Any],
        tool_schemas: tuple[ToolSchema, ...],
    ) -> ModelResponse:
        return await call_runtime_model(
            self, request, handle, cancellation, payload, tool_schemas
        )

    def _consume_cancelled_provider_request_id(self, run_id: str) -> str | None:
        consumer = getattr(self.provider, "consume_cancelled_request_id", None)
        if not callable(consumer):
            return None
        try:
            value = consumer(run_id)
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _is_empty_response(response: ModelResponse) -> bool:
        return (
            response.completion is None
            and not response.tool_calls
            and not response.content.strip()
        )

    @staticmethod
    def _model_response_error(
        response: ModelResponse,
        max_result_bytes: int,
    ) -> str | None:
        if response.completion and response.tool_calls:
            return "provider returned completion and tool calls together"
        if NoructEmployeeRuntimeService._is_empty_response(response):
            return _EMPTY_RESPONSE_ERROR
        if response.completion:
            if not response.completion.summary.strip():
                return "completion summary must be non-empty"
            encoded = json.dumps(
                to_primitive(response.completion),
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            if len(encoded) > max_result_bytes:
                return "completion exceeds the result byte limit"
        seen_call_ids: set[str] = set()
        for call in response.tool_calls:
            if not call.call_id or not call.name:
                return "tool calls require call_id and name"
            if call.call_id in seen_call_ids:
                return "tool call IDs must be unique"
            seen_call_ids.add(call.call_id)
            if not isinstance(call.arguments, dict):
                return "tool call arguments must be an object"
            if "_provider_arguments_error" in call.arguments:
                return "tool call arguments were rejected"
        return None

    @staticmethod
    def _usage_budget_reason(
        request: EmployeeRunRequest,
        usage: Usage,
        *,
        before_model: bool,
    ) -> str | None:
        limits = request.limits
        if before_model and usage.model_calls >= limits.max_model_calls:
            return "max_model_calls"
        if usage.input_tokens > limits.max_input_tokens or (
            before_model and usage.input_tokens >= limits.max_input_tokens
        ):
            return "max_input_tokens"
        if usage.output_tokens > limits.max_output_tokens or (
            before_model and usage.output_tokens >= limits.max_output_tokens
        ):
            return "max_output_tokens"
        if usage.cost_usd > limits.max_cost_usd or (
            limits.max_cost_usd > 0
            and before_model
            and usage.cost_usd >= limits.max_cost_usd
        ):
            return "max_cost_usd"
        return None

    def _base_result(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        status: RunStatus,
        *,
        summary: str,
        partial: bool,
        failure: Failure | None = None,
        completion: CompletionEnvelope | None = None,
    ) -> EmployeeRunResult:
        row = self.store.get_run(handle.run_id) or {}
        return EmployeeRunResult(
            run_id=handle.run_id,
            request_id=handle.request_id,
            job_id=request.task.job_id,
            task_id=request.task.task_id,
            employee_id=request.employee.employee_id,
            status=status,
            summary=summary,
            output_artifact_refs=completion.artifact_refs if completion else (),
            acceptance_evidence=completion.acceptance_evidence if completion else (),
            unresolved_issues=(
                completion.unresolved_issues
                if completion
                else ("Kernel decision required.",)
            ),
            observations=completion.observations if completion else (),
            suggested_followups=completion.suggested_followups if completion else (),
            signals=completion.signals if completion else (),
            partial_result=partial,
            usage=self.store.get_usage(handle.run_id),
            last_event_seq=0,
            started_at=datetime.fromisoformat(row["started_at"])
            if row.get("started_at")
            else None,
            finished_at=utc_now(),
            failure=failure,
        )

    def _succeeded(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        completion: CompletionEnvelope,
        *,
        employee_session: EmployeeSessionUpdate | None,
    ) -> EmployeeRunResult:
        encoded = json.dumps(
            to_primitive(completion),
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > request.limits.max_result_bytes:
            raise _InvalidModelOutput("completion exceeds the result byte limit")
        result = self._base_result(
            request,
            handle,
            RunStatus.SUCCEEDED,
            summary=completion.summary,
            partial=False,
            completion=completion,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_SUCCEEDED,
            {"summary_bytes": len(completion.summary.encode("utf-8"))},
            employee_session=employee_session,
        )

    def _assignee_mismatch(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        completion: CompletionEnvelope,
    ) -> EmployeeRunResult:
        """Project a typed assignment contradiction to the parent Kernel.

        The private worker may finish its local turn after emitting a structured
        completion.  Assignment selection remains a Firm responsibility, so an
        exact mismatch must be terminally recorded as a safe failed attempt;
        otherwise the Kernel cannot decide whether a bounded reroute to an
        already-frozen employee is permitted.
        """

        failure = Failure(
            "ASSIGNEE_CAPABILITY_MISMATCH",
            FailureCategory.INPUT,
            "The assigned employee returned a typed assignment mismatch.",
            retryable=False,
            origin="noruct-authority",
        )
        result = self._base_result(
            request,
            handle,
            RunStatus.FAILED,
            summary=completion.summary,
            partial=True,
            failure=failure,
            completion=completion,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_FAILED,
            {
                "failure_code": failure.code,
                "category": failure.category.value,
                "signal": "ASSIGNEE_MISMATCH",
            },
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
            summary=failure.message_safe,
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_FAILED,
            {"failure_code": failure.code, "category": failure.category.value},
        )

    def _budget(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        reason: str,
    ) -> EmployeeRunResult:
        failure = Failure(
            "RUN_BUDGET_EXHAUSTED",
            FailureCategory.TIMEOUT
            if reason == "max_wall_time_ms"
            else FailureCategory.MODEL,
            f"Run limit reached: {reason}",
            retryable=True,
            origin="employee-foundation",
        )
        result = self._base_result(
            request,
            handle,
            RunStatus.BUDGET_EXHAUSTED,
            summary=failure.message_safe,
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result, EventType.RUN_BUDGET_EXHAUSTED, {"limit": reason}
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
            origin="employee-foundation",
        )
        result = self._base_result(
            request,
            handle,
            RunStatus.CANCELLED,
            summary=failure.message_safe,
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result, EventType.RUN_CANCELLED, {"reason": failure.message_safe}
        )

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
        await asyncio.gather(
            *(worker.close() for worker in self._workers.values()),
            return_exceptions=True,
        )
        if self._owned_root is not None:
            self._owned_root.cleanup()


class _BudgetReached(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _PolicyRejected(Exception):
    pass


from .runtime_execution import (  # noqa: E402
    call_runtime_model,
    execute_runtime,
)
from .runtime_execution_resume import (  # noqa: E402
    project_resume_history,
    resume_approval_batch,
)
