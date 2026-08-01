from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import AsyncIterator, Mapping

from dynamic_firm.runtime.models import (
    CancelReceipt,
    EmployeeRunRequest,
    EmployeeRunResult,
    Failure,
    FailureCategory,
    RunEvent,
    RunHandle,
    RunSignal,
    RunStatus,
    Usage,
)

from .models import GraphPatch, ReplanContext


@dataclass(frozen=True, slots=True)
class ScriptedOutcome:
    summary: str
    delay_seconds: float = 0.0
    status: RunStatus = RunStatus.SUCCEEDED
    acceptance_evidence: tuple[str, ...] = ()
    unresolved_issues: tuple[str, ...] = ()
    output_artifact_refs: tuple[str, ...] = ()
    signals: tuple[RunSignal, ...] = ()
    usage: Usage = field(default_factory=lambda: Usage(model_calls=1))
    failure: Failure | None = None
    synthesize_failure: bool = True


class ScriptedEmployeeExecutionPort:
    def __init__(
        self,
        outcomes: Mapping[
            str | tuple[str, str],
            ScriptedOutcome | tuple[ScriptedOutcome, ...],
        ],
    ) -> None:
        self.outcomes = dict(outcomes)
        self.requests: list[EmployeeRunRequest] = []
        self.maximum_parallelism = 0
        self.active = 0
        self.started_order: list[str] = []
        self.finished_order: list[str] = []
        self._runs: dict[str, asyncio.Task[EmployeeRunResult]] = {}
        self._requests: dict[str, EmployeeRunRequest] = {}
        self._outcome_offsets: dict[str | tuple[str, str], int] = {}

    async def start(self, request: EmployeeRunRequest) -> RunHandle:
        key: str | tuple[str, str]
        employee_key = (request.task.task_id, request.employee.employee_id)
        if employee_key in self.outcomes:
            key = employee_key
        elif request.task.task_id in self.outcomes:
            key = request.task.task_id
        else:
            raise KeyError(f"No scripted outcome for task {request.task.task_id}")
        configured = self.outcomes[key]
        if isinstance(configured, tuple):
            offset = self._outcome_offsets.get(key, 0)
            if offset >= len(configured):
                raise KeyError(f"No scripted outcome remains for task {request.task.task_id}")
            outcome = configured[offset]
            self._outcome_offsets[key] = offset + 1
        else:
            outcome = configured
        run_id = f"scripted-run-{len(self.requests) + 1}"
        handle = RunHandle(run_id=run_id, request_id=request.request_id)
        self.requests.append(request)
        self._requests[run_id] = request
        self._runs[run_id] = asyncio.create_task(
            self._execute(run_id, request, outcome)
        )
        return handle

    async def _execute(
        self,
        run_id: str,
        request: EmployeeRunRequest,
        outcome: ScriptedOutcome,
    ) -> EmployeeRunResult:
        started_at = datetime.now(UTC)
        self.active += 1
        self.maximum_parallelism = max(self.maximum_parallelism, self.active)
        self.started_order.append(request.task.task_id)
        status = outcome.status
        try:
            await asyncio.sleep(outcome.delay_seconds)
        except asyncio.CancelledError:
            status = RunStatus.CANCELLED
        finally:
            self.active -= 1
            self.finished_order.append(request.task.task_id)
        failure = outcome.failure
        if (
            status not in {RunStatus.SUCCEEDED, RunStatus.CANCELLED}
            and outcome.synthesize_failure
        ):
            failure = failure or Failure(
                code="SCRIPTED_FAILURE",
                category=FailureCategory.INTERNAL,
                message_safe="Scripted employee execution failed.",
            )
        return EmployeeRunResult(
            run_id=run_id,
            request_id=request.request_id,
            job_id=request.task.job_id,
            task_id=request.task.task_id,
            employee_id=request.employee.employee_id,
            status=status,
            summary=outcome.summary,
            output_artifact_refs=outcome.output_artifact_refs,
            acceptance_evidence=outcome.acceptance_evidence,
            unresolved_issues=outcome.unresolved_issues,
            observations=(),
            suggested_followups=(),
            signals=outcome.signals,
            partial_result=False,
            usage=outcome.usage,
            last_event_seq=1,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            failure=failure,
        )

    async def observe(
        self,
        handle: RunHandle,
        after_seq: int = 0,
    ) -> AsyncIterator[RunEvent]:
        if False:
            yield  # pragma: no cover

    async def cancel(self, handle: RunHandle, reason: str) -> CancelReceipt:
        task = self._runs[handle.run_id]
        accepted = not task.done()
        if accepted:
            task.cancel()
        return CancelReceipt(
            run_id=handle.run_id,
            accepted=accepted,
            status=RunStatus.CANCELLING if accepted else (await task).status,
            reason=reason,
        )

    async def collect(self, handle: RunHandle) -> EmployeeRunResult:
        return await self._runs[handle.run_id]


class StaticReplanner:
    def __init__(self, patches_by_task: Mapping[str, GraphPatch]) -> None:
        self.patches_by_task = dict(patches_by_task)
        self.contexts: list[ReplanContext] = []

    async def propose(self, context: ReplanContext) -> GraphPatch | None:
        self.contexts.append(context)
        return self.patches_by_task.get(context.trigger_task.task_id)
