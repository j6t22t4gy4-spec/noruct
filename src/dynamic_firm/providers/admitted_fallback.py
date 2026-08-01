"""Fail-closed, policy-admitted model fallback.

This adapter deliberately does not replace the legacy fallback wrapper.  Its
configuration fixes the provider order; the policy can only pre-approve an
already-adjacent label pair and classify a safe provider error code.  It never
selects a provider, reads credentials, or changes the configured order.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from dynamic_firm.company.fallback_admission import (
    FallbackAttemptState,
    FallbackDecision,
    FallbackFailureKind,
    admit_fallback,
)
from dynamic_firm.runtime.models import (
    ModelRequest,
    ModelResponse,
    ModelStreamProgress,
    StructuredOutputRequest,
    StructuredOutputResponse,
    Usage,
)
from dynamic_firm.runtime.ports import (
    CancellationToken,
    ModelProviderError,
    ModelProviderPort,
    OperationCancelled,
    StreamingModelProviderPort,
    observe_model_fanout_event,
)


@dataclass(frozen=True, slots=True)
class FallbackAdmissionPolicy:
    """Data-only admission input for an already configured ordered chain."""

    approved_pairs: frozenset[tuple[str, str]]
    failure_kinds: Mapping[str, FallbackFailureKind]

    def admits_pair(self, source: str, destination: str) -> bool:
        return (source, destination) in self.approved_pairs

    def failure_kind_for(self, error: ModelProviderError) -> FallbackFailureKind:
        """Return the explicitly registered kind, otherwise fail closed."""

        return self.failure_kinds.get(error.code, FallbackFailureKind.OTHER)


class AdmittedFallbackModelProvider:
    """Try the next fixed provider only after an explicit admission decision."""

    def __init__(
        self,
        providers: Sequence[tuple[str, ModelProviderPort]],
        *,
        policy: FallbackAdmissionPolicy,
    ) -> None:
        if not providers:
            raise ValueError("Fallback provider chain requires a primary provider")
        if len(providers) > 5:
            raise ValueError("Fallback provider chain supports at most five routes")
        labels = tuple(label for label, _ in providers)
        if any(not label.strip() for label in labels) or len(set(labels)) != len(labels):
            raise ValueError("Fallback provider route labels must be non-empty and unique")
        if not isinstance(policy, FallbackAdmissionPolicy):
            raise TypeError("an explicit FallbackAdmissionPolicy is required")
        self.providers = tuple(providers)
        self.policy = policy

    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        return await self._complete(
            request,
            cancellation,
            invoke=lambda provider: provider.complete(request, cancellation),
        )

    async def complete_structured(
        self,
        request: StructuredOutputRequest,
        cancellation: CancellationToken,
    ) -> StructuredOutputResponse:
        """Missing/unsupported structured capability never switches routes."""

        consumed = Usage()
        for index, (_, provider) in enumerate(self.providers):
            cancellation.raise_if_cancelled()
            invoke = getattr(provider, "complete_structured", None)
            if not callable(invoke):
                raise _not_admitted(consumed)
            try:
                response = await invoke(request, cancellation)
                return replace(response, usage=consumed.plus(response.usage))
            except OperationCancelled:
                raise
            except ModelProviderError as error:
                consumed = consumed.plus(error.usage)
                if not self._may_continue(index, error, partial_stream=False):
                    raise _not_admitted(consumed) from error
        raise AssertionError("provider fallback chain unexpectedly exhausted")

    async def complete_stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        progress: Callable[[ModelStreamProgress], None],
    ) -> ModelResponse:
        consumed = Usage()
        for index, (_, provider) in enumerate(self.providers):
            cancellation.raise_if_cancelled()
            label, _ = self.providers[index]
            started = time.monotonic()
            child_invocation_id = observe_model_fanout_event(
                "START", label, request, started=started
            )
            if isinstance(provider, StreamingModelProviderPort):
                # A streaming transport cannot prove that an error occurred
                # before all user-visible output.  Treat even a silent failure
                # as unknown partial output and refuse a duplicate retry.
                try:
                    response = await provider.complete_stream(request, cancellation, progress)
                    observe_model_fanout_event(
                        "TERMINAL", label, request,
                        invocation_id=child_invocation_id, started=started,
                        terminal_status="SUCCEEDED", response=response,
                    )
                    return replace(response, usage=consumed.plus(response.usage))
                except (asyncio.CancelledError, OperationCancelled):
                    observe_model_fanout_event(
                        "TERMINAL", label, request,
                        invocation_id=child_invocation_id, started=started,
                        terminal_status="INDETERMINATE", safe_error_code="RUN_CANCELLED",
                    )
                    raise
                except ModelProviderError as error:
                    observe_model_fanout_event(
                        "TERMINAL", label, request,
                        invocation_id=child_invocation_id, started=started,
                        terminal_status="FAILED", safe_error_code=error.code,
                    )
                    consumed = consumed.plus(error.usage)
                    raise _not_admitted(consumed) from error
            try:
                response = await provider.complete(request, cancellation)
                observe_model_fanout_event(
                    "TERMINAL", label, request,
                    invocation_id=child_invocation_id, started=started,
                    terminal_status="SUCCEEDED", response=response,
                )
                return replace(response, usage=consumed.plus(response.usage))
            except (asyncio.CancelledError, OperationCancelled):
                observe_model_fanout_event(
                    "TERMINAL", label, request,
                    invocation_id=child_invocation_id, started=started,
                    terminal_status="INDETERMINATE", safe_error_code="RUN_CANCELLED",
                )
                raise
            except ModelProviderError as error:
                observe_model_fanout_event(
                    "TERMINAL", label, request,
                    invocation_id=child_invocation_id, started=started,
                    terminal_status="FAILED", safe_error_code=error.code,
                )
                consumed = consumed.plus(error.usage)
                if not self._may_continue(index, error, partial_stream=False):
                    raise _not_admitted(consumed) from error
        raise AssertionError("provider fallback chain unexpectedly exhausted")

    async def _complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        *,
        invoke: Callable[[ModelProviderPort], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        consumed = Usage()
        for index, (_, provider) in enumerate(self.providers):
            cancellation.raise_if_cancelled()
            label, _ = self.providers[index]
            started = time.monotonic()
            child_invocation_id = observe_model_fanout_event(
                "START", label, request, started=started
            )
            try:
                result = await invoke(provider)
                observe_model_fanout_event(
                    "TERMINAL", label, request,
                    invocation_id=child_invocation_id, started=started,
                    terminal_status="SUCCEEDED", response=result,
                )
                return replace(result, usage=consumed.plus(result.usage))
            except (asyncio.CancelledError, OperationCancelled):
                observe_model_fanout_event(
                    "TERMINAL", label, request,
                    invocation_id=child_invocation_id, started=started,
                    terminal_status="INDETERMINATE", safe_error_code="RUN_CANCELLED",
                )
                raise
            except ModelProviderError as error:
                observe_model_fanout_event(
                    "TERMINAL", label, request,
                    invocation_id=child_invocation_id, started=started,
                    terminal_status="FAILED", safe_error_code=error.code,
                )
                consumed = consumed.plus(error.usage)
                if not self._may_continue(index, error, partial_stream=False):
                    raise _not_admitted(consumed) from error
        raise AssertionError("provider fallback chain unexpectedly exhausted")

    def _may_continue(
        self,
        index: int,
        error: ModelProviderError,
        *,
        partial_stream: bool,
    ) -> bool:
        if index >= len(self.providers) - 1:
            return False
        source, _ = self.providers[index]
        destination, _ = self.providers[index + 1]
        state = FallbackAttemptState(
            equivalence_group_preapproved=self.policy.admits_pair(source, destination),
            retryable=error.retryable,
            partial_stream=partial_stream,
            # This ModelProviderPort boundary has no tool/effect continuation
            # surface.  Streaming failure is separately treated as unknown.
            effect_started=False,
            failure_kind=self.policy.failure_kind_for(error),
        )
        return admit_fallback(state) is FallbackDecision.ALLOWED


def _not_admitted(usage: Usage) -> ModelProviderError:
    return ModelProviderError(
        "FALLBACK_NOT_ADMITTED",
        "A configured fallback route is not admitted for this failure.",
        retryable=False,
        usage=usage,
    )
