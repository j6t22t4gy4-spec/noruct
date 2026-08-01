from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from dynamic_firm.kernel.graph import GraphValidationError, graph_from_proposal
from dynamic_firm.kernel.models import (
    ExecutionReplicaAggregation,
    ExecutionReplicaSpec,
    ExecutionReplicaStrategy,
    JobTask,
    PlanProposal,
)


_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ROOT_FIELDS = {"mode", "rationale", "assumptions", "tasks", "final_task_id"}
_TASK_REQUIRED_FIELDS = {
    "task_id",
    "objective",
    "depends_on",
    "required_capabilities",
    "acceptance_criteria",
    "risk_level",
}
_TASK_OPTIONAL_FIELDS = {"execution_replica"}
_REPLICA_FIELDS = {
    "group_id",
    "replica_id",
    "strategy",
    "scope",
    "aggregation_task_id",
    "aggregation",
    "marginal_value_reason",
}


class PlanOutputError(ValueError):
    """Structured output failed the exact field or type contract."""


class PlanProposalError(ValueError):
    """A well-shaped output violated company planning invariants."""


@dataclass(frozen=True, slots=True)
class ParsedPlan:
    proposal: PlanProposal
    source_mode: str
    rationale: str


def plan_json_schema(*, max_tasks: int = 6) -> dict[str, Any]:
    # Keep the wire schema inside the conservative structured-output subset.
    # Length, pattern, item-count, and uniqueness constraints are enforced by
    # parse_plan_proposal after transport; several authenticated agent backends
    # reject those JSON Schema keywords before a model response is produced.
    string_array = {
        "type": "array",
        "items": {"type": "string"},
    }
    task = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "objective": {"type": "string"},
            "depends_on": string_array,
            "required_capabilities": string_array,
            "acceptance_criteria": string_array,
            "risk_level": {"type": "string", "enum": ["LOW"]},
            "execution_replica": {
                "type": ["object", "null"],
                "properties": {
                    "group_id": {"type": "string"},
                    "replica_id": {"type": "string"},
                    "strategy": {
                        "type": "string",
                        "enum": [item.value for item in ExecutionReplicaStrategy],
                    },
                    "scope": {"type": "string"},
                    "aggregation_task_id": {"type": "string"},
                    "aggregation": {
                        "type": "string",
                        "enum": [item.value for item in ExecutionReplicaAggregation],
                    },
                    "marginal_value_reason": {"type": "string"},
                },
                "required": sorted(_REPLICA_FIELDS),
                "additionalProperties": False,
            },
        },
        # Strict structured-output providers require every property to be
        # present; null is the explicit non-replica value. The local parser
        # still accepts omission for older provider fixtures and saved plans.
        "required": sorted(_TASK_REQUIRED_FIELDS | _TASK_OPTIONAL_FIELDS),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["SOLO", "GRAPH"]},
            "rationale": {"type": "string"},
            "assumptions": string_array,
            "tasks": {
                "type": "array",
                "items": task,
            },
            "final_task_id": {"type": "string"},
        },
        "required": sorted(_ROOT_FIELDS),
        "additionalProperties": False,
    }


def parse_plan_proposal(
    value: Mapping[str, Any],
    *,
    proposal_id: str,
    goal: str,
    max_tasks: int,
    available_capabilities: tuple[str, ...],
    max_temporary_roles: int,
) -> ParsedPlan:
    if not isinstance(value, Mapping):
        raise PlanOutputError("Plan output must be an object")
    _exact_fields(value, _ROOT_FIELDS, "plan")
    mode = _string(value["mode"], "mode", max_length=16)
    if mode not in {"SOLO", "GRAPH"}:
        raise PlanOutputError("mode must be SOLO or GRAPH")
    rationale = _string(value["rationale"], "rationale", max_length=1000)
    assumptions = _string_list(
        value["assumptions"],
        "assumptions",
        maximum=8,
        item_max_length=500,
        allow_empty=True,
    )
    raw_tasks = value["tasks"]
    if not isinstance(raw_tasks, list):
        raise PlanOutputError("tasks must be an array")
    if not raw_tasks or len(raw_tasks) > max_tasks:
        raise PlanProposalError(f"Plan must contain between 1 and {max_tasks} tasks")
    tasks = tuple(_parse_task(item, index) for index, item in enumerate(raw_tasks))
    final_task_id = _slug(value["final_task_id"], "final_task_id")

    if mode == "SOLO" and (len(tasks) != 1 or tasks[0].task_id != final_task_id):
        raise PlanProposalError("SOLO mode requires exactly one final task")
    if mode == "GRAPH" and len(tasks) < 2:
        raise PlanProposalError("GRAPH mode requires at least two tasks")
    if mode == "GRAPH":
        final = next((task for task in tasks if task.task_id == final_task_id), None)
        if final is not None and not final.depends_on:
            raise PlanProposalError("GRAPH final task requires at least one dependency")

    proposal = PlanProposal(
        proposal_id=proposal_id,
        goal=goal,
        tasks=tasks,
        final_task_id=final_task_id,
        assumptions=assumptions,
    )
    try:
        graph_from_proposal(proposal, max_tasks=max_tasks)
    except GraphValidationError as exc:
        raise PlanProposalError(str(exc)) from None

    available = set(available_capabilities)
    missing = {
        capability
        for task in tasks
        for capability in task.required_capabilities
        if capability not in available
    }
    if len(missing) > max_temporary_roles:
        raise PlanProposalError(
            f"Plan requires {len(missing)} temporary capabilities; limit is {max_temporary_roles}"
        )
    return ParsedPlan(proposal=proposal, source_mode=mode, rationale=rationale)


