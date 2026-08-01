"""Bounded, non-authoritative projection of Company execution for the terminal.

The product event stream remains the source of truth.  This module turns that
stream into a small operator-facing activity feed: it answers *what is the
Company doing now, why is it waiting, and what happens next?* without exposing
private prompts, tool arguments, or model reasoning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from dynamic_firm.product.events import ProductEvent, ProductEventType


@dataclass(frozen=True, slots=True)
class TerminalActivityItem:
    """One bounded item suitable for a live terminal activity feed."""

    sequence: int
    stage: str
    label: str
    detail: str


@dataclass(frozen=True, slots=True)
class TerminalFlowSnapshot:
    """The complete UI projection; never an execution or Company state source."""

    stage: str
    now: str
    why: str
    next_step: str
    guard: str
    items: tuple[TerminalActivityItem, ...]


_GUIDANCE: dict[str, tuple[str, str]] = {
    "READY": ("Keep the persistent company ready", "Wait for an explicit goal"),
    "COMMAND": ("Keep local controls outside Company execution", "Return control without a model call"),
    "COMPILING": ("Analyze dependencies before staffing", "Choose the smallest useful company"),
    "COMPOSING": ("Derive only runnable work dependencies", "Freeze a bounded plan or safe fallback"),
    "ORGANIZING": ("Keep roles minimal and scoped", "Start independent ready work"),
    "EXECUTING": ("Advance the active task within authority", "Re-evaluate when evidence arrives"),
    "VERIFYING": ("Check the produced result before integration", "Accept, retry, or reroute the affected task"),
    "REPLANNING": ("Contain recovery to the affected task", "Resume only the revised dependency path"),
    "AWAITING REVIEW": ("Hold the protected action at the policy boundary", "Wait for the operator decision"),
    "BLOCKED": ("Keep the refused protected action unexecuted", "Return control or choose another route"),
    "RESPONDING": ("Write the integrated result to the answer lane", "Return the composer to ready"),
    "FINALIZING": ("Close the current result without hidden work", "Preserve the completed Company state"),
    "SAFE FAILURE": ("Keep failure bounded and visible", "Return the unresolved condition"),
}

_EVENT_PRESENTATION: dict[ProductEventType, tuple[str, str]] = {
    ProductEventType.INPUT_ROUTED: ("COMPILING", "Goal routed"),
    ProductEventType.WORKSPACE_IDENTITY: ("COMPILING", "Workspace checked"),
    ProductEventType.CAPABILITY_READY: ("READY", "Capability ready"),
    ProductEventType.COMPILER_STARTED: ("COMPOSING", "Company is scoping work"),
    ProductEventType.PLAN_ACCEPTED: ("ORGANIZING", "Execution plan accepted"),
    ProductEventType.PLAN_FALLBACK: ("ORGANIZING", "Safe solo plan selected"),
    ProductEventType.FIRM_ADMISSION: ("ORGANIZING", "Firm execution shape admitted"),
    ProductEventType.ORGANIZATION_ADMISSION: ("ORGANIZING", "Specialist need evaluated"),
    ProductEventType.TASK_ASSIGNED: ("ORGANIZING", "Ready work assigned"),
    ProductEventType.GRAPH_PATCH_APPLIED: ("REPLANNING", "Execution structure update applied"),
    ProductEventType.EMPLOYEE_STARTED: ("EXECUTING", "Employee started assigned work"),
    ProductEventType.MODEL_WORKING: ("EXECUTING", "Model call in progress"),
    ProductEventType.MODEL_STREAMING: ("RESPONDING", "Company answer streaming"),
    ProductEventType.CONTEXT_COMPACTED: ("EXECUTING", "Working context compacted"),
    ProductEventType.TOOL_BATCH_PLANNED: ("EXECUTING", "Approved tool work prepared"),
    ProductEventType.TOOL_REQUESTED: ("EXECUTING", "Tool action requested"),
    ProductEventType.APPROVAL_REQUIRED: ("AWAITING REVIEW", "Operator approval required"),
    ProductEventType.APPROVAL_RESOLVED: ("EXECUTING", "Approval decision applied"),
    ProductEventType.VALIDATION_RECORDED: ("VERIFYING", "Result validation recorded"),
    ProductEventType.TOOL_RUNNING: ("EXECUTING", "Tool action running"),
    ProductEventType.TOOL_FINISHED: ("EXECUTING", "Tool action completed"),
    ProductEventType.EMPLOYEE_FINISHED: ("VERIFYING", "Employee run finished"),
    ProductEventType.TASK_RETRY: ("REPLANNING", "Affected task is being retried"),
    ProductEventType.TASK_REROUTED: ("REPLANNING", "Affected task is being rerouted"),
    ProductEventType.JOB_FINISHED: ("FINALIZING", "Company job finished"),
}


class TerminalFlowProjector:
    """Project a bounded event history without blocking or owning the runtime."""

    def __init__(self, *, limit: int = 12) -> None:
        if limit < 1:
            raise ValueError("Terminal activity limit must be positive")
        self._limit = limit
        self._items: list[TerminalActivityItem] = []
        self._sequence = 0
        self._stage = "READY"
        self._now = "Company ready"
        self._company_work_mode = ""

    @property
    def stage(self) -> str:
        return self._stage

    @property
    def latest(self) -> str:
        return self._now

    def record_system(self, message: str, *, stage: str | None = None) -> None:
        """Record a local controller/UI event without pretending it was runtime work."""

        chosen_stage = stage or self._stage
        self._record(chosen_stage, "Local operator surface", message)
        if stage is not None:
            self._stage = stage

    def record_event(self, event: ProductEvent) -> None:
        """Record an execution event, omitting answer token deltas from the timeline."""

        if event.type == ProductEventType.INPUT_ROUTED:
            self._company_work_mode = str(
                event.data.get("company_work_mode", "")
            ).upper()
        if (
            event.type == ProductEventType.MODEL_STREAMING
            and event.data.get("stream_kind") == "text_delta"
        ):
            self._stage = "RESPONDING"
            self._now = "Company answer streaming"
            return
        stage, label = _EVENT_PRESENTATION.get(
            event.type,
            (self._stage, "Company event received"),
        )
        if self._company_work_mode == "DIRECT":
            direct_projection = {
                ProductEventType.INPUT_ROUTED: ("RESPONDING", "Direct Company turn routed"),
                ProductEventType.PLAN_ACCEPTED: ("RESPONDING", "Direct employee selected"),
                ProductEventType.TASK_ASSIGNED: ("RESPONDING", "Persistent employee assigned"),
                ProductEventType.EMPLOYEE_STARTED: ("RESPONDING", "Employee answering directly"),
                ProductEventType.MODEL_WORKING: ("RESPONDING", "Direct answer in progress"),
            }
            stage, label = direct_projection.get(event.type, (stage, label))
        if event.type == ProductEventType.APPROVAL_RESOLVED and any(
            marker in event.message.lower() for marker in ("deny", "unavailable")
        ):
            stage, label = "BLOCKED", "Protected action was not approved"
        self._stage = stage
        detail = event.message
        if event.type == ProductEventType.JOB_FINISHED:
            metrics = event.data.get("metrics")
            source = metrics if isinstance(metrics, Mapping) else event.data
            facts: list[str] = []
            for key, suffix in (
                ("unique_employee_count", "employees"),
                ("maximum_parallelism", "max parallel"),
                ("graph_patch_count", "workflow revisions"),
                ("task_mutation_count", "task recoveries"),
                ("manager_integration_count", "manager integrations"),
            ):
                value = source.get(key)
                if isinstance(value, int):
                    facts.append(f"{value} {suffix}")
            if facts:
                detail = f"{detail} · {' · '.join(facts)}"
        self._record(stage, label, detail)

    def snapshot(self) -> TerminalFlowSnapshot:
        why, next_step = _GUIDANCE.get(
            self._stage,
            ("Maintain the bounded Company state", "Re-evaluate the active Job"),
        )
        guard = (
            "protected action remains unexecuted"
            if self._stage in {"AWAITING REVIEW", "BLOCKED"}
            else "authority and Company state remain local"
        )
        return TerminalFlowSnapshot(
            stage=self._stage,
            now=self._now,
            why=why,
            next_step=next_step,
            guard=guard,
            items=tuple(self._items),
        )

    def _record(self, stage: str, label: str, detail: str) -> None:
        clean = " ".join(str(detail).replace("\x00", "").split())[:240]
        self._sequence += 1
        self._now = clean or label
        self._items.append(
            TerminalActivityItem(
                sequence=self._sequence,
                stage=stage,
                label=label,
                detail=clean or "No additional detail",
            )
        )
        self._items = self._items[-self._limit :]
