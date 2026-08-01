from __future__ import annotations

"""Provider-free Knowledge command adapter for interactive product surfaces."""

from pathlib import Path

from dynamic_firm.product.knowledge_commands import execute_local_knowledge_command
from dynamic_firm.product.modern_tui import ModernTerminalCommandResult


KNOWLEDGE_COMMANDS = frozenset(
    {
        "/remember",
        "/knowledge",
        "/intent",
        "/decision",
        "/question",
        "/research",
        "/workbench",
    }
)


def execute_knowledge_command(
    state_path: Path,
    command: str,
    argument: str,
) -> ModernTerminalCommandResult | None:
    """Execute a bounded local Knowledge/Intent command without a model call."""

    if command not in KNOWLEDGE_COMMANDS:
        return None
    try:
        messages = execute_local_knowledge_command(state_path, command, argument)
    except (OSError, ValueError) as exc:
        messages = (f"Local Knowledge command failed safely · {exc}",)
    return ModernTerminalCommandResult(messages=messages)
