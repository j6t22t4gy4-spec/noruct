"""Argument schema for durable, explicitly operated schedule commands."""

from __future__ import annotations

import argparse
from pathlib import Path


def add_schedule_commands(commands: argparse._SubParsersAction) -> None:
    """Register schedule and local schedule-service command schemas."""

    schedule = commands.add_parser(
        "schedule",
        help="Manage durable Company Job schedules; foreground daemon dispatch remains explicitly operator-started.",
    )
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_list = schedule_commands.add_parser("list", help="List enabled schedules without running them.")
    schedule_list.add_argument("--all", action="store_true", help="Include paused schedules.")
    schedule_list.add_argument("--state", type=Path, default=None)
    schedule_list.add_argument("--json", action="store_true")
    schedule_status = schedule_commands.add_parser("status", help="Show local schedule scheduler state; no daemon is started.")
    schedule_status.add_argument("--state", type=Path, default=None)
    schedule_status.add_argument("--json", action="store_true")
    schedule_create = schedule_commands.add_parser("create", help="Create an interval schedule; it cannot run until an explicit tick.")
    schedule_create.add_argument("goal", help="Self-contained Company goal for each future Job.")
    schedule_create.add_argument("--every-minutes", type=int, required=True, help="Bounded interval in whole minutes (1 through 43,200).")
    schedule_create.add_argument("--name", default=None)
    schedule_create.add_argument("--workspace", type=Path, default=Path.cwd())
    schedule_create.add_argument("--state", type=Path, default=None)
    schedule_create.add_argument("--confirm", action="store_true")
    schedule_create.add_argument("--json", action="store_true")
    schedule_cron = schedule_commands.add_parser("cron-create", help="Create a UTC five-field cron schedule; it still runs only through explicit tick/run/foreground daemon.")
    schedule_cron.add_argument("goal", help="Self-contained Company goal for each future Job.")
    schedule_cron.add_argument("--cron", required=True, help="Five fields: minute hour day month weekday; UTC only.")
    schedule_cron.add_argument("--name", default=None)
    schedule_cron.add_argument("--workspace", type=Path, default=Path.cwd())
    schedule_cron.add_argument("--state", type=Path, default=None)
    schedule_cron.add_argument("--confirm", action="store_true")
    schedule_cron.add_argument("--json", action="store_true")
    for name, help_text in (("pause", "Pause one schedule."), ("resume", "Resume one schedule."), ("remove", "Remove one schedule.")):
        item = schedule_commands.add_parser(name, help=help_text)
        item.add_argument("schedule_id")
        item.add_argument("--state", type=Path, default=None)
        item.add_argument("--confirm", action="store_true")
        item.add_argument("--json", action="store_true")
    for name, help_text in (("tick", "Claim due schedules once and run ordinary bounded Company Jobs."), ("run", "Force one enabled schedule through the ordinary Company Job path.")):
        item = schedule_commands.add_parser(name, help=help_text)
        if name == "run":
            item.add_argument("schedule_id")
        else:
            item.add_argument("--limit", type=int, default=4, help="Maximum due schedules to run sequentially.")
        item.add_argument("--state", type=Path, default=None)
        item.add_argument("--confirm", action="store_true")
        item.add_argument("--json", action="store_true")
    schedule_daemon = schedule_commands.add_parser(
        "daemon",
        help="Run a foreground, operator-confirmed scheduler loop. It dispatches only normal read-only Company Jobs and stops with the terminal.",
    )
    schedule_daemon.add_argument("--poll-seconds", type=float, default=60.0, help="Bounded foreground polling interval (5 through 3600 seconds).")
    schedule_daemon.add_argument("--limit", type=int, default=4, help="Maximum due schedules claimed per poll (1 through 32).")
    schedule_daemon.add_argument("--max-cycles", type=int, default=None, help="Optional bounded cycle count for supervised operation/testing.")
    schedule_daemon.add_argument("--state", type=Path, default=None)
    schedule_daemon.add_argument("--confirm", action="store_true")
    schedule_daemon.add_argument("--json", action="store_true")
    schedule_service = schedule_commands.add_parser(
        "service",
        help="Manage one operator-confirmed local child process for the schedule daemon; it never enables boot start or automatic restart.",
    )
    schedule_service_commands = schedule_service.add_subparsers(dest="schedule_service_command", required=True)
    schedule_service_status = schedule_service_commands.add_parser("status", help="Inspect the local schedule service record without starting it.")
    schedule_service_status.add_argument("--state", type=Path, default=None)
    schedule_service_status.add_argument("--json", action="store_true")
    schedule_service_logs = schedule_service_commands.add_parser("logs", help="Read a bounded, terminal-redacted tail of the recorded local schedule-service log.")
    schedule_service_logs.add_argument("--lines", type=int, default=80, help="Tail line count (1 through 400).")
    schedule_service_logs.add_argument("--state", type=Path, default=None)
    schedule_service_logs.add_argument("--json", action="store_true")
    for _schedule_service_action, _schedule_service_help in (
        ("start", "Start the schedule daemon in one local child process."),
        ("restart", "Explicitly stop then start the schedule daemon in a new local child process."),
    ):
        _item = schedule_service_commands.add_parser(_schedule_service_action, help=_schedule_service_help)
        _item.add_argument("--poll-seconds", type=float, default=60.0)
        _item.add_argument("--limit", type=int, default=4)
        _item.add_argument("--state", type=Path, default=None)
        _item.add_argument("--log-file", type=Path, default=None)
        _item.add_argument("--confirm", action="store_true")
        _item.add_argument("--json", action="store_true")
    for _schedule_service_action, _schedule_service_help in (
        ("stop", "Stop the recorded local schedule-service child process."),
        ("reset", "Clear the schedule-service restart-loop circuit after inspection."),
    ):
        _item = schedule_service_commands.add_parser(_schedule_service_action, help=_schedule_service_help)
        _item.add_argument("--state", type=Path, default=None)
        _item.add_argument("--confirm", action="store_true")
        _item.add_argument("--json", action="store_true")


