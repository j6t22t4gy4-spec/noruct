"""Interactive composer, picker, and approval behavior for the inline TUI.

The host UI remains responsible for output, status, and Product event state. This
mixin only composes those host operations into operator interaction flows.
"""

from __future__ import annotations

import os
import sys
from typing import Any, TextIO

from dynamic_firm.product.models import ModelOption, filter_model_options
from dynamic_firm.runtime.models import ApprovalDecision, ApprovalRequest, Usage

from .terminal import FrameRow, hard_wrap_display
from .tui_constants import BOLD, CLEAR_SCREEN, CYAN, RESET, SLASH_COMMANDS as _SLASH_COMMANDS


def _is_real_tty(stream: TextIO) -> bool:
    try:
        return os.isatty(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False

def _usage_text(usage: Usage) -> str:
    tokens = usage.input_tokens + usage.output_tokens
    cached = f" · {usage.cached_input_tokens:,} cached" if usage.cached_input_tokens else ""
    cost = f" · ${usage.cost_usd:.4f}" if usage.cost_usd else ""
    return (
        f"{usage.model_calls} model call{'s' if usage.model_calls != 1 else ''}"
        f" · {usage.tool_calls} tool call{'s' if usage.tool_calls != 1 else ''}"
        f" · {tokens:,} tokens{cached}{cost}"
    )


class InlineTerminalInteractionMixin:
    def _install_readline(self) -> None:
        if not self.interactive or self.stdin is not sys.stdin:
            return
        try:
            import readline
        except ImportError:
            return
        self._readline = readline
        self._previous_completer = readline.get_completer()

        def complete(text: str, state: int) -> str | None:
            matches = [item for item in _SLASH_COMMANDS if item.startswith(text)]
            return matches[state] if state < len(matches) else None

        readline.set_completer(complete)
        try:
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass
        try:
            # Add one completed Noruct goal below instead of recording every
            # physical line of a trailing-backslash multiline input.
            readline.set_auto_history(False)
            self._readline_auto_history_disabled = True
        except (AttributeError, TypeError):
            pass

    def _uses_readline_editor(self) -> bool:
        """Use one owner for the logical input buffer and terminal cursor."""

        return (
            self._readline is not None
            and self.stdin is sys.stdin
            and self.stdout is sys.stdout
            and _is_real_tty(self.stdin)
            and _is_real_tty(self.stdout)
        )

    def _readline_prompt(self, text: str, *styles: str) -> str:
        """Mark ANSI bytes as zero-width where the readline backend supports it."""

        if not (self.color and not self.plain and styles):
            return text
        documentation = str(getattr(self._readline, "__doc__", "") or "").lower()
        if "libedit" in documentation:
            # macOS libedit reorders marked ANSI spans during redisplay. A plain
            # four-cell prompt keeps wrap/backspace calculations exact.
            return text
        prefix = "".join(styles)
        return f"\001{prefix}\002{text}\001{RESET}\002"

    def _read_goal_line(self, prompt: str) -> str | None:
        if self._uses_readline_editor():
            try:
                return input(self._readline_prompt(prompt, BOLD, CYAN))
            except EOFError:
                return None
        self._write(self._style(prompt, BOLD, CYAN))
        value = self.stdin.readline()
        return None if value == "" else value.rstrip("\r\n")

    def seed_input_history(self, values: tuple[str, ...]) -> None:
        """Replace this terminal's editor history with one session's goals.

        ``readline`` is process-global, so session changes must replace rather
        than append history.  This keeps another Company's goals out of the
        active composer and prevents a recalled entry from being silently
        truncated.  Non-interactive streams intentionally retain no editor
        state.
        """

        if not self._uses_readline_editor() or self._readline is None:
            return
        retained: list[str] = []
        for value in values[-100:]:
            normalized = value.strip()
            if not normalized or len(normalized.encode("utf-8")) > 8_000:
                continue
            if normalized in retained:
                retained.remove(normalized)
            retained.append(normalized)
        try:
            self._readline.clear_history()
            for value in retained:
                self._readline.add_history(value)
        except Exception:
            # The editor is optional product polish; the Company ledger is
            # still canonical when a platform readline implementation fails.
            return

    def show_usage(self, usage: Usage, *, label: str = "Session usage") -> None:
        self.clear_status()
        self._write_frame(label, (FrameRow(_usage_text(usage)),), tone="muted")

    def show_status(
        self,
        *,
        session_id: str,
        turn_count: int,
        usage: Usage,
        review_mode: str | None = None,
    ) -> None:
        self.clear_status()
        rows = (
            FrameRow(f"session    {session_id[:12]} · {turn_count} turn(s)"),
            FrameRow(f"model      {self._model or 'default'}"),
            FrameRow(f"backend    {self._provider or 'configured provider'}"),
            FrameRow(f"authority  {self._authority or 'read-only'}"),
            FrameRow(f"review     retention={review_mode or 'approval'}"),
            FrameRow("           employee skill=approval only"),
            FrameRow(
                f"roster     r{self._roster_revision} · "
                f"{self._active_employee_count} active"
                if self._roster_revision
                else "roster     unavailable"
            ),
            FrameRow(f"workspace  {self._workspace}"),
            FrameRow(f"details    {'expanded' if self.details else 'collapsed'}"),
            FrameRow("usage", divider=True),
            FrameRow(_usage_text(usage)),
        )
        self._write_frame("Status", rows, footer="esc not needed · returns immediately", tone="muted")

    def set_roster(self, *, revision: int, active_employee_count: int) -> None:
        if revision < 0 or active_employee_count < 0:
            raise ValueError("Roster revision and active employee count cannot be negative")
        self._roster_revision = revision
        self._active_employee_count = active_employee_count

    def show_help(self) -> None:
        self.clear_status()
        rows = (
            FrameRow("Conversation", divider=True),
            FrameRow("/new       begin a new company session"),
            FrameRow("/sessions  list recent company sessions"),
            FrameRow("/clear     clear and repaint the terminal"),
            FrameRow("Knowledge", divider=True),
            FrameRow("/remember <text>    save a private local Knowledge record"),
            FrameRow("/knowledge [query]  inspect status or retrieve a bounded local view"),
            FrameRow("/workbench [intent-id] show local Knowledge · Intent · Decision · Job relations"),
            FrameRow("/knowledge folder … manage user-owned raw Knowledge Folders"),
            FrameRow("/intent [id]        list active intents or inspect one intent"),
            FrameRow("/intent create …     create a draft; /intent activate <id> does not start a Job"),
            FrameRow("/decision [due|id]  list decisions, reviews due, or inspect one"),
            FrameRow("/decision record …  propose a Decision; /decision review <id> drafts research only"),
            FrameRow("/workbench ready <intent-id>  check bounded evidence before explicit `noruct intent run`"),
            FrameRow("Model", divider=True),
            FrameRow("/model             choose from discovered models"),
            FrameRow("/model <model-id>  switch this session directly"),
            FrameRow("/mode [standard|economy]  inspect or switch model-context economy"),
            FrameRow("/review            choose domain-scoped review policy"),
            FrameRow("Inspection", divider=True),
            FrameRow("/status    session, model, authority, and usage"),
            FrameRow("/usage     accumulated session usage"),
            FrameRow("/skills [goal]  list external SKILL.md instructions or preview Job selection"),
            FrameRow("/details [on|off]  expand or collapse execution evidence"),
            FrameRow("/view [expand|collapse]  toggle the bottom live dock (ctrl+o)"),
            FrameRow("Control", divider=True),
            FrameRow("/quit      leave Noruct"),
        )
        self._write_frame("Commands", rows, footer="end a line with \\ for multiline", tone="muted")

    def choose_review_mode(self, current: str) -> str | None:
        self.clear_status()
        options = (
            (
                "approval",
                "Ask before applying a reversible employee dormancy proposal.",
            ),
            (
                "auto-review",
                "Apply only full-window repeated underuse; escalate failures and safety events.",
            ),
            (
                "always-approve",
                "Apply any valid retention proposal; hard integrity checks remain enabled.",
            ),
        )
        rows: list[FrameRow] = [
            FrameRow("review domains", divider=True),
            FrameRow("retention       configurable below"),
            FrameRow("employee skill  approval only · no automatic apply/rollback"),
            FrameRow("retention policy", divider=True),
        ]
        for index, (mode, description) in enumerate(options, 1):
            marker = "●" if mode == current else "○"
            suffix = "  current" if mode == current else ""
            rows.append(FrameRow(f"{index}  {marker} {mode}{suffix}"))
            rows.append(FrameRow(f"   {description}"))
        rows.append(FrameRow("hard gates", divider=True))
        rows.append(FrameRow("hash · latest evidence · stale ROSTER · exact before/after · decoder"))
        self._write("\n")
        self._write_frame(
            "SELECT REVIEW MODE",
            tuple(rows),
            footer="Enter cancels · only retention is changed",
            tone="accent",
        )
        while True:
            self._write("  Select review mode: ")
            value = self.stdin.readline()
            if value == "":
                return None
            choice = value.strip().lower()
            if not choice or choice in {"q", "quit", "cancel"}:
                self.commit("Review mode unchanged.", tone="muted")
                return None
            aliases = {"1": "approval", "2": "auto-review", "3": "always-approve"}
            selected = aliases.get(choice, choice)
            if selected in {item[0] for item in options}:
                return selected
            self.commit("Choose 1, 2, 3, or Enter to cancel.", tone="warning")

    def review_mode_switched(self, *, previous: str, current: str) -> None:
        if previous == current:
            self.commit(f"Review mode unchanged · {current}", tone="muted")
            return
        self.commit(f"✓ Retention review · {previous} → {current}", tone="success")
        self.commit("  Applies only to reversible evidence-backed dormancy.", tone="muted")
        self.commit("  Hash, stale and ROSTER integrity gates remain enabled.", tone="muted")

    def choose_model(
        self,
        options: tuple[ModelOption, ...],
        *,
        provider: str,
    ) -> str | None:
        """Choose a session model without invoking a provider or model."""

        catalog = options
        visible = options

        def render_picker(query: str = "") -> None:
            rows: list[FrameRow] = [
                FrameRow(f"provider  {provider or 'configured provider'}"),
                FrameRow("matching models" if query else "available models", divider=True),
            ]
            for index, option in enumerate(visible, 1):
                marker = "●" if option.current else "○"
                suffix = "  current" if option.current else ""
                rows.append(FrameRow(f"{index:>2}  {marker} {option.model_id}{suffix}"))
            rows.extend(
                (
                    FrameRow("custom", divider=True),
                    FrameRow("s <query>  search local model ids"),
                    FrameRow("a          show all models"),
                    FrameRow("c          enter another model id"),
                )
            )
            self._write("\n")
            self._write_frame(
                "SELECT MODEL",
                tuple(rows),
                footer="Enter cancels · session only",
                tone="accent",
            )

        render_picker()
        while True:
            self._write("  Select model: ")
            value = self.stdin.readline()
            if value == "":
                return None
            choice = value.strip()
            if not choice or choice.lower() in {"q", "quit", "cancel"}:
                self.commit("Model switch cancelled.", tone="muted")
                return None
            if choice.lower() in {"c", "custom"}:
                self._write("  Model id: ")
                custom = self.stdin.readline()
                if custom == "":
                    return None
                selected = custom.strip()
                if selected:
                    return selected
                self.commit("Model id cannot be empty.", tone="warning")
                continue
            lowered = choice.lower()
            if lowered in {"a", "all"}:
                visible = catalog
                render_picker()
                continue
            if lowered.startswith("s ") or lowered.startswith("search "):
                _, _, query = choice.partition(" ")
                try:
                    matches = filter_model_options(catalog, query)
                except ValueError as exc:
                    self.commit(str(exc), tone="warning")
                    continue
                if not matches:
                    self.commit("No local model id matches that search.", tone="warning")
                    continue
                visible = matches
                render_picker(query)
                continue
            try:
                index = int(choice) - 1
            except ValueError:
                self.commit("Choose a listed number, c for custom, or Enter to cancel.", tone="warning")
                continue
            if 0 <= index < len(visible):
                return visible[index].model_id
            self.commit("Choose one of the listed models.", tone="warning")

    def model_switched(self, *, previous: str, current: str) -> None:
        self._model = current
        if previous == current:
            self.commit(f"Model unchanged · {current}", tone="muted")
            return
        self.commit(f"✓ Model switched · {previous} → {current}", tone="success")
        self.commit("  Applies to this company session and its next turn.", tone="muted")

    def toggle_details(self, value: str = "") -> bool:
        normalized = value.strip().lower()
        if normalized in {"on", "expanded", "full"}:
            self.details = True
        elif normalized in {"off", "collapsed", "compact"}:
            self.details = False
        elif normalized:
            raise ValueError("Use /details, /details on, or /details off.")
        else:
            self.details = not self.details
        self.commit(
            f"Execution details: {'expanded' if self.details else 'collapsed'}",
            tone="muted",
        )
        return self.details

    def clear_screen(self) -> None:
        self.clear_status()
        if self.interactive and not self.plain:
            self._write(CLEAR_SCREEN)
        self.banner(
            workspace=self._workspace,
            session_id=self._session_id or None,
            model=self._model,
            provider=self._provider,
            authority=self._authority,
            version=self._version,
            roster_revision=self._roster_revision,
            active_employee_count=self._active_employee_count,
            employee_roles=self._employee_roles,
            capabilities=self._capabilities,
            tools=self._tool_names,
        )

    def read_goal(self) -> str | None:
        self.clear_status()
        parts: list[str] = []
        if self.plain:
            top = bottom = ""
        else:
            top, bottom = self._input_rules()
            self._write(self._style(top, CYAN) + "\n")
        while True:
            if self.plain:
                prompt = "> " if not parts else ".. "
            else:
                prompt = "│ ❯ " if not parts else "│ … "
            line = self._read_goal_line(prompt)
            if line is None:
                if bottom:
                    self._write(self._style(bottom, CYAN) + "\n")
                return None if not parts else "\n".join(parts).strip()
            if line.endswith("\\"):
                parts.append(line[:-1])
                continue
            parts.append(line)
            result = "\n".join(parts).strip()
            if bottom:
                self._write(self._style(bottom, CYAN) + "\n")
            if result and self._readline is not None:
                try:
                    self._readline.add_history(result)
                except Exception:
                    pass
            return result

    def ask_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.clear_status()
        preview_lines = request.preview.splitlines() or [request.preview]
        has_diff = any(
            line.startswith(("--- ", "+++ ", "@@ ", "@@-", "@@"))
            for line in preview_lines
        )
        rows: list[FrameRow] = [
            FrameRow(f"action  {request.tool_name}"),
            FrameRow(f"impact  {request.effect.value.lower()} · {request.risk.value.lower()} risk"),
            FrameRow(f"scope   {request.resource_key}"),
            FrameRow("PROPOSED CHANGE", divider=True),
        ]
        if has_diff:
            diff_started = False
            chunk_width = max(8, self._width() - 8)
            for line in preview_lines:
                if line.startswith(("--- ", "+++ ", "@@")):
                    diff_started = True
                if not diff_started:
                    rows.append(FrameRow(line))
                    continue
                chunks = hard_wrap_display(line, chunk_width)
                rows.append(FrameRow(chunks[0], wrap=False))
                rows.extend(FrameRow(f"  ↳ {chunk}", wrap=False) for chunk in chunks[1:])
        else:
            rows.extend(FrameRow(line) for line in preview_lines)
        rows.append(FrameRow("DECISION", divider=True))
        self._write("\n")
        self._write_frame(
            "APPROVAL · REQUIRED",
            tuple(rows),
            footer="Enter defaults to deny",
            tone="warning",
        )
        if request.allow_session:
            prompt = "  [1] Allow once  [2] Allow workspace edits this session  [3] Deny\n  Select [3]: "
            allowed = {"1", "2", "3", "", "y", "n"}
        else:
            prompt = "  [1] Allow once  [2] Deny\n  Select [2]: "
            allowed = {"1", "2", "", "y", "n"}
        while True:
            self._write(prompt)
            value = self.stdin.readline()
            if value == "":
                return ApprovalDecision.DENY
            choice = value.strip().lower()
            if choice not in allowed:
                self._write("  Choose one of the listed options.\n")
                continue
            if choice in {"", "3", "n"}:
                return ApprovalDecision.DENY
            if request.allow_session and choice == "2":
                return ApprovalDecision.ALLOW_SESSION
            if not request.allow_session and choice == "2":
                return ApprovalDecision.DENY
            return ApprovalDecision.ALLOW_ONCE

    def close(self) -> None:
        self.clear_status()
        if self._readline is not None:
            try:
                self._readline.set_completer(self._previous_completer)
            except Exception:
                pass
            if self._readline_auto_history_disabled:
                try:
                    self._readline.set_auto_history(True)
                except (AttributeError, TypeError):
                    pass
                self._readline_auto_history_disabled = False
        if self.interactive and not self.plain:
            self._write(RESET)
