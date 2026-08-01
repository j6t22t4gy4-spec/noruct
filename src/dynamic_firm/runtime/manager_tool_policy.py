"""Dependency-light identity helpers for Manager-only runtime tools."""

from __future__ import annotations


MANAGER_TOOL_PREFIX = "manager_"


def is_manager_tool(tool_name: str) -> bool:
    return tool_name.startswith(MANAGER_TOOL_PREFIX)
