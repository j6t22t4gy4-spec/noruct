"""Pure terminal presentation constants shared by Product TUI components."""

from __future__ import annotations


RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
MAGENTA = "\x1b[35m"
BRAND_VIOLET = "\x1b[38;2;216;180;254m"
BRAND_PURPLE = "\x1b[38;2;167;139;250m"
BRAND_INDIGO = "\x1b[38;2;129;140;248m"
BRAND_BLUE = "\x1b[38;2;96;165;250m"
BRAND_CYAN = "\x1b[38;2;103;232;249m"
CLEAR_LINE = "\r\x1b[2K"
CLEAR_SCREEN = "\x1b[2J\x1b[H"
SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"
ALT_SCREEN_ENTER = "\x1b[?1049h"
ALT_SCREEN_EXIT = "\x1b[?1049l"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
ASCII_WORDMARK = (
    "███╗   ██╗ ██████╗ ██████╗ ██╗   ██╗ ██████╗████████╗",
    "████╗  ██║██╔═══██╗██╔══██╗██║   ██║██╔════╝╚══██╔══╝",
    "██╔██╗ ██║██║   ██║██████╔╝██║   ██║██║        ██║",
    "██║╚██╗██║██║   ██║██╔══██╗██║   ██║██║        ██║",
    "██║ ╚████║╚██████╔╝██║  ██║╚██████╔╝╚██████╗   ██║",
    "╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝ ╚═════╝  ╚═════╝   ╚═╝",
)
WORDMARK_GRADIENT = (
    BRAND_VIOLET,
    BRAND_VIOLET,
    BRAND_PURPLE,
    BRAND_INDIGO,
    BRAND_BLUE,
    BRAND_CYAN,
)
SLASH_COMMANDS = (
    "/help", "/remember", "/knowledge", "/workbench", "/intent", "/decision",
    "/model", "/mode", "/review", "/status", "/usage", "/details", "/view",
    "/sessions", "/new", "/clear", "/quit",
)
