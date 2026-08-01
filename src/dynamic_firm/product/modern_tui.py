"""Compatibility facade for the composable optional modern Product terminal."""

from __future__ import annotations

from .modern_tui_app import (
    create_modern_terminal_app,
    modern_terminal_available,
    modern_terminal_install_hint,
    run_modern_terminal,
)
from .modern_tui_contracts import (
    ModernTerminalCommandResult,
    ModernTerminalController,
    ModernTerminalResult,
    ModernTerminalSnapshot,
    ModernTerminalUnavailable,
    SessionInputHistory,
    SessionInputHistorySelection,
)

__all__ = (
    "ModernTerminalCommandResult",
    "ModernTerminalController",
    "ModernTerminalResult",
    "ModernTerminalSnapshot",
    "ModernTerminalUnavailable",
    "SessionInputHistory",
    "SessionInputHistorySelection",
    "create_modern_terminal_app",
    "modern_terminal_available",
    "modern_terminal_install_hint",
    "run_modern_terminal",
)
