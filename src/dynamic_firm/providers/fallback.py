"""Bounded provider failover behind the Noruct model-provider contract.

This is a routing wrapper, not a credential pool: every child provider keeps
its own existing credential boundary.  It retries only failures already
classified retryable by the parent-owned provider transports.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

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
)


@dataclass(frozen=True, slots=True)
class FallbackAttempt:
    route: str
    code: str


@dataclass(frozen=True, slots=True)
class FallbackProviderConfig:
    """Private factory input; child configuration stays parent-owned."""

    primary: object
    fallbacks: tuple[tuple[str, object], ...]


class FallbackModelProvider:
    """Try explicitly configured providers in order on retryable failures only."""

    def __init__(self, providers: Sequence[tuple[str, ModelProviderPort]]) -> None:
        if not providers:
            raise ValueError("Fallback provider chain requires a primary provider")
        if len(providers) > 5:
            raise ValueError("Fallback provider chain supports at most five routes")
        if any(not label.strip() for label, _ in providers):
            raise ValueError("Fallback provider route labels must be non-empty")
        self.providers = tuple(providers)
        self.attempts: tuple[FallbackAttempt, ...] = ()
        self.selected_route: str | None = None

    @property
    def model_call_ceiling(self) -> int:
        return sum(
            _provider_call_ceiling(provider)
            for _, provider in self.providers
        )

    @property
    def structured_model_call_ceiling(self) -> int:
        return sum(
            _provider_call_ceiling(provider, structured=True)
            for _, provider in self.providers
        )

    async def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        failures: list[FallbackAttempt] = []
        consumed = Usage()
        for index, (label, provider) in enumerate(self.providers):
            cancellation.raise_if_cancelled()
            try:
                response = await provider.complete(request, cancellation)
                self.attempts = tuple(failures)
                self.selected_route = label
                return replace(
                    response,
                    usage=consumed.plus(_charged_usage(response.usage)),
                )
            except ModelProviderError as exc:
                failures.append(FallbackAttempt(label, exc.code))
                consumed = consumed.plus(_charged_error_usage(exc))
                if not exc.retryable or index == len(self.providers) - 1:
                    self.attempts = tuple(failures)
                    self.selected_route = None
                    raise ModelProviderError(
                        exc.code,
                        exc.message_safe,
                        retryable=exc.retryable,
                        usage=consumed,
                    ) from exc
            except OperationCancelled:
                raise
            except Exception as exc:
                failures.append(FallbackAttempt(label, "MODEL_PROVIDER_UNEXPECTED"))
                consumed = consumed.plus(Usage(model_calls=1))
                self.attempts = tuple(failures)
                self.selected_route = None
                raise ModelProviderError(
                    "MODEL_PROVIDER_UNEXPECTED",
                    "A configured model route failed unexpectedly.",
                    retryable=False,
                    usage=consumed,
                ) from exc
        raise AssertionError("provider fallback chain unexpectedly exhausted")

    async def complete_structured(
        self,
        request: StructuredOutputRequest,
        cancellation: CancellationToken,
    ) -> StructuredOutputResponse:
        """Preserve the bounded failover contract for strict structured output."""

        failures: list[FallbackAttempt] = []
        consumed = Usage()
        for index, (label, provider) in enumerate(self.providers):
            cancellation.raise_if_cancelled()
            complete_structured = getattr(provider, "complete_structured", None)
            if not callable(complete_structured):
                failures.append(FallbackAttempt(label, "MODEL_STRUCTURED_OUTPUT_UNSUPPORTED"))
                if index < len(self.providers) - 1:
                    continue
                self.attempts = tuple(failures)
                self.selected_route = None
                raise ModelProviderError(
                    "MODEL_STRUCTURED_OUTPUT_UNSUPPORTED",
                    "Configured model routes do not support structured output.",
                    retryable=False,
                    usage=consumed,
                )
            try:
                response = await complete_structured(request, cancellation)
                self.attempts = tuple(failures)
                self.selected_route = label
                return replace(
                    response,
                    usage=consumed.plus(_charged_usage(response.usage)),
                )
            except ModelProviderError as exc:
                failures.append(FallbackAttempt(label, exc.code))
                consumed = consumed.plus(_charged_error_usage(exc))
                capability_miss = (
                    exc.code == "MODEL_STRUCTURED_OUTPUT_UNSUPPORTED"
                )
                if (
                    (not exc.retryable and not capability_miss)
                    or index == len(self.providers) - 1
                ):
                    self.attempts = tuple(failures)
                    self.selected_route = None
                    raise ModelProviderError(
                        exc.code,
                        exc.message_safe,
                        retryable=exc.retryable,
                        usage=consumed,
                    ) from exc
            except OperationCancelled:
                raise
            except Exception as exc:
                failures.append(FallbackAttempt(label, "MODEL_PROVIDER_UNEXPECTED"))
                consumed = consumed.plus(Usage(model_calls=1))
                self.attempts = tuple(failures)
                self.selected_route = None
                raise ModelProviderError(
                    "MODEL_PROVIDER_UNEXPECTED",
                    "A configured structured-output route failed unexpectedly.",
                    retryable=False,
                    usage=consumed,
                ) from exc
        raise AssertionError("provider structured fallback chain unexpectedly exhausted")

    async def complete_stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        progress: Callable[[ModelStreamProgress], None],
    ) -> ModelResponse:
        failures: list[FallbackAttempt] = []
        consumed = Usage()
        for index, (label, provider) in enumerate(self.providers):
            cancellation.raise_if_cancelled()
            emitted = False

            def relay(value: ModelStreamProgress) -> None:
                nonlocal emitted
                emitted = True
                progress(value)

            try:
                response = (
                    await provider.complete_stream(request, cancellation, relay)
                    if isinstance(provider, StreamingModelProviderPort)
                    else await provider.complete(request, cancellation)
                )
                self.attempts = tuple(failures)
                self.selected_route = label
                return replace(
                    response,
                    usage=consumed.plus(_charged_usage(response.usage)),
                )
            except ModelProviderError as exc:
                failures.append(FallbackAttempt(label, exc.code))
                consumed = consumed.plus(_charged_error_usage(exc))
                # A second provider could otherwise produce a duplicate turn
                # after partial user-visible output from the first route.
                if emitted or not exc.retryable or index == len(self.providers) - 1:
                    self.attempts = tuple(failures)
                    self.selected_route = None
                    raise ModelProviderError(
                        exc.code,
                        exc.message_safe,
                        retryable=exc.retryable,
                        usage=consumed,
                    ) from exc
            except OperationCancelled:
                raise
            except Exception as exc:
                failures.append(FallbackAttempt(label, "MODEL_PROVIDER_UNEXPECTED"))
                consumed = consumed.plus(Usage(model_calls=1))
                self.attempts = tuple(failures)
                self.selected_route = None
                raise ModelProviderError(
                    "MODEL_PROVIDER_UNEXPECTED",
                    "A configured streaming model route failed unexpectedly.",
                    retryable=False,
                    usage=consumed,
                ) from exc
        raise AssertionError("provider fallback chain unexpectedly exhausted")


def _charged_usage(usage: Usage) -> Usage:
    return replace(usage, model_calls=max(1, usage.model_calls))


def _charged_error_usage(error: ModelProviderError) -> Usage:
    if (
        error.code == "MODEL_STRUCTURED_OUTPUT_UNSUPPORTED"
        and error.usage.model_calls == 0
    ):
        return error.usage
    return _charged_usage(error.usage)


def _provider_call_ceiling(
    provider: ModelProviderPort,
    *,
    structured: bool = False,
) -> int:
    if structured and not callable(getattr(provider, "complete_structured", None)):
        return 0
    attribute = (
        "structured_model_call_ceiling" if structured else "model_call_ceiling"
    )
    value = getattr(provider, attribute, 1)
    minimum = 0 if structured else 1
    if type(value) is not int or value < minimum:
        qualifier = "a non-negative" if structured else "a positive"
        raise ValueError(f"Provider {attribute} must be {qualifier} integer")
    return value
