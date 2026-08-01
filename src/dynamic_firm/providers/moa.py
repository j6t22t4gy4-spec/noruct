"""Bounded Mixture-of-Agents provider wrapper.

Reference models are advisory-only and execute concurrently.  Only the
aggregator receives tools and therefore remains the sole acting model.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from dynamic_firm.company.untrusted_advisory_evidence import (
    AdvisoryAvailability,
    UntrustedAdvisoryEvidence,
)
from dynamic_firm.runtime.models import (
    ModelMessage,
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


_ADVISOR_PROMPT = "You are an advisory reference model. Analyze the task and give concise private guidance. Do not call tools or claim to execute actions."


@dataclass(frozen=True, slots=True)
class MoAProviderConfig:
    aggregator: object
    references: tuple[tuple[str, object], ...]


class MixtureOfAgentsProvider:
    """Run up to eight advisory models then let one aggregator act."""

    def __init__(self, aggregator: ModelProviderPort, references: Sequence[tuple[str, ModelProviderPort]]) -> None:
        if not 1 <= len(references) <= 8:
            raise ValueError("Mixture of Agents requires one through eight reference providers")
        if any(
            not isinstance(label, str) or not label or any(char.isspace() for char in label)
            for label, _ in references
        ):
            raise ValueError("Mixture of Agents reference labels must be opaque tokens")
        self.aggregator = aggregator
        self.references = tuple(references)

    @property
    def model_call_ceiling(self) -> int:
        return _provider_call_ceiling(self.aggregator) + sum(
            _provider_call_ceiling(provider)
            for _, provider in self.references
        )

    @property
    def structured_model_call_ceiling(self) -> int:
        if not callable(getattr(self.aggregator, "complete_structured", None)):
            return 0
        return _provider_call_ceiling(
            self.aggregator,
            structured=True,
        ) + sum(_provider_call_ceiling(provider) for _, provider in self.references)

    async def _advise(
        self,
        label: str,
        provider: ModelProviderPort,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> tuple[UntrustedAdvisoryEvidence, Usage]:
        advisory = replace(request, messages=(ModelMessage("system", _ADVISOR_PROMPT), *request.messages), tools=())
        started = time.monotonic()
        child_invocation_id = observe_model_fanout_event(
            "START", label, advisory, started=started
        )
        try:
            response = await provider.complete(advisory, cancellation)
            observe_model_fanout_event(
                "TERMINAL",
                label,
                advisory,
                invocation_id=child_invocation_id,
                started=started,
                terminal_status="SUCCEEDED",
                response=response,
            )
            try:
                evidence = UntrustedAdvisoryEvidence(
                    label,
                    AdvisoryAvailability.AVAILABLE,
                    response.content.strip() or "(no advice)",
                )
            except ValueError:
                # A reference response that cannot fit the bounded untrusted
                # envelope is unavailable to the aggregator, never a reason
                # to widen a prompt or make the reference authoritative.
                evidence = UntrustedAdvisoryEvidence(
                    label, AdvisoryAvailability.UNAVAILABLE, None
                )
            return evidence, _charged_usage(response.usage)
        except (asyncio.CancelledError, OperationCancelled):
            observe_model_fanout_event(
                "TERMINAL",
                label,
                advisory,
                invocation_id=child_invocation_id,
                started=started,
                terminal_status="INDETERMINATE",
                safe_error_code="RUN_CANCELLED",
            )
            raise
        except ModelProviderError as exc:
            observe_model_fanout_event(
                "TERMINAL",
                label,
                advisory,
                invocation_id=child_invocation_id,
                started=started,
                terminal_status="FAILED",
                safe_error_code=exc.code,
            )
            return (
                UntrustedAdvisoryEvidence(label, AdvisoryAvailability.UNAVAILABLE, None),
                _charged_usage(exc.usage),
            )
        except Exception:
            observe_model_fanout_event(
                "TERMINAL",
                label,
                advisory,
                invocation_id=child_invocation_id,
                started=started,
                terminal_status="INDETERMINATE",
                safe_error_code="PROVIDER_INDETERMINATE",
            )
            return (
                UntrustedAdvisoryEvidence(label, AdvisoryAvailability.UNAVAILABLE, None),
                Usage(model_calls=1),
            )

    async def _prepared(self, request: ModelRequest, cancellation: CancellationToken) -> tuple[ModelRequest, Usage]:
        results = await asyncio.gather(*(self._advise(label, provider, request, cancellation) for label, provider in self.references))
        usage = Usage()
        advisory_messages: list[ModelMessage] = []
        for evidence, attempt_usage in results:
            usage = usage.plus(attempt_usage)
            message = evidence.aggregator_message()
            advisory_messages.append(ModelMessage(message["role"], message["content"]))
        # Each reference is a bounded, source-labelled user-content envelope.
        # No mutable provider outcome crosses this call boundary into a ledger
        # or into a system-role instruction for the acting aggregator.
        return replace(request, messages=(*request.messages, *advisory_messages)), usage

    @staticmethod
    def _with_usage(response: ModelResponse, advisory: Usage) -> ModelResponse:
        return replace(response, usage=response.usage.plus(advisory))

    async def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        prepared, advisory = await self._prepared(request, cancellation)
        try:
            response = await self.aggregator.complete(prepared, cancellation)
        except ModelProviderError as exc:
            raise _error_with_advisory(exc, advisory) from exc
        except (asyncio.CancelledError, OperationCancelled):
            raise
        except Exception as exc:
            raise _unexpected_aggregator_error(advisory) from exc
        return self._with_usage(
            replace(response, usage=_charged_usage(response.usage)),
            advisory,
        )

    async def complete_stream(self, request: ModelRequest, cancellation: CancellationToken, progress: Callable[[ModelStreamProgress], None]) -> ModelResponse:
        prepared, advisory = await self._prepared(request, cancellation)
        if isinstance(self.aggregator, StreamingModelProviderPort):
            try:
                response = await self.aggregator.complete_stream(prepared, cancellation, progress)
            except ModelProviderError as exc:
                raise _error_with_advisory(exc, advisory) from exc
            except (asyncio.CancelledError, OperationCancelled):
                raise
            except Exception as exc:
                raise _unexpected_aggregator_error(advisory) from exc
        else:
            try:
                response = await self.aggregator.complete(prepared, cancellation)
            except ModelProviderError as exc:
                raise _error_with_advisory(exc, advisory) from exc
            except (asyncio.CancelledError, OperationCancelled):
                raise
            except Exception as exc:
                raise _unexpected_aggregator_error(advisory) from exc
        return self._with_usage(
            replace(response, usage=_charged_usage(response.usage)),
            advisory,
        )

    async def complete_structured(
        self,
        request: StructuredOutputRequest,
        cancellation: CancellationToken,
    ) -> StructuredOutputResponse:
        """Let references advise while the sole aggregator owns the JSON contract."""

        cancellation.raise_if_cancelled()
        complete_structured = getattr(self.aggregator, "complete_structured", None)
        if not callable(complete_structured):
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_UNSUPPORTED",
                "The configured aggregation route does not support structured output.",
                retryable=False,
                usage=Usage(),
            )
        advisory_request = ModelRequest(
            messages=request.messages,
            tools=(),
            model_profile=request.model_profile,
            run_id=request.request_id,
            call_index=request.call_index,
        )
        prepared, advisory = await self._prepared(advisory_request, cancellation)
        structured_request = replace(request, messages=prepared.messages)
        try:
            response = await complete_structured(structured_request, cancellation)
        except ModelProviderError as exc:
            raise _error_with_advisory(exc, advisory) from exc
        except (asyncio.CancelledError, OperationCancelled):
            raise
        except Exception as exc:
            raise _unexpected_aggregator_error(advisory) from exc
        return replace(
            response,
            usage=_charged_usage(response.usage).plus(advisory),
        )


def _charged_usage(usage: Usage) -> Usage:
    return replace(usage, model_calls=max(1, usage.model_calls))


def _error_with_advisory(
    error: ModelProviderError,
    advisory: Usage,
) -> ModelProviderError:
    return ModelProviderError(
        error.code,
        error.message_safe,
        retryable=error.retryable,
        usage=advisory.plus(_charged_error_usage(error)),
    )


def _charged_error_usage(error: ModelProviderError) -> Usage:
    if (
        error.code == "MODEL_STRUCTURED_OUTPUT_UNSUPPORTED"
        and error.usage.model_calls == 0
    ):
        return error.usage
    return _charged_usage(error.usage)


def _unexpected_aggregator_error(advisory: Usage) -> ModelProviderError:
    return ModelProviderError(
        "MODEL_PROVIDER_UNEXPECTED",
        "The configured aggregation route failed unexpectedly.",
        retryable=False,
        usage=advisory.plus(Usage(model_calls=1)),
    )


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
