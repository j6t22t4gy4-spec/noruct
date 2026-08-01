"""Loopback Graph Workbench application adapter.

The Graph dashboard is a surface over existing Graph and ACTIVE JOB projections.
It receives explicit callbacks rather than importing CLI configuration or the
Firm Kernel, so a future GUI can host the same user-governed views without
creating a second continuation or Graph authority.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Callable, Mapping, TextIO

from dynamic_firm.product.graph_workbench_dashboard import serve_graph_workbench_dashboard


GRAPH_DASHBOARD_OK = 0


@dataclass(frozen=True, slots=True)
class GraphDashboardPorts:
    graph_snapshot: Callable[[], Mapping[str, object]]
    job_catalog: Callable[[], Mapping[str, object]]
    job_snapshot: Callable[[str | None], Mapping[str, object]]
    operator_snapshot: Callable[[], Mapping[str, object]]
    resolve_proposal: Callable[[str, str, bool], Mapping[str, object]]
    save_future_constraints: Callable[[Mapping[str, object]], Mapping[str, object]]


def run_graph_dashboard(
    args: argparse.Namespace,
    *,
    ports: GraphDashboardPorts,
    output: TextIO,
) -> int:
    """Serve one explicit local Graph Workbench session.

    Starting a listener is itself operator-confirmed.  The dashboard receives
    read projections and explicit callbacks only; the callback that resolves a
    proposal remains responsible for the retained Work Order, frozen policy,
    provider assembly, and receipt-bound Kernel continuation.
    """

    if not args.confirm:
        raise ValueError(
            "Graph dashboard requires --confirm because it starts a local loopback HTTP listener"
        )

    def announce(host: str, port: int) -> None:
        record = {
            "started": True,
            "url": f"http://{host}:{port}/",
            "authority": "loopback_read_only_graph_workbench",
        }
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output, flush=True)
        else:
            print(f"Graph workbench · {record['url']}", file=output, flush=True)
            print("Read-only loopback projection; use Ctrl+C to stop.", file=output, flush=True)

    try:
        serve_graph_workbench_dashboard(
            graph_snapshot=ports.graph_snapshot,
            job_catalog=ports.job_catalog,
            job_snapshot=ports.job_snapshot,
            operator_snapshot=ports.operator_snapshot,
            resolve_proposal=ports.resolve_proposal,
            save_future_constraints=ports.save_future_constraints,
            port=int(args.port),
            maximum_requests=args.max_requests,
            on_ready=announce,
        )
    except KeyboardInterrupt:
        pass
    return GRAPH_DASHBOARD_OK
