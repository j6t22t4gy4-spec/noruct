"""Pure geometry, formatting, and input-grapheme helpers for Product TUI."""

from __future__ import annotations

import os
import unicodedata
from typing import Any, Mapping, TextIO

from .terminal import display_width, truncate_display


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _is_real_tty(stream: TextIO) -> bool:
    """Reject isatty-only test doubles when enabling terminal mode changes."""

    try:
        return os.isatty(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False


def _compact_path(value: str, width: int) -> str:
    home = str(os.path.expanduser("~"))
    display = "~" + value[len(home) :] if value.startswith(home) else value
    if display_width(display) <= width:
        return display
    if width <= 1:
        return "…"
    tail = display[-(width - 1) :]
    while display_width(tail) > width - 1:
        tail = tail[1:]
    return "…" + tail


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {sec:02d}s"
    return f"{sec}s"




def _short_identity(value: str, fallback: str) -> str:
    clean = value.strip() or fallback
    if clean.startswith("temp-job-"):
        specialty = clean.rsplit("-", 1)[-1]
        return truncate_display(specialty.replace("_", " ") + " specialist", 32)
    for prefix in ("employee-", "task-"):
        if clean.startswith(prefix):
            clean = clean[len(prefix) :]
    return truncate_display(clean.replace("_", " ").replace("-", " "), 32)


def _job_metrics_text(data: Mapping[str, Any]) -> str:
    nested = data.get("metrics")
    source = nested if isinstance(nested, Mapping) else data
    facts: list[str] = []
    for key, label in (
        ("unique_employee_count", "employees"),
        ("temporary_role_count", "temporary"),
        ("maximum_parallelism", "max parallel"),
        ("graph_patch_count", "workflow revisions"),
        ("task_mutation_count", "task recoveries"),
        ("manager_integration_count", "manager integrations"),
    ):
        value = source.get(key)
        if isinstance(value, int):
            facts.append(f"{value} {label}")
    return " · ".join(facts)


def _compact_number(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    return f"{value / 1_000_000:.1f}m".replace(".0m", "m")


def _compact_session(value: str, width: int = 8) -> str:
    clean = value.removeprefix("session-")
    return truncate_display(clean, width)


def _fit_segments(
    segments: tuple[tuple[int, str], ...],
    width: int,
    *,
    separator: str = " · ",
) -> str:
    """Keep high-priority status segments and shed low-priority tails first."""

    available = max(1, width)
    active = [(priority, value) for priority, value in segments if value]
    if not active:
        return ""
    while len(active) > 1 and display_width(separator.join(value for _, value in active)) > available:
        lowest = min(priority for priority, _ in active)
        index = max(i for i, (priority, _) in enumerate(active) if priority == lowest)
        active.pop(index)
    return truncate_display(separator.join(value for _, value in active), available)


def _is_grapheme_extend(value: str) -> bool:
    """Return the common terminal-input characters extending a prior glyph."""

    codepoint = ord(value)
    return (
        unicodedata.combining(value) != 0
        or unicodedata.category(value) in {"Mc", "Me"}
        or 0xFE00 <= codepoint <= 0xFE0F  # variation selectors
        or 0x1F3FB <= codepoint <= 0x1F3FF  # emoji skin-tone modifiers
        or codepoint == 0x20E3  # keycap enclosing mark
    )


def _is_regional_indicator(value: str) -> bool:
    return 0x1F1E6 <= ord(value) <= 0x1F1FF


def _drop_last_typeahead_grapheme(buffer: bytearray) -> None:
    """Delete one visible input unit from a live raw-byte typeahead buffer.

    The full terminal composer is still owned by readline. This only covers
    bytes typed while the live dock owns raw mode. The implementation handles
    the high-value extended-grapheme cases (combining marks, emoji modifiers,
    variation/keycap marks, ZWJ sequences and paired regional indicators) and
    deliberately falls back to the old UTF-8 codepoint boundary when an
    incomplete multibyte sequence is in flight.
    """

    if not buffer:
        return
    try:
        text = buffer.decode("utf-8")
    except UnicodeDecodeError:
        index = len(buffer) - 1
        while index > 0 and buffer[index] & 0xC0 == 0x80:
            index -= 1
        del buffer[index:]
        return
    if not text:
        buffer.clear()
        return

    end = len(text)
    while end > 0 and _is_grapheme_extend(text[end - 1]):
        end -= 1
    if end == 0:
        buffer.clear()
        return
    start = end - 1

    # A regional-indicator pair is one flag glyph. Count its contiguous run
    # and include its preceding mate exactly when this is the second half.
    if _is_regional_indicator(text[start]):
        run_start = start
        while run_start > 0 and _is_regional_indicator(text[run_start - 1]):
            run_start -= 1
        if (end - run_start) % 2 == 0:
            start -= 1

    # Include preceding joined bases: woman + ZWJ + laptop is one glyph.
    while start > 0 and text[start - 1] == "\u200d":
        start -= 1
        while start > 0 and _is_grapheme_extend(text[start - 1]):
            start -= 1
        if start > 0:
            start -= 1

    del buffer[len(text[:start].encode("utf-8")) :]

