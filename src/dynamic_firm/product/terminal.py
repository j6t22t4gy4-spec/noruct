from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from dynamic_firm._vendor.runtime_safety.ansi_strip import (
    strip_ansi as _source_strip_ansi,
)

_UNSAFE_C0 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(value: str) -> str:
    """Strip full ECMA-48 escapes plus unsafe C0 controls from display text."""

    return _UNSAFE_C0.sub("", _source_strip_ansi(value))


def display_width(value: str) -> int:
    width = 0
    for char in strip_ansi(value).expandtabs(4):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def truncate_display(value: str, width: int, suffix: str = "…") -> str:
    if width <= 0:
        return ""
    clean = strip_ansi(value).expandtabs(4)
    if display_width(clean) <= width:
        return clean
    suffix_width = min(display_width(suffix), width)
    budget = width - suffix_width
    output: list[str] = []
    used = 0
    for char in clean:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        )
        if used + char_width > budget:
            break
        output.append(char)
        used += char_width
    return "".join(output) + (suffix if suffix_width else "")


def pad_display(value: str, width: int) -> str:
    clipped = truncate_display(value, width)
    return clipped + " " * max(0, width - display_width(clipped))


def _prefix_for_width(value: str, width: int) -> tuple[str, str]:
    used = 0
    split = 0
    last_space = -1
    for index, char in enumerate(value):
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        )
        if used + char_width > width:
            break
        used += char_width
        split = index + 1
        if char.isspace():
            last_space = split
    if split == len(value):
        return value, ""
    if last_space > 0:
        return value[:last_space].rstrip(), value[last_space:].lstrip()
    return value[:split], value[split:]


def wrap_display(value: str, width: int) -> tuple[str, ...]:
    width = max(1, width)
    output: list[str] = []
    paragraphs = strip_ansi(value).expandtabs(4).splitlines() or [""]
    for paragraph in paragraphs:
        remaining = paragraph
        if not remaining:
            output.append("")
            continue
        while display_width(remaining) > width:
            head, remaining = _prefix_for_width(remaining, width)
            output.append(head)
        output.append(remaining)
    return tuple(output)


def hard_wrap_display(value: str, width: int) -> tuple[str, ...]:
    width = max(1, width)
    remaining = strip_ansi(value).expandtabs(4)
    if not remaining:
        return ("",)
    output: list[str] = []
    while display_width(remaining) > width:
        used = 0
        split = 0
        for index, char in enumerate(remaining):
            char_width = 0 if unicodedata.combining(char) else (
                2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
            )
            if used + char_width > width:
                break
            used += char_width
            split = index + 1
        output.append(remaining[:split])
        remaining = remaining[split:]
    output.append(remaining)
    return tuple(output)


@dataclass(frozen=True, slots=True)
class FrameRow:
    text: str = ""
    wrap: bool = True
    divider: bool = False


def _rule(left: str, right: str, label: str, width: int) -> str:
    safe = truncate_display(label.strip(), max(0, width - 6))
    prefix = left + (f"─ {safe} " if safe else "─")
    return prefix + "─" * max(0, width - display_width(prefix) - 1) + right


def frame_lines(
    title: str,
    rows: tuple[FrameRow, ...],
    width: int,
    *,
    footer: str = "",
) -> tuple[str, ...]:
    width = max(20, width)
    inner = width - 4
    output = [_rule("╭", "╮", title, width)]
    for row in rows:
        if row.divider:
            output.append(_rule("├", "┤", row.text, width))
            continue
        values = wrap_display(row.text, inner) if row.wrap else (truncate_display(row.text, inner),)
        for value in values:
            output.append(f"│ {pad_display(value, inner)} │")
    output.append(_rule("╰", "╯", footer, width))
    return tuple(output)
