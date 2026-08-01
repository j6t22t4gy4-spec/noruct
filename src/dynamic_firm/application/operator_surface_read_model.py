"""Read-only Company operator projection shared by terminal and loopback UI.

This component joins existing Company and runtime evidence into the established
``noruct.operator-surface.v1`` payload. It is presentation-only: it cannot
assemble a provider, mutate a Job, alter an Artifact, resolve an approval, or
write Knowledge/Evolution state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.application.modern_terminal_operator_state import (
    inspect_operator_state,
)
from dynamic_firm.company import (
    CompanyStateStore,
    PersistentExecutiveManager,
    decode_active_roster,
    manager_operating_report,
)
from dynamic_firm.product.operator_surface import build_operator_surface_snapshot
from dynamic_firm.runtime.company_budget import CompanyCostBudgetPolicy


@dataclass(frozen=True, slots=True)
class OperatorSurfaceReadModel:
    """Read-only facts needed by an operator renderer and Settings inventory."""

    manager_report: Any
    review_mode: str
    evolution_mode: str
    operator_snapshot: Mapping[str, object]
    operating_report: tuple[str, ...]


def read_operator_surface(
    state_path: Path,
    *,
    roster_snapshot: Any | None = None,
) -> OperatorSurfaceReadModel:
    """Load the canonical read-only operator snapshot from local state."""

    with CompanyStateStore(state_path) as company_store:
        roster = roster_snapshot or decode_active_roster(company_store.roster())
        manager = PersistentExecutiveManager.optional_from_roster(
            roster.employees,
            roster_revision=roster.revision,
        )
        manager_report = manager_operating_report(
            manager,
            company_store.list_episodes(),
            skill_versions=(
                company_store.list_employee_skills(
                    employee_id=manager.identity.employee_id,
                    active_only=True,
                )
                if manager is not None
                else ()
            ),
        )
        budget_policy = CompanyCostBudgetPolicy.from_mapping(
            company_store.company_cost_budget_policy()
        )
        review_mode = company_store.retention_review_mode().value
        evolution_mode = company_store.evolution_autonomy_mode().value
    inspection, attention, supplemental_attention = inspect_operator_state(
        state_path,
        budget_policy=budget_policy,
    )
    operator_surface = build_operator_surface_snapshot(
        manager_report=manager_report,
        inspection=inspection,
        attention=attention,
        supplemental_attention=supplemental_attention,
    )
    return OperatorSurfaceReadModel(
        manager_report=manager_report,
        review_mode=review_mode,
        evolution_mode=evolution_mode,
        operator_snapshot=operator_surface.as_dict(),
        operating_report=operator_surface.lines(),
    )


__all__ = ["OperatorSurfaceReadModel", "read_operator_surface"]
