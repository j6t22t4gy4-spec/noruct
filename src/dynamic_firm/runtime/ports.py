from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Callable, Iterator, Mapping, Protocol, runtime_checkable

from .models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalResolutionReceipt,
    CancelReceipt,
    CompletionEnvelope,
    CompletionValidation,
    EmployeeRunRequest,
    EmployeeRunResult,
    ModelRequest,
    ModelResponse,
    ModelStreamProgress,
    RunEvent,
    RunHandle,
    StructuredOutputRequest,
    StructuredOutputResponse,
    ToolResult,
    Usage,
)


class OperationCancelled(Exception):
    """Cooperative cancellation observed by a provider or tool."""


# A provider composite may report its own bounded child calls while the
# runtime owns durable attribution.  Context-local registration keeps this
# observation per executing task: it is never mutable provider state, route
# selection, or a ledger read API.
_model_fanout_observer: ContextVar[Callable[..., object] | None] = ContextVar(
    "model_fanout_observer", default=None
)


@contextmanager
def observe_model_fanout(observer: Callable[..., object]) -> Iterator[None]:
    token = _model_fanout_observer.set(observer)
    try:
        yield
    finally:
        _model_fanout_observer.reset(token)


def observe_model_fanout_event(*args: Any, **kwargs: Any) -> object | None:
    observer = _model_fanout_observer.get()
    return None if observer is None else observer(*args, **kwargs)


class ModelProviderError(Exception):
    """Safe provider failure that can cross the runtime port."""

    def __init__(
        self,
        code: str,
        message_safe: str,
        *,
        retryable: bool,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(message_safe)
        self.code = code
        self.message_safe = message_safe
        self.retryable = retryable
        # Provider wrappers may have already consumed calls before the safe
        # failure crossed this port. Preserve that aggregate so Company budget
        # accounting cannot erase failed fallback/advisor attempts.
        self.usage = usage or Usage()


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason = ""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str) -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise OperationCancelled(self._reason or "Run cancelled")


class ModelProviderPort(Protocol):
    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse: ...


class CompletionValidatorPort(Protocol):
    def validate(
        self,
        request: EmployeeRunRequest,
        completion: CompletionEnvelope,
    ) -> CompletionValidation: ...


@runtime_checkable
class StreamingModelProviderPort(Protocol):
    async def complete_stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        progress: Callable[[ModelStreamProgress], None],
    ) -> ModelResponse: ...


class ApprovalPort(Protocol):
    async def request(
        self,
        request: ApprovalRequest,
        cancellation: CancellationToken,
    ) -> ApprovalDecision: ...


class StructuredOutputProviderPort(Protocol):
    async def complete_structured(
        self,
        request: StructuredOutputRequest,
        cancellation: CancellationToken,
    ) -> StructuredOutputResponse: ...


class ToolHandler(Protocol):
    async def __call__(
        self,
        arguments: Mapping[str, object],
        cancellation: CancellationToken,
    ) -> str: ...


class EmployeeExecutionPort(Protocol):
    async def start(self, request: EmployeeRunRequest) -> RunHandle: ...

    async def observe(
        self,
        handle: RunHandle,
        after_seq: int = 0,
    ) -> AsyncIterator[RunEvent]: ...

    async def cancel(self, handle: RunHandle, reason: str) -> CancelReceipt: ...

    async def collect(self, handle: RunHandle) -> EmployeeRunResult: ...

    async def list_pending_approvals(
        self,
        run_id: str | None = None,
    ) -> tuple[ApprovalRecord, ...]: ...

    async def resolve_approval(
        self,
        action_id: str,
        decision: ApprovalDecision,
        *,
        decided_by: str = "interactive-user",
    ) -> ApprovalResolutionReceipt: ...


class ToolExecutionPort(Protocol):
    async def execute(self, *args, **kwargs) -> ToolResult: ...
