"""Persistent live Product Terminal UI surface."""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, TextIO

from dynamic_firm.runtime.models import ApprovalDecision, ApprovalRequest

from .events import ProductEvent, ProductEventType
from .terminal import (
    FrameRow,
    display_width,
    frame_lines,
    pad_display,
    truncate_display,
    wrap_display,
)
from .tui_constants import (
    BOLD,
    BRAND_CYAN,
    BRAND_PURPLE,
    CYAN,
    DIM,
    GREEN,
    HIDE_CURSOR,
    RED,
    SYNC_END,
    SYNC_START,
    YELLOW,
)
from .tui_inline import InlineTerminalUI
from .tui_live_dock import LiveTerminalDockMixin
from .tui_live_assessment import live_assessment_entries
from .tui_primitives import (
    _compact_number,
    _compact_session,
    _duration,
    _fit_segments,
    _is_real_tty,
    _job_metrics_text,
    _short_identity,
)

@dataclass(slots=True)
class _LiveTask:
    task_id: str
    employee: str
    label: str
    status: str = "working"
    detail: str = ""


class LiveTerminalUI(LiveTerminalDockMixin, InlineTerminalUI):
    """Single fixed composer surface below a normal conversation transcript.

    The surface owns a reserved block at the physical bottom of the terminal and
    is repainted with absolute cursor addressing. Conversation answers and local
    command output use only the scroll region above it, so surface rows can never
    become transcript entries.
    """

    def __init__(
        self,
        *,
        stdin: TextIO,
        stdout: TextIO,
        interactive: bool | None = None,
        color: bool | None = None,
        animations: bool | None = None,
        details: bool = False,
        plain: bool = False,
        terminal_width: int | None = None,
        terminal_height: int | None = None,
        live_screen: bool | None = None,
    ) -> None:
        super().__init__(
            stdin=stdin,
            stdout=stdout,
            interactive=interactive,
            color=color,
            animations=animations,
            details=details,
            plain=plain,
            terminal_width=terminal_width,
        )
        detected_live = _is_real_tty(stdin) and _is_real_tty(stdout)
        self.live_screen = (
            detected_live if live_screen is None else bool(live_screen)
        ) and not plain
        self._terminal_height = terminal_height
        self._live_active = False
        self._live_previous_lines: tuple[str, ...] = ()
        self._live_previous_size = (0, 0, 0)
        self._live_reserved_rows = 0
        self._live_physical_height = 0
        self._live_expanded = True
        self._live_termios_state: tuple[Any, int] | None = None
        self._live_hotkey_thread: threading.Thread | None = None
        self._live_typeahead = bytearray()
        self._live_escape_input = 0
        self._live_generation = 0
        self._live_transcript_mode = False
        self._live_tick = 0
        self._live_stage = "IDLE"
        self._live_status = "Company ready"
        self._live_goal = ""
        self._live_route = ""
        self._live_plan_mode = ""
        self._live_expected_tasks = 0
        self._live_tasks: dict[str, _LiveTask] = {}
        self._live_activity: list[tuple[str, str]] = []
        self._live_result_status = ""
        self._live_result_strip = ""
        self._live_modal_title = ""
        self._live_modal_rows: list[tuple[str, str]] = []
        self._live_prompt = ""

    def _input_rules(self) -> tuple[str, str]:
        action = "collapse" if self._live_expanded else "expand"
        footer = _fit_segments(
            (
                (100, self._model or "default model"),
                (95, f"/view {action}"),
                (90, "/model"),
                (70, "\\ multiline"),
                (60, "/help"),
            ),
            max(1, self._width() - 6),
        )
        lines = frame_lines("", (), self._width(), footer=footer)
        return lines[0], lines[-1]

    def toggle_live_view(self, value: str = "") -> bool:
        normalized = value.strip().lower()
        if normalized in {"expanded", "expand", "on", "full"}:
            self._live_expanded = True
        elif normalized in {"compact", "collapse", "off"}:
            self._live_expanded = False
        elif normalized in {"", "toggle"}:
            self._live_expanded = not self._live_expanded
        else:
            raise ValueError("Use /view, /view expand, or /view collapse.")
        self.commit(
            f"Live dock: {'expanded' if self._live_expanded else 'compact'}",
            tone="muted",
        )
        return self._live_expanded

    def _read_goal_line(self, prompt: str) -> str | None:
        with self._lock:
            buffered = bytes(self._live_typeahead)
            self._live_typeahead.clear()
        if not buffered:
            return super()._read_goal_line(prompt)
        text = buffered.decode("utf-8", errors="ignore")
        if "\n" in text:
            line, remainder = text.split("\n", 1)
            with self._lock:
                self._live_typeahead.extend(remainder.encode("utf-8"))
            self._write(self._style(prompt, BOLD, CYAN) + line + "\n")
            return line
        if self._uses_readline_editor() and self._readline is not None:
            def prefill() -> None:
                self._readline.insert_text(text)
                self._readline.redisplay()

            try:
                self._readline.set_startup_hook(prefill)
                return super()._read_goal_line(prompt)
            finally:
                self._readline.set_startup_hook()
        return text

    def _persistent_live_available(self) -> bool:
        width, height = self._live_size()
        return self.live_screen and width >= 40 and height >= 14

    def _prepare_live_idle_locked(self) -> None:
        self._live_stage = "IDLE"
        self._live_status = "Waiting for your next goal"
        if not self._live_goal:
            self._live_goal = "No goal submitted yet"
        if not self._live_activity:
            self._live_activity = [("Company surface is ready", "muted")]
        self._live_prompt = ""

    def read_goal(self) -> str | None:
        if not self._persistent_live_available():
            return super().read_goal()

        parts: list[str] = []
        while True:
            with self._lock:
                thread = (
                    self._pause_live_runtime_locked()
                    if self._live_active
                    else None
                )
            self._finish_live_runtime_pause(thread)
            with self._lock:
                self._prepare_live_idle_locked()
                self._enter_live_locked(runtime=False)
                self._open_live_input_region_locked()
            prompt = "╭─ ❯ " if not parts else "├─ … "
            try:
                line = self._read_goal_line(prompt)
            finally:
                with self._lock:
                    self._close_live_input_region_locked()
            if line is None:
                with self._lock:
                    self._exit_live_locked()
                return None if not parts else "\n".join(parts).strip()
            if line.endswith("\\"):
                parts.append(line[:-1])
                continue
            parts.append(line)
            result = "\n".join(parts).strip()
            if result and self._readline is not None:
                try:
                    self._readline.add_history(result)
                except Exception:
                    pass
            # Local commands and pickers write into the conversation region
            # above the fixed surface. The next read restores composer ownership.
            if result.startswith(("/", "?")):
                with self._lock:
                    self._begin_live_transcript_locked()
            return result

    def _live_size(self) -> tuple[int, int]:
        if self._terminal_width is not None and self._terminal_height is not None:
            return max(24, self._terminal_width), max(8, self._terminal_height)
        try:
            size = shutil.get_terminal_size((96, 28))
        except Exception:
            size = os.terminal_size((96, 28))
        width = self._terminal_width if self._terminal_width is not None else size.columns
        height = self._terminal_height if self._terminal_height is not None else size.lines
        return max(1, min(220, width)), max(1, height)

    def _live_dock_height(self, terminal_height: int) -> int:
        if not self._live_expanded:
            return 6
        # Keep at least three physical rows above the dock for the readline
        # composer. Very small terminals degrade to the compact frame because
        # a 14-row detail surface and a stable input region cannot both fit.
        return min(20, max(14, terminal_height - 4), max(6, terminal_height - 3))

    @staticmethod
    def _live_rule(
        width: int,
        label: str = "",
        *,
        left: str = "╭",
        right: str = "╮",
    ) -> str:
        safe = truncate_display(label.strip(), max(0, width - 6))
        prefix = left + "─" + (f" {safe} " if safe else "")
        return prefix + "─" * max(0, width - display_width(prefix) - 1) + right

    def _live_panel(
        self,
        title: str,
        entries: list[tuple[str, str]],
        width: int,
        height: int,
        *,
        tail: bool = False,
    ) -> list[tuple[str, str]]:
        width = max(8, width)
        height = max(3, height)
        slots = height - 2
        selected = entries[-slots:] if tail else entries[:slots]
        rows: list[tuple[str, str]] = [
            (self._live_rule(width, title), "accent"),
        ]
        inner = max(1, width - 4)
        for text, tone in selected:
            rows.append((f"│ {pad_display(text, inner)} │", tone))
        while len(rows) < height - 1:
            rows.append((f"│ {' ' * inner} │", "normal"))
        rows.append((self._live_rule(width, left="╰", right="╯"), "accent"))
        return rows

    def _live_elapsed(self) -> str:
        if self._live_stage == "IDLE":
            return "ready"
        return _duration(time.monotonic() - self._turn_started) if self._turn_started else "0s"


    def _live_assessment_entries(self) -> list[tuple[str, str]]:
        return live_assessment_entries(
            stage=self._live_stage,
            status=self._live_status,
            tasks=self._live_tasks.values(),
        )

    def _live_style_line(self, text: str, tone: str, width: int) -> str:
        fitted = pad_display(text, width)
        styles = {
            "accent": (BRAND_PURPLE,),
            "success": (GREEN,),
            "warning": (YELLOW,),
            "error": (RED,),
            "muted": (DIM,),
            "header": (BOLD, BRAND_CYAN),
        }.get(tone, ())
        return self._style(fitted, *styles)

    def _live_compact_frame(self, width: int, height: int) -> tuple[str, ...]:
        control = "/view expand" if self._live_stage == "IDLE" else "ctrl+o expand"
        rows: list[tuple[str, str]] = [
            (self._live_rule(width, f"◆ NORUCT · {self._live_stage}"), "header"),
            (
                f"│ {pad_display(self._live_status, max(1, width - 4))} │",
                "accent",
            ),
            (
                f"│ {pad_display('goal  ' + self._live_goal, max(1, width - 4))} │",
                "normal",
            ),
        ]
        body_slots = max(0, height - 5)
        entries = self._live_assessment_entries()[:body_slots]
        for text, tone in entries:
            rows.append((f"│ {pad_display(text, max(1, width - 4))} │", tone))
        while len(rows) < height - 2:
            rows.append((f"│ {' ' * max(1, width - 4)} │", "normal"))
        rows.append((self._live_rule(width, self._live_elapsed(), left="├", right="┤"), "muted"))
        rows.append(
            (
                self._live_rule(
                    width,
                    f"[ {control} ]",
                    left="╰",
                    right="╯",
                ),
                "accent",
            )
        )
        return tuple(self._live_style_line(text, tone, width) for text, tone in rows[:height])

    def _live_frame(self, width: int, height: int) -> tuple[str, ...]:
        if height < 14:
            return self._live_compact_frame(width, height)

        stage_label = f"◆ NORUCT · LIVE COMPANY  {self._live_stage}"
        meta = _fit_segments(
            (
                (100, self._live_status),
                (95, self._live_elapsed()),
                (85, self._model or "default model"),
                (75, f"session {_compact_session(self._session_id)}" if self._session_id else ""),
                (65, self._authority),
            ),
            max(1, width - 4),
        )
        goal_lines = wrap_display(self._live_goal or "Waiting for a company goal", max(1, width - 4))
        rows: list[tuple[str, str]] = [
            (self._live_rule(width, stage_label), "header"),
            (f"│ {pad_display(meta, max(1, width - 4))} │", "accent"),
            (self._live_rule(width, "CURRENT GOAL", left="├", right="┤"), "accent"),
            (f"│ {pad_display(goal_lines[0], max(1, width - 4))} │", "normal"),
        ]

        if self._live_modal_title:
            body_height = height - 6
            rows.extend(
                self._live_panel(
                    self._live_modal_title,
                    self._live_modal_rows,
                    width,
                    body_height,
                )
            )
            prompt = f"  ❯ {self._live_prompt}" if self._live_prompt else ""
            rows.append((pad_display(prompt, width), "warning"))
            rows.append(
                (
                    self._live_rule(width, "approval is fail-closed", left="╰", right="╯"),
                    "warning",
                )
            )
            return tuple(self._live_style_line(text, tone, width) for text, tone in rows[:height])

        # The fixed surface reports work state only. Model prose has exactly one
        # owner: the conversation transcript above this surface.
        main_height = height - 5
        if width >= 88:
            left_width = max(42, int(width * 0.54))
            right_width = width - left_width - 1
            left = self._live_panel(
                "ACTIVE WORK",
                self._live_task_entries(),
                left_width,
                main_height,
            )
            right = self._live_panel(
                "CURRENT ASSESSMENT",
                self._live_assessment_entries(),
                right_width,
                main_height,
                tail=True,
            )
            for left_row, right_row in zip(left, right, strict=True):
                rows.append((left_row[0] + " " + right_row[0], right_row[1] if right_row[1] != "normal" else left_row[1]))
        else:
            combined = [*self._live_task_entries(), *self._live_assessment_entries()]
            rows.extend(
                self._live_panel(
                    "COMPANY WORK",
                    combined,
                    width,
                    main_height,
                    tail=len(combined) > main_height - 2,
                )
            )

        footer = _fit_segments(
            (
                (110, self._live_result_strip),
                (
                    100,
                    "[ /view collapse ]"
                    if self._live_stage == "IDLE"
                    else "[ ctrl+o collapse ]",
                ),
                (95, self._live_result_status.lower()),
                (90, f"roster r{self._roster_revision}" if self._roster_revision else ""),
                (80, f"{len(self._live_tasks)} active task(s)"),
                (70, self._provider),
            ),
            max(1, width - 6),
        )
        rows.append((self._live_rule(width, footer, left="╰", right="╯"), "muted"))
        return tuple(self._live_style_line(text, tone, width) for text, tone in rows[:height])

    def _render_live_locked(self, *, force: bool = False) -> None:
        if not self._live_active:
            return
        terminal_width, terminal_height = self._live_size()
        target_rows = self._live_dock_height(terminal_height)
        if (
            terminal_height != self._live_physical_height
            or target_rows != self._live_reserved_rows
        ):
            self._reserve_live_rows_locked(target_rows)
        width = max(24, terminal_width - 1)
        height = self._live_reserved_rows
        size_changed = self._live_previous_size != (width, height, terminal_height)
        lines = self._live_frame(width, height)
        if len(lines) != height:
            raise RuntimeError("Live terminal dock must fill its reserved rows exactly")
        dock_start = max(1, terminal_height - height + 1)
        buffer = SYNC_START + HIDE_CURSOR
        previous = () if size_changed else self._live_previous_lines
        for index, line in enumerate(lines):
            old = previous[index] if index < len(previous) else None
            if not force and old == line:
                continue
            buffer += f"\x1b[{dock_start + index};1H\x1b[2K{line}"
        buffer += f"\x1b[{terminal_height};1H" + SYNC_END
        self._write(buffer)
        self._live_previous_lines = lines
        self._live_previous_size = (width, height, terminal_height)

    def _add_live_activity(self, message: str, *, tone: str = "normal") -> None:
        safe = " ".join(message.split()).strip()
        if not safe:
            return
        self._live_activity.append((f"{self._live_elapsed():>5}  {safe}", tone))
        del self._live_activity[:-40]

    def _live_task(self, event: ProductEvent) -> _LiveTask | None:
        if not event.task_id:
            return None
        task = self._live_tasks.get(event.task_id)
        if task is None:
            task = _LiveTask(
                task_id=event.task_id,
                employee=_short_identity(event.employee_id, "employee"),
                label=_short_identity(event.task_id, "task"),
            )
            self._live_tasks[event.task_id] = task
        elif event.employee_id:
            task.employee = _short_identity(event.employee_id, "employee")
        return task

    def begin_goal(self, goal: str, *, echo: bool = True) -> None:
        if not self._persistent_live_available():
            super().begin_goal(goal, echo=echo)
            return
        with self._lock:
            self._turn_started = time.monotonic()
            self._status_started = self._turn_started
            self._live_tick = 0
            self._live_stage = "ROUTING"
            self._live_status = "Routing request"
            self._live_goal = goal.strip()
            self._live_route = ""
            self._live_plan_mode = ""
            self._live_expected_tasks = 0
            self._live_tasks = {}
            self._live_activity = []
            self._live_result_status = ""
            self._live_result_strip = ""
            self._live_modal_title = ""
            self._live_modal_rows = []
            self._live_prompt = ""
            self._answer_stream_active = False
            self._answer_stream_parts = []
            self._answer_stream_mismatch = False
            self._add_live_activity("Goal received", tone="accent")
            self._enter_live_locked()

    def status(self, message: str) -> None:
        if not self._live_active or self._live_transcript_mode:
            super().status(message)
            return
        with self._lock:
            self._live_status = " ".join(message.split()).strip()
            self._render_live_locked()

    def clear_status(self) -> None:
        if not self._live_active or self._live_transcript_mode:
            super().clear_status()

    def commit(self, message: str, *, tone: str = "normal") -> None:
        if not self._live_active or self._live_transcript_mode:
            super().commit(message, tone=tone)
            return
        with self._lock:
            self._add_live_activity(message, tone=tone)
            self._render_live_locked()

    def handle_event(self, event: ProductEvent) -> None:
        if not self._live_active:
            super().handle_event(event)
            return
        if (
            event.type == ProductEventType.MODEL_STREAMING
            and self._live_route == "CONVERSATION"
            and event.data.get("stream_kind") == "text_delta"
            and str(event.data.get("text", ""))
        ):
            self.answer_delta(str(event.data["text"]))
            return
        with self._lock:
            if event.type == ProductEventType.INPUT_ROUTED:
                self._live_route = str(event.data.get("route", ""))
                roster_revision = event.data.get("roster_revision")
                employee_count = event.data.get("active_employee_count")
                if isinstance(roster_revision, int) and isinstance(employee_count, int):
                    self.set_roster(revision=roster_revision, active_employee_count=employee_count)
                if self._live_route == "CONVERSATION":
                    self._live_stage = "ANSWERING"
                    self._live_status = "Answering directly"
                    self._add_live_activity("Direct conversation · compiler skipped", tone="muted")
                else:
                    self._live_stage = "PLANNING"
                    self._live_status = "Planning the smallest sufficient company"
                    self._add_live_activity("Company goal route selected", tone="accent")
            elif event.type == ProductEventType.CAPABILITY_READY:
                self._live_status = "External read capability is available"
                self._add_live_activity("External read capability ready", tone="muted")
            elif event.type == ProductEventType.WORKSPACE_IDENTITY:
                status = str(event.data.get("status", "FAILED"))
                if status == "READY":
                    suffix = " · bounded" if event.data.get("truncated") is True else ""
                    self._live_status = "Workspace context identified"
                    self._add_live_activity(
                        f"Workspace identity ready{suffix}", tone="muted"
                    )
                else:
                    code = str(event.data.get("failure_code", "UNAVAILABLE"))
                    self._live_status = "Continuing without company learning context"
                    self._add_live_activity(
                        f"Workspace identity unavailable · {code}", tone="warning"
                    )
            elif event.type == ProductEventType.COMPILER_STARTED:
                self._live_stage = "COMPILING"
                self._live_status = "Analyzing dependencies and minimum staffing"
                self._add_live_activity("Compiler started dependency analysis", tone="accent")
            elif event.type == ProductEventType.PLAN_FALLBACK:
                self._live_plan_mode = str(event.data.get("mode", "SOLO_FALLBACK"))
                self._live_expected_tasks = int(event.data.get("task_count", 0) or 0)
                self._live_stage = "READY"
                self._live_status = "Using safe fallback plan"
                self._add_live_activity(f"Plan fallback · {event.message}", tone="warning")
            elif event.type == ProductEventType.PLAN_ACCEPTED:
                self._live_plan_mode = str(event.data.get("mode", "plan"))
                self._live_expected_tasks = int(event.data.get("task_count", 0) or 0)
                self._live_stage = "ANSWERING" if self._live_plan_mode == "DIRECT" else "READY"
                self._live_status = (
                    "Preparing direct response"
                    if self._live_plan_mode == "DIRECT"
                    else f"{self._live_plan_mode.lower()} plan accepted"
                )
                manager_context = ""
                if event.data.get("planning_owner_kind") == "PERSISTENT_MANAGER" and event.data.get(
                    "manager_planning_brief_digest"
                ):
                    manager_context = (
                        " · Manager context "
                        f"{int(event.data.get('manager_planning_skill_count', 0) or 0)} Skill(s)"
                    )
                self._add_live_activity(
                    f"{self._live_plan_mode.lower()} plan · {self._live_expected_tasks} task(s)"
                    + manager_context,
                    tone="accent",
                )
            elif event.type == ProductEventType.FIRM_ADMISSION:
                admitted = event.data.get("admitted") is True
                mode = str(event.data.get("effective_company_work_mode", "SOLO_JOB"))
                temporary = int(event.data.get("temporary_role_demand", 0) or 0)
                if self._live_route == "CONVERSATION":
                    self._live_stage = "ANSWERING"
                    self._live_status = "Preparing direct assignment"
                    if self.details:
                        self._add_live_activity(
                            f"Firm admission · {mode.lower()} · no workflow",
                            tone="muted",
                        )
                elif admitted:
                    self._live_stage = "ORGANIZING"
                    self._live_status = "Firm execution shape admitted"
                    self._add_live_activity(
                        f"Firm admitted · {mode.lower()} · {temporary} temporary",
                        tone="accent",
                    )
                else:
                    self._live_stage = "SAFE FAILURE"
                    self._live_status = "Firm admission denied"
                    self._add_live_activity(event.message, tone="warning")
            elif event.type == ProductEventType.ORGANIZATION_ADMISSION:
                admitted = event.data.get("admitted") is True
                capability = str(event.data.get("capability", "specialist"))
                if admitted:
                    self._live_stage = "ESCALATING"
                    self._live_status = f"Evaluating {capability} specialist placement"
                    self._add_live_activity(
                        f"Specialist need admitted · {capability}",
                        tone="accent",
                    )
                else:
                    self._live_status = "Continuing bounded solo execution"
                    if self.details:
                        self._add_live_activity(
                            f"Organization kept solo · {event.message}",
                            tone="muted",
                        )
            elif event.type == ProductEventType.TASK_ASSIGNED:
                role = str(event.data.get("employee_role", "employee"))
                tenure = str(event.data.get("employee_tenure", "persistent"))
                if self._live_route == "CONVERSATION":
                    self._live_stage = "ANSWERING"
                    self._live_status = f"{role} is answering directly"
                    if self.details:
                        self._add_live_activity(
                            f"Direct assignment · {role} · {tenure}",
                            tone="muted",
                        )
                else:
                    self._live_stage = "ORGANIZING"
                    task = self._live_task(event)
                    if task is not None:
                        task.employee = truncate_display(role, 32)
                        task.status = "assigned"
                        task.detail = tenure
                        self._live_status = f"{role} owns {task.label}"
                        self._add_live_activity(
                            f"{role} assigned {task.label} · {tenure}",
                            tone="accent",
                        )
            elif event.type == ProductEventType.GRAPH_PATCH_APPLIED:
                operation = str(event.data.get("semantic_operation", "UPDATE")).lower()
                self._live_stage = "REPLANNING"
                self._live_status = f"Execution structure {operation} applied"
                self._add_live_activity(event.message, tone="accent")
            elif event.type == ProductEventType.EMPLOYEE_STARTED:
                self._live_stage = "ANSWERING" if self._live_route == "CONVERSATION" else "EXECUTING"
                task = self._live_task(event)
                if task is not None:
                    task.status = "working"
                    task.detail = ""
                    self._live_status = f"{task.employee} is working"
                    self._add_live_activity(f"{task.employee} started {task.label}")
            elif event.type == ProductEventType.MODEL_WORKING:
                self._live_status = event.message
            elif event.type == ProductEventType.MODEL_STREAMING:
                self._live_status = (
                    "Receiving model response"
                    if event.data.get("stream_kind") == "text_delta"
                    else event.message
                )
            elif event.type in {
                ProductEventType.CONTEXT_COMPACTED,
                ProductEventType.TOOL_BATCH_PLANNED,
            }:
                self._live_status = event.message
                self._add_live_activity(event.message, tone="muted")
            elif event.type in {ProductEventType.TOOL_REQUESTED, ProductEventType.TOOL_RUNNING}:
                self._live_stage = "EXECUTING"
                task = self._live_task(event)
                tool = str(event.data.get("tool_name", "tool"))
                if task is not None:
                    task.status = "tool"
                    task.detail = tool
                self._live_status = event.message
                self._add_live_activity(event.message, tone="accent")
            elif event.type == ProductEventType.APPROVAL_REQUIRED:
                self._live_stage = "REVIEW"
                self._live_status = "Waiting for user review"
                self._add_live_activity("Approval required before a protected action", tone="warning")
            elif event.type == ProductEventType.APPROVAL_RESOLVED:
                denied = any(marker in event.message.lower() for marker in ("deny", "unavailable"))
                self._live_stage = "BLOCKED" if denied else "EXECUTING"
                self._live_status = event.message
                self._add_live_activity(event.message, tone="error" if denied else "success")
            elif event.type == ProductEventType.VALIDATION_RECORDED:
                passed = event.data.get("passed") is True
                task = self._live_task(event)
                if task is not None:
                    task.status = "working" if passed else "verifying"
                    task.detail = str(event.data.get("name", "validation"))
                self._live_stage = "VERIFYING"
                self._live_status = event.message
                self._add_live_activity(event.message, tone="success" if passed else "warning")
            elif event.type == ProductEventType.TOOL_FINISHED:
                failed = "failed" in event.message.lower()
                task = self._live_task(event)
                if task is not None:
                    task.status = "failed" if failed else "working"
                    task.detail = str(event.data.get("tool_name", "tool"))
                self._live_status = event.message
                self._add_live_activity(event.message, tone="error" if failed else "success")
            elif event.type == ProductEventType.EMPLOYEE_FINISHED:
                succeeded = "succeeded" in event.message.lower()
                task = self._live_task(event)
                if task is not None:
                    task.status = "succeeded" if succeeded else "failed"
                    terminal = event.data.get("terminal_summary")
                    summary = (
                        str(terminal.get("summary", "")).strip()
                        if isinstance(terminal, Mapping)
                        else ""
                    )
                    task.detail = truncate_display(summary, 72) if summary else (
                        "done" if succeeded else "needs review"
                    )
                self._live_status = event.message
                self._add_live_activity(event.message, tone="success" if succeeded else "warning")
            elif event.type in {ProductEventType.TASK_RETRY, ProductEventType.TASK_REROUTED}:
                task = self._live_task(event)
                if task is not None:
                    task.status = "retry" if event.type == ProductEventType.TASK_RETRY else "rerouted"
                    task.detail = f"attempt {int(event.data.get('target_attempt', 1) or 1)}"
                self._live_stage = "RECOVERING"
                self._live_status = event.message
                self._add_live_activity(event.message, tone="warning")
            elif event.type == ProductEventType.JOB_FINISHED:
                succeeded = event.data.get("status") == "SUCCEEDED"
                metrics = _job_metrics_text(event.data)
                detail = f"{event.message} · {metrics}" if metrics else event.message
                self._live_stage = "COMPLETE" if succeeded else "FAILED"
                self._live_result_status = "SUCCEEDED" if succeeded else "FAILED"
                self._live_status = detail
                self._add_live_activity(detail, tone="success" if succeeded else "error")
            self._render_live_locked()

    def answer_delta(self, delta: str) -> None:
        """Write a real provider delta to the transcript, never to the surface."""

        if not self._live_active:
            super().answer_delta(delta)
            return
        first_delta = not self._answer_stream_active
        with self._lock:
            self._live_stage = "RESPONDING"
            self._live_status = "Writing company response"
            thread = self._pause_live_runtime_locked() if first_delta else None
            if first_delta:
                self._render_live_locked()
        self._finish_live_runtime_pause(thread)
        with self._lock:
            if first_delta:
                self._begin_live_transcript_locked()
            super().answer_delta(delta)

    def answer(self, text: str) -> None:
        if not self._live_active:
            super().answer(text)
            return
        value = text.strip() or "No response was produced."
        if self._answer_stream_active:
            with self._lock:
                super().answer(value)
                self._end_live_transcript_locked()
                self._live_stage = (
                    "COMPLETE" if self._live_result_status != "FAILED" else "FAILED"
                )
                self._live_status = "Response written to conversation"
                self._render_live_locked(force=True)
            return
        with self._lock:
            self._live_stage = "RESPONDING"
            self._live_status = "Writing company response"
            thread = self._pause_live_runtime_locked()
            self._render_live_locked()
        self._finish_live_runtime_pause(thread)
        with self._lock:
            self._begin_live_transcript_locked()
        try:
            super().answer(value)
        finally:
            with self._lock:
                self._end_live_transcript_locked()
        with self._lock:
            self._live_stage = "COMPLETE" if self._live_result_status != "FAILED" else "FAILED"
            self._live_status = "Response written to conversation"
            self._render_live_locked()

    def result_details(self, result: Any) -> None:
        if not self._live_active:
            super().result_details(result)
            return
        usage = result.metrics.usage
        tokens = usage.input_tokens + usage.output_tokens
        succeeded = result.status.value == "SUCCEEDED"
        with self._lock:
            self._live_result_status = result.status.value
            self._live_result_strip = _fit_segments(
                (
                    (100, f"{'✓' if succeeded else '△'} {result.status.value.lower()}"),
                    (90, f"{result.metrics.unique_employee_count} staff"),
                    (80, f"{result.metrics.maximum_parallelism} parallel" if result.metrics.maximum_parallelism > 1 else ""),
                    (70, f"{usage.model_calls} model"),
                    (60, f"{usage.tool_calls} tool" if usage.tool_calls else ""),
                    (50, f"{_compact_number(tokens)} tok"),
                ),
                88,
            )
            for issue in result.unresolved_issues:
                self._add_live_activity(f"Unresolved · {issue}", tone="warning")
            self._live_stage = "COMPLETE" if succeeded else "FAILED"
            self._live_status = "Result written to conversation"
            self._render_live_locked()
        with self._lock:
            thread = self._pause_live_runtime_locked()
        self._finish_live_runtime_pause(thread)
        with self._lock:
            self._begin_live_transcript_locked()
        try:
            super().result_details(result)
        finally:
            with self._lock:
                self._end_live_transcript_locked()
                self._prepare_live_idle_locked()
                self._render_live_locked(force=True)

    def ask_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        if not self._live_active:
            return super().ask_approval(request)
        with self._lock:
            self._live_stage = "REVIEW"
            self._live_status = "Opening approval review"
            self._add_live_activity("Approval opened above the fixed surface", tone="warning")
            thread = self._pause_live_runtime_locked()
            self._render_live_locked()
        self._finish_live_runtime_pause(thread)
        with self._lock:
            self._begin_live_transcript_locked()
        try:
            decision = super().ask_approval(request)
        finally:
            with self._lock:
                self._end_live_transcript_locked()
        with self._lock:
            self._live_status = "Approval decision recorded"
            self._enter_live_locked()
        return decision

    def close(self) -> None:
        with self._lock:
            self._exit_live_locked()
        super().close()
