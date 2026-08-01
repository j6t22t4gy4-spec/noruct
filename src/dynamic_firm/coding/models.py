from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dynamic_firm.runtime.models import Usage


class FileChangeKind(StrEnum):
    ADD = "ADD"
    MODIFY = "MODIFY"


@dataclass(frozen=True, slots=True)
class WorkspaceFileChange:
    path: str
    kind: FileChangeKind
    base_sha256: str | None
    new_sha256: str
    old_content: str | None
    new_content: str


@dataclass(frozen=True, slots=True)
class WorkspaceChangeSet:
    change_set_id: str
    workspace_id: str
    files: tuple[WorkspaceFileChange, ...]
    total_bytes: int


@dataclass(frozen=True, slots=True)
class CodingWorkRequest:
    task_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    dependency_context: tuple[str, ...]
    workspace: Path
    model_profile: str
    max_wall_time_ms: int
    # The immutable task capability contract lets bounded wrappers distinguish
    # a final implementation worker from read-only evidence without relying
    # on an arbitrary task identifier.
    required_capabilities: tuple[str, ...] = ()
    # Bounded, authority-free task context projected by the Firm Kernel.  This
    # is distinct from dependency artifacts: it lets a delegated coding
    # employee retain the Work Order's concrete outcome when a Manager's
    # compiler has shortened the local task label.
    task_context: tuple[str, ...] = ()
    validation_feedback: tuple[ValidationAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationAttempt:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CodingWorkResult:
    summary: str
    acceptance_evidence: tuple[str, ...] = ()
    unresolved_issues: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    suggested_followups: tuple[str, ...] = ()
    # These are untrusted suggestions from the disposable coding worker.  The
    # parent re-validates and independently requests approval before executing
    # any of them in the real workspace.
    verification_commands: tuple[str, ...] = ()
    validation_attempts: tuple[ValidationAttempt, ...] = ()
    usage: Usage = field(default_factory=Usage)
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ShadowCodingOutcome:
    worker_result: CodingWorkResult
    change_set: WorkspaceChangeSet | None
    worker_attempts: tuple[CodingWorkResult, ...] = ()
    recovery_budget_exhausted: bool = False
    recovery_budget_reason: str | None = None


class CodingExecutionProgressKind(StrEnum):
    WORKER_STARTED = "WORKER_STARTED"
    WORKER_COMPLETED = "WORKER_COMPLETED"
    VALIDATION_RECORDED = "VALIDATION_RECORDED"


@dataclass(frozen=True, slots=True)
class CodingExecutionProgress:
    kind: CodingExecutionProgressKind
    call_index: int
    worker_result: CodingWorkResult | None = None
    validation_attempt: ValidationAttempt | None = None
    candidate_changed_paths: tuple[str, ...] = ()
