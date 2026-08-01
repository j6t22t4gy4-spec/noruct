from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.frozen_route_admission import FrozenRouteAdmission
from dynamic_firm.company.frozen_task_route_provider import FrozenTaskRouteProvider
from dynamic_firm.company.route_provider_registry import FrozenRouteProviderRegistry

from .loop import EmployeeAgentLoop
from .models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResolutionReceipt,
    CancelReceipt,
    EmployeeRunRequest,
    EmployeeRunResult,
    RunEvent,
    RunHandle,
    RunStatus,
    validate_request,
)
from .ports import (
    ApprovalPort,
    CancellationToken,
    CompletionValidatorPort,
    ModelProviderPort,
)
from .prompt import PromptBuilder
from .store import RunStore
from .tools import ToolRegistry


class NativeEmployeeRuntimeService:
    """Process-local execution service with SQLite replay as the durable boundary."""

    def __init__(
        self,
        *,
        store: RunStore,
        provider: ModelProviderPort,
        registry: ToolRegistry,
        approval_port: ApprovalPort | None = None,
        prompt_builder: PromptBuilder | None = None,
        completion_validator: CompletionValidatorPort | None = None,
        frozen_route_binding_resolver: Callable[
            [EmployeeRunRequest], ExecutionRouteBinding
        ] | None = None,
        frozen_route_admission_resolver: Callable[
            [EmployeeRunRequest], FrozenRouteAdmission
        ] | None = None,
        frozen_route_registry: FrozenRouteProviderRegistry | None = None,
        recover_on_startup: bool = True,
    ) -> None:
        has_frozen_route_resolver = (
            frozen_route_binding_resolver is not None
            or frozen_route_admission_resolver is not None
        )
        if has_frozen_route_resolver != (frozen_route_registry is not None):
            raise ValueError(
                "frozen route resolver and registry must be supplied together"
            )
        self.store = store
        self._registry = registry
        self._approval_port = approval_port
        self._prompt_builder = prompt_builder
        self._completion_validator = completion_validator
        self.loop = self._new_loop(provider)
        self._frozen_route_binding_resolver = frozen_route_binding_resolver
        self._frozen_route_admission_resolver = frozen_route_admission_resolver
        self._frozen_provider: FrozenTaskRouteProvider | None = None
        self._frozen_loop: EmployeeAgentLoop | None = None
        if frozen_route_registry is not None:
            self._frozen_provider = FrozenTaskRouteProvider(
                store.resolve_frozen_route_binding,
                frozen_route_registry,
            )
            self._frozen_loop = self._new_loop(self._frozen_provider)
        self._tasks: dict[str, asyncio.Task[EmployeeRunResult]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self._accepting = True
        self.recovered_results = (
            store.recover_interrupted_runs(preserve_waiting_approvals=True)
            if recover_on_startup
            else []
        )
        store.subscribe(self._on_event)

    def _new_loop(self, provider: ModelProviderPort) -> EmployeeAgentLoop:
        return EmployeeAgentLoop(
            store=self.store,
            provider=provider,
            registry=self._registry,
            approval_port=self._approval_port,
            prompt_builder=self._prompt_builder,
            completion_validator=self._completion_validator,
        )

    def _frozen_binding_for(self, request: EmployeeRunRequest) -> ExecutionRouteBinding | None:
        if self._frozen_route_binding_resolver is None:
            return None
        binding = self._frozen_route_binding_resolver(request)
        if not isinstance(binding, ExecutionRouteBinding):
            raise TypeError("frozen route resolver must return an ExecutionRouteBinding")
        return binding

    def _frozen_admission_for(self, request: EmployeeRunRequest) -> FrozenRouteAdmission | None:
        if self._frozen_route_admission_resolver is None:
            return None
        admission = self._frozen_route_admission_resolver(request)
        if not isinstance(admission, FrozenRouteAdmission):
            raise TypeError("frozen route admission resolver must return a FrozenRouteAdmission")
        return admission

    def _frozen_route_for(
        self, request: EmployeeRunRequest
    ) -> tuple[ExecutionRouteBinding | None, FrozenRouteAdmission | None]:
        binding = self._frozen_binding_for(request)
        admission = self._frozen_admission_for(request)
        if admission is None:
            return binding, None
        if binding is not None and binding != admission.binding:
            raise ValueError("frozen route admission binding must match frozen route binding")
        return admission.binding, admission

    @property
    def _active_loop(self) -> EmployeeAgentLoop:
        return self._frozen_loop or self.loop

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
        # The resolver supplies an already-selected immutable binding.  It is
        # intentionally called before persistence/dispatch so an idempotent
        # retry that drifts route fails before any provider call.
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
                    self._active_loop.run(request, handle, token, resume=True),
                    name=f"employee-run-resume:{handle.run_id}",
                )
            return handle
        token = CancellationToken()
        self._tokens[handle.run_id] = token
        self._signals.setdefault(handle.run_id, asyncio.Event())
        task = asyncio.create_task(
            self._active_loop.run(request, handle, token),
            name=f"employee-run:{handle.run_id}",
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

    async def close(self, reason: str = "Runtime service shutdown", grace_seconds: float = 1.0) -> None:
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
            done, still_pending = await asyncio.wait(pending, timeout=grace_seconds)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
