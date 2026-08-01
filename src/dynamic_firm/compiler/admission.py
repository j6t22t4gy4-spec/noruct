from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from dynamic_firm.kernel.models import ReplanContext, TaskStatus
from dynamic_firm.runtime.models import RunStatus, SignalCode


_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class OrganizationAdmissionReason(StrEnum):
    TYPED_CAPABILITY_GAP = "TYPED_CAPABILITY_GAP"
    SIGNAL_NOT_SUPPORTED = "SIGNAL_NOT_SUPPORTED"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    ATTEMPT_NOT_SUCCESSFUL = "ATTEMPT_NOT_SUCCESSFUL"
    CAPABILITY_ALREADY_ASSIGNED = "CAPABILITY_ALREADY_ASSIGNED"
    FINAL_TASK_UNAVAILABLE = "FINAL_TASK_UNAVAILABLE"
    TASK_LIMIT_INSUFFICIENT = "TASK_LIMIT_INSUFFICIENT"
    TEMPORARY_ROLE_LIMIT_EXHAUSTED = "TEMPORARY_ROLE_LIMIT_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class OrganizationAdmissionDecision:
    admitted: bool
    reason: OrganizationAdmissionReason
    capability: str = ""
    trigger_task_id: str = ""
    graph_version: int = 0
    expands_final_task: bool = False


class TypedCapabilityAdmissionPolicy:
    """Admit organization expansion only from one bounded typed capability gap."""

    def decide(self, context: ReplanContext) -> OrganizationAdmissionDecision:
        base = {
            "trigger_task_id": context.trigger_task.task_id,
            "graph_version": context.graph.version,
            "expands_final_task": (
                context.trigger_task.task_id == context.graph.final_task_id
            ),
        }
        if context.signal.code != SignalCode.CAPABILITY_MISSING:
            return OrganizationAdmissionDecision(
                False,
                OrganizationAdmissionReason.SIGNAL_NOT_SUPPORTED,
                **base,
            )
        capability = context.signal.value.strip()
        if not _CAPABILITY.fullmatch(capability):
            return OrganizationAdmissionDecision(
                False,
                OrganizationAdmissionReason.CAPABILITY_INVALID,
                capability=capability,
                **base,
            )
        result = context.trigger_task.runtime_result
        if (
            context.trigger_task.status != TaskStatus.SUCCEEDED
            or result is None
            or result.status != RunStatus.SUCCEEDED
        ):
            return OrganizationAdmissionDecision(
                False,
                OrganizationAdmissionReason.ATTEMPT_NOT_SUCCESSFUL,
                capability=capability,
                **base,
            )
        assignee = next(
            (
                employee
                for employee in context.roster
                if employee.employee_id == context.trigger_task.assignee_id
            ),
            None,
        )
        if assignee is not None and capability in assignee.capabilities:
            return OrganizationAdmissionDecision(
                False,
                OrganizationAdmissionReason.CAPABILITY_ALREADY_ASSIGNED,
                capability=capability,
                **base,
            )
        if any(
            capability in task.required_capabilities
            for task in context.graph.tasks
        ):
            return OrganizationAdmissionDecision(
                False,
                OrganizationAdmissionReason.CAPABILITY_ALREADY_ASSIGNED,
                capability=capability,
                **base,
            )
        final = next(
            (
                task
                for task in context.graph.tasks
                if task.task_id == context.graph.final_task_id
            ),
            None,
        )
        if final is None or (
            final.task_id != context.trigger_task.task_id
            and final.status != TaskStatus.PENDING
        ):
            return OrganizationAdmissionDecision(
                False,
                OrganizationAdmissionReason.FINAL_TASK_UNAVAILABLE,
                capability=capability,
                **base,
            )
        required_new_tasks = 2 if final.task_id == context.trigger_task.task_id else 1
        if len(context.graph.tasks) + required_new_tasks > context.request.job_limits.max_tasks:
            return OrganizationAdmissionDecision(
                False,
                OrganizationAdmissionReason.TASK_LIMIT_INSUFFICIENT,
                capability=capability,
                **base,
            )
        capable = any(
            employee.active and capability in employee.capabilities
            for employee in context.roster
        )
        temporary_count = sum(employee.temporary for employee in context.roster)
        if (
            not capable
            and temporary_count >= context.request.job_limits.max_temporary_roles
        ):
            return OrganizationAdmissionDecision(
                False,
                OrganizationAdmissionReason.TEMPORARY_ROLE_LIMIT_EXHAUSTED,
                capability=capability,
                **base,
            )
        return OrganizationAdmissionDecision(
            True,
            OrganizationAdmissionReason.TYPED_CAPABILITY_GAP,
            capability=capability,
            **base,
        )
