"""First-party provider boundary over the private wire-safety implementation."""

from __future__ import annotations

from typing import Any

from dynamic_firm._vendor.runtime_safety.message_safety import (
    repair_tool_call_arguments,
    sanitize_structure_surrogates,
)


def sanitize_wire_payload(payload: Any) -> None:
    sanitize_structure_surrogates(payload)


def parse_tool_arguments(raw_arguments: str | None, tool_name: str) -> dict[str, Any] | None:
    return repair_tool_call_arguments(raw_arguments, tool_name)
