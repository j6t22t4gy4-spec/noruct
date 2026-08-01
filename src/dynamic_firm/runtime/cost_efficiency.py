"""Deterministic model-context economy projection.

This is intentionally *not* a billing estimator and never changes the durable
run ledger, tool result, approval preview, or failure evidence.  ``economy``
only reduces repetitive successful tool text at the final model-request
boundary.  That keeps the behavior reviewable and makes a provider's real
usage receipt the only source of truth for token or USD accounting.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Sequence

from .models import CostEfficiencyMode, ModelMessage


_MINIMUM_SOURCE_CHARS = 2_048
_MAX_PROJECTED_CHARS = 8_000
_HEAD_CHARS = 5_500
_TAIL_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class CostEfficiencyProjection:
    """Auditable model-facing projection statistics, never a cost claim."""

    messages: tuple[ModelMessage, ...]
    projected_message_count: int = 0
    chars_before: int = 0
    chars_after: int = 0

    @property
    def applied(self) -> bool:
        return self.projected_message_count > 0


def _collapse_repeated_lines(value: str) -> str:
    """Preserve first occurrence order while making consecutive noise explicit."""

    lines = value.splitlines(keepends=True)
    if not lines:
        return value
    result: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        count = 1
        while index + count < len(lines) and lines[index + count] == line:
            count += 1
        result.append(line)
        if count > 1:
            ending = "\n" if line.endswith("\n") else ""
            result.append(f"[noruct economy: previous line repeated {count - 1} times]{ending}")
        index += count
    return "".join(result)


def _bounded_text(value: str) -> str:
    if len(value) <= _MAX_PROJECTED_CHARS:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    omitted = len(value) - _HEAD_CHARS - _TAIL_CHARS
    return (
        value[:_HEAD_CHARS]
        + "\n\n"
        + (
            "[noruct economy: middle omitted from model context; "
            f"{omitted} chars; full successful tool receipt remains in the local ledger; "
            f"sha256={digest}]"
        )
        + "\n\n"
        + value[-_TAIL_CHARS:]
    )


def _project_text(value: str) -> str:
    return _bounded_text(_collapse_repeated_lines(value))


def _project_json_strings(value: object) -> tuple[object, bool]:
    """Keep a successful JSON tool receipt structured while compacting text leaves."""

    if isinstance(value, str):
        if len(value) < _MINIMUM_SOURCE_CHARS:
            return value, False
        projected = _project_text(value)
        return projected, projected != value
    if isinstance(value, list):
        changed = False
        projected: list[object] = []
        for item in value:
            replacement, item_changed = _project_json_strings(item)
            projected.append(replacement)
            changed = changed or item_changed
        return projected, changed
    if isinstance(value, dict):
        changed = False
        projected: dict[str, object] = {}
        for key, item in value.items():
            replacement, item_changed = _project_json_strings(item)
            projected[str(key)] = replacement
            changed = changed or item_changed
        return projected, changed
    return value, False


def _project_tool_content(value: str) -> str:
    """Use a JSON-preserving projection when a tool returned structured text."""

    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return _project_text(value)
    projected, changed = _project_json_strings(decoded)
    if not changed:
        return value
    return json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CostEfficiencyProjector:
    """Reduce only safe, noisy tool-result context in an explicit mode."""

    revision = "noruct-economy-tool-context-v1"

    def project(
        self,
        messages: Sequence[ModelMessage],
        *,
        mode: CostEfficiencyMode,
    ) -> CostEfficiencyProjection:
        original = tuple(messages)
        if mode is not CostEfficiencyMode.ECONOMY:
            return CostEfficiencyProjection(original)

        projected: list[ModelMessage] = []
        changed = 0
        chars_before = 0
        chars_after = 0
        for message in original:
            # Failed results and non-tool messages are diagnostic/authority
            # material.  Never transform them; success-only text is the
            # narrow, reversible economy surface.
            if message.role != "tool" or not isinstance(message.content, dict):
                projected.append(message)
                continue
            if message.content.get("ok") is not True or message.content.get("error_code"):
                projected.append(message)
                continue
            content = message.content.get("content")
            if not isinstance(content, str) or len(content) < _MINIMUM_SOURCE_CHARS:
                projected.append(message)
                continue
            compacted = _project_tool_content(content)
            if compacted == content:
                projected.append(message)
                continue
            replacement = dict(message.content)
            replacement["content"] = compacted
            projected.append(replace(message, content=replacement))
            changed += 1
            chars_before += len(content)
            chars_after += len(compacted)
        return CostEfficiencyProjection(
            tuple(projected),
            projected_message_count=changed,
            chars_before=chars_before,
            chars_after=chars_after,
        )
