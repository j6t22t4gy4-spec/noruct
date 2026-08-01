"""Bounded Manager supervision contracts for an executing Company Job.

The Manager is allowed to interpret concise operational evidence. It cannot
create work, grant a permission, alter a budget, or mutate the graph. The
Kernel may only consume one typed signal from a valid decision through its
existing replanner and graph-admission gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from dynamic_firm.runtime.models import EmployeeRunResult, RunSignal, SignalCode

from .models import CompanyRunRequest, JobGraph, JobTask


class ManagerSupervisionAction(StrEnum):
    CONTINUE = "CONTINUE"
    SIGNAL = "SIGNAL"


_MANAGER_SIGNAL_CODES = frozenset(
    {
        SignalCode.CAPABILITY_MISSING,
        SignalCode.ASSUMPTION_INVALIDATED,
        SignalCode.CONSTRAINT_CHANGED,
        SignalCode.VALIDATION_FAILED,
        SignalCode.GRAPH_STALLED,
    }
)


@dataclass(frozen=True, slots=True)
class ManagerSupervisionContext:
    job_id: str
    graph_version: int
    task_id: str
    priority: str
    remaining_wall_time_ms: int
    required_capabilities: tuple[str, ...]
    capability_shortage: tuple[str, ...]
    conflicting_outcome: bool
    result_status: str
    unresolved_issue_count: int


@dataclass(frozen=True, slots=True)
class ManagerSupervisionDecision:
    action: ManagerSupervisionAction
    rationale: str
    signal: RunSignal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ManagerSupervisionAction):
            raise ValueError("Manager supervision action is invalid")
        if (
            not isinstance(self.rationale, str)
            or not self.rationale.strip()
            or len(self.rationale) > 256
        ):
            raise ValueError("Manager supervision rationale is invalid")
        if self.signal is not None and not isinstance(self.signal, RunSignal):
            raise ValueError("Manager supervision signal must be typed")
        if self.action is ManagerSupervisionAction.CONTINUE:
            if self.signal is not None:
                raise ValueError("CONTINUE supervision cannot carry a signal")
            return
        if self.signal is None or self.signal.code not in _MANAGER_SIGNAL_CODES:
            raise ValueError("Manager supervision signal is not permitted")
        if (
            not isinstance(self.signal.value, str)
            or len(self.signal.value) > 160
            or not isinstance(self.signal.evidence, tuple)
            or len(self.signal.evidence) > 8
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.encode("utf-8")) > 160
                for item in self.signal.evidence
            )
        ):
            raise ValueError("Manager supervision signal is too large")


class ManagerSupervisionPort(Protocol):
    async def assess(
        self,
        context: ManagerSupervisionContext,
    ) -> ManagerSupervisionDecision: ...


def supervision_context(
    *,
    request: CompanyRunRequest,
    graph: JobGraph,
    task: JobTask,
    result: EmployeeRunResult,
    remaining_wall_time_ms: int,
) -> ManagerSupervisionContext:
    active_capabilities = {
        capability
        for employee in request.roster
        if employee.active
        for capability in employee.capabilities
    }
    shortage = tuple(
        capability
        for capability in task.required_capabilities
        if capability not in active_capabilities
    )
    return ManagerSupervisionContext(
        job_id=request.job_id,
        graph_version=graph.version,
        task_id=task.task_id,
        priority="FINAL_INTEGRATION" if task.task_id == graph.final_task_id else "SPECIALIST",
        remaining_wall_time_ms=max(0, remaining_wall_time_ms),
        required_capabilities=task.required_capabilities,
        capability_shortage=shortage,
        conflicting_outcome=bool(result.unresolved_issues),
        result_status=result.status.value,
        unresolved_issue_count=len(result.unresolved_issues),
    )
