"""Argument schema for read-only and explicit Company session controls."""

from __future__ import annotations

import argparse
from pathlib import Path


def add_session_commands(commands: argparse._SubParsersAction) -> None:
    """Register bounded session inspection and mutation command schemas."""

    session = commands.add_parser(
        "session",
        help="Inspect and control one persistent Company session without contacting a model.",
    )
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_search = session_commands.add_parser("search", help="Search the bounded local transcript projection.")
    session_search.add_argument("query")
    session_search.add_argument("--session", dest="session_id", default=None)
    session_search.add_argument("--limit", type=int, default=20)
    session_search.add_argument("--state", type=Path, default=None)
    session_search.add_argument("--json", action="store_true")
    session_branch = session_commands.add_parser("branch", help="Create a new session from a local transcript checkpoint.")
    session_branch.add_argument("session_id")
    session_branch.add_argument("--title", default=None)
    session_branch.add_argument("--through-message", type=int, default=None)
    session_branch.add_argument("--state", type=Path, default=None)
    session_branch.add_argument("--confirm", action="store_true")
    session_branch.add_argument("--json", action="store_true")
    session_rewind = session_commands.add_parser("rewind", help="Remove transcript rows after a checkpoint; Firm turns remain immutable.")
    session_rewind.add_argument("session_id")
    session_rewind.add_argument("through_message", type=int)
    session_rewind.add_argument("--state", type=Path, default=None)
    session_rewind.add_argument("--confirm", action="store_true")
    session_rewind.add_argument("--json", action="store_true")

