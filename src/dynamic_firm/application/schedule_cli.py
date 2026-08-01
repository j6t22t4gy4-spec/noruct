"""Schedule command dispatch composed behind the global CLI ingress.

Schedule state remains owned by the existing local stores. The injected ports
provide ordinary Job construction and execution without importing the CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from dynamic_firm.product import ScheduleServiceStore, schedule_service_state_path
from dynamic_firm.product.schedules import ScheduleStore, ScheduledJob


@dataclass(frozen=True)
class SchedulePorts:
    state_path_for: Callable[[argparse.Namespace, Mapping[str, object]], Path]
    run_config_for: Callable[[argparse.Namespace, Mapping[str, object]], Any]
    provider_config_for: Callable[[Any], Any]
    run_goal_for: Callable[..., Any]
    roster_for: Callable[[Any], Any]
    log_tail: Callable[..., dict[str, object]]
    company_goal_route: Any
    exit_ok: int


def _scheduled_job_primitive(item: ScheduledJob) -> dict[str, object]:
    return {
        "schedule_id": item.schedule_id,
        "name": item.name,
        "goal": item.goal,
        "workspace": str(item.workspace),
        "interval_minutes": item.interval_minutes,
        "schedule_type": item.schedule_type,
        "cron_expression": item.cron_expression,
        "enabled": item.enabled,
        "next_run_at": item.next_run_at.isoformat(),
        "last_run_at": item.last_run_at.isoformat() if item.last_run_at else None,
        "last_job_id": item.last_job_id,
        "last_status": item.last_status,
        "run_count": item.run_count,
    }


def _scheduled_run_config(
    item: ScheduledJob,
    args: argparse.Namespace,
    settings: Mapping[str, object],
    run_config_for: Callable[[argparse.Namespace, Mapping[str, object]], Any],
) -> Any:
    """Materialize a normal, read-only Job from a stored schedule record."""

    synthetic = argparse.Namespace(
        goal=item.goal,
        workspace=item.workspace,
        state=args.state,
        provider_kind=None,
        base_url=None,
        model=None,
        codex_command=None,
        api_key_env=None,
        no_auth=None,
        request_timeout=None,
        max_wall_time=None,
        max_model_calls=None,
        max_tool_calls=None,
        max_cost_usd=None,
        cost_mode=None,
        permission_mode="read-only",
        employee_runtime=None,
        runtime_python=None,
        skills_dir=None,
    )
    return run_config_for(synthetic, settings)


def _schedule_service_record(
    *, action: str, state_path: Path, service_state_path: Path, record: Mapping[str, object]
) -> dict[str, object]:
    return {
        "schedule": "noruct_owned_local_service",
        "action": action,
        "job_state_path": str(state_path),
        "service_state_path": str(service_state_path),
        "background_service": True,
        "automatic_boot_start": False,
        "automatic_restart": False,
        "automatic_learning_apply": False,
        "record": dict(record),
    }


def _run_schedule_service_command(
    args: argparse.Namespace,
    settings: Mapping[str, object],
    output: TextIO,
    *,
    ports: SchedulePorts,
) -> int:
    """Operate one local schedule-daemon child without changing schedule authority."""

    state_path = ports.state_path_for(args, settings)
    service_state = schedule_service_state_path(state_path)
    action = args.schedule_service_command
    with ScheduleServiceStore(service_state) as store:
        if action == "status":
            payload = _schedule_service_record(action=action, state_path=state_path, service_state_path=service_state, record=store.status().to_dict())
        elif action == "logs":
            current = store.status().to_dict()
            payload = _schedule_service_record(action=action, state_path=state_path, service_state_path=service_state, record=current)
            payload["log"] = ports.log_tail(
                Path(str(current["log_path"])) if current.get("log_path") else None,
                lines=int(args.lines),
            )
        elif action == "stop":
            if not args.confirm:
                raise ValueError("Schedule service stop requires --confirm")
            payload = _schedule_service_record(action=action, state_path=state_path, service_state_path=service_state, record=store.stop().to_dict())
        elif action == "reset":
            if not args.confirm:
                raise ValueError("Schedule service reset requires --confirm")
            payload = _schedule_service_record(action=action, state_path=state_path, service_state_path=service_state, record=store.reset().to_dict())
        else:
            if not args.confirm:
                raise ValueError(f"Schedule service {action} requires --confirm because scheduled Jobs can consume provider quota")
            if not 5 <= args.poll_seconds <= 3600:
                raise ValueError("Schedule service poll interval must be between 5 and 3600 seconds")
            if not 1 <= args.limit <= 32:
                raise ValueError("Schedule service limit must be between 1 and 32")
            if action == "restart":
                store.stop()
            log_path = args.log_file.expanduser().resolve() if args.log_file is not None else service_state.with_suffix(".log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            reservation = store.reserve_start(poll_seconds=float(args.poll_seconds), limit=int(args.limit), log_path=log_path)
            command = [
                sys.executable, "-m", "dynamic_firm", "--config", str(args.config.expanduser().resolve()),
                "schedule", "daemon", "--state", str(state_path), "--poll-seconds", str(args.poll_seconds),
                "--limit", str(args.limit), "--confirm",
            ]
            try:
                with log_path.open("ab", buffering=0) as log_file:
                    process = subprocess.Popen(
                        command, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                        start_new_session=True, close_fds=True,
                    )
            except OSError:
                store.stop()
                raise
            started = store.mark_started(run_id=reservation.run_id or "", pid=process.pid)
            payload = _schedule_service_record(action=action, state_path=state_path, service_state_path=service_state, record=started.to_dict())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), file=output)
    else:
        record = payload["record"]
        assert isinstance(record, dict)
        print(f"Schedule service {action} · {record['state']}", file=output)
        if action in {"start", "restart"}:
            print(f"PID {record['pid']} · logs: {record['log_path']}", file=output)
        elif action == "logs":
            log = payload["log"]
            assert isinstance(log, dict)
            if not log.get("available"):
                print("Schedule service log is not available.", file=output)
            else:
                for line in log["lines"]:
                    print(str(line), file=output)
        print("No automatic boot start, automatic restart, external delivery, or automatic learning apply was enabled.", file=output)
    return ports.exit_ok


def run_schedule_command(
    args: argparse.Namespace,
    settings: Mapping[str, object],
    output: TextIO,
    *,
    provider_factory: Callable[[Any], Any],
    ports: SchedulePorts,
) -> int:
    """Manage schedules and dispatch them only through ordinary ``run_goal``.

    A foreground daemon is deliberately an operator-owned terminal process,
    not a hidden service. It carries one explicit start confirmation and still
    materializes each stored record as the same read-only Job used by `tick`.
    """

    if args.schedule_command == "service":
        return _run_schedule_service_command(args, settings, output, ports=ports)
    state_path = ports.state_path_for(args, settings)
    with ScheduleStore(state_path) as store:
        if args.schedule_command in {"create", "cron-create"}:
            if not args.confirm:
                raise ValueError("Schedule create requires --confirm")
            name = args.name or args.goal.strip()[:72]
            record = _scheduled_job_primitive(
                store.create_cron(
                    name=name, goal=args.goal, workspace=args.workspace.expanduser().resolve(), expression=args.cron,
                ) if args.schedule_command == "cron-create" else store.create(
                    name=name,
                    goal=args.goal,
                    workspace=args.workspace.expanduser().resolve(),
                    interval_minutes=args.every_minutes,
                )
            )
        elif args.schedule_command == "list":
            record = {
                "schedules": [
                    _scheduled_job_primitive(item)
                    for item in store.list(include_disabled=args.all)
                ],
                "scheduler": "manual_tick_only",
            }
        elif args.schedule_command == "status":
            all_items = store.list(include_disabled=True)
            record = {
                "scheduler": "manual_tick_only",
                "enabled": sum(1 for item in all_items if item.enabled),
                "paused": sum(1 for item in all_items if not item.enabled),
                "external_delivery": "disabled",
                "background_daemon": "disabled",
            }
        elif args.schedule_command in {"pause", "resume"}:
            if not args.confirm:
                raise ValueError(f"Schedule {args.schedule_command} requires --confirm")
            record = _scheduled_job_primitive(
                store.set_enabled(args.schedule_id, enabled=args.schedule_command == "resume")
            )
        elif args.schedule_command == "remove":
            if not args.confirm:
                raise ValueError("Schedule remove requires --confirm")
            if not store.remove(args.schedule_id):
                raise ValueError(f"Schedule was not found: {args.schedule_id}")
            record = {"schedule_id": args.schedule_id, "removed": True}
        else:
            if not args.confirm:
                raise ValueError(
                    f"Schedule {args.schedule_command} requires --confirm because it can consume provider quota"
                )
            if args.schedule_command == "daemon":
                if not 5 <= args.poll_seconds <= 3600:
                    raise ValueError("Schedule daemon poll interval must be between 5 and 3600 seconds")
                if not 1 <= args.limit <= 32:
                    raise ValueError("Schedule daemon limit must be between 1 and 32")
                if args.max_cycles is not None and not 1 <= args.max_cycles <= 10_000:
                    raise ValueError("Schedule daemon max_cycles must be between 1 and 10000")
            def dispatch(claimed: tuple[ScheduledJob, ...]) -> list[dict[str, object]]:
                runs: list[dict[str, object]] = []
                for item in claimed:
                    config = _scheduled_run_config(item, args, settings, ports.run_config_for)
                    provider = provider_factory(ports.provider_config_for(config))
                    result = asyncio.run(
                        ports.run_goal_for(
                            config,
                            provider,
                            route=ports.company_goal_route,
                            roster_snapshot=ports.roster_for(config),
                        )
                    )
                    finalized = store.complete(
                        item.schedule_id, job_id=result.job_id, status=result.status.value
                    )
                    runs.append(
                        {
                            "schedule": _scheduled_job_primitive(finalized),
                            "job_id": result.job_id,
                            "status": result.status.value,
                            "summary": result.summary,
                        }
                    )
                return runs
            if args.schedule_command == "daemon":
                cycles: list[dict[str, object]] = []
                cycle_count = 0
                try:
                    while args.max_cycles is None or cycle_count < args.max_cycles:
                        claimed = store.claim_due(limit=args.limit)
                        runs = dispatch(claimed)
                        cycles.append({"cycle": cycle_count + 1, "claimed_count": len(claimed), "runs": runs})
                        cycle_count += 1
                        if args.max_cycles is None or cycle_count < args.max_cycles:
                            time.sleep(args.poll_seconds)
                except KeyboardInterrupt:
                    pass
                record = {
                    "scheduler": "foreground_operator_confirmed_daemon",
                    "poll_seconds": args.poll_seconds,
                    "cycles": cycles,
                    "stopped": "terminal_interrupt_or_requested_cycle_limit",
                }
            else:
                claimed = (
                    (store.claim_one(args.schedule_id),)
                    if args.schedule_command == "run"
                    else store.claim_due(limit=args.limit)
                )
                record = {
                    "scheduler": "manual_tick_only",
                    "claimed_count": len(claimed),
                    "runs": dispatch(claimed),
                }

    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2), file=output)
        return ports.exit_ok
    if args.schedule_command in {"create", "cron-create"}:
        cadence = f"cron {record['cron_expression']} UTC" if record.get("schedule_type") == "cron" else f"every {record['interval_minutes']} minute(s)"
        print(
            f"Schedule created · {record['schedule_id']} · {cadence}",
            file=output,
        )
        print("It runs only through `noruct schedule tick --confirm` or `schedule run --confirm`.", file=output)
    elif args.schedule_command == "list":
        schedules = record["schedules"]
        assert isinstance(schedules, list)
        if not schedules:
            print("No enabled schedules. Create one with `noruct schedule create … --confirm`.", file=output)
        for item in schedules:
            assert isinstance(item, dict)
            state = "enabled" if item["enabled"] else "paused"
            print(
                f"{item['schedule_id']} · {state} · {('cron ' + str(item['cron_expression']) + ' UTC') if item.get('schedule_type') == 'cron' else 'every ' + str(item['interval_minutes']) + 'm'} · next {item['next_run_at']} · {item['name']}",
                file=output,
            )
    elif args.schedule_command == "status":
        print(
            f"Schedule runtime · manual tick only · {record['enabled']} enabled · {record['paused']} paused",
            file=output,
        )
        print("No background daemon, script runner, or external delivery is enabled.", file=output)
    elif args.schedule_command in {"tick", "run"}:
        print(f"Schedule dispatch · {record['claimed_count']} Job(s) claimed", file=output)
        for item in record["runs"]:
            assert isinstance(item, dict)
            print(f"  {item['job_id']} · {item['status']} · {item['summary']}", file=output)
    elif args.schedule_command == "daemon":
        print(f"Schedule daemon stopped · {len(record['cycles'])} poll cycle(s) · foreground only", file=output)
        print("No detached service, automatic startup, external delivery, or permission escalation was created.", file=output)
    else:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    return ports.exit_ok