def _parse_task(value: object, index: int) -> JobTask:
    if not isinstance(value, Mapping):
        raise PlanOutputError(f"tasks[{index}] must be an object")
    prefix = f"tasks[{index}]"
    _required_and_optional_fields(
        value,
        required=_TASK_REQUIRED_FIELDS,
        optional=_TASK_OPTIONAL_FIELDS,
        name=prefix,
    )
    risk_level = _string(value["risk_level"], f"{prefix}.risk_level", max_length=16)
    if risk_level != "LOW":
        raise PlanProposalError(f"{prefix}.risk_level must be LOW")
    replica = _parse_execution_replica(value.get("execution_replica"), prefix)
    return JobTask(
        task_id=_slug(value["task_id"], f"{prefix}.task_id"),
        objective=_string(value["objective"], f"{prefix}.objective", max_length=2000),
        depends_on=_slug_list(value["depends_on"], f"{prefix}.depends_on", maximum=6),
        required_capabilities=_slug_list(
            value["required_capabilities"],
            f"{prefix}.required_capabilities",
            maximum=4,
            allow_empty=False,
        ),
        acceptance_criteria=_string_list(
            value["acceptance_criteria"],
            f"{prefix}.acceptance_criteria",
            maximum=8,
            item_max_length=500,
            allow_empty=False,
        ),
        risk_level=risk_level,
        execution_replica=replica,
    )


def _parse_execution_replica(value: object, prefix: str) -> ExecutionReplicaSpec | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PlanOutputError(f"{prefix}.execution_replica must be an object or null")
    name = f"{prefix}.execution_replica"
    _exact_fields(value, _REPLICA_FIELDS, name)
    try:
        strategy = ExecutionReplicaStrategy(
            _string(value["strategy"], f"{name}.strategy", max_length=32)
        )
        aggregation = ExecutionReplicaAggregation(
            _string(value["aggregation"], f"{name}.aggregation", max_length=32)
        )
    except ValueError as exc:
        raise PlanOutputError(f"{name} has an unsupported enum value") from exc
    return ExecutionReplicaSpec(
        group_id=_slug(value["group_id"], f"{name}.group_id"),
        replica_id=_slug(value["replica_id"], f"{name}.replica_id"),
        strategy=strategy,
        scope=_string(value["scope"], f"{name}.scope", max_length=500),
        aggregation_task_id=_slug(
            value["aggregation_task_id"], f"{name}.aggregation_task_id"
        ),
        aggregation=aggregation,
        marginal_value_reason=_string(
            value["marginal_value_reason"],
            f"{name}.marginal_value_reason",
            max_length=500,
        ),
    )


def _exact_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise PlanOutputError(f"{name} field names must be strings")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise PlanOutputError(f"{name} fields are invalid: {'; '.join(details)}")


def _required_and_optional_fields(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
    name: str,
) -> None:
    if any(not isinstance(key, str) for key in value):
        raise PlanOutputError(f"{name} field names must be strings")
    actual = set(value)
    missing = sorted(required - actual)
    unknown = sorted(actual - required - optional)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise PlanOutputError(f"{name} fields are invalid: {'; '.join(details)}")


def _string(value: object, name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise PlanOutputError(f"{name} must be a non-empty string up to {max_length} characters")
    return value.strip()


def _slug(value: object, name: str) -> str:
    text = _string(value, name, max_length=64)
    if not _SLUG.fullmatch(text):
        raise PlanOutputError(f"{name} must be a lowercase slug")
    return text


def _string_list(
    value: object,
    name: str,
    *,
    maximum: int,
    item_max_length: int,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PlanOutputError(f"{name} must be an array")
    if len(value) > maximum or (not allow_empty and not value):
        raise PlanOutputError(f"{name} must contain between {0 if allow_empty else 1} and {maximum} items")
    items = tuple(_string(item, f"{name}[]", max_length=item_max_length) for item in value)
    if len(items) != len(set(items)):
        raise PlanOutputError(f"{name} cannot contain duplicates")
    return items


def _slug_list(
    value: object,
    name: str,
    *,
    maximum: int,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PlanOutputError(f"{name} must be an array")
    if len(value) > maximum or (not allow_empty and not value):
        raise PlanOutputError(f"{name} has an invalid item count")
    items = tuple(_slug(item, f"{name}[]") for item in value)
    if len(items) != len(set(items)):
        raise PlanOutputError(f"{name} cannot contain duplicates")
    return items
