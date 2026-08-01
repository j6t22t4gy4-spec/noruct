from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from dynamic_firm.kernel.models import (
    GraphPatchEvent,
    GraphPatchProposalEvent,
    JobMutationEvent,
    TaskAssignmentEvent,
    TaskMutationType,
)
from dynamic_firm.runtime.models import EventType, RunEvent


class ProductEventType(StrEnum):
    INPUT_ROUTED = "INPUT_ROUTED"
    WORKSPACE_IDENTITY = "WORKSPACE_IDENTITY"
    CAPABILITY_READY = "CAPABILITY_READY"
    COMPILER_STARTED = "COMPILER_STARTED"
    PLAN_ACCEPTED = "PLAN_ACCEPTED"
    PLAN_FALLBACK = "PLAN_FALLBACK"
    FIRM_ADMISSION = "FIRM_ADMISSION"
    ORGANIZATION_ADMISSION = "ORGANIZATION_ADMISSION"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    GRAPH_PATCH_APPLIED = "GRAPH_PATCH_APPLIED"
    EMPLOYEE_STARTED = "EMPLOYEE_STARTED"
    MODEL_WORKING = "MODEL_WORKING"
    MODEL_STREAMING = "MODEL_STREAMING"
    CONTEXT_COMPACTED = "CONTEXT_COMPACTED"
    TOOL_BATCH_PLANNED = "TOOL_BATCH_PLANNED"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"
    VALIDATION_RECORDED = "VALIDATION_RECORDED"
    TOOL_RUNNING = "TOOL_RUNNING"
    TOOL_FINISHED = "TOOL_FINISHED"
    EMPLOYEE_FINISHED = "EMPLOYEE_FINISHED"
    TASK_RETRY = "TASK_RETRY"
    TASK_REROUTED = "TASK_REROUTED"
    JOB_FINISHED = "JOB_FINISHED"
    LEARNING_PROJECTION_FAILED = "LEARNING_PROJECTION_FAILED"


@dataclass(frozen=True, slots=True)
class ProductEvent:
    type: ProductEventType
    message: str
    job_id: str = ""
    task_id: str = ""
    employee_id: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


