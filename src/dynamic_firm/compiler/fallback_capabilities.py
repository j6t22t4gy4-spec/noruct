"""Deterministic capability selection and shape checks for compiler fallback."""

from __future__ import annotations

import re

from dynamic_firm.kernel.models import JobTask, PlanProposal

from .models import CompilerRequest


_ACTION_CAPABILITY_NAMES = frozenset(
    {
        "host_action", "command_execution", "process_execution",
        "terminal_execution", "workspace_write", "external_action",
        "browser_action", "settings_change", "messaging", "deployment",
    }
)
_ACTION_CAPABILITY_PARTS = frozenset(
    {
        "action", "browser", "command", "delete", "deploy", "execute",
        "external", "install", "message", "messaging", "mutation", "process",
        "publish", "settings", "terminal", "write",
    }
)


def host_action_capability(request: CompilerRequest) -> str:
    if request.required_final_action_capability:
        return request.required_final_action_capability
    available = {capability.strip() for capability in request.available_capabilities if capability.strip()}
    for preferred in (
        "host_action", "command_execution", "process_execution", "terminal_execution",
        "external_action", "browser_action", "settings_change", "messaging",
        "deployment", "general_reasoning", "conversation",
    ):
        if preferred in available:
            return preferred
    return solo_first_capability(request)


def valid_host_action_shape(proposal: PlanProposal, request: CompilerRequest) -> bool:
    action_capability = host_action_capability(request)
    final = next(task for task in proposal.tasks if task.task_id == proposal.final_task_id)
    if action_capability not in final.required_capabilities:
        return False
    return all(
        task.task_id == proposal.final_task_id
        or not any(
            capability == action_capability
            or capability == "implementation"
            or capability_may_own_effect(capability)
            for capability in task.required_capabilities
        )
        for task in proposal.tasks
    )


def capability_may_own_effect(capability: str) -> bool:
    return capability in _ACTION_CAPABILITY_NAMES or bool(
        set(capability.split("_")) & _ACTION_CAPABILITY_PARTS
    )


def has_required_review_boundary(proposal: PlanProposal) -> bool:
    final = next(task for task in proposal.tasks if task.task_id == proposal.final_task_id)
    tasks = {task.task_id: task for task in proposal.tasks}
    return any(
        dependency in tasks and task_has_review_capability(tasks[dependency])
        for dependency in final.depends_on
    )


def task_has_review_capability(task: JobTask) -> bool:
    return any(
        capability in {"review", "independent_review", "validation", "verification"}
        or capability.endswith("_review")
        for capability in task.required_capabilities
    )


def review_capability(request: CompilerRequest) -> str | None:
    for capability in sorted({item.strip() for item in request.available_capabilities if item.strip()}):
        if (
            capability in {"review", "independent_review", "validation", "verification"}
            or capability.endswith("_review")
        ):
            return capability
    return "independent_review" if request.max_temporary_roles >= 1 else None


def reporting_capability(request: CompilerRequest) -> str:
    available = {capability.strip() for capability in request.available_capabilities if capability.strip()}
    for preferred in ("conversation", "general_reasoning", "repository_analysis", "evidence_synthesis"):
        if preferred in available:
            return preferred
    return sorted(available)[0] if available else "conversation"


def solo_first_capability(request: CompilerRequest) -> str:
    available = tuple(sorted({item.strip() for item in request.available_capabilities if item.strip()}))
    if not available:
        return "repository_analysis"
    goal_tokens = tuple(token for token in re.findall(r"[a-z0-9]+", request.goal.lower()) if len(token) >= 3)

    def score(capability: str) -> tuple[int, int, str]:
        parts = tuple(part for part in capability.split("_") if len(part) >= 3)
        matches = sum(
            1
            for part in parts
            if any(
                token == part
                or (len(part) >= 4 and token.startswith(part))
                or (len(token) >= 4 and part.startswith(token))
                for token in goal_tokens
            )
        )
        return matches, len(parts), capability

    best = max(available, key=score)
    if score(best)[0] > 0:
        return best
    if "repository_analysis" in available:
        return "repository_analysis"
    return available[0]
