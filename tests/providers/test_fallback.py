from __future__ import annotations

import asyncio
import unittest

from dynamic_firm.providers.fallback import FallbackModelProvider
from dynamic_firm.providers.fallback import FallbackProviderConfig
from dynamic_firm.cli import RunCommandConfig, _provider_config
from dynamic_firm.runtime.models import RunLimits
from dynamic_firm.compiler import CompilerRequest, DynamicWorkflowCompiler, PlanningMode
from dynamic_firm.runtime.models import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StructuredOutputRequest,
    StructuredOutputResponse,
    Usage,
)
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError
from tests.compiler.test_parser import plan, task


class _Provider:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error; self.calls = 0

    async def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        self.calls += 1
        if self.error: raise self.error
        return ModelResponse(content="fallback result", tool_calls=(), usage=Usage())


class _StreamingProvider(_Provider):
    async def complete_stream(self, request: ModelRequest, cancellation: CancellationToken, progress) -> ModelResponse:
        self.calls += 1
        progress(type("Progress", (), {"text_delta": "partial"})())
        raise self.error or ModelProviderError("MODEL_TRANSPORT_ERROR", "failed", retryable=True)


class _StructuredProvider(_Provider):
    def __init__(self, *, response=None, error: ModelProviderError | None = None) -> None:
        super().__init__()
        self.structured_response = response
        self.structured_error = error
        self.structured_calls = 0

    async def complete_structured(self, request, cancellation):
        self.structured_calls += 1
        if self.structured_error:
            raise self.structured_error
        return self.structured_response


def _request() -> ModelRequest:
    return ModelRequest(run_id="run", call_index=1, messages=(), tools=(), model_profile="test")


def _structured_request() -> StructuredOutputRequest:
    return StructuredOutputRequest(
        messages=(ModelMessage("user", "plan"),),
        schema_name="plan",
        json_schema={"type": "object"},
        model_profile="test",
        request_id="request",
    )


def _plan_value() -> dict:
    return plan(
        "GRAPH",
        [
            task("research"),
            task("finalize", depends_on=("research",), capability="evidence_synthesis"),
        ],
        "finalize",
    )


