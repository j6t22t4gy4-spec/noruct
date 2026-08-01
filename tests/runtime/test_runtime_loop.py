from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import replace

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
    CompletionValidation,
    CostEfficiencyMode,
    EventType,
    ModelResponse,
    ModelStreamProgress,
    RunLimits,
    RunStatus,
    RunSignal,
    SignalCode,
    ToolCall,
    ToolEffect,
    ToolRisk,
    IdempotencyMode,
    Usage,
)
from dynamic_firm.runtime.context_compaction import ContextCompactionResult
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.ports import ModelProviderError
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import FixtureReader, ToolDefinition, ToolRegistry
from tests.runtime.helpers import completion, make_request


class RuntimeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_employee_session_contention_fails_before_second_model_call(self) -> None:
        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(completion=completion("first terminal")),
                ModelResponse(completion=completion("third terminal")),
            ],
            blocked_calls=(0,),
        )
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
        )
        first = replace(
            make_request(request_id="session-owner"),
            session_key="shared-session",
        )
        second = replace(
            make_request(request_id="session-contender"),
            task=replace(make_request().task, job_id="job-2", task_id="task-2"),
            session_key="shared-session",
        )

        first_handle = await service.start(first)
        await provider.wait_until_started(0)
        second_result = await service.collect(await service.start(second))

        self.assertEqual(second_result.status, RunStatus.FAILED)
        self.assertIsNotNone(second_result.failure)
        assert second_result.failure is not None
        self.assertEqual(second_result.failure.code, "EMPLOYEE_SESSION_BUSY")
        self.assertEqual(provider.call_count, 1)

        provider.release(0)
        first_result = await service.collect(first_handle)
        third = replace(
            make_request(request_id="session-after-release"),
            task=replace(make_request().task, job_id="job-3", task_id="task-3"),
            session_key="shared-session",
        )
        third_result = await service.collect(await service.start(third))

        self.assertEqual(first_result.status, RunStatus.SUCCEEDED)
        self.assertEqual(third_result.status, RunStatus.SUCCEEDED)
        self.assertEqual(provider.call_count, 2)
        await service.close()
        store.close()
    async def test_composite_provider_physical_calls_are_preserved(self) -> None:
        class CompositeProvider:
            model_call_ceiling = 2

            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, request, cancellation):  # type: ignore[no-untyped-def]
                self.calls += 1
                return ModelResponse(
                    completion=completion("composite complete"),
                    usage=Usage(model_calls=2, cost_usd=0.2),
                )

        store = RunStore()
        provider = CompositeProvider()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
        )

        result = await service.collect(
            await service.start(make_request(limits=RunLimits(max_model_calls=3)))
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertAlmostEqual(result.usage.cost_usd, 0.2)
        self.assertEqual(provider.calls, 1)
        store.close()

    async def test_composite_provider_ceiling_is_admitted_before_employee_call(self) -> None:
        class CompositeProvider:
            model_call_ceiling = 2

            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, request, cancellation):  # type: ignore[no-untyped-def]
                self.calls += 1
                return ModelResponse(completion=completion("must not execute"))

        store = RunStore()
        provider = CompositeProvider()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
        )

        result = await service.collect(
            await service.start(make_request(limits=RunLimits(max_model_calls=1)))
        )

        self.assertEqual(result.status, RunStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.usage.model_calls, 0)
        self.assertEqual(provider.calls, 0)
        store.close()

    async def test_provider_failure_keeps_composite_usage(self) -> None:
        class FailingCompositeProvider:
            model_call_ceiling = 2

            async def complete(self, request, cancellation):  # type: ignore[no-untyped-def]
                raise ModelProviderError(
                    "MODEL_TRANSPORT_ERROR",
                    "provider failed",
                    retryable=True,
                    usage=Usage(model_calls=2, cost_usd=0.3),
                )

        store = RunStore()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=FailingCompositeProvider(),
            registry=ToolRegistry(),
        )

        result = await service.collect(
            await service.start(make_request(limits=RunLimits(max_model_calls=3)))
        )

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertAlmostEqual(result.usage.cost_usd, 0.3)
        store.close()

    async def test_completion_validation_failure_repairs_once_in_same_run(self) -> None:
        class ExactValidator:
            def __init__(self) -> None:
                self.summaries: list[str] = []

            def validate(self, request, candidate):
                self.summaries.append(candidate.summary)
                if candidate.summary == "public_evidence=rollback-ready":
                    return CompletionValidation(True)
                return CompletionValidation(
                    False,
                    ("public-evidence", "exact-output"),
                    "expect:public_evidence=rollback-ready",
                )

        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    completion=completion("public_evidence=unknown"),
                    usage=Usage(input_tokens=10, output_tokens=4),
                ),
                ModelResponse(
                    completion=completion("public_evidence=rollback-ready"),
                    usage=Usage(input_tokens=14, output_tokens=5),
                ),
            ]
        )
        validator = ExactValidator()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
            completion_validator=validator,
        )

        result = await service.collect(await service.start(make_request()))
        events = store.list_events(result.run_id)
        validations = [
            event
            for event in events
            if event.type == EventType.VALIDATION_RECORDED
        ]
        repair_messages = [
            message.content
            for message in provider.requests[1].messages
            if message.role == "user"
            and isinstance(message.content, dict)
            and message.content.get("runtime_error")
            == "COMPLETION_VALIDATION_FAILED"
        ]

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertEqual(
            validator.summaries,
            ["public_evidence=unknown", "public_evidence=rollback-ready"],
        )
        self.assertEqual([item.payload["passed"] for item in validations], [False, True])
        self.assertEqual(
            validations[0].payload["failed_checks"],
            ["public-evidence", "exact-output"],
        )
        self.assertEqual(len(repair_messages), 1)
        self.assertEqual(
            repair_messages[0]["failed_checks"],
            ["public-evidence", "exact-output"],
        )
        self.assertNotIn("failed_check", repair_messages[0])
        self.assertEqual(
            repair_messages[0]["message"],
            "expect:public_evidence=rollback-ready",
        )
        self.assertNotIn("public_evidence=unknown", str(repair_messages[0]))
        store.close()

    async def test_second_completion_validation_failure_is_non_retryable(self) -> None:
        class AlwaysFail:
            def validate(self, request, candidate):
                return CompletionValidation(
                    False,
                    ("exact-output",),
                    "expect:exact-output-contract",
                )

        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(completion=completion("first")),
                ModelResponse(completion=completion("second")),
                ModelResponse(completion=completion("must not run")),
            ]
        )
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
            completion_validator=AlwaysFail(),
        )

        result = await service.collect(await service.start(make_request()))
        validations = [
            event
            for event in store.list_events(result.run_id)
            if event.type == EventType.VALIDATION_RECORDED
        ]

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.failure.code, "COMPLETION_VALIDATION_FAILED")
        self.assertFalse(result.failure.retryable)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(len(validations), 2)
        self.assertFalse(validations[0].payload["passed"])
        store.close()

    async def test_completion_repair_respects_existing_model_call_budget(self) -> None:
        class AlwaysFail:
            def validate(self, request, candidate):
                return CompletionValidation(
                    False,
                    ("exact-output",),
                    "expect:exact-output-contract",
                )

        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(completion=completion("first")),
                ModelResponse(completion=completion("must not run")),
            ]
        )
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
            completion_validator=AlwaysFail(),
        )

        result = await service.collect(
            await service.start(
                make_request(limits=RunLimits(max_model_calls=1))
            )
        )

        self.assertEqual(result.status, RunStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.failure.message_safe, "Run limit reached: max_model_calls")
        self.assertEqual(provider.call_count, 1)
        store.close()

    async def test_schema_and_completion_repairs_have_independent_counters(self) -> None:
        class ExactValidator:
            def validate(self, request, candidate):
                if candidate.summary == "valid":
                    return CompletionValidation(True)
                return CompletionValidation(
                    False,
                    ("exact-output",),
                    "expect:valid",
                )

        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(),
                ModelResponse(completion=completion("invalid")),
                ModelResponse(completion=completion("valid")),
            ]
        )
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
            completion_validator=ExactValidator(),
        )

        result = await service.collect(
            await service.start(
                make_request(
                    limits=RunLimits(
                        max_model_calls=3,
                        max_consecutive_errors=2,
                    )
                )
            )
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.usage.model_calls, 3)
        self.assertEqual(provider.call_count, 3)
        store.close()

    async def test_cancellation_interrupts_completion_repair_call(self) -> None:
        class AlwaysFail:
            def validate(self, request, candidate):
                return CompletionValidation(
                    False,
                    ("exact-output",),
                    "expect:exact-output-contract",
                )

        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(completion=completion("first")),
                ModelResponse(completion=completion("must not arrive")),
            ],
            blocked_calls=(1,),
        )
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
            completion_validator=AlwaysFail(),
        )
        handle = await service.start(make_request())
        await provider.wait_until_started(1)
        await service.cancel(handle, "test cancellation")

        result = await service.collect(handle)

        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(result.failure.code, "RUN_CANCELLED")
        self.assertEqual(provider.call_count, 2)
        store.close()

    async def test_invalid_completion_validator_result_fails_closed(self) -> None:
        class InvalidValidator:
            def validate(self, request, candidate):
                return CompletionValidation(
                    False,
                    ("INVALID CHECK",),
                    "expect:value",
                )

        store = RunStore()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=ScriptedModelProvider(
                [ModelResponse(completion=completion("candidate"))]
            ),
            registry=ToolRegistry(),
            completion_validator=InvalidValidator(),
        )

        result = await service.collect(await service.start(make_request()))

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.failure.code, "COMPLETION_VALIDATOR_INVALID")
        self.assertFalse(result.failure.retryable)
        store.close()

    async def test_stream_progress_is_a_bounded_runtime_fact_not_an_answer(self) -> None:
        class StreamingFixture:
            async def complete_stream(self, request, cancellation, progress):
                cancellation.raise_if_cancelled()
                progress(ModelStreamProgress(1, 7))
                progress(ModelStreamProgress(2, 19, True))
                return ModelResponse(completion=completion("Validated final answer"))

        store = RunStore()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=StreamingFixture(),
            registry=ToolRegistry(),
        )

        result = await service.collect(await service.start(make_request()))
        progress_events = [
            event
            for event in store.list_events(result.run_id)
            if event.type == EventType.MODEL_STREAM_PROGRESS
        ]

        self.assertEqual(result.summary, "Validated final answer")
        self.assertEqual([event.payload["received_chars"] for event in progress_events], [7, 19])
        self.assertEqual(progress_events[-1].payload["finished"], True)
        self.assertNotIn("Validated final answer", str(progress_events))
        store.close()

    async def test_explicitly_safe_read_batch_runs_in_parallel_with_atomic_usage(self) -> None:
        store = RunStore()
        both_started = asyncio.Event()
        started = 0

        async def handler(arguments, cancellation):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.5)
            cancellation.raise_if_cancelled()
            return str(arguments["key"])

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="parallel_read",
                description="Read an independent fixture value.",
                input_schema={"type": "object"},
                effect=ToolEffect.READ,
                risk=ToolRisk.LOW,
                idempotency_mode=IdempotencyMode.NATURAL_KEY,
                validator=lambda arguments: {"key": str(arguments["key"])},
                resource_key=lambda arguments: f"fixture:{arguments['key']}",
                handler=handler,
                output_limit_bytes=16,
                parallel_safe=True,
            )
        )
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("parallel-1", "parallel_read", {"key": "a"}),
                        ToolCall("parallel-2", "parallel_read", {"key": "b"}),
                    )
                ),
                ModelResponse(completion=completion("Parallel evidence collected")),
            ]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)
        result = await service.collect(
            await service.start(
                make_request(
                    tool_names=("parallel_read",),
                    resource_patterns=("fixture:*",),
                )
            )
        )
        plans = [
            event
            for event in store.list_events(result.run_id)
            if event.type == EventType.TOOL_BATCH_PLANNED
        ]

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.usage.tool_calls, 2)
        self.assertEqual(plans[0].payload["mode"], "PARALLEL")
        self.assertEqual(plans[0].payload["reason"], "independent_read_only")
        store.close()

    async def test_assignee_mismatch_completion_becomes_typed_failed_result(self) -> None:
        store = RunStore()
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="This task needs another eligible employee.",
                        signals=(
                            RunSignal(
                                SignalCode.ASSIGNEE_MISMATCH,
                                "repository_analysis",
                                ("typed:mismatch",),
                            ),
                        ),
                    )
                )
            ]
        )
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
        )

        result = await service.collect(await service.start(make_request()))
        events = store.list_events(result.run_id)

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.failure.code, "ASSIGNEE_CAPABILITY_MISMATCH")
        self.assertEqual(result.failure.category.value, "INPUT")
        self.assertEqual(result.signals[0].code, SignalCode.ASSIGNEE_MISMATCH)
        self.assertEqual(events[-1].type, EventType.RUN_FAILED)
        store.close()

    async def test_direct_completion(self) -> None:
        store = RunStore()
        provider = ScriptedModelProvider(
            [ModelResponse(completion=completion("Direct answer"), usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.01))]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=ToolRegistry())

        handle = await service.start(make_request())
        result = await service.collect(handle)
        events = store.list_events(handle.run_id)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.summary, "Direct answer")
        self.assertEqual(result.usage.model_calls, 1)
        self.assertEqual(events[-1].type, EventType.RUN_SUCCEEDED)
        self.assertEqual(sum(event.usage_delta.model_calls for event in events if event.usage_delta), 1)
        store.close()

    async def test_read_tool_then_completion_preserves_event_order_and_usage(self) -> None:
        store = RunStore()
        fixture = FixtureReader({"bug": "off-by-one at calculator.py:7"})
        registry = ToolRegistry()
        registry.register(fixture.definition())
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    tool_calls=(ToolCall("call-1", "read_fixture", {"key": "bug"}),),
                    usage=Usage(input_tokens=8, output_tokens=2, cost_usd=0.01),
                ),
                ModelResponse(
                    completion=completion("Found the boundary bug"),
                    usage=Usage(input_tokens=12, output_tokens=4, cost_usd=0.02),
                ),
            ]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)

        handle = await service.start(make_request(tool_names=("read_fixture",), resource_patterns=("fixture:*",)))
        result = await service.collect(handle)
        events = store.list_events(handle.run_id)
        event_types = [event.type for event in events]
        summed = Usage()
        for event in events:
            if event.usage_delta:
                summed = summed.plus(event.usage_delta)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(fixture.call_count, 1)
        self.assertLess(event_types.index(EventType.TOOL_INTENT_RECORDED), event_types.index(EventType.TOOL_STARTED))
        self.assertLess(event_types.index(EventType.TOOL_STARTED), event_types.index(EventType.TOOL_SUCCEEDED))
        self.assertEqual(result.usage, summed)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertEqual(result.usage.tool_calls, 1)
        store.close()

    async def test_economy_mode_projects_successful_tool_context_without_mutating_receipt(self) -> None:
        store = RunStore()
        original = ("duplicate repository listing\n" * 700) + "".join(
            f"unique source line {index:04d}\n" for index in range(500)
        )
        fixture = FixtureReader({"large": original})
        registry = ToolRegistry()
        registry.register(fixture.definition())
        provider = ScriptedModelProvider(
            [
                ModelResponse(tool_calls=(ToolCall("call-1", "read_fixture", {"key": "large"}),)),
                ModelResponse(completion=completion("Economy projection reviewed")),
            ]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)
        request = make_request(
            tool_names=("read_fixture",),
            resource_patterns=("fixture:*",),
            limits=RunLimits(cost_efficiency_mode=CostEfficiencyMode.ECONOMY),
        )

        result = await service.collect(await service.start(request))
        events = store.list_events(result.run_id)
        receipt = next(message for message in store.list_messages(result.run_id) if message.role == "tool")
        provider_tool = next(message for message in provider.requests[1].messages if message.role == "tool")

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertIn(EventType.CONTEXT_ECONOMY_PROJECTED, [event.type for event in events])
        self.assertEqual(receipt.content["content"], original)
        self.assertLess(len(str(provider_tool.content)), len(str(receipt.content)))
        self.assertIn("noruct economy", str(provider_tool.content))
        self.assertIn("previous line repeated", str(provider_tool.content))
        store.close()

    async def test_repeated_invalid_arguments_fail_without_calling_handler(self) -> None:
        store = RunStore()
        fixture = FixtureReader({"bug": "evidence"})
        registry = ToolRegistry()
        registry.register(fixture.definition())
        provider = ScriptedModelProvider(
            [
                ModelResponse(tool_calls=(ToolCall("bad-1", "read_fixture", {}),)),
                ModelResponse(tool_calls=(ToolCall("bad-2", "read_fixture", {"wrong": "bug"}),)),
            ]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)

        handle = await service.start(make_request(tool_names=("read_fixture",), resource_patterns=("fixture:*",)))
        result = await service.collect(handle)
        events = store.list_events(handle.run_id)

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.failure.code, "TOOL_ERRORS_EXHAUSTED")
        self.assertEqual(fixture.call_count, 0)
        self.assertEqual(sum(event.type == EventType.TOOL_INTENT_RECORDED for event in events), 2)
        self.assertEqual(sum(event.type == EventType.TOOL_STARTED for event in events), 0)
        store.close()

    async def test_ungranted_tool_is_denied_before_handler(self) -> None:
        store = RunStore()
        fixture = FixtureReader({"bug": "evidence"})
        registry = ToolRegistry()
        registry.register(fixture.definition())
        provider = ScriptedModelProvider(
            [ModelResponse(tool_calls=(ToolCall("call-denied", "read_fixture", {"key": "bug"}),))]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)

        handle = await service.start(make_request())
        result = await service.collect(handle)

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.failure.category.value, "POLICY")
        self.assertEqual(fixture.call_count, 0)
        self.assertNotIn("read_fixture", [tool.name for tool in provider.requests[0].tools])
        store.close()

    async def test_model_call_limit_stops_before_an_extra_call(self) -> None:
        store = RunStore()
        fixture = FixtureReader({"bug": "evidence"})
        registry = ToolRegistry()
        registry.register(fixture.definition())
        provider = ScriptedModelProvider(
            [
                ModelResponse(tool_calls=(ToolCall("call-1", "read_fixture", {"key": "bug"}),)),
                ModelResponse(completion=completion("must not be called")),
            ]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)
        request = make_request(
            tool_names=("read_fixture",),
            resource_patterns=("fixture:*",),
            limits=RunLimits(max_model_calls=1),
        )

        result = await service.collect(await service.start(request))

        self.assertEqual(result.status, RunStatus.BUDGET_EXHAUSTED)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(result.failure.message_safe, "Run limit reached: max_model_calls")
        store.close()

    async def test_cumulative_tool_output_limit_bounds_model_context(self) -> None:
        store = RunStore()
        fixture = FixtureReader({"first": "abcd", "second": "efgh"})
        registry = ToolRegistry()
        registry.register(fixture.definition())
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("call-1", "read_fixture", {"key": "first"}),
                        ToolCall("call-2", "read_fixture", {"key": "second"}),
                    )
                ),
                ModelResponse(completion=completion("Used only the bounded evidence")),
            ]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)
        request = make_request(
            tool_names=("read_fixture",),
            resource_patterns=("fixture:*",),
            limits=RunLimits(max_tool_output_bytes=6),
        )

        result = await service.collect(await service.start(request))
        tool_messages = [
            message.content
            for message in provider.requests[1].messages
            if message.role == "tool"
        ]

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(store.get_tool_output_bytes(result.run_id), 4)
        self.assertEqual(fixture.call_count, 2)
        self.assertEqual(tool_messages[0]["content"], "abcd")
        self.assertEqual(tool_messages[1]["error_code"], "OUTPUT_LIMIT")
        store.close()

    async def test_model_wait_is_stopped_by_wall_time_limit(self) -> None:
        store = RunStore()
        provider = ScriptedModelProvider(
            [ModelResponse(completion=completion("must not arrive"))],
            blocked_calls=(0,),
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=ToolRegistry())
        request = make_request(limits=RunLimits(max_wall_time_ms=10))

        result = await service.collect(await service.start(request))

        self.assertEqual(result.status, RunStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.failure.category.value, "TIMEOUT")
        self.assertEqual(result.failure.message_safe, "Run limit reached: max_wall_time_ms")
        self.assertEqual(provider.call_count, 1)
        store.close()

    async def test_context_preparation_cannot_dispatch_after_wall_time_expires(self) -> None:
        class SlowCompactor:
            revision = "slow-test"

            def compact(self, messages, **_limits):  # type: ignore[no-untyped-def]
                time.sleep(0.03)
                return ContextCompactionResult(tuple(messages), False)

        store = RunStore()
        provider = ScriptedModelProvider(
            [ModelResponse(completion=completion("must not execute"))]
        )
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=provider,
            registry=ToolRegistry(),
        )
        service.loop.context_compactor = SlowCompactor()  # type: ignore[assignment]

        result = await service.collect(
            await service.start(make_request(limits=RunLimits(max_wall_time_ms=10)))
        )

        self.assertEqual(result.status, RunStatus.BUDGET_EXHAUSTED)
        self.assertEqual(provider.call_count, 0)
        store.close()


if __name__ == "__main__":
    unittest.main()
