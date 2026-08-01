"""Compatibility facade for the composable Product terminal surface.

The public UI classes remain importable here while rendering, interactions, live
terminal lifecycle, and formatting each stay within their own bounded component.
"""

from __future__ import annotations

from .tui_constants import (
    ALT_SCREEN_ENTER,
    ALT_SCREEN_EXIT,
    CLEAR_LINE,
    CLEAR_SCREEN,
    SHOW_CURSOR,
)
from .tui_inline import InlineTerminalUI
from .tui_interactions import _usage_text
from .tui_live import LiveTerminalUI
from .tui_primitives import _drop_last_typeahead_grapheme

__all__ = (
    "ALT_SCREEN_ENTER",
    "ALT_SCREEN_EXIT",
    "CLEAR_LINE",
    "CLEAR_SCREEN",
    "SHOW_CURSOR",
    "InlineTerminalUI",
    "LiveTerminalUI",
    "_drop_last_typeahead_grapheme",
    "_usage_text",
)
