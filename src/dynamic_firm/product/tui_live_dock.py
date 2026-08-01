"""Persistent live-dock projection and terminal lifecycle behavior."""

from __future__ import annotations

import os
import threading
import time
from typing import TextIO

from .terminal import truncate_display
from .tui_constants import (
    HIDE_CURSOR,
    RESET,
    SHOW_CURSOR,
    SPINNER as _SPINNER,
    SYNC_END,
    SYNC_START,
)
from .tui_primitives import _drop_last_typeahead_grapheme


def _is_real_tty(stream: TextIO) -> bool:
    try:
        return os.isatty(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False


class LiveTerminalDockMixin:
    def _live_task_entries(self) -> list[tuple[str, str]]:
        if self._live_stage == "IDLE":
            return [
                ("organization  idle · no active work", "accent"),
                ("○ persistent company ready for a goal", "muted"),
            ]
        mode = self._live_plan_mode.lower() if self._live_plan_mode else "forming"
        expected = self._live_expected_tasks or len(self._live_tasks)
        entries: list[tuple[str, str]] = [
            (
                f"organization  {mode} · {expected or '—'} task"
                f"{'s' if expected != 1 else ''}",
                "accent",
            )
        ]
        glyphs = {
            "working": "◇",
            "tool": "◆",
            "verifying": "◈",
            "retry": "↻",
            "rerouted": "↪",
            "succeeded": "✓",
            "failed": "×",
        }
        tones = {
            "succeeded": "success",
            "failed": "error",
            "retry": "warning",
            "rerouted": "warning",
            "tool": "accent",
        }
        for task in self._live_tasks.values():
            employee = truncate_display(task.employee, 22)
            label = task.label
            detail = f" · {task.detail}" if task.detail else ""
            entries.append(
                (
                    f"{glyphs.get(task.status, '◇')} {employee}  {label}{detail}",
                    tones.get(task.status, "normal"),
                )
            )
        if len(entries) == 1:
            entries.append((f"{_SPINNER[self._live_tick % len(_SPINNER)]} execution structure pending", "muted"))
        return entries

    def _live_activity_entries(self) -> list[tuple[str, str]]:
        if self._live_stage == "IDLE":
            current = (f"○ {self._live_status}", "accent")
            return [current, *self._live_activity]
        current = (
            f"{_SPINNER[self._live_tick % len(_SPINNER)]} {self._live_status}",
            "accent",
        )
        return [current, *self._live_activity]

    def _start_live_ticker_locked(self) -> None:
        if not self.animations:
            return
        generation = self._live_generation

        def animate() -> None:
            while True:
                time.sleep(0.1)
                with self._lock:
                    if not self._live_active or generation != self._live_generation:
                        return
                    if self._live_prompt:
                        continue
                    self._live_tick += 1
                    self._render_live_locked()

        thread = threading.Thread(
            target=animate,
            name="noruct-live-viewport",
            daemon=True,
        )
        thread.start()

    def _start_live_hotkey_locked(self) -> None:
        if not (_is_real_tty(self.stdin) and _is_real_tty(self.stdout)):
            return
        try:
            import termios
        except ImportError:
            return

        try:
            descriptor = self.stdin.fileno()
            previous = termios.tcgetattr(descriptor)
            current = termios.tcgetattr(descriptor)
            current[3] &= ~(termios.ICANON | termios.ECHO | termios.IEXTEN)
            current[3] |= termios.ISIG
            current[6][termios.VMIN] = 0
            current[6][termios.VTIME] = 1
            termios.tcsetattr(descriptor, termios.TCSANOW, current)
        except (AttributeError, OSError, ValueError):
            return
        self._live_termios_state = (previous, descriptor)
        generation = self._live_generation

        def listen() -> None:
            while True:
                with self._lock:
                    if not self._live_active or generation != self._live_generation:
                        return
                try:
                    value = os.read(descriptor, 32)
                except OSError:
                    return
                if not value:
                    continue
                with self._lock:
                    if not self._live_active or generation != self._live_generation:
                        return
                    toggle = self._buffer_live_input_locked(value)
                    if toggle:
                        self._live_expanded = not self._live_expanded
                        self._resize_live_dock_locked()

        thread = threading.Thread(
            target=listen,
            name="noruct-live-dock-hotkey",
            daemon=True,
        )
        self._live_hotkey_thread = thread
        thread.start()

    def _buffer_live_input_locked(self, value: bytes) -> bool:
        toggle = False
        for byte in value:
            if self._live_escape_input:
                if self._live_escape_input == 1 and byte in {0x4F, 0x5B}:
                    self._live_escape_input = 2
                    continue
                if 0x40 <= byte <= 0x7E:
                    self._live_escape_input = 0
                continue
            if byte == 0x1B:
                self._live_escape_input = 1
                continue
            if byte == 0x0F:
                toggle = not toggle
                continue
            if byte in {0x08, 0x7F}:
                _drop_last_typeahead_grapheme(self._live_typeahead)
                continue
            if byte in {0x0A, 0x0D}:
                if not self._live_typeahead or self._live_typeahead[-1] != 0x0A:
                    self._live_typeahead.append(0x0A)
                continue
            if byte >= 0x20 and len(self._live_typeahead) < 8_192:
                self._live_typeahead.append(byte)
        return toggle

    def _restore_live_hotkey_locked(self) -> None:
        state = self._live_termios_state
        self._live_termios_state = None
        if state is None:
            return
        previous, descriptor = state
        try:
            import termios
        except ImportError:
            return
        try:
            termios.tcsetattr(descriptor, termios.TCSANOW, previous)
        except (OSError, ValueError):
            pass

    def _pause_live_runtime_locked(self) -> threading.Thread | None:
        """Stop ticker/hotkey generations without removing the dock."""

        self._live_generation += 1
        thread = self._live_hotkey_thread
        self._live_hotkey_thread = None
        return thread

    def _finish_live_runtime_pause(self, thread: threading.Thread | None) -> None:
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.25)
        with self._lock:
            self._restore_live_hotkey_locked()

    def _anchor_live_bottom_locked(self) -> None:
        _, terminal_height = self._live_size()
        self._write(f"\x1b[r\x1b[{terminal_height};1H")

    def _open_live_input_region_locked(self) -> None:
        """Keep readline above the persistent dock using a scroll margin."""

        _, terminal_height = self._live_size()
        prompt_row = max(1, terminal_height - self._live_reserved_rows)
        self._write(
            f"\x1b[1;{prompt_row}r\x1b[{prompt_row};1H\x1b[2K{SHOW_CURSOR}"
        )

    def _close_live_input_region_locked(self) -> None:
        _, terminal_height = self._live_size()
        self._write(f"\x1b[r\x1b[{terminal_height};1H")

    def _begin_live_transcript_locked(self) -> None:
        """Give output methods the scroll region above the fixed surface."""

        if not self._live_active:
            return
        _, terminal_height = self._live_size()
        transcript_bottom = max(1, terminal_height - self._live_reserved_rows)
        self._live_transcript_mode = True
        self._write(
            f"\x1b[1;{transcript_bottom}r"
            f"\x1b[{transcript_bottom};1H{SHOW_CURSOR}"
        )

    def _end_live_transcript_locked(self) -> None:
        if not self._live_transcript_mode:
            return
        _, terminal_height = self._live_size()
        self._live_transcript_mode = False
        self._write(f"\x1b[r\x1b[{terminal_height};1H{HIDE_CURSOR}")

    def _reserve_live_rows_locked(self, target_rows: int) -> None:
        """Resize the bottom reservation without scrolling surface rows.

        Growth scrolls blank rows through only the transcript region above the
        current surface. Shrink clears released rows. Neither path emits a live
        frame through newline-based terminal history.
        """

        _, terminal_height = self._live_size()
        target_rows = max(1, min(target_rows, terminal_height - 1))
        current_rows = self._live_reserved_rows
        previous_terminal_height = self._live_physical_height or terminal_height
        if current_rows == target_rows and previous_terminal_height == terminal_height:
            return

        buffer = SYNC_START + HIDE_CURSOR + "\x1b[r"
        if current_rows:
            current_start = max(1, previous_terminal_height - current_rows + 1)
            for index in range(current_rows):
                row = current_start + index
                if row <= terminal_height:
                    buffer += f"\x1b[{row};1H\x1b[2K"

        if target_rows > current_rows:
            growth = target_rows - current_rows
            transcript_bottom = max(1, terminal_height - current_rows)
            buffer += f"\x1b[1;{transcript_bottom}r\x1b[{transcript_bottom};1H"
            buffer += "\r\n" * growth
            buffer += "\x1b[r"

        target_start = max(1, terminal_height - target_rows + 1)
        for index in range(target_rows):
            buffer += f"\x1b[{target_start + index};1H\x1b[2K"
        buffer += f"\x1b[{terminal_height};1H" + SYNC_END
        self._write(buffer)
        self._live_reserved_rows = target_rows
        self._live_physical_height = terminal_height
        self._live_previous_lines = ()
        self._live_previous_size = (0, 0, 0)

    def _resize_live_dock_locked(self) -> None:
        _, terminal_height = self._live_size()
        target_rows = self._live_dock_height(terminal_height)
        self._reserve_live_rows_locked(target_rows)
        self._render_live_locked(force=True)

    def _enter_live_locked(self, *, runtime: bool = True) -> None:
        _, terminal_height = self._live_size()
        if not self._live_active:
            self._live_active = True
        self._end_live_transcript_locked()
        self._reserve_live_rows_locked(self._live_dock_height(terminal_height))
        self._live_generation += 1
        if runtime:
            self._start_live_hotkey_locked()
        self._render_live_locked(force=True)
        if runtime:
            self._start_live_ticker_locked()

    def _exit_live_locked(self) -> None:
        if not self._live_active:
            return
        self._live_generation += 1
        self._live_hotkey_thread = None
        self._restore_live_hotkey_locked()
        self._live_active = False
        self._live_transcript_mode = False
        reserved_rows = self._live_reserved_rows
        _, terminal_height = self._live_size()
        dock_start = max(1, terminal_height - reserved_rows + 1)
        clear = SYNC_START + HIDE_CURSOR + "\x1b[r"
        for index in range(reserved_rows):
            clear += f"\x1b[{dock_start + index};1H\x1b[2K"
        clear += f"\x1b[{dock_start};1H" + SYNC_END + RESET + SHOW_CURSOR
        self._live_previous_lines = ()
        self._live_previous_size = (0, 0, 0)
        self._live_reserved_rows = 0
        self._live_physical_height = 0
        self._write(clear)
