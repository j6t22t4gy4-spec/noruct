"""Shared Company-goal preparation for CLI, TUI and editor ingress.

The product has several interactive surfaces, but they must create the same
effective configuration, frozen ROSTER snapshot, provider and optional coding
worker before entering the Firm Runtime.  This module owns that composition
sequence only.  The injected runner remains the existing authoritative
Company/KERNEL path, so extracting this layer cannot create a second runtime
or bypass approvals.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping


@dataclass(frozen=True)
class PreparedGoalExecution:
    """One ingress's frozen runtime dependencies before a Company Job starts."""

    config: Any
    roster_snapshot: Any
    provider: Any
    coding_worker: Any | None
    frozen_route_composition: Any | None = None


@dataclass(frozen=True)
class GoalExecutionServices:
    """Product-neutral adapters around the existing Company execution path.

    ``config_for`` and ``roster_for`` remain the only sources for effective
    configuration and active Employee identity.  Keeping these dependencies
    explicit makes it possible to test or replace a terminal surface without
    duplicating a second provider/ROSTER assembly path.
    """

    config_for: Callable[[argparse.Namespace, Mapping[str, object]], Any]
    roster_for: Callable[[Any], Any]
    provider_config_for: Callable[[Any], Any]
    provider_factory: Callable[[Any], Any]
    coding_worker_for: Callable[[Any, Any], Any | None]
    approval_available_for: Callable[[Any], bool]
    runner: Callable[..., Awaitable[Any]]
    frozen_route_composition_for: Callable[[Any, Any], Any | None] | None = None

    def prepare(
        self,
        args: argparse.Namespace,
        settings: Mapping[str, object],
    ) -> PreparedGoalExecution:
        """Build one consistent local execution envelope without starting a Job."""

        config = self.config_for(args, settings)
        roster_snapshot = self.roster_for(config)
        frozen_route_composition = (
            self.frozen_route_composition_for(config, roster_snapshot)
            if self.frozen_route_composition_for is not None
            else None
        )
        provider_config = self.provider_config_for(config)
        return PreparedGoalExecution(
            config=config,
            roster_snapshot=roster_snapshot,
            provider=self.provider_factory(provider_config),
            coding_worker=self.coding_worker_for(provider_config, config),
            frozen_route_composition=frozen_route_composition,
        )

    async def execute(
        self,
        prepared: PreparedGoalExecution,
        *,
        approval_port: Any | None = None,
        event_sink: Callable[[Any], None] | None = None,
        prior_context: tuple[str, ...] = (),
        route: Any = None,
        session_key: str = "",
        request_id: str | None = None,
        job_id: str | None = None,
        task_evidence: Any | None = None,
        execution_origin: Any | None = None,
        work_order_override: Any | None = None,
    ) -> Any:
        """Enter the single injected Firm Runtime path with the frozen envelope."""

        arguments = {
            "approval_port": (
                approval_port
                if self.approval_available_for(prepared.config)
                else None
            ),
            "coding_worker": prepared.coding_worker,
            "event_sink": event_sink,
            "prior_context": prior_context,
            "route": route,
            "roster_snapshot": prepared.roster_snapshot,
            "session_key": session_key,
            "request_id": request_id,
            "job_id": job_id,
            "task_evidence": task_evidence,
            "execution_origin": execution_origin,
        }
        if work_order_override is not None:
            arguments["work_order_override"] = work_order_override
        if prepared.frozen_route_composition is not None:
            arguments["frozen_route_composition"] = prepared.frozen_route_composition
        return await self.runner(prepared.config, prepared.provider, **arguments)
