from __future__ import annotations

from dataclasses import dataclass

from .models import EmployeeRecord, JobTask


@dataclass(frozen=True, slots=True)
class StaffingDecision:
    employee: EmployeeRecord | None
    created_temporary: bool = False


def staff_task(
    task: JobTask,
    roster: tuple[EmployeeRecord, ...],
    *,
    busy_employee_ids: set[str],
    pinned_employee_ids: set[str] | None = None,
    excluded_employee_ids: set[str] | None = None,
    job_id: str,
    temporary_roles_created: int,
    max_temporary_roles: int,
    model_profile: str = "scripted",
) -> StaffingDecision:
    pinned = pinned_employee_ids or set()
    excluded = excluded_employee_ids or set()
    required = set(task.required_capabilities)
    capable = [
        employee
        for employee in roster
        if employee.active
        and employee.employee_id not in excluded
        and required.issubset(employee.capabilities)
    ]
    available = [
        employee for employee in capable if employee.employee_id not in busy_employee_ids
    ]
    if available:
        # A pin is a deterministic preference, not an unsafe force: the
        # employee still needs the requested capability and must be idle.
        employee = min(
            available,
            key=lambda item: (
                0 if item.employee_id in pinned else 1,
                len(item.capabilities),
                item.employee_id,
            ),
        )
        return StaffingDecision(employee)
    if capable:
        return StaffingDecision(None)
    if temporary_roles_created >= max_temporary_roles:
        return StaffingDecision(None)
    capability_slug = "-".join(sorted(required)) or "general"
    sequence = temporary_roles_created + 1
    return StaffingDecision(
        EmployeeRecord(
            employee_id=f"temp-{job_id}-{sequence}-{capability_slug}",
            role=f"Temporary {capability_slug.replace('-', ' ').title()} Specialist",
            capabilities=tuple(sorted(required)),
            temporary=True,
            model_profile=model_profile,
        ),
        created_temporary=True,
    )
