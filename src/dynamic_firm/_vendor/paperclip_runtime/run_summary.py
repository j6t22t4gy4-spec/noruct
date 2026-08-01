"""Dependency-free bounded terminal-result projector adapted from Paperclip.

Upstream: https://github.com/paperclipai/paperclip
Commit: ce7dedf33d2689673826ffdcfd6af7ee06be39af
Source file: server/src/services/heartbeat-run-summary.ts
SHA-256: 74fe22544b627e88ddf640162a42be6aa4af6801fd4b6bb3a379f6755caae058
Copyright (c) 2025 Paperclip AI. SPDX-License-Identifier: MIT.

Modifications: ported from TypeScript to dependency-free Python and narrowed to
Noruct's already-redacted EmployeeRunResult shape. It exposes a short terminal
summary and scalar usage only; it never projects messages, prompts, tool output,
artifacts, credentials, or an upstream runtime identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TERMINAL_SUMMARY_MAX_CHARS = 500


def _truncate_text(value: object, maximum: int = TERMINAL_SUMMARY_MAX_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:maximum] if len(value) > maximum else value


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _nonnegative_number(value: object) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return value
    return None


def summarize_terminal_result(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the bounded terminal state safe for an operator status surface.

    Callers remain responsible for the mandatory Noruct redaction boundary before
    persistence or display. This function deliberately accepts a plain mapping
    so private upstream types cannot cross the product boundary.
    """

    if not isinstance(result, Mapping):
        return None

    summary: dict[str, Any] = {}
    text = _truncate_text(result.get("summary"))
    if text is not None:
        summary["summary"] = text

    status = result.get("status")
    if isinstance(status, str) and status:
        summary["status"] = status

    usage = result.get("usage")
    if isinstance(usage, Mapping):
        bounded_usage: dict[str, Any] = {}
        for key in ("model_calls", "tool_calls", "input_tokens", "cached_input_tokens", "output_tokens"):
            value = _nonnegative_int(usage.get(key))
            if value is not None:
                bounded_usage[key] = value
        cost = _nonnegative_number(usage.get("cost_usd"))
        if cost is not None:
            bounded_usage["cost_usd"] = cost
        if bounded_usage:
            summary["usage"] = bounded_usage

    failure = result.get("failure")
    if isinstance(failure, Mapping):
        code = failure.get("code")
        if isinstance(code, str) and code:
            summary["failure_code"] = code

    return summary or None
