from __future__ import annotations

import asyncio
from collections.abc import Sequence

from dynamic_firm.runtime.models import ModelRequest, ModelResponse
from dynamic_firm.runtime.ports import CancellationToken, OperationCancelled


class ScriptedModelProvider:
    """Deterministic provider derived from the scripted-model testing pattern."""

    def __init__(
        self,
        responses: Sequence[ModelResponse | Exception],
        *,
        blocked_calls: Sequence[int] = (),
    ) -> None:
        self._responses = tuple(responses)
        self._blocked_calls = set(blocked_calls)
        self._release: dict[int, asyncio.Event] = {}
        self._started: dict[int, asyncio.Event] = {}
        self.requests: list[ModelRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def _started_event(self, index: int) -> asyncio.Event:
        return self._started.setdefault(index, asyncio.Event())

    def _release_event(self, index: int) -> asyncio.Event:
        return self._release.setdefault(index, asyncio.Event())

    async def wait_until_started(self, index: int, timeout: float = 1.0) -> None:
        await asyncio.wait_for(self._started_event(index).wait(), timeout)

    def release(self, index: int) -> None:
        self._release_event(index).set()

    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        index = len(self.requests)
        self.requests.append(request)
        self._started_event(index).set()
        if index in self._blocked_calls:
            released = asyncio.create_task(self._release_event(index).wait())
            cancelled = asyncio.create_task(cancellation.wait())
            done: set[asyncio.Task] = set()
            pending: set[asyncio.Task] = {released, cancelled}
            try:
                done, pending = await asyncio.wait(
                    {released, cancelled},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if cancelled in done:
                raise OperationCancelled(cancellation.reason or "Run cancelled")
        cancellation.raise_if_cancelled()
        if index >= len(self._responses):
            raise RuntimeError("Scripted model has no response for this call")
        response = self._responses[index]
        if isinstance(response, Exception):
            raise response
        return response
