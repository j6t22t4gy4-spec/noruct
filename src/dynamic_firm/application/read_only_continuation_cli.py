"""CLI adapter for receipt-bound read-only continuation and device handoff.

The adapter owns parsed-command flow and terminal rendering only.  It receives
the runtime composition through explicit ports, so it cannot reconstruct a Job
from audit content or create a second Work Order/approval authority.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from dynamic_firm.company.work_order_portfolio import WorkOrderPortfolioStore
from dynamic_firm.kernel.models import JobStatus


CONTINUATION_OK = 0
CONTINUATION_JOB_FAILED = 4


@dataclass(frozen=True, slots=True)
class ReadOnlyContinuationCliPorts:
    """CLI-specific composition supplied by the product ingress."""

    load_config: Callable[[argparse.Namespace], Any]
    provider_config_for: Callable[[Any], object]
    provider_factory: Callable[[object], object]
    continue_partial: Callable[..., Any]
    handoff_partial: Callable[..., object]
    render_result: Callable[..., None]


def _retained_request_goal(state_path: Path, job_id: str) -> str:
    portfolio_path = state_path.with_name(f"{state_path.stem}.work-orders.db")
    try:
        with WorkOrderPortfolioStore(portfolio_path) as work_orders:
            return work_orders.continuation_request(job_id).goal
    except KeyError as exc:
        raise ValueError(
            f"No retained read-only continuation request exists for Job {job_id!r}"
        ) from exc


def _configuration_for_retained_job(
    args: argparse.Namespace,
    *,
    state_path: Path,
    ports: ReadOnlyContinuationCliPorts,
) -> object:
    config_args = argparse.Namespace(**{**vars(args), "goal": _retained_request_goal(state_path, args.job_id)})
    config = ports.load_config(config_args)
    if config.state_path != state_path:
        raise ValueError("Continuation state path does not match the selected Company state")
    return config


def run_read_only_continuation_command(
    args: argparse.Namespace,
    *,
    state_path: Path,
    ports: ReadOnlyContinuationCliPorts,
    output: TextIO,
) -> int:
    """Dispatch the only CLI paths for a read-only same-Job continuation."""

    if args.command == "continue-read-only":
        if not args.confirm:
            raise ValueError("Read-only partial continuation requires --confirm")
        config = _configuration_for_retained_job(args, state_path=state_path, ports=ports)
        provider_config = ports.provider_config_for(config)
        result = asyncio.run(
            ports.continue_partial(
                config=config,
                provider_config=provider_config,
                provider_factory=ports.provider_factory,
                job_id=args.job_id,
            )
        )
        ports.render_result(result, as_json=args.json, output=output)
        return CONTINUATION_OK if result.status == JobStatus.SUCCEEDED else CONTINUATION_JOB_FAILED

    if args.command == "handoff-read-only":
        if not args.confirm:
            raise ValueError("Read-only continuation handoff requires --confirm")
        config = _configuration_for_retained_job(args, state_path=state_path, ports=ports)
        admission = ports.handoff_partial(
            config=config,
            job_id=args.job_id,
            target_device_id=args.target_device_id,
        )
        payload: Mapping[str, object] = {
            "schema": "noruct.partial-read-only-handoff.v1",
            "status": "TRANSFERRED",
            "job_id": admission.job_id,
            "target_device_id": args.target_device_id,
            "graph_digest": admission.graph_digest,
            "completed_task_ids": admission.completed_task_ids,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(
                f"Read-only continuation authority transferred to {args.target_device_id} for {admission.job_id}.",
                file=output,
            )
        return CONTINUATION_OK

    raise ValueError(f"Unknown read-only continuation command: {args.command}")
