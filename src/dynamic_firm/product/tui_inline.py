"""Scrollback-safe inline Product Terminal UI."""

from __future__ import annotations

import shutil
import threading
import time
from typing import Any, Mapping, TextIO

from dynamic_firm.runtime.models import Usage

from .events import ProductEvent, ProductEventType
from .terminal import (
    FrameRow,
    display_width,
    frame_lines,
    hard_wrap_display,
    pad_display,
    truncate_display,
    wrap_display,
)
from .tui_constants import (
    ASCII_WORDMARK as _ASCII_WORDMARK,
    BOLD,
    BRAND_BLUE,
    BRAND_CYAN,
    BRAND_INDIGO,
    BRAND_PURPLE,
    BRAND_VIOLET,
    CLEAR_LINE,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    RED,
    RESET,
    SPINNER as _SPINNER,
    SYNC_END,
    SYNC_START,
    WORDMARK_GRADIENT as _WORDMARK_GRADIENT,
    YELLOW,
)
from .tui_interactions import InlineTerminalInteractionMixin
from .tui_primitives import (
    _compact_number,
    _compact_path,
    _compact_session,
    _duration,
    _fit_segments,
    _isatty,
    _job_metrics_text,
    _short_identity,
)



class InlineTerminalUI(InlineTerminalInteractionMixin):
    """Width-aware, scrollback-safe terminal product surface."""

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
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        detected_tty = _isatty(stdin) and _isatty(stdout)
        self.interactive = detected_tty if interactive is None else interactive
        self.color = self.interactive if color is None else color
        self.animations = detected_tty if animations is None else animations
        self.details = details
        self.plain = plain
        self._terminal_width = terminal_width
        self._status_active = False
        self._status_message = ""
        self._status_started = 0.0
        self._turn_started = 0.0
        self._status_tick = 0
        self._status_generation = 0
        self._spinner_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._workspace = ""
        self._session_id = ""
        self._model = ""
        self._provider = ""
        self._authority = ""
        self._version = ""
        self._roster_revision = 0
        self._active_employee_count = 0
        self._employee_roles: tuple[str, ...] = ()
        self._capabilities: tuple[str, ...] = ()
        self._tool_names: tuple[str, ...] = ()
        self._route = ""
        self._activity_started = False
        self._answer_stream_active = False
        self._answer_stream_parts: list[str] = []
        self._answer_stream_mismatch = False
        self._readline: Any | None = None
        self._previous_completer: Any | None = None
        self._readline_auto_history_disabled = False
        self._install_readline()

    def _style(self, text: str, *styles: str) -> str:
        return "".join(styles) + text + RESET if self.color and not self.plain and styles else text

    def _write(self, value: str) -> None:
        self.stdout.write(value)
        self.stdout.flush()

    def _width(self) -> int:
        if self._terminal_width is not None:
            return max(40, min(120, self._terminal_width))
        try:
            return max(40, min(120, shutil.get_terminal_size((88, 24)).columns))
        except Exception:
            return 88

    def _rule(self, label: str = "") -> str:
        width = self._width()
        safe = truncate_display(f" {label.strip()} " if label else "", width)
        return safe + "─" * max(0, width - display_width(safe))

    def _write_frame(
        self,
        title: str,
        rows: tuple[FrameRow, ...],
        *,
        footer: str = "",
        tone: str = "accent",
        dim_body: bool = False,
    ) -> None:
        if self.plain:
            if title:
                self._write(title + "\n")
            for row in rows:
                if row.divider:
                    if row.text:
                        self._write(row.text + "\n")
                    continue
                for line in wrap_display(row.text, self._width()):
                    self._write(line + "\n")
            if footer:
                self._write(footer + "\n")
            return
        styles = {
            "accent": (CYAN,),
            "warning": (YELLOW,),
            "error": (RED,),
            "success": (GREEN,),
            "muted": (DIM,),
        }.get(tone, ())
        lines = frame_lines(title, rows, self._width(), footer=footer)
        for index, line in enumerate(lines):
            is_border = index in {0, len(lines) - 1} or line.startswith("├")
            if is_border:
                self._write(self._style(line, *styles) + "\n")
            elif dim_body:
                self._write(self._style(line, DIM) + "\n")
            else:
                self._write(line + "\n")

    def _clear_status_locked(self) -> None:
        self._status_generation += 1
        if self._status_active and self.interactive:
            self._write(SYNC_START + CLEAR_LINE + SYNC_END)
        self._status_active = False
        self._status_message = ""

    def clear_status(self) -> None:
        with self._lock:
            self._clear_status_locked()

    def _status_text_locked(self) -> str:
        frame = _SPINNER[self._status_tick % len(_SPINNER)]
        elapsed = _duration(time.monotonic() - (self._turn_started or self._status_started))
        model = f" · {self._model}" if self._model else ""
        return truncate_display(f"{frame} {self._status_message} · {elapsed}{model}", self._width())

    def _paint_status_locked(self) -> None:
        value = self._style(self._status_text_locked(), DIM, CYAN)
        self._write(SYNC_START + CLEAR_LINE + value + SYNC_END)
        self._status_active = True

    def _animate_status(self, generation: int) -> None:
        while True:
            time.sleep(0.1)
            with self._lock:
                if not self._status_active or generation != self._status_generation:
                    return
                self._status_tick += 1
                self._paint_status_locked()

    def status(self, message: str) -> None:
        if not self.interactive or self.plain:
            return
        safe = " ".join(message.split()).strip()
        with self._lock:
            start_animation = not self._status_active
            self._status_message = safe
            self._status_started = self._status_started or time.monotonic()
            self._paint_status_locked()
            if self.animations and start_animation:
                generation = self._status_generation
                self._spinner_thread = threading.Thread(
                    target=self._animate_status,
                    args=(generation,),
                    name="noruct-tui-status",
                    daemon=True,
                )
                self._spinner_thread.start()

    def commit(self, message: str, *, tone: str = "normal") -> None:
        with self._lock:
            self._clear_status_locked()
            styles = {
                "success": (GREEN,),
                "warning": (YELLOW,),
                "error": (RED,),
                "muted": (DIM,),
                "accent": (MAGENTA,),
            }.get(tone, ())
            for line in wrap_display(message, self._width()):
                self._write(self._style(line, *styles) + "\n")

    def _write_company_badge(self) -> None:
        label = "COMPANY ONLINE"
        top = "╭" + "─" * (display_width(label) + 4) + "╮"
        bottom = "╰" + "─" * (display_width(label) + 4) + "╯"
        self._write(self._style(top, BRAND_PURPLE) + "\n")
        if self.color:
            self._write(
                self._style("│ ", BRAND_PURPLE)
                + self._style("◆", BOLD, BRAND_CYAN)
                + " "
                + self._style(label, BOLD)
                + self._style(" │", BRAND_PURPLE)
                + "\n"
            )
        else:
            self._write(f"│ ◆ {label} │\n")
        self._write(self._style(bottom, BRAND_PURPLE) + "\n")

    def _write_full_brand(
        self,
        *,
        workspace: str,
        session_id: str | None,
        model: str,
        provider: str,
        authority: str,
        version: str,
        roster_revision: int,
        active_employee_count: int,
        employee_roles: tuple[str, ...],
        capabilities: tuple[str, ...],
        tools: tuple[str, ...],
    ) -> None:
        self._write_company_badge()
        self._write("\n")
        for line, color in zip(_ASCII_WORDMARK, _WORDMARK_GRADIENT, strict=True):
            self._write("  " + self._style(line, BOLD, color) + "\n")
        self._write("\n")
        rows: list[FrameRow] = [
            FrameRow(f"NORUCT {version} · Dynamic Firm Runtime".strip()),
            FrameRow("company", divider=True),
        ]
        if roster_revision:
            rows.append(
                FrameRow(f"roster        r{roster_revision} · {active_employee_count} active")
            )
        if employee_roles:
            rows.append(FrameRow("employees     " + " · ".join(employee_roles)))
        if capabilities:
            rows.append(FrameRow("capabilities  " + " · ".join(capabilities)))
        rows.extend(
            (
                FrameRow("execution", divider=True),
                FrameRow(f"model         {model or 'default model'}"),
                FrameRow(f"via           {provider or 'configured provider'}"),
                FrameRow(f"authority     {authority or 'read-only'}"),
            )
        )
        if tools:
            rows.append(FrameRow("tools         " + " · ".join(tools)))
        rows.extend(
            (
                FrameRow("workflow      compiled work · dependencies · ready-set parallelism"),
                FrameRow("limits        6 tasks · 3 parallel · 1 replan · 2 temporary roles"),
                FrameRow("session", divider=True),
                FrameRow(
                    f"workspace     {_compact_path(workspace, max(8, self._width() - 17))}"
                ),
            )
        )
        if session_id:
            rows.append(FrameRow(f"session       {session_id[:12]}"))
        footer = _fit_segments(
            (
                (100, "company ready"),
                (90, "/model"),
                (80, "/help"),
                (60, "/details"),
            ),
            max(1, self._width() - 6),
        )
        self._write_frame(
            "company snapshot",
            tuple(rows),
            footer=("* approval required · " if any(item.endswith("*") for item in tools) else "")
            + footer,
        )
        self._write("\n")

    def banner(
        self,
        *,
        workspace: str,
        session_id: str | None = None,
        model: str = "",
        provider: str = "",
        authority: str = "",
        version: str = "",
        roster_revision: int = 0,
        active_employee_count: int = 0,
        employee_roles: tuple[str, ...] = (),
        capabilities: tuple[str, ...] = (),
        tools: tuple[str, ...] = (),
    ) -> None:
        self.clear_status()
        self._workspace = workspace
        self._session_id = session_id or ""
        self._model = model
        self._provider = provider
        self._authority = authority
        self._version = version
        self._employee_roles = employee_roles
        self._capabilities = capabilities
        self._tool_names = tools
        self.set_roster(
            revision=roster_revision,
            active_employee_count=active_employee_count,
        )
        if self.plain:
            self._write(f"Noruct {version}".strip() + "\n")
            self._write("Dynamic Firm Runtime\n")
            self._write(f"model {model or 'default model'} · {provider or 'configured provider'}\n")
            self._write(f"authority {authority or 'read-only'}\n")
            self._write(f"workspace {workspace}\n")
            if roster_revision:
                self._write(
                    f"roster r{roster_revision} · {active_employee_count} active\n"
                )
            if session_id:
                self._write(f"session {session_id[:12]}\n")
            return
        if self._width() >= 58:
            self._write_full_brand(
                workspace=workspace,
                session_id=session_id,
                model=model,
                provider=provider,
                authority=authority,
                version=version,
                roster_revision=roster_revision,
                active_employee_count=active_employee_count,
                employee_roles=employee_roles,
                capabilities=capabilities,
                tools=tools,
            )
            return
        inner = self._width() - 4
        product = model or "default model"
        rows: list[FrameRow] = [
            FrameRow(f"◆ NORUCT {version}".strip(), wrap=False),
            FrameRow("Dynamic Firm Runtime", wrap=False),
            FrameRow("session", divider=True),
            FrameRow(f"model      {product}"),
        ]
        if provider:
            rows.append(FrameRow(f"backend    {provider}"))
        rows.extend(
            (
                FrameRow(f"authority  {authority or 'read-only'}"),
                FrameRow(f"workspace  {_compact_path(workspace, max(8, inner - 11))}"),
            )
        )
        if roster_revision:
            rows.append(
                FrameRow(
                    f"roster     r{roster_revision} · "
                    f"{active_employee_count} active"
                )
            )
        if employee_roles:
            rows.append(FrameRow("employees  " + " · ".join(employee_roles)))
        if capabilities:
            rows.append(FrameRow("capabilities  " + " · ".join(capabilities)))
        if tools:
            rows.append(FrameRow("tools      " + " · ".join(tools)))
        if session_id:
            rows.append(FrameRow(f"session    {session_id[:12]}"))
        footer = _fit_segments(
            (
                (100, "company ready"),
                (90, "/model"),
                (80, "/help"),
                (60, "/details"),
            ),
            max(1, self._width() - 6),
        )
        self._write_frame(
            "company snapshot",
            tuple(rows),
            footer=footer,
        )
        self._write("\n")

    def _input_rules(self) -> tuple[str, str]:
        footer = _fit_segments(
            (
                (100, self._model or "default model"),
                (90, "/model"),
                (70, "\\ multiline"),
                (60, "/help"),
            ),
            max(1, self._width() - 6),
        )
        lines = frame_lines(
            "",
            (),
            self._width(),
            footer=footer,
        )
        return lines[0], lines[-1]

    def _write_goal_card(self, goal: str) -> None:
        if self.plain:
            self.commit(f"> {goal}")
            return
        self._write_frame(
            "",
            (FrameRow(f"❯ {goal}"),),
            footer=_fit_segments(
                (
                    (100, self._model or "default model"),
                    (90, "/model"),
                    (60, "routing automatically"),
                ),
                max(1, self._width() - 6),
            ),
            tone="accent",
        )

    def begin_goal(self, goal: str, *, echo: bool = True) -> None:
        self._turn_started = time.monotonic()
        self._status_started = self._turn_started
        self._status_tick = 0
        self._route = ""
        self._activity_started = False
        self._answer_stream_active = False
        self._answer_stream_parts = []
        self._answer_stream_mismatch = False
        if echo:
            self._write_goal_card(goal)
        self.status("Routing request")

    def _ensure_activity(self) -> None:
        if self._activity_started:
            return
        self.clear_status()
        self._write("\n")
        self.commit("◆ Company plan", tone="accent")
        self._activity_started = True

    def _activity(self, message: str, *, tone: str = "muted") -> None:
        styles = {
            "success": (GREEN,),
            "warning": (YELLOW,),
            "error": (RED,),
            "muted": (DIM,),
            "accent": (MAGENTA,),
        }.get(tone, ())
        if self.plain:
            message = message.replace("├─ ", "").replace("└─ ", "").replace("│  ", "")
        width = max(1, self._width() - (0 if self.plain else 3))
        with self._lock:
            self._clear_status_locked()
            for index, line in enumerate(wrap_display(message, width)):
                prefix = "" if index == 0 or self.plain else "│  "
                self._write(self._style(prefix + line, *styles) + "\n")

    def _event_card(
        self,
        title: str,
        rows: tuple[FrameRow, ...],
        *,
        footer: str = "",
        tone: str = "muted",
    ) -> None:
        self.clear_status()
        self._write("\n")
        self._write_frame(title, rows, footer=footer, tone=tone)

    def handle_event(self, event: ProductEvent) -> None:
        if event.type == ProductEventType.INPUT_ROUTED:
            route = str(event.data.get("route", ""))
            self._route = route
            roster_revision = event.data.get("roster_revision")
            active_employee_count = event.data.get("active_employee_count")
            if isinstance(roster_revision, int) and isinstance(active_employee_count, int):
                self.set_roster(
                    revision=roster_revision,
                    active_employee_count=active_employee_count,
                )
            self.status("Answering directly" if route == "CONVERSATION" else "Planning the company")
            if self.details:
                self.commit(f"◇ Route · {route.lower().replace('_', ' ')}", tone="muted")
        elif event.type == ProductEventType.WORKSPACE_IDENTITY:
            status = str(event.data.get("status", "FAILED"))
            if status == "READY":
                suffix = " · bounded" if event.data.get("truncated") is True else ""
                self.commit(f"◇ Workspace identity ready{suffix}", tone="muted")
                self.status("Workspace context identified")
            else:
                code = str(event.data.get("failure_code", "UNAVAILABLE"))
                self.commit(f"△ Workspace identity unavailable · {code}", tone="warning")
                self.status("Continuing without company learning context")
        elif event.type == ProductEventType.CAPABILITY_READY:
            self.commit("◇ External read capability ready", tone="muted")
            self.status("External read capability is available")
        elif event.type == ProductEventType.COMPILER_STARTED:
            self._ensure_activity()
            self._activity("├─ ◇ Compiler · finding the smallest sufficient company")
            self.status("Compiling the smallest sufficient company")
        elif event.type == ProductEventType.PLAN_FALLBACK:
            self._ensure_activity()
            self._activity(f"├─ △ Plan fallback · {event.message}", tone="warning")
        elif event.type == ProductEventType.PLAN_ACCEPTED:
            mode = str(event.data.get("mode", "plan")).lower().replace("_", " ")
            count = int(event.data.get("task_count", 0) or 0)
            owner_kind = str(event.data.get("planning_owner_kind", ""))
            if mode == "direct":
                if self.details:
                    self.commit("◆ Direct response · compiler skipped", tone="muted")
                self.status("Preparing response")
            else:
                self._ensure_activity()
                manager_context = ""
                if owner_kind == "PERSISTENT_MANAGER" and event.data.get(
                    "manager_planning_brief_digest"
                ):
                    skill_count = int(
                        event.data.get("manager_planning_skill_count", 0) or 0
                    )
                    outcome_count = int(
                        event.data.get("manager_planning_outcome_count", 0) or 0
                    )
                    manager_context = (
                        f" · context {skill_count} Skill(s) / {outcome_count} outcome(s)"
                    )
                self._activity(
                    (
                        f"├─ ◆ Plan · {mode} · {count} task{'s' if count != 1 else ''}"
                        + (
                            " · Manager staffing"
                            if owner_kind == "PERSISTENT_MANAGER"
                            else ""
                        )
                        + manager_context
                    ),
                    tone="accent",
                )
                self.status("Company plan ready")
        elif event.type == ProductEventType.FIRM_ADMISSION:
            admitted = event.data.get("admitted") is True
            mode = str(event.data.get("effective_company_work_mode", "SOLO_JOB"))
            temporary = int(event.data.get("temporary_role_demand", 0) or 0)
            delegated = int(event.data.get("manager_delegation_task_count", 0) or 0)
            if self._route == "CONVERSATION":
                if self.details:
                    self.commit(
                        f"◇ Firm admission · {mode.lower()} · no workflow",
                        tone="muted",
                    )
                self.status("Preparing direct assignment")
            elif admitted:
                self._ensure_activity()
                self._activity(
                    (
                        f"├─ ◆ Firm admitted · {mode.lower()} · {delegated} delegated "
                        f"· {temporary} temporary"
                    ),
                    tone="accent",
                )
                self.status("Firm execution shape admitted")
            else:
                self.commit(f"△ Firm admission denied · {event.message}", tone="warning")
        elif event.type == ProductEventType.ORGANIZATION_ADMISSION:
            admitted = event.data.get("admitted") is True
            capability = str(event.data.get("capability", "specialist"))
            if admitted:
                self._ensure_activity()
                self._activity(
                    f"├─ ◆ Specialist need admitted · {capability}",
                    tone="accent",
                )
                self.status(f"Evaluating {capability} specialist placement")
            elif self.details:
                self.commit(f"◇ Organization kept solo · {event.message}", tone="muted")
        elif event.type == ProductEventType.TASK_ASSIGNED:
            role = str(event.data.get("employee_role", "employee"))
            tenure = str(event.data.get("employee_tenure", "persistent"))
            task = _short_identity(event.task_id, "task")
            if self._route == "CONVERSATION":
                if self.details:
                    self.commit(
                        f"◆ Direct assignment · {role} · {tenure}",
                        tone="muted",
                    )
                self.status(f"{role} is answering directly")
                return
            self._ensure_activity()
            self._activity(f"├─ ◆ {role} assigned · {task} · {tenure}", tone="accent")
            self.status(f"{role} owns {task}")
        elif event.type == ProductEventType.GRAPH_PATCH_APPLIED:
            self._ensure_activity()
            operation = str(event.data.get("semantic_operation", "UPDATE")).lower()
            self._activity(f"├─ ◆ {event.message}", tone="accent")
            self.status(f"Execution structure {operation} applied")
        elif event.type == ProductEventType.EMPLOYEE_STARTED:
            if self._route == "CONVERSATION":
                self.status("Preparing response")
                return
            self._ensure_activity()
            employee = _short_identity(event.employee_id, "employee")
            task = _short_identity(event.task_id, "task")
            self._activity(f"├─ ◇ {employee} · {task}")
            self.status(f"{employee} is working")
        elif event.type == ProductEventType.MODEL_WORKING:
            self.status(event.message)
        elif event.type == ProductEventType.MODEL_STREAMING:
            delta = str(event.data.get("text", ""))
            if (
                self._route == "CONVERSATION"
                and event.data.get("stream_kind") == "text_delta"
                and delta
            ):
                self.answer_delta(delta)
            else:
                self.status(
                    "Receiving model response"
                    if event.data.get("stream_kind") == "text_delta"
                    else event.message
                )
        elif event.type in {
            ProductEventType.CONTEXT_COMPACTED,
            ProductEventType.TOOL_BATCH_PLANNED,
        }:
            if self.details:
                self.commit(f"│  ◇ {event.message}", tone="muted")
            self.status(event.message)
        elif event.type == ProductEventType.TOOL_REQUESTED:
            if self.details:
                self.commit(f"│  ◇ {event.message}", tone="muted")
            self.status(event.message)
        elif event.type == ProductEventType.TOOL_RUNNING:
            self.status(event.message)
        elif event.type == ProductEventType.APPROVAL_REQUIRED:
            self.status("Review required before continuing")
        elif event.type == ProductEventType.APPROVAL_RESOLVED:
            denied = any(marker in event.message.lower() for marker in ("deny", "unavailable"))
            self._activity(
                f"│  {'×' if denied else '✓'} {event.message}",
                tone="error" if denied else "success",
            )
        elif event.type == ProductEventType.VALIDATION_RECORDED:
            passed = event.data.get("passed") is True
            attempt = int(event.data.get("attempt", 1) or 1)
            name = str(event.data.get("name", "validation"))
            detail = str(event.data.get("detail", "")).strip()
            rows = [
                FrameRow(f"{'✓' if passed else '×'} {name}"),
                FrameRow(f"attempt  {attempt}"),
            ]
            if detail:
                rows.append(FrameRow(f"detail   {detail}"))
            self._event_card(
                f"VALIDATION · {'PASS' if passed else 'FAIL'}",
                tuple(rows),
                footer=_short_identity(event.task_id, "task"),
                tone="success" if passed else "error",
            )
        elif event.type == ProductEventType.TOOL_FINISHED:
            failed = "failed" in event.message.lower()
            tool = str(event.data.get("tool_name", "tool"))
            output_bytes = int(event.data.get("output_bytes", 0) or 0)
            error_code = str(event.data.get("error_code", "") or "")
            rows = [FrameRow(f"{'×' if failed else '✓'} {tool}")]
            if error_code:
                rows.append(FrameRow(f"error    {error_code}"))
            elif output_bytes:
                rows.append(FrameRow(f"output   {output_bytes:,} bytes"))
            self._event_card(
                f"TOOL · {'FAILED' if failed else 'DONE'}",
                tuple(rows),
                footer=_short_identity(event.task_id, "task"),
                tone="error" if failed else "success",
            )
        elif event.type == ProductEventType.EMPLOYEE_FINISHED:
            if self._route == "CONVERSATION":
                self.clear_status()
                return
            succeeded = "succeeded" in event.message.lower()
            employee = _short_identity(event.employee_id, "employee")
            task = _short_identity(event.task_id, "task")
            self._activity(
                f"{'└─' if succeeded else '├─'} "
                f"{'✓' if succeeded else '△'} {employee} · {task}",
                tone="success" if succeeded else "warning",
            )
        elif event.type in {
            ProductEventType.TASK_RETRY,
            ProductEventType.TASK_REROUTED,
        }:
            task = _short_identity(event.task_id, "task")
            target_attempt = int(event.data.get("target_attempt", 1) or 1)
            failure_kind = str(event.data.get("failure_kind", "failure")).lower().replace(
                "_", " "
            )
            if event.type == ProductEventType.TASK_RETRY:
                employee = _short_identity(event.employee_id, "employee")
                self._activity(
                    f"├─ ↻ Retry · {task} · attempt {target_attempt} · {employee} · {failure_kind}",
                    tone="warning",
                )
            else:
                previous = _short_identity(
                    str(event.data.get("from_employee_id", "employee")), "employee"
                )
                employee = _short_identity(event.employee_id, "employee")
                self._activity(
                    f"├─ ↪ Reroute · {task} · {previous} → {employee} · "
                    f"attempt {target_attempt} · {failure_kind}",
                    tone="warning",
                )
            self.status(f"Recovering from {failure_kind}")
        elif event.type == ProductEventType.JOB_FINISHED:
            succeeded = event.data.get("status") == "SUCCEEDED"
            metrics = _job_metrics_text(event.data)
            metric_suffix = f" · {metrics}" if metrics else ""
            if succeeded and self._activity_started:
                self.commit(f"✓ Company execution complete{metric_suffix}", tone="success")
            elif succeeded:
                self.clear_status()
            else:
                self._ensure_activity()
                self.commit(f"× {event.message}{metric_suffix}", tone="error")

    def answer_delta(self, delta: str) -> None:
        """Append one canonical direct-answer delta to the sole transcript lane."""

        if not delta:
            return
        with self._lock:
            self._clear_status_locked()
            if not self._answer_stream_active:
                self._answer_stream_active = True
                self._answer_stream_parts = []
                self._answer_stream_mismatch = False
                if not self.plain:
                    self._write("\n")
                    self._write(self._style("● Noruct", MAGENTA) + "\n  ")
            self._answer_stream_parts.append(delta)
            self._write(delta)

    def answer(self, text: str) -> None:
        self.clear_status()
        value = text.strip() or "No response was produced."
        if self._answer_stream_active:
            with self._lock:
                streamed = "".join(self._answer_stream_parts)
                if value.startswith(streamed):
                    self._write(value[len(streamed) :])
                elif streamed.strip() != value:
                    # A provider protocol mismatch cannot be erased from terminal
                    # scrollback. Keep one owner/label and append the canonical result
                    # in that same lane while recording the condition for tests/support.
                    self._answer_stream_mismatch = True
                    self._write("\n  △ canonical result\n  " + value)
                if not value.endswith("\n"):
                    self._write("\n")
                self._answer_stream_active = False
                self._answer_stream_parts = []
            return
        if self.plain:
            self._write(value + "\n")
            return
        self._write("\n")
        self.commit("● Noruct", tone="accent")
        width = max(1, self._width() - 2)
        for line in wrap_display(value, width):
            self._write("  " + line + "\n")

    def result_details(self, result: Any) -> None:
        usage = result.metrics.usage
        status = result.status.value.lower().replace("_", " ")
        succeeded = result.status.value == "SUCCEEDED"
        elapsed = _duration(time.monotonic() - self._turn_started) if self._turn_started else "0s"
        changed = tuple(
            item.removeprefix("Applied shadow change: ")
            for item in result.acceptance_evidence
            if item.startswith("Applied shadow change: ")
        )
        if changed:
            self._event_card(
                f"CHANGES · {len(changed)} FILE{'S' if len(changed) != 1 else ''}",
                (FrameRow("changed  " + ", ".join(changed)),),
                tone="success",
            )
        if result.unresolved_issues:
            self._event_card(
                "UNRESOLVED",
                tuple(FrameRow(f"• {item}") for item in result.unresolved_issues),
                tone="warning",
            )
        if self.details and result.acceptance_evidence:
            self._event_card(
                "EVIDENCE",
                tuple(FrameRow(f"• {item}") for item in result.acceptance_evidence),
                tone="muted",
            )
        tokens = usage.input_tokens + usage.output_tokens
        footer = _fit_segments(
            (
                (100, f"{'✓' if succeeded else '△'} {status}"),
                (
                    95,
                    f"session {_compact_session(self._session_id)}"
                    if self._session_id
                    else "one-shot",
                ),
                (90, self._authority or "read-only"),
                (75, f"{result.metrics.unique_employee_count} staff"),
                (
                    65,
                    f"{result.metrics.maximum_parallelism} parallel"
                    if result.metrics.maximum_parallelism > 1
                    else "",
                ),
                (60, f"{usage.model_calls} model"),
                (45, f"{usage.tool_calls} tool" if usage.tool_calls else ""),
                (50, f"{_compact_number(tokens)} tok"),
                (92, elapsed),
                (30, self._model),
            ),
            max(1, self._width() - (0 if self.plain else 2)),
        )
        prefix = "" if self.plain else "  "
        self.commit(prefix + footer, tone="success" if succeeded else "warning")