def product_event_from_run(event: RunEvent) -> ProductEvent | None:
    base = {
        "job_id": event.job_id,
        "task_id": event.task_id,
        "employee_id": event.employee_id,
        "data": dict(event.payload),
    }
    if event.type == EventType.RUN_STARTED:
        return ProductEvent(
            ProductEventType.EMPLOYEE_STARTED,
            f"{event.employee_id} started {event.task_id}",
            **base,
        )
    if event.type == EventType.MODEL_CALL_STARTED:
        return ProductEvent(
            ProductEventType.MODEL_WORKING,
            f"{event.employee_id} is reasoning",
            **base,
        )
    if event.type == EventType.MODEL_RECOVERY_REQUESTED:
        attempt = int(event.payload.get("attempt", 1) or 1)
        maximum = int(event.payload.get("max_consecutive_errors", 1) or 1)
        return ProductEvent(
            ProductEventType.MODEL_WORKING,
            f"No model reply · recovery {attempt}/{maximum}",
            **base,
        )
    if event.type == EventType.MODEL_TEXT_DELTA:
        return ProductEvent(
            ProductEventType.MODEL_STREAMING,
            str(event.payload.get("text", "")),
            **{**base, "data": {**base["data"], "stream_kind": "text_delta"}},
        )
    if event.type == EventType.MODEL_STREAM_PROGRESS:
        received = int(event.payload.get("received_chars", 0) or 0)
        return ProductEvent(
            ProductEventType.MODEL_STREAMING,
            f"Receiving model response · {received} chars",
            **{**base, "data": {**base["data"], "stream_kind": "progress"}},
        )
    if event.type == EventType.CONTEXT_COMPACTED:
        removed = int(event.payload.get("removed_message_count", 0) or 0)
        return ProductEvent(
            ProductEventType.CONTEXT_COMPACTED,
            f"Compacted {removed} historical messages",
            **base,
        )
    if event.type == EventType.TOOL_BATCH_PLANNED:
        mode = str(event.payload.get("mode", "SEQUENTIAL")).lower()
        count = int(event.payload.get("call_count", 0) or 0)
        return ProductEvent(
            ProductEventType.TOOL_BATCH_PLANNED,
            f"{mode} tool batch · {count} calls",
            **base,
        )
    if event.type == EventType.TOOL_INTENT_RECORDED:
        tool_name = str(event.payload.get("tool_name", "tool"))
        return ProductEvent(
            ProductEventType.TOOL_REQUESTED,
            f"{event.employee_id} requested {tool_name}",
            **base,
        )
    if event.type == EventType.APPROVAL_REQUIRED:
        preview = str(event.payload.get("preview", "Action approval required"))
        return ProductEvent(ProductEventType.APPROVAL_REQUIRED, preview, **base)
    if event.type == EventType.APPROVAL_RESOLVED:
        decision = str(event.payload.get("decision", "resolved"))
        return ProductEvent(
            ProductEventType.APPROVAL_RESOLVED,
            f"Approval {decision.lower().replace('_', ' ')}",
            **base,
        )
    if event.type == EventType.VALIDATION_RECORDED:
        passed = event.payload.get("passed") is True
        validation_kind = str(
            event.payload.get("validation_kind", "candidate")
        )
        failed_checks = event.payload.get("failed_checks", ())
        first_failed = (
            str(failed_checks[0])
            if isinstance(failed_checks, list) and failed_checks
            else ""
        )
        name = str(
            event.payload.get("name")
            or first_failed
            or (
                "completion-contract"
                if validation_kind == "completion"
                else "validation"
            )
        )
        attempt = int(event.payload.get("attempt", 1) or 1)
        return ProductEvent(
            ProductEventType.VALIDATION_RECORDED,
            f"{name} {'passed' if passed else 'failed'}",
            job_id=event.job_id,
            task_id=event.task_id,
            employee_id=event.employee_id,
            data={
                **base["data"],
                "attempt": attempt,
                "name": name,
                "passed": passed,
                "validation_kind": validation_kind,
                "failed_checks": failed_checks,
            },
        )
    if event.type == EventType.TOOL_STARTED:
        tool_name = str(event.payload.get("tool_name", "tool"))
        return ProductEvent(
            ProductEventType.TOOL_RUNNING,
            f"Running {tool_name}",
            **base,
        )
    if event.type in {EventType.TOOL_SUCCEEDED, EventType.TOOL_FAILED}:
        tool_name = str(event.payload.get("tool_name", "tool"))
        outcome = "completed" if event.type == EventType.TOOL_SUCCEEDED else "failed"
        return ProductEvent(
            ProductEventType.TOOL_FINISHED,
            f"{tool_name} {outcome}",
            **base,
        )
    if event.type in {
        EventType.RUN_SUCCEEDED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
        EventType.RUN_BUDGET_EXHAUSTED,
    }:
        outcome = event.type.value.removeprefix("RUN_").lower().replace("_", " ")
        return ProductEvent(
            ProductEventType.EMPLOYEE_FINISHED,
            f"{event.employee_id} {outcome}: {event.task_id}",
            **base,
        )
    return None


def product_event_from_mutation(event: JobMutationEvent) -> ProductEvent:
    is_retry = event.mutation_type == TaskMutationType.RETRY
    return ProductEvent(
        ProductEventType.TASK_RETRY if is_retry else ProductEventType.TASK_REROUTED,
        (
            f"Retrying {event.task_id} with {event.to_employee_id}"
            if is_retry
            else (
                f"Rerouting {event.task_id} from {event.from_employee_id} "
                f"to {event.to_employee_id}"
            )
        ),
        task_id=event.task_id,
        employee_id=event.to_employee_id,
        data={
            "mutation_type": event.mutation_type.value,
            "failure_kind": event.failure_kind.value,
            "source_attempt": event.source_attempt_sequence,
            "target_attempt": event.target_attempt_sequence,
            "from_employee_id": event.from_employee_id,
            "to_employee_id": event.to_employee_id,
            "event_id": event.event_id,
        },
    )


