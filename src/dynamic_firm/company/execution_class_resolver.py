"""Derive a non-authoritative execution class from a frozen plan projection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .operating import CompanyWorkMode
from .organization_plan import FrozenOrganizationPlan, OrganizationPlanRoute


class OrganizationExecutionStage(StrEnum):
    FRAME = "FRAME"
    EXPLORE = "EXPLORE"
    SELECT = "SELECT"
    INTEGRATE = "INTEGRATE"
    VERIFY = "VERIFY"


class ExecutionClass(StrEnum):
    STRONG_SOLO = "STRONG_SOLO"
    FRAME = "FRAME"
    INDEPENDENT_EXPLORATION = "INDEPENDENT_EXPLORATION"
    SELECTION = "SELECTION"
    INTEGRATION = "INTEGRATION"
    INDEPENDENT_VERIFICATION = "INDEPENDENT_VERIFICATION"


_PLAN_ROUTE = {
    OrganizationExecutionStage.FRAME: OrganizationPlanRoute.TASK_DEPENDENCY,
    OrganizationExecutionStage.EXPLORE: OrganizationPlanRoute.INFORMATION_EVIDENCE,
    OrganizationExecutionStage.SELECT: OrganizationPlanRoute.ASSIGNMENT,
    OrganizationExecutionStage.INTEGRATE: OrganizationPlanRoute.ARTIFACT_COMMUNICATION,
    OrganizationExecutionStage.VERIFY: OrganizationPlanRoute.VERIFICATION,
}
_TEAM_CLASS = {
    OrganizationExecutionStage.FRAME: ExecutionClass.FRAME,
    OrganizationExecutionStage.EXPLORE: ExecutionClass.INDEPENDENT_EXPLORATION,
    OrganizationExecutionStage.SELECT: ExecutionClass.SELECTION,
    OrganizationExecutionStage.INTEGRATE: ExecutionClass.INTEGRATION,
    OrganizationExecutionStage.VERIFY: ExecutionClass.INDEPENDENT_VERIFICATION,
}


@dataclass(frozen=True, slots=True)
class ExecutionClassProjection:
    plan_digest: str
    stage: OrganizationExecutionStage
    work_mode: CompanyWorkMode
    source_route: OrganizationPlanRoute
    execution_class: ExecutionClass


def derive_execution_class(
    plan: FrozenOrganizationPlan | None,
    observed_bindings: Mapping[str, str] | None,
    stage: OrganizationExecutionStage | str,
    work_mode: CompanyWorkMode | str,
) -> ExecutionClassProjection:
    """Validate the retained plan before producing a future-route requirement."""

    if plan is None or observed_bindings is None:
        raise ValueError("a current FrozenOrganizationPlan and observed bindings are required")
    if not isinstance(plan, FrozenOrganizationPlan):
        raise TypeError("plan must be a FrozenOrganizationPlan")
    plan.validate(observed_bindings)
    stage = OrganizationExecutionStage(stage)
    work_mode = CompanyWorkMode(work_mode)
    source_route = _PLAN_ROUTE[stage]
    execution_class = (
        ExecutionClass.STRONG_SOLO
        if work_mode in {CompanyWorkMode.DIRECT, CompanyWorkMode.SOLO_JOB}
        else _TEAM_CLASS[stage]
    )
    return ExecutionClassProjection(plan.content_digest, stage, work_mode, source_route, execution_class)
