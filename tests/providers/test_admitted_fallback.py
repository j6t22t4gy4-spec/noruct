from __future__ import annotations

import asyncio
import unittest

from dynamic_firm.company.fallback_admission import FallbackFailureKind
from dynamic_firm.providers.admitted_fallback import (
    AdmittedFallbackModelProvider,
    FallbackAdmissionPolicy,
)
from dynamic_firm.runtime.models import (
    ModelRequest,
    ModelResponse,
    StructuredOutputRequest,
    Usage,
)
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError, OperationCancelled


def _request() -> ModelRequest:
    return ModelRequest(run_id="run", call_index=1, messages=(), tools=(), model_profile="test")


def _policy(*, pairs: frozenset[tuple[str, str]] = frozenset({("primary", "backup")})) -> FallbackAdmissionPolicy:
    return FallbackAdmissionPolicy(
        approved_pairs=pairs,
        failure_kinds={
            "MODEL_TRANSPORT_ERROR": FallbackFailureKind.TRANSPORT,
            "MODEL_RATE_LIMITED": FallbackFailureKind.RATE_LIMIT,
        },
    )


class _Provider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        self.calls += 1
        if self.error:
            raise self.error
        return ModelResponse(content="ok", tool_calls=(), usage=Usage(model_calls=1))


class _StreamingProvider(_Provider):
    def __init__(self, error: Exception | None = None, *, emits_progress: bool = False) -> None:
        super().__init__(error)
        self.emits_progress = emits_progress

    async def complete_stream(self, request, cancellation, progress) -> ModelResponse:
        self.calls += 1
        if self.emits_progress:
            progress(object())
        if self.error:
            raise self.error
        return ModelResponse(content="ok", tool_calls=(), usage=Usage(model_calls=1))


class _StructuredProvider(_Provider):
    async def complete_structured(self, request, cancellation):
        self.calls += 1
        if self.error:
            raise self.error
        raise AssertionError("not used by this focused test")


class AdmittedFallbackModelProviderTests(unittest.TestCase):
    def test_explicit_pair_and_typed_transport_failure_admit_next_fixed_child(self) -> None:
        primary = _Provider(ModelProviderError("MODEL_TRANSPORT_ERROR", "safe", retryable=True, usage=Usage(model_calls=1)))
        backup = _Provider()
        provider = AdmittedFallbackModelProvider((("primary", primary), ("backup", backup)), policy=_policy())

        response = asyncio.run(provider.complete(_request(), CancellationToken()))

        self.assertEqual(response.content, "ok")
        self.assertEqual(response.usage.model_calls, 2)
        self.assertEqual((primary.calls, backup.calls), (1, 1))

    def test_missing_policy_pair_fails_closed_with_preserved_usage(self) -> None:
        primary = _Provider(ModelProviderError("MODEL_TRANSPORT_ERROR", "safe", retryable=True, usage=Usage(model_calls=3)))
        backup = _Provider()
        provider = AdmittedFallbackModelProvider((("primary", primary), ("backup", backup)), policy=_policy(pairs=frozenset()))

        with self.assertRaises(ModelProviderError) as raised:
            asyncio.run(provider.complete(_request(), CancellationToken()))

        self.assertEqual(raised.exception.code, "FALLBACK_NOT_ADMITTED")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.usage.model_calls, 3)
        self.assertEqual((primary.calls, backup.calls), (1, 0))

    def test_unknown_or_nonretryable_failure_never_admits_next_child(self) -> None:
        for error in (
            ModelProviderError("MODEL_SECRET_MISSING", "safe", retryable=True),
            ModelProviderError("MODEL_TRANSPORT_ERROR", "safe", retryable=False),
        ):
            with self.subTest(error=error.code, retryable=error.retryable):
                primary, backup = _Provider(error), _Provider()
                provider = AdmittedFallbackModelProvider((("primary", primary), ("backup", backup)), policy=_policy())
                with self.assertRaises(ModelProviderError) as raised:
                    asyncio.run(provider.complete(_request(), CancellationToken()))
                self.assertEqual(raised.exception.code, "FALLBACK_NOT_ADMITTED")
                self.assertEqual(backup.calls, 0)

    def test_structured_missing_or_unsupported_method_never_switches_route(self) -> None:
        primary, backup = _Provider(), _StructuredProvider()
        provider = AdmittedFallbackModelProvider((("primary", primary), ("backup", backup)), policy=_policy())
        request = StructuredOutputRequest(messages=(), schema_name="test", json_schema={}, model_profile="test", request_id="id")

        with self.assertRaises(ModelProviderError) as raised:
            asyncio.run(provider.complete_structured(request, CancellationToken()))

        self.assertEqual(raised.exception.code, "FALLBACK_NOT_ADMITTED")
        self.assertEqual(backup.calls, 0)

    def test_stream_failure_is_unknown_partial_and_never_switches_even_without_progress(self) -> None:
        primary = _StreamingProvider(ModelProviderError("MODEL_TRANSPORT_ERROR", "safe", retryable=True, usage=Usage(model_calls=1)))
        backup = _Provider()
        provider = AdmittedFallbackModelProvider((("primary", primary), ("backup", backup)), policy=_policy())

        with self.assertRaises(ModelProviderError) as raised:
            asyncio.run(provider.complete_stream(_request(), CancellationToken(), lambda _: None))

        self.assertEqual(raised.exception.code, "FALLBACK_NOT_ADMITTED")
        self.assertEqual((primary.calls, backup.calls), (1, 0))

    def test_stream_progress_never_allows_a_second_child(self) -> None:
        primary = _StreamingProvider(
            ModelProviderError("MODEL_TRANSPORT_ERROR", "safe", retryable=True),
            emits_progress=True,
        )
        backup = _Provider()
        provider = AdmittedFallbackModelProvider((("primary", primary), ("backup", backup)), policy=_policy())

        with self.assertRaises(ModelProviderError) as raised:
            asyncio.run(provider.complete_stream(_request(), CancellationToken(), lambda _: None))

        self.assertEqual(raised.exception.code, "FALLBACK_NOT_ADMITTED")
        self.assertEqual((primary.calls, backup.calls), (1, 0))

    def test_cancellation_propagates_without_fallback(self) -> None:
        primary, backup = _Provider(), _Provider()
        cancellation = CancellationToken()
        cancellation.cancel("stop")
        provider = AdmittedFallbackModelProvider((("primary", primary), ("backup", backup)), policy=_policy())

        with self.assertRaises(OperationCancelled):
            asyncio.run(provider.complete(_request(), cancellation))

        self.assertEqual((primary.calls, backup.calls), (0, 0))
