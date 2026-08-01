from __future__ import annotations

import asyncio
import unittest

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import EventType, ModelResponse
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry
from tests.runtime.helpers import completion, make_request


class RuntimeObserveTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_replays_then_receives_terminal_event(self) -> None:
        store = RunStore()
        provider = ScriptedModelProvider(
            [ModelResponse(completion=completion("observed"))],
            blocked_calls=(0,),
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=ToolRegistry())
        handle = await service.start(make_request())
        await provider.wait_until_started(0)

        async def consume():
            return [event async for event in service.observe(handle)]

        observer = asyncio.create_task(consume())
        provider.release(0)
        events = await asyncio.wait_for(observer, 1.0)

        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[-1].type, EventType.RUN_SUCCEEDED)
        store.close()


if __name__ == "__main__":
    unittest.main()

