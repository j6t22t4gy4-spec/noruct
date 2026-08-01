"""Approval-only Company Settings proposal commands for the Modern terminal."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from dynamic_firm.company import (
    CompanyStateStore,
    EmployeeSkillPatchService,
    EmployeeSkillProcedure,
    PersistentExecutiveManager,
    RosterPatchService,
    decode_active_roster,
)
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.product.modern_tui import ModernTerminalCommandResult


def propose_settings_roster_revision(
    state_path: Path,
    payload: Mapping[str, object],
    *,
    manager_only: bool,
) -> ModernTerminalCommandResult:
    """Create a user-reviewed ROSTER proposal without applying it."""

    required = (
        {"model_profile", "role", "rationale"}
        if manager_only
        else {"employee_id", "model_profile", "role", "capabilities", "rationale"}
    )
    if set(payload) != required:
        return ModernTerminalCommandResult(
            messages=("Company Settings revision payload is malformed.",)
        )
    values: dict[str, str] = {}
    for field in {"employee_id", "model_profile", "role", "rationale"} & required:
        value = payload.get(field)
        if not isinstance(value, str):
            return ModernTerminalCommandResult(
                messages=(f"Company Settings {field} must be a short text value.",)
            )
        normalized = value.strip()
        maximum = 500 if field == "rationale" else 128
        if not normalized or len(normalized.encode("utf-8")) > maximum or "\x00" in normalized:
            return ModernTerminalCommandResult(
                messages=(f"Company Settings {field} is missing or too long.",)
            )
        values[field] = normalized
    raw_capabilities: tuple[str, ...]
    if manager_only:
        raw_capabilities = ()
    else:
        raw = payload.get("capabilities")
        if not isinstance(raw, (list, tuple)) or not raw or len(raw) > 24:
            return ModernTerminalCommandResult(
                messages=("Employee capabilities must be a non-empty list of at most 24 values.",)
            )
        if not all(isinstance(item, str) for item in raw):
            return ModernTerminalCommandResult(
                messages=("Employee capabilities must be text values.",)
            )
        raw_capabilities = tuple(item.strip() for item in raw)
        if any(not item or len(item.encode("utf-8")) > 128 for item in raw_capabilities):
            return ModernTerminalCommandResult(
                messages=("Employee capabilities contain an empty or oversized value.",)
            )
    try:
        with CompanyStateStore(state_path) as company_store:
            roster = decode_active_roster(company_store.roster())
            manager = PersistentExecutiveManager.optional_from_roster(
                roster.employees,
                roster_revision=roster.revision,
            )
            if manager_only and manager is None:
                raise ValueError("Manager revision requires a persistent Manager")
            employee_id = (
                manager.identity.employee_id
                if manager_only and manager is not None
                else values.get("employee_id", "")
            )
            current = next(
                (item for item in roster.employees if item.employee_id == employee_id),
                None,
            )
            if current is None:
                raise ValueError("ROSTER employee does not exist")
            if not manager_only and manager is not None and employee_id == manager.identity.employee_id:
                raise ValueError("Use Manager profile controls for the persistent Manager")
            patch = RosterPatchService(company_store).propose_update_employee(
                EmployeeRecord(
                    employee_id=current.employee_id,
                    role=values["role"],
                    capabilities=current.capabilities if manager_only else raw_capabilities,
                    active=current.active,
                    temporary=current.temporary,
                    model_profile=values["model_profile"],
                ),
                rationale=values["rationale"],
                actor="user:modern-settings",
            )
    except (OSError, ValueError) as exc:
        return ModernTerminalCommandResult(
            messages=(f"Company Settings revision was not proposed · {exc}",)
        )
    label = "Manager" if manager_only else "Employee"
    return ModernTerminalCommandResult(
        messages=(
            f"{label} ROSTER Patch proposed · {patch.patch_id}",
            "It has not changed the active roster or a running Job. Review, then explicitly approve and apply it:",
            f"noruct company roster-preview {patch.patch_id}",
            f"noruct company roster-approve {patch.patch_id} --confirm",
            f"noruct company roster-apply {patch.patch_id} --confirm",
        )
    )


def propose_settings_skill_patch(
    state_path: Path,
    payload: Mapping[str, object],
) -> ModernTerminalCommandResult:
    """Create one user-reviewed Employee Skill Patch without applying it."""

    required = {
        "employee_id", "skill_key", "context_key", "purpose", "steps",
        "verification_steps", "prohibitions", "correction_id", "rationale",
    }
    if set(payload) != required:
        return ModernTerminalCommandResult(
            messages=("Company Skill Patch payload is malformed.",)
        )
    text_values: dict[str, str] = {}
    for field in required - {"steps", "verification_steps", "prohibitions"}:
        value = payload.get(field)
        if not isinstance(value, str):
            return ModernTerminalCommandResult(
                messages=(f"Company Skill Patch {field} must be text.",)
            )
        maximum = 500 if field in {"purpose", "rationale"} else 120
        normalized = value.strip()
        if not normalized or len(normalized.encode("utf-8")) > maximum or "\x00" in normalized:
            return ModernTerminalCommandResult(
                messages=(f"Company Skill Patch {field} is missing or too long.",)
            )
        text_values[field] = normalized

    def string_list(field: str, *, required_value: bool) -> tuple[str, ...] | None:
        raw = payload.get(field)
        if not isinstance(raw, (list, tuple)) or len(raw) > 32 or not all(isinstance(item, str) for item in raw):
            return None
        normalized = tuple(item.strip() for item in raw)
        if any(not item or len(item.encode("utf-8")) > 500 for item in normalized):
            return None
        if required_value and not normalized:
            return None
        return normalized

    steps = string_list("steps", required_value=True)
    verification_steps = string_list("verification_steps", required_value=True)
    prohibitions = string_list("prohibitions", required_value=False)
    if steps is None or verification_steps is None or prohibitions is None:
        return ModernTerminalCommandResult(
            messages=("Skill procedure lists must be bounded non-empty text (except optional prohibitions).",)
        )
    try:
        with CompanyStateStore(state_path) as company_store:
            patch = EmployeeSkillPatchService(company_store).propose_user_correction(
                EmployeeSkillProcedure(
                    employee_id=text_values["employee_id"],
                    skill_key=text_values["skill_key"],
                    context_key=text_values["context_key"],
                    purpose=text_values["purpose"],
                    steps=steps,
                    verification_steps=verification_steps,
                    prohibitions=prohibitions,
                ),
                correction_id=text_values["correction_id"],
                rationale=text_values["rationale"],
                actor="user:modern-settings",
            )
    except (OSError, ValueError) as exc:
        return ModernTerminalCommandResult(
            messages=(f"Company Skill Patch was not proposed · {exc}",)
        )
    return ModernTerminalCommandResult(
        messages=(
            f"Employee Skill Patch proposed · {patch.patch_id}",
            "It has not changed the active employee procedure or a running Job. Review, then explicitly approve and apply it:",
            f"noruct company skill-preview {patch.patch_id}",
            f"noruct company skill-approve {patch.patch_id} --confirm",
            f"noruct company skill-apply {patch.patch_id} --confirm",
        )
    )