def product_event_from_assignment(event: TaskAssignmentEvent) -> ProductEvent:
    tenure = "temporary" if event.employee_temporary else "persistent"
    role = event.employee_role or event.employee_id
    assignment_label = (
        f"{role} replica {event.replica_id} assigned {event.task_id} · "
        f"{event.replica_strategy.lower()}"
        if event.replica_group_id
        else f"{role} assigned {event.task_id} · {tenure}"
    )
    return ProductEvent(
        ProductEventType.TASK_ASSIGNED,
        assignment_label,
        job_id=event.job_id,
        task_id=event.task_id,
        employee_id=event.employee_id,
        data={
            "graph_version": event.graph_version,
            "employee_role": role,
            "employee_temporary": event.employee_temporary,
            "employee_tenure": tenure,
            "required_capabilities": event.required_capabilities,
            "depends_on": event.depends_on,
            "attempt": event.attempt,
            "final_task": event.final_task,
            "selection_reason": event.selection_reason,
            "active_task_count": event.active_task_count,
            "capability_profile_digest": event.capability_profile_digest,
            "capability_material_digest": event.capability_material_digest,
            "task_relevance": event.task_relevance,
            "chosen_over_employee_ids": event.chosen_over_employee_ids,
            "profile_difference": event.profile_difference,
            "execution_instance_id": event.execution_instance_id,
            "replica_group_id": event.replica_group_id,
            "replica_id": event.replica_id,
            "replica_strategy": event.replica_strategy,
            "replica_scope": event.replica_scope,
            "replica_value_reason": event.replica_value_reason,
        },
    )


def product_event_from_graph_patch(
    event: GraphPatchEvent,
    *,
    job_id: str = "",
) -> ProductEvent:
    operation = event.patch.semantic_operation.value
    if operation == "INSERT":
        count = len(event.added_task_ids)
        message = (
            f"Organization expanded · {count} task{'s' if count != 1 else ''} added"
            f" · lease Δ${event.mutation_lease.cost_usd:.4f}"
        )
    else:
        message = f"Execution structure {operation.lower()} applied"
    return ProductEvent(
        ProductEventType.GRAPH_PATCH_APPLIED,
        message,
        job_id=job_id,
        task_id=event.patch.trigger_task_id,
        data={
            "event_id": event.event_id,
            "patch_id": event.patch.patch_id,
            "semantic_operation": operation,
            "rationale": event.patch.rationale,
            "expected_gain": event.patch.expected_gain,
            "base_graph_version": event.patch.base_graph_version,
            "target_graph_version": event.target_graph_version,
            "trigger_task_id": event.patch.trigger_task_id,
            "added_task_ids": event.added_task_ids,
            "cancelled_task_ids": event.cancelled_task_ids,
            "mutation_lease": {
                "model_calls": event.mutation_lease.model_calls,
                "tool_calls": event.mutation_lease.tool_calls,
                "cost_usd": event.mutation_lease.cost_usd,
            },
        },
    )


def product_event_from_graph_patch_proposal(
    event: GraphPatchProposalEvent,
    *,
    job_id: str = "",
) -> ProductEvent:
    """Project a resolved PROPOSE decision without exposing patch prose."""

    return ProductEvent(
        ProductEventType.APPROVAL_RESOLVED,
        f"Graph proposal {event.status.value.lower()}",
        job_id=job_id,
        task_id=event.patch.trigger_task_id,
        data={
            "action_id": event.event_id,
            "decision": event.status.value,
            "approval_kind": "GRAPH_PATCH_PROPOSAL",
            "patch_id": event.patch.patch_id,
            "semantic_operation": event.patch.semantic_operation.value,
            "base_graph_version": event.patch.base_graph_version,
            "proposed_lease": {
                "model_calls": event.proposed_lease.model_calls,
                "tool_calls": event.proposed_lease.tool_calls,
                "cost_usd": event.proposed_lease.cost_usd,
            },
        },
    )
