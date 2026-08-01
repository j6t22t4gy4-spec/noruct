from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence

from .models import ActionPolicy, ToolCall, ToolEffect, ToolRisk
from .tools import ToolRegistry


class ToolBatchMode(StrEnum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL = "PARALLEL"


@dataclass(frozen=True, slots=True)
class ToolBatchPlan:
    mode: ToolBatchMode
    reason: str
    call_count: int


class PermissionPreservingToolBatchPlanner:
    """Permit concurrency only when every already-granted call is read-only.

    The rules adapt the registered foundation's conservative batch gating, but use Noruct's
    first-party ToolDefinition and ActionPolicy instead of tool-name heuristics.
    Approval, mutation, unknown tools, invalid arguments and handlers that did
    not explicitly opt in always remain sequential.
    """

    def plan(
        self,
        calls: Sequence[ToolCall],
        *,
        registry: ToolRegistry,
        policy: ActionPolicy,
        prior_tool_counts: Mapping[str, int],
    ) -> ToolBatchPlan:
        if len(calls) <= 1:
            return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "single_call", len(calls))

        grants = {grant.tool_name: grant for grant in policy.tool_grants}
        staged_counts = dict(prior_tool_counts)
        for call in calls:
            definition = registry.get(call.name)
            if definition is None:
                return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "unknown_tool", len(calls))
            grant = grants.get(call.name)
            if grant is None or ToolEffect.READ not in grant.allowed_effects:
                return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "grant_not_read_only", len(calls))
            if definition.effect != ToolEffect.READ:
                return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "mutation_or_external_effect", len(calls))
            if definition.risk != ToolRisk.LOW:
                return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "elevated_risk", len(calls))
            if (
                definition.requires_approval or grant.requires_approval
            ) and not policy.auto_approves(definition.name):
                return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "approval_required", len(calls))
            if not definition.parallel_safe:
                return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "handler_not_opted_in", len(calls))
            try:
                validated = definition.validator(call.arguments)
                resource_key = definition.resource_key(validated)
            except (KeyError, TypeError, ValueError):
                return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "arguments_not_validated", len(calls))
            if not any(
                fnmatch.fnmatchcase(resource_key, pattern)
                for pattern in grant.resource_patterns
            ):
                return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "resource_not_granted", len(calls))
            staged_counts[call.name] = staged_counts.get(call.name, 0) + 1
            if staged_counts[call.name] > grant.max_calls:
                return ToolBatchPlan(ToolBatchMode.SEQUENTIAL, "call_limit", len(calls))

        return ToolBatchPlan(ToolBatchMode.PARALLEL, "independent_read_only", len(calls))
