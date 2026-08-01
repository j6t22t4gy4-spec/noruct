"""Core Foundation command schema."""

from __future__ import annotations

import argparse
from pathlib import Path


def add_foundation_core_commands(commands: argparse._SubParsersAction, *, default_state_path: Path) -> None:
    """Register runtime/source qualification commands without executing them."""

    parser = commands.add_parser
    for name, help_text in (
        ("status", "Show source integrity and optional dependency readiness without running an agent."),
        ("cutover-status", "Show the fail-closed default-runtime and rollback admission gate."),
        ("verify-source", "Hash every vendored upstream file and verify the exact source tree."),
        ("inventory", "Show complete private foundation intake coverage and Noruct activation boundaries without starting an agent."),
    ):
        command = parser(name, help=help_text); command.add_argument("--json", action="store_true")
    preview = parser("migration-preview", help="Inspect local Employee Runtime migration readiness without changing state or defaults.")
    preview.add_argument("--state", type=Path, default=default_state_path); preview.add_argument("--json", action="store_true")
    apply = parser("migration-apply", help="Create a verified local backup and record the no-transform transition to the default runtime.")
    apply.add_argument("--state", type=Path, default=default_state_path)
    apply.add_argument("--backup-dir", type=Path, default=None); apply.add_argument("--confirm", action="store_true"); apply.add_argument("--json", action="store_true")
    smoke = parser("smoke", help="Run the shipped employee loop through the isolated Noruct execution port.")
    smoke.add_argument("--timeout", type=float, default=90.0); smoke.add_argument("--json", action="store_true")
    preflight = parser("preflight", help="Verify a chosen worker Python through the exact isolated employee-runtime path.")
    preflight.add_argument("--runtime-python", required=True); preflight.add_argument("--timeout", type=float, default=90.0); preflight.add_argument("--json", action="store_true")
    for name, help_text in (
        ("parity", "Run the offline direct/tool/approval/cancel preview contract matrix."),
        ("reliability", "Run offline direct/Company/approval/discovery/cancel/recovery reliability qualification."),
    ):
        command = parser(name, help=help_text); command.add_argument("--runtime-python", required=True); command.add_argument("--json", action="store_true")
    readiness = parser("readiness", help="Combine exact worker qualification and offline preview-parity evidence.")
    readiness.add_argument("--runtime-python", required=True); readiness.add_argument("--timeout", type=float, default=90.0); readiness.add_argument("--json", action="store_true")