class FallbackModelProviderTests(unittest.TestCase):
    def test_retryable_failure_moves_to_next_explicit_route(self) -> None:
        primary = _Provider(error=ModelProviderError("MODEL_TRANSPORT_ERROR", "network", retryable=True)); backup = _Provider()
        provider = FallbackModelProvider((("primary", primary), ("backup", backup)))
        response = asyncio.run(provider.complete(_request(), CancellationToken()))
        self.assertEqual(response.content, "fallback result")
        self.assertEqual(response.usage.model_calls, 2)
        self.assertEqual((primary.calls, backup.calls), (1, 1))
        self.assertEqual(provider.attempts[0].route, "primary")
        self.assertEqual(provider.selected_route, "backup")

    def test_nonretryable_failure_never_uses_backup(self) -> None:
        primary = _Provider(error=ModelProviderError("MODEL_SECRET_MISSING", "missing", retryable=False)); backup = _Provider()
        provider = FallbackModelProvider((("primary", primary), ("backup", backup)))
        with self.assertRaises(ModelProviderError) as raised:
            asyncio.run(provider.complete(_request(), CancellationToken()))
        self.assertEqual(raised.exception.usage.model_calls, 1)
        self.assertEqual((primary.calls, backup.calls), (1, 0))
        self.assertIsNone(provider.selected_route)

    def test_streamed_partial_output_never_falls_back(self) -> None:
        primary = _StreamingProvider(error=ModelProviderError("MODEL_TRANSPORT_ERROR", "network", retryable=True)); backup = _Provider()
        provider = FallbackModelProvider((("primary", primary), ("backup", backup)))
        with self.assertRaises(ModelProviderError) as raised:
            asyncio.run(provider.complete_stream(_request(), CancellationToken(), lambda _: None))
        self.assertEqual(raised.exception.usage.model_calls, 1)
        self.assertEqual((primary.calls, backup.calls), (1, 0))

    def test_structured_output_skips_unsupported_route_and_selects_backup(self) -> None:
        primary = _Provider()
        backup = _StructuredProvider(response=StructuredOutputResponse(value=_plan_value()))
        provider = FallbackModelProvider((("primary", primary), ("backup", backup)))

        response = asyncio.run(provider.complete_structured(_structured_request(), CancellationToken()))

        self.assertEqual(response.value["final_task_id"], "finalize")
        self.assertEqual(response.usage.model_calls, 1)
        self.assertEqual(backup.structured_calls, 1)
        self.assertEqual(provider.attempts[0].code, "MODEL_STRUCTURED_OUTPUT_UNSUPPORTED")
        self.assertEqual(provider.selected_route, "backup")
        self.assertEqual(provider.structured_model_call_ceiling, 1)

    def test_dynamic_compiler_uses_structured_fallback_route(self) -> None:
        primary = _StructuredProvider(
            error=ModelProviderError("MODEL_TRANSPORT_ERROR", "network", retryable=True)
        )
        backup = _StructuredProvider(
            response=StructuredOutputResponse(value=_plan_value(), usage=Usage(model_calls=1))
        )
        compiler = DynamicWorkflowCompiler(
            FallbackModelProvider((("primary", primary), ("backup", backup)))
        )

        decision = asyncio.run(
            compiler.compile(
                CompilerRequest(
                    request_id="compiler",
                    goal="Research and independently synthesize the repository findings",
                    workspace_manifest=("README.md",),
                    available_capabilities=("repository_analysis", "evidence_synthesis"),
                    model_profile="test",
                )
            )
        )

        self.assertEqual(decision.mode, PlanningMode.DYNAMIC)
        self.assertEqual(
            tuple(task.task_id for task in decision.proposal.tasks),
            ("research", "finalize"),
        )
        self.assertEqual(decision.usage.model_calls, 2)
        self.assertEqual((primary.structured_calls, backup.structured_calls), (1, 1))

    def test_call_ceilings_compose_child_fan_out_and_skip_unsupported_structured_routes(self) -> None:
        plain = _Provider()
        plain.model_call_ceiling = 2
        structured = _StructuredProvider(
            response=StructuredOutputResponse(value=_plan_value())
        )
        structured.model_call_ceiling = 3
        structured.structured_model_call_ceiling = 4

        provider = FallbackModelProvider(
            (("plain", plain), ("structured", structured))
        )

        self.assertEqual(provider.model_call_ceiling, 5)
        self.assertEqual(provider.structured_model_call_ceiling, 4)

    def test_nested_unsupported_structured_route_has_zero_fan_out(self) -> None:
        unsupported = FallbackModelProvider((("plain", _Provider()),))
        backup = _StructuredProvider(
            response=StructuredOutputResponse(value=_plan_value())
        )
        provider = FallbackModelProvider(
            (("unsupported", unsupported), ("backup", backup))
        )

        response = asyncio.run(
            provider.complete_structured(_structured_request(), CancellationToken())
        )

        self.assertEqual(provider.structured_model_call_ceiling, 1)
        self.assertEqual(response.usage.model_calls, 1)

    def test_unexpected_provider_failure_is_safe_and_charged(self) -> None:
        provider = FallbackModelProvider(
            (("primary", _Provider(error=RuntimeError("private details"))),)
        )

        with self.assertRaises(ModelProviderError) as raised:
            asyncio.run(provider.complete(_request(), CancellationToken()))

        self.assertEqual(raised.exception.code, "MODEL_PROVIDER_UNEXPECTED")
        self.assertEqual(raised.exception.usage.model_calls, 1)
        self.assertNotIn("private details", raised.exception.message_safe)

    def test_cli_provider_config_wraps_explicit_fallback_route(self) -> None:
        config = RunCommandConfig(
            goal="test", workspace=__import__("pathlib").Path.cwd(), state_path=__import__("pathlib").Path("/tmp/noruct-test.db"),
            provider_kind="openai_api", base_url="http://127.0.0.1:8080/v1", model="primary", codex_model=None,
            codex_command="codex", api_key_env=None, request_timeout_seconds=5, permission_mode="read-only", run_limits=RunLimits(),
            fallback_routes=({"kind": "ollama", "model": "backup"},),
        )
        result = _provider_config(config)
        self.assertIsInstance(result, FallbackProviderConfig)
        self.assertEqual(result.fallbacks[0][0], "ollama:backup")
