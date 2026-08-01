from __future__ import annotations

import asyncio
import unittest

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import (
    EventType,
    IdempotencyMode,
    ModelResponse,
    RunStatus,
    ToolCall,
    ToolEffect,
    ToolRisk,
)
from dynamic_firm.runtime.ports import CancellationToken, OperationCancelled
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolDefinition, ToolRegistry
from tests.runtime.helpers import completion, make_request


class RuntimeCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_while_model_is_waiting(self) -> None:
        store = RunStore()
        provider = ScriptedModelProvider(
            [ModelResponse(completion=completion("not delivered"))],
            blocked_calls=(0,),
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=ToolRegistry())
        handle = await service.start(make_request())
        await provider.wait_until_started(0)

        first = await service.cancel(handle, "User changed the goal")
        second = await service.cancel(handle, "Duplicate cancel")
        result = await service.collect(handle)
        event_types = [event.type for event in store.list_events(handle.run_id)]

        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(event_types.count(EventType.CANCEL_REQUESTED), 1)
        self.assertEqual(event_types.count(EventType.RUN_CANCELLED), 1)
        store.close()

    async def test_cancel_propagates_to_running_tool(self) -> None:
        store = RunStore()
        tool_started = asyncio.Event()
        handler_calls = 0

        def validate(arguments):
            if arguments:
                raise ValueError("slow_read accepts no arguments")
            return {}

        async def handler(arguments, cancellation: CancellationToken) -> str:
            nonlocal handler_calls
            handler_calls += 1
            tool_started.set()
            await cancellation.wait()
            raise OperationCancelled(cancellation.reason)

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="slow_read",
                description="Block until cancellation for the cancellation fixture.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effect=ToolEffect.READ,
                risk=ToolRisk.LOW,
                idempotency_mode=IdempotencyMode.CALL_KEY,
                validator=validate,
                resource_key=lambda arguments: "fixture:slow",
                handler=handler,
            )
        )
        provider = ScriptedModelProvider(
            [ModelResponse(tool_calls=(ToolCall("slow-call", "slow_read", {}),))]
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)
        handle = await service.start(
            make_request(tool_names=("slow_read",), resource_patterns=("fixture:*",))
        )
        await asyncio.wait_for(tool_started.wait(), 1.0)

        await service.cancel(handle, "Stop the tool")
        result = await service.collect(handle)
        event_types = [event.type for event in store.list_events(handle.run_id)]

        self.assertEqual(handler_calls, 1)
        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertIn(EventType.TOOL_FAILED, event_types)
        self.assertLess(event_types.index(EventType.CANCEL_REQUESTED), event_types.index(EventType.RUN_CANCELLED))
        store.close()


if __name__ == "__main__":
    unittest.main()

