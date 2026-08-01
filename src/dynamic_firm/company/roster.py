from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from dynamic_firm.kernel.models import EmployeeRecord

from .models import RosterVersion


_EMPLOYEE_FIELDS = frozenset(
    {
        "employee_id",
        "role",
        "capabilities",
        "active",
        "temporary",
        "model_profile",
    }
)
_LEGACY_DEFAULT_EXECUTION_PROFILES = frozenset({"company-default", "roster-default"})


class RosterSnapshotError(ValueError):
    """The active persistent roster cannot safely authorize execution."""


@dataclass(frozen=True, slots=True)
class ActiveRosterSnapshot:
    """Validated, immutable active employees from one persisted ROSTER revision."""

    revision: int
    parent_revision: int | None
    employees: tuple[EmployeeRecord, ...]
    total_employee_count: int
    created_at: str

    @property
    def active_employee_count(self) -> int:
        return len(self.employees)

    @property
    def available_capabilities(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    capability
                    for employee in self.employees
                    for capability in employee.capabilities
                }
            )
        )

    def resolve_execution_profiles(self, default_execution_class: str) -> tuple[EmployeeRecord, ...]:
        """Apply legacy config only to explicitly legacy default profiles."""

        selected = _required_text(default_execution_class, "default execution class")
        return tuple(
            replace(employee, model_profile=selected)
            if employee.model_profile in _LEGACY_DEFAULT_EXECUTION_PROFILES
            else employee
            for employee in self.employees
        )


def decode_active_roster(version: RosterVersion) -> ActiveRosterSnapshot:
    """Fail closed while converting persisted mappings into an execution snapshot."""

    if type(version.revision) is not int or version.revision < 1:
        raise RosterSnapshotError("ROSTER revision must be a positive integer")
    if not version.created_at.strip():
        raise RosterSnapshotError("ROSTER created_at must be non-empty")

    decoded: list[EmployeeRecord] = []
    employee_ids: set[str] = set()
    for index, raw in enumerate(version.employees):
        employee = _decode_employee(raw, index=index)
        if employee.employee_id in employee_ids:
            raise RosterSnapshotError(
                f"ROSTER employee ids must be unique: {employee.employee_id}"
            )
        employee_ids.add(employee.employee_id)
        if employee.temporary:
            raise RosterSnapshotError(
                "Persistent ROSTER cannot contain temporary employees: "
                f"{employee.employee_id}"
            )
        if employee.active:
            decoded.append(employee)

    if not decoded:
        raise RosterSnapshotError(
            "Active ROSTER requires at least one active persistent employee"
        )
    return ActiveRosterSnapshot(
        revision=version.revision,
        parent_revision=version.parent_revision,
        employees=tuple(decoded),
        total_employee_count=len(version.employees),
        created_at=version.created_at,
    )


def _decode_employee(raw: Mapping[str, Any], *, index: int) -> EmployeeRecord:
    if not isinstance(raw, Mapping):
        raise RosterSnapshotError(f"ROSTER employee {index} must be an object")
    fields = set(raw)
    if fields != _EMPLOYEE_FIELDS:
        missing = sorted(_EMPLOYEE_FIELDS - fields)
        unknown = sorted(fields - _EMPLOYEE_FIELDS)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise RosterSnapshotError(
            f"ROSTER employee {index} has an invalid schema ({'; '.join(details)})"
        )

    capabilities_value = raw["capabilities"]
    if not isinstance(capabilities_value, (list, tuple)):
        raise RosterSnapshotError(
            f"ROSTER employee {index} capabilities must be an array"
        )
    capabilities = tuple(
        _required_text(value, f"ROSTER employee {index} capability")
        for value in capabilities_value
    )
    if not capabilities:
        raise RosterSnapshotError(
            f"ROSTER employee {index} requires at least one capability"
        )
    if len(capabilities) != len(set(capabilities)):
        raise RosterSnapshotError(
            f"ROSTER employee {index} capabilities must be unique"
        )

    active = raw["active"]
    temporary = raw["temporary"]
    if type(active) is not bool:
        raise RosterSnapshotError(f"ROSTER employee {index} active must be boolean")
    if type(temporary) is not bool:
        raise RosterSnapshotError(
            f"ROSTER employee {index} temporary must be boolean"
        )
    return EmployeeRecord(
        employee_id=_required_text(raw["employee_id"], f"ROSTER employee {index} id"),
        role=_required_text(raw["role"], f"ROSTER employee {index} role"),
        capabilities=capabilities,
        active=active,
        temporary=temporary,
        model_profile=_required_text(
            raw["model_profile"], f"ROSTER employee {index} model_profile"
        ),
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RosterSnapshotError(f"{field} must be a non-empty string")
    return value.strip()
