from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from dynamic_firm.company.operating import (
    CompanyWorkMode,
    classify_company_input,
)


class InputRoute(StrEnum):
    """Product-level execution lanes; users never choose workflow details."""

    CONVERSATION = "CONVERSATION"
    COMPANY_GOAL = "COMPANY_GOAL"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    route: InputRoute
    reason: str


def route_interactive_input(value: str) -> RoutingDecision:
    """Compatibility projection of the Company operating decision.

    `CONVERSATION` no longer means that the input sits outside the Company; it
    is the legacy public name for a Company-owned DIRECT turn.  SOLO_JOB and
    TEAM_JOB continue to project to COMPANY_GOAL until callers migrate to the
    typed operating contract.
    """

    decision = classify_company_input(value)
    route = (
        InputRoute.CONVERSATION
        if decision.work_mode == CompanyWorkMode.DIRECT
        else InputRoute.COMPANY_GOAL
    )
    return RoutingDecision(route=route, reason=decision.reason.value)
