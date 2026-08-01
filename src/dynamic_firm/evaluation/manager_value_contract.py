"""Immutable four-way qualification contract for Manager-led Firm value.

This is intentionally a *campaign contract*, not a claimed outcome.  It fixes
the counterfactual arms and their comparability requirements before live quota
is consumed.  A future live runner must seal one result for every exact slot;
it may not reinterpret an existing 2-way Firm Value campaign as Manager proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .firm_value_v2 import FirmValueV2FixtureKind, firm_value_v2_fixture_contract


MANAGER_VALUE_CONTRACT_SCHEMA = "noruct.manager-value-qualification.v1"


class ManagerValueArm(StrEnum):
    SINGLE_EMPLOYEE = "SINGLE_EMPLOYEE"
    HOMOGENEOUS_GRAPH = "HOMOGENEOUS_GRAPH"
    HETEROGENEOUS_GRAPH = "HETEROGENEOUS_GRAPH"
    MANAGER_LED_FIRM = "MANAGER_LED_FIRM"


@dataclass(frozen=True, slots=True)
class ManagerValueFixture:
    fixture: str
    fixture_revision: str
    validation_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagerValueQualificationContract:
    schema_version: str
    arms: tuple[ManagerValueArm, ...]
    fixtures: tuple[ManagerValueFixture, ...]
    exact_slots: tuple[tuple[str, str], ...]
    frozen_dimensions: tuple[str, ...]
    required_outcomes: tuple[str, ...]
    prohibited_inferences: tuple[str, ...]
    sealed_control_plane_implemented: bool = True
    # This means the in-process executor exists. It does *not* claim that a
    # live 16-slot campaign has completed or that the Manager has won it.
    live_campaign_implemented: bool = True
    outcome_claimed: bool = False


def manager_value_qualification_contract() -> ManagerValueQualificationContract:
    """Return the surface-neutral, provider-free campaign shape.

    The two graph arms deliberately stay distinct: a homogeneous graph measures
    clone/role-play overhead, while a heterogeneous graph measures a real
    capability-bound organization without a persistent Manager.  Only the
    fourth arm can attribute any incremental result to the Manager.
    """

    fixtures = tuple(
        ManagerValueFixture(
            fixture=fixture.value,
            fixture_revision=firm_value_v2_fixture_contract(fixture).fixture_revision,
            validation_command=firm_value_v2_fixture_contract(fixture).validation_command,
        )
        for fixture in FirmValueV2FixtureKind
    )
    arms = tuple(ManagerValueArm)
    return ManagerValueQualificationContract(
        schema_version=MANAGER_VALUE_CONTRACT_SCHEMA,
        arms=arms,
        fixtures=fixtures,
        exact_slots=tuple((fixture.fixture, arm.value) for fixture in fixtures for arm in arms),
        frozen_dimensions=(
            "work_order_and_fixture_revision",
            "source_and_distribution_revision",
            "model_and_provider_profile",
            "tool_permission_and_approval_policy",
            "total_model_call_and_wall_time_budget",
            "validator_and_acceptance_contract",
            "knowledge_and_workspace_scope",
        ),
        required_outcomes=(
            "lower_decile_quality",
            "complete_failure_rate",
            "safety_failure_rate",
            "validation_recovery_attempt_and_success_rate",
            "runtime_user_intervention_count",
            "external_effect_error_and_unknown_rate",
            "reported_cost_or_same_model_call_proxy_and_latency",
            "requested_and_granted_approval_count",
            "specialist_replan_and_supervision_count",
        ),
        prohibited_inferences=(
            "No 2-way SOLO/DYNAMIC result proves Manager value.",
            "No offline scripted fixture is production Manager evidence.",
            "No aggregate average may hide an unsafe or negative-transfer slot.",
        ),
    )
