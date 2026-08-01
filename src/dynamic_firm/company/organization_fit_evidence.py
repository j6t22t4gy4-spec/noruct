"""Pure extraction of Organization Fit evidence from frozen typed receipts.

This module is deliberately a projection only.  It does not inspect Work
Order prose, choose an organization, or own admission/runtime authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from .frontdoor import WorkOrder
from .operating import RequestedEffect
from .organization_fit import OrganizationFitLevel, OrganizationFitProfile
from .work_order_portfolio_models import PortfolioSchedulingEnvelope


@dataclass(frozen=True, slots=True)
class OrganizationFitEvidenceReference:
    """A field-level reference to one explicit typed input fact."""

    output_field: str
    source_type: str
    source_field: str

    @property
    def input_path(self) -> str:
        return f"{self.source_type}.{self.source_field}"


@dataclass(frozen=True, slots=True)
class OrganizationFitEvidence:
    """Immutable profile plus its explainable extraction receipts."""

    profile: OrganizationFitProfile
    evidence_references: tuple[OrganizationFitEvidenceReference, ...]
    team_candidate: bool

    def evidence_for(self, output_field: str) -> tuple[OrganizationFitEvidenceReference, ...]:
        """Return the typed references for one profile or candidate field."""

        return tuple(
            reference
            for reference in self.evidence_references
            if reference.output_field == output_field
        )


def extract_organization_fit(
    work_order: WorkOrder,
    scheduling: PortfolioSchedulingEnvelope,
) -> OrganizationFitEvidence:
    """Extract only dimensions proven by explicit frozen receipt fields.

    Work Order objective, requested outcome, constraints, and the initial
    coordination classification are intentionally not read.  In particular,
    prose requesting a team cannot alter this result.
    """

    if not isinstance(work_order, WorkOrder):
        raise TypeError("work_order must be WorkOrder")
    if not isinstance(scheduling, PortfolioSchedulingEnvelope):
        raise TypeError("scheduling must be PortfolioSchedulingEnvelope")
    if scheduling.work_order_id != work_order.work_order_id:
        raise ValueError("Scheduling envelope must bind to the Work Order")

    values: dict[str, OrganizationFitLevel] = {}
    references: list[OrganizationFitEvidenceReference] = []

    def record(
        output_field: str,
        value: OrganizationFitLevel,
        source_type: str,
        source_field: str,
    ) -> None:
        values[output_field] = value
        references.append(
            OrganizationFitEvidenceReference(
                output_field=output_field,
                source_type=source_type,
                source_field=source_field,
            )
        )

    if scheduling.dependency_work_order_ids:
        record(
            "dependency_coupling",
            OrganizationFitLevel.HIGH,
            "PortfolioSchedulingEnvelope",
            "dependency_work_order_ids",
        )

    if len(scheduling.required_capabilities) >= 2:
        record(
            "information_dispersion",
            OrganizationFitLevel.HIGH,
            "PortfolioSchedulingEnvelope",
            "required_capabilities",
        )

    if work_order.acceptance_criteria:
        record(
            "verifiability",
            OrganizationFitLevel.HIGH,
            "WorkOrder",
            "acceptance_criteria",
        )

    decision = work_order.operating_decision
    if decision.requires_independent_review:
        record(
            "verifiability",
            OrganizationFitLevel.HIGH,
            "WorkOrder.operating_decision",
            "requires_independent_review",
        )

    effect = decision.requested_effect
    if effect is RequestedEffect.READ:
        record(
            "risk_irreversibility",
            OrganizationFitLevel.LOW,
            "WorkOrder.operating_decision",
            "requested_effect",
        )
    elif effect in (RequestedEffect.WORKSPACE_CHANGE, RequestedEffect.HOST_ACTION):
        record(
            "risk_irreversibility",
            OrganizationFitLevel.HIGH,
            "WorkOrder.operating_decision",
            "requested_effect",
        )

    if scheduling.deadline_at is not None:
        record(
            "latency_sensitivity",
            OrganizationFitLevel.HIGH,
            "PortfolioSchedulingEnvelope",
            "deadline_at",
        )

    independent_evidence = (
        bool(scheduling.dependency_work_order_ids)
        or len(scheduling.required_capabilities) >= 2
        or decision.requires_independent_review
    )
    if independent_evidence:
        references.append(
            OrganizationFitEvidenceReference(
                output_field="team_candidate",
                source_type=(
                    "PortfolioSchedulingEnvelope"
                    if scheduling.dependency_work_order_ids
                    or len(scheduling.required_capabilities) >= 2
                    else "WorkOrder.operating_decision"
                ),
                source_field=(
                    "dependency_work_order_ids"
                    if scheduling.dependency_work_order_ids
                    else (
                        "required_capabilities"
                        if len(scheduling.required_capabilities) >= 2
                        else "requires_independent_review"
                    )
                ),
            )
        )

    return OrganizationFitEvidence(
        profile=OrganizationFitProfile.from_mapping(values),
        evidence_references=tuple(references),
        team_candidate=independent_evidence,
    )


__all__ = [
    "OrganizationFitEvidence",
    "OrganizationFitEvidenceReference",
    "extract_organization_fit",
]
