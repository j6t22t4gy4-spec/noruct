"""Argument schema for explicitly supervised inbound gateway commands."""

from __future__ import annotations

import argparse
from pathlib import Path


_RECEIVERS = ("telegram", "slack", "discord", "email", "ntfy", "matrix", "mattermost")


def add_gateway_commands(commands: argparse._SubParsersAction) -> None:
    """Register gateway and local gateway-service command schemas."""

    gateway = commands.add_parser(
        "gateway",
        help="Run a foreground supervisor for explicitly selected configured inbound channels; it owns no external platform identity.",
    )
    gateway_commands = gateway.add_subparsers(dest="gateway_command", required=True)
    gateway_status = gateway_commands.add_parser("status", help="Show configured foreground receiver readiness without starting one.")
    gateway_status.add_argument("--json", action="store_true")
    gateway_dashboard = gateway_commands.add_parser(
        "dashboard",
        help="Serve a loopback-only, read-only gateway status dashboard; it owns no gateway or Company state.",
    )
    gateway_dashboard.add_argument("--port", type=int, default=0, help="Loopback port (0 selects an available port).")
    gateway_dashboard.add_argument("--max-requests", type=int, default=None, help="Optional request cap for diagnostics/tests.")
    gateway_dashboard.add_argument("--state", type=Path, default=None)
    gateway_dashboard.add_argument("--confirm", action="store_true")
    gateway_dashboard.add_argument("--json", action="store_true")
    gateway_run = gateway_commands.add_parser("run", help="Supervise explicitly selected configured inbound receivers in the foreground; it stops with the terminal.")
    gateway_run.add_argument("--receiver", choices=("telegram", "slack", "discord", "email", "ntfy", "matrix", "mattermost"), action="append", required=True, help="Configured receiver to run; repeatable.")
    gateway_run.add_argument("--poll-seconds", type=float, default=15.0, help="Delay between complete receiver rounds (5 through 3600 seconds).")
    gateway_run.add_argument("--receiver-seconds", type=float, default=10.0, help="Maximum time supplied to each selected receiver (1 through 60 seconds).")
    gateway_run.add_argument("--max-cycles", type=int, default=None, help="Optional foreground cycle cap (1 through 10000).")
    gateway_run.add_argument("--state", type=Path, default=None)
    gateway_run.add_argument("--confirm", action="store_true")
    gateway_run.add_argument("--json", action="store_true")
    gateway_service = gateway_commands.add_parser(
        "service",
        help="Manage an operator-confirmed local gateway child process with persisted status and a restart-loop breaker.",
    )
    gateway_service_commands = gateway_service.add_subparsers(dest="gateway_service_command", required=True)
    gateway_service_status = gateway_service_commands.add_parser("status", help="Inspect one local gateway service record without starting it.")
    gateway_service_status.add_argument("--state", type=Path, default=None)
    gateway_service_status.add_argument("--json", action="store_true")
    gateway_service_logs = gateway_service_commands.add_parser(
        "logs",
        help="Read a bounded, terminal-redacted tail of the recorded local gateway log; it never starts or stops the service.",
    )
    gateway_service_logs.add_argument("--lines", type=int, default=80, help="Tail line count (1 through 400).")
    gateway_service_logs.add_argument("--state", type=Path, default=None)
    gateway_service_logs.add_argument("--json", action="store_true")
    gateway_service_start = gateway_service_commands.add_parser("start", help="Start configured receivers in one local child process.")
    gateway_service_start.add_argument("--receiver", choices=("telegram", "slack", "discord", "email", "ntfy", "matrix", "mattermost"), action="append", required=True)
    gateway_service_start.add_argument("--poll-seconds", type=float, default=15.0)
    gateway_service_start.add_argument("--receiver-seconds", type=float, default=10.0)
    gateway_service_start.add_argument("--state", type=Path, default=None)
    gateway_service_start.add_argument("--log-file", type=Path, default=None)
    gateway_service_start.add_argument("--confirm", action="store_true")
    gateway_service_start.add_argument("--json", action="store_true")
    gateway_service_restart = gateway_service_commands.add_parser("restart", help="Explicitly stop then start configured receivers in one new local child process.")
    gateway_service_restart.add_argument("--receiver", choices=("telegram", "slack", "discord", "email", "ntfy", "matrix", "mattermost"), action="append", required=True)
    gateway_service_restart.add_argument("--poll-seconds", type=float, default=15.0)
    gateway_service_restart.add_argument("--receiver-seconds", type=float, default=10.0)
    gateway_service_restart.add_argument("--state", type=Path, default=None)
    gateway_service_restart.add_argument("--log-file", type=Path, default=None)
    gateway_service_restart.add_argument("--confirm", action="store_true")
    gateway_service_restart.add_argument("--json", action="store_true")
    for _service_action, _service_help in (
        ("stop", "Stop the recorded local gateway child process."),
        ("reset", "Clear the restart-loop circuit after inspection."),
    ):
        _item = gateway_service_commands.add_parser(_service_action, help=_service_help)
        _item.add_argument("--state", type=Path, default=None)
        _item.add_argument("--confirm", action="store_true")
        _item.add_argument("--json", action="store_true")

