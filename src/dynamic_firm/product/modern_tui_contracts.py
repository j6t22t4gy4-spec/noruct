"""Framework-neutral contracts for the optional modern Product terminal."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Protocol, Sequence

from dynamic_firm.product.events import ProductEvent
from dynamic_firm.product.models import ModelOption
from dynamic_firm.runtime.models import ApprovalRequest
from dynamic_firm.runtime.ports import ApprovalPort


class ModernTerminalUnavailable(ValueError):
    """The user selected the optional surface without its audited extra."""


@dataclass(frozen=True, slots=True)
class SessionInputHistorySelection:
    value: str
    index: int | None
    total: int
    restored_draft: bool = False


class SessionInputHistory:
    """Framework-free composer history for one selected Company session."""

    def __init__(self, values: Sequence[str] = ()) -> None:
        self._entries: list[str] = []
        self._index: int | None = None
        self._draft = ""
        self.replace(values)

    @property
    def entries(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def replace(self, values: Sequence[str]) -> None:
        retained: list[str] = []
        for value in values[-100:]:
            normalized = str(value).strip()
            if not normalized or len(normalized.encode("utf-8")) > 8_000:
                continue
            if normalized in retained:
                retained.remove(normalized)
            retained.append(normalized)
        self._entries = retained
        self._index = None
        self._draft = ""

    def remember(self, value: str) -> None:
        normalized = value.strip()
        if not normalized or len(normalized.encode("utf-8")) > 8_000:
            return
        if normalized in self._entries:
            self._entries.remove(normalized)
        self._entries.append(normalized)
        self._entries = self._entries[-100:]
        self._index = None
        self._draft = ""

    def move(self, direction: int, current_value: str) -> SessionInputHistorySelection | None:
        if direction not in {-1, 1}:
            raise ValueError("Input history direction must be -1 or 1")
        if not self._entries:
            return None
        if self._index is None:
            if direction > 0:
                return None
            self._draft = current_value
            self._index = len(self._entries) - 1
        else:
            candidate = self._index + direction
            if candidate < 0:
                return SessionInputHistorySelection(
                    value=self._entries[self._index],
                    index=self._index,
                    total=len(self._entries),
                )
            if candidate >= len(self._entries):
                self._index = None
                return SessionInputHistorySelection(
                    value=self._draft,
                    index=None,
                    total=len(self._entries),
                    restored_draft=True,
                )
            self._index = candidate
        return SessionInputHistorySelection(
            value=self._entries[self._index],
            index=self._index,
            total=len(self._entries),
        )


@dataclass(frozen=True, slots=True)
class ModernTerminalSnapshot:
    workspace: str
    session_id: str
    model: str
    provider: str
    authority: str
    version: str
    roster_revision: int
    active_employee_count: int
    employee_roles: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    settings_entries: tuple[Mapping[str, object], ...] = ()
    review_mode: str = "approval"
    evolution_mode: str = "never"
    operating_report: tuple[str, ...] = ()
    # A surface-neutral, read-only projection owned by product.operator_surface.
    # ``operating_report`` remains as the compact compatibility projection.
    operator_snapshot: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModernTerminalResult:
    summary: str
    status: str
    details: tuple[str, ...] = ()
    # This is an ephemeral projection of the authoritative JobResult. It is
    # not a second session, graph, or Company state store.
    company_report_mode: str = ""
    reporting_owner_employee_id: str = ""
    execution_owner_employee_id: str = ""
    report_requires_attention: bool = False


@dataclass(frozen=True, slots=True)
class ModernTerminalCommandResult:
    messages: tuple[str, ...] = ()
    clear: bool = False
    clear_answer: bool = False
    exit_requested: bool = False
    open_settings: bool = False
    open_model_picker: bool = False
    open_graph_controls: bool = False
    open_job_audit: bool = False
    job_audit_job_id: str = ""
    provider_login_requested: bool = False


class ModernTerminalController(Protocol):
    """First-party contract supplied by the CLI/session layer."""

    def snapshot(self) -> ModernTerminalSnapshot: ...

    def initial_messages(self) -> Sequence[str]: ...

    def input_history(self) -> Sequence[str]: ...

    def model_options(self) -> Sequence[ModelOption]: ...

    def provider_login(self) -> Sequence[str]: ...

    def graph_control_snapshot(self) -> Mapping[str, object]: ...

    def apply_graph_control(self, submission: Mapping[str, object]) -> Sequence[str]: ...

    def apply_graph_blueprint_action(
        self, submission: Mapping[str, object]
    ) -> Sequence[str]: ...

    def preview_graph(self, goal: str) -> Sequence[str]: ...

    def job_audit_snapshot(self, job_id: str | None = None) -> Mapping[str, object]: ...

    def job_audit_catalog(self) -> Mapping[str, object]: ...

    async def decide_graph_proposal(
        self,
        *,
        job_id: str,
        proposal_id: str,
        approve: bool,
        approval_port: ApprovalPort | None,
    ) -> ModernTerminalResult: ...

    async def resume_partial_read_only_job(
        self,
        *,
        job_id: str,
    ) -> ModernTerminalResult: ...

    async def handoff_partial_read_only_job(
        self,
        *,
        job_id: str,
        target_device_id: str,
    ) -> ModernTerminalResult: ...

    async def execute_goal(
        self,
        goal: str,
        event_sink: Callable[[ProductEvent], None],
        approval_port: ApprovalPort | None,
    ) -> ModernTerminalResult: ...

    async def execute_command(self, command: str) -> ModernTerminalCommandResult: ...

