"""Terminal runtime-model records kept behind the public models facade."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .models import EventType, Failure, RunSignal, RunStatus, Usage


@dataclass(frozen=True, slots=True)
class PromptSnapshot:
    system_prompt: str
    user_message: str
    prompt_hash: str
    context_hash: str
    knowledge_projection: Mapping[str, Any] = field(default_factory=dict)
    audit_user_message: str = ""


@dataclass(frozen=True, slots=True)
class RunEvent:
    event_id: str
    run_id: str
    seq: int
    job_id: str
    task_id: str
    employee_id: str
    type: EventType
    payload: Mapping[str, Any]
    usage_delta: Usage | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class EmployeeRunResult:
    run_id: str
    request_id: str
    job_id: str
    task_id: str
    employee_id: str
    status: RunStatus
    summary: str
    output_artifact_refs: tuple[str, ...]
    acceptance_evidence: tuple[str, ...]
    unresolved_issues: tuple[str, ...]
    observations: tuple[str, ...]
    suggested_followups: tuple[str, ...]
    signals: tuple[RunSignal, ...]
    partial_result: bool
    usage: Usage
    last_event_seq: int
    started_at: datetime | None
    finished_at: datetime
    failure: Failure | None = None


@dataclass(frozen=True, slots=True)
class CancelReceipt:
    run_id: str
    accepted: bool
    status: RunStatus
    reason: str


def to_primitive(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if is_dataclass(value):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_primitive(item) for item in value]
    return value
