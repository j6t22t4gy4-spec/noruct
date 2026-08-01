"""Surface-neutral Graph Blueprint preference helpers.

These helpers convert optional UI/CLI fields into an inert next-Job preference.
They neither persist a Blueprint nor create a Work Order, budget lease, or
Employee run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dynamic_firm.company import GraphMutationPolicy, GraphUserConstraints


def graph_registry_path(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.stem}.graph-blueprints.db")


def community_blueprint_registry_path(state_path: Path) -> Path:
    """Keep pending public artifacts separate from local execution Blueprints."""

    return state_path.with_name(f"{state_path.stem}.community-blueprints.db")


def graph_constraints_from_args(
    args: argparse.Namespace,
    *,
    existing: GraphUserConstraints,
    include_budget: bool = True,
) -> GraphUserConstraints:
    """Apply explicitly supplied surface fields without persisting a preference."""

    return GraphUserConstraints(
        pinned_employee_ids=(
            tuple(args.pin_employee)
            if getattr(args, "pin_employee", None) is not None
            else existing.pinned_employee_ids
        ),
        excluded_employee_ids=(
            tuple(args.exclude_employee)
            if getattr(args, "exclude_employee", None) is not None
            else existing.excluded_employee_ids
        ),
        require_independent_review=(
            True
            if getattr(args, "require_independent_review", None) is True
            else existing.require_independent_review
        ),
        max_concurrency=(
            args.max_concurrency
            if getattr(args, "max_concurrency", None) is not None
            else existing.max_concurrency
        ),
        max_cost_usd=(
            args.max_cost_usd
            if include_budget and getattr(args, "max_cost_usd", None) is not None
            else existing.max_cost_usd
        ),
        max_wall_time_ms=(
            args.max_wall_time_ms
            if getattr(args, "max_wall_time_ms", None) is not None
            else existing.max_wall_time_ms
        ),
        mutation_policy=(
            GraphMutationPolicy(args.mutation_policy)
            if getattr(args, "mutation_policy", None) is not None
            else existing.mutation_policy
        ),
    )
