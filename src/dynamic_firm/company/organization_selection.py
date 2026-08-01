"""Deterministic, non-authoritative organization selection evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .organization_fit import OrganizationFitLevel
from .organization_fit_evidence import OrganizationFitEvidence


class OrganizationSelection(StrEnum):
    """The only selection outcomes produced by this pure projection."""

    DIRECT_OR_STRONG_SOLO = "DIRECT_OR_STRONG_SOLO"
    TEAM_EXPERIMENT_CANDIDATE = "TEAM_EXPERIMENT_CANDIDATE"


class OrganizationSelectionReason(StrEnum):
    """Fixed reasons explaining the conservative selection table."""

    CORE_EVIDENCE_MISSING = "CORE_EVIDENCE_MISSING"
    NO_INDEPENDENT_VALUE_EVIDENCE = "NO_INDEPENDENT_VALUE_EVIDENCE"
    INDEPENDENT_WORK_EVIDENCE = "INDEPENDENT_WORK_EVIDENCE"
    INFORMATION_DISPERSION_EVIDENCE = "INFORMATION_DISPERSION_EVIDENCE"
    INDEPENDENT_VERIFICATION_EVIDENCE = "INDEPENDENT_VERIFICATION_EVIDENCE"


@dataclass(frozen=True, slots=True)
class OrganizationSelectionResult:
    """Immutable selection evidence; it cannot admit or dispatch work."""

    selection: OrganizationSelection
    reason_codes: tuple[OrganizationSelectionReason, ...]
    profile_digest: str

    @property
    def reason_code(self) -> OrganizationSelectionReason:
        """Return the first stable reason for callers needing one code."""

        return self.reason_codes[0]


# Selection is conservative when any profile dimension is absent.  Keeping
# this list explicit makes the rule independent of dataclass implementation
# details and prevents future fields from silently changing the table.
_CORE_PROFILE_DIMENSIONS = (
    "decomposability",
    "dependency_coupling",
    "context_coupling",
    "information_dispersion",
    "verifiability",
    "risk_irreversibility",
    "error_correlation",
    "latency_sensitivity",
)

# Each entry requires both the dimension evidence and the extractor's exact
# candidate receipt.  Labels, counts, preferences, and prose cannot match.
_INDEPENDENT_EVIDENCE = (
    (
        OrganizationSelectionReason.INDEPENDENT_WORK_EVIDENCE,
        "dependency_coupling",
        "PortfolioSchedulingEnvelope",
        "dependency_work_order_ids",
    ),
    (
        OrganizationSelectionReason.INFORMATION_DISPERSION_EVIDENCE,
        "information_dispersion",
        "PortfolioSchedulingEnvelope",
        "required_capabilities",
    ),
    (
        OrganizationSelectionReason.INDEPENDENT_VERIFICATION_EVIDENCE,
        "verifiability",
        "WorkOrder.operating_decision",
        "requires_independent_review",
    ),
)


def _has_exact_reference(
    evidence: OrganizationFitEvidence,
    output_field: str,
    source_type: str,
    source_field: str,
) -> bool:
    return any(
        reference.output_field == output_field
        and reference.source_type == source_type
        and reference.source_field == source_field
        for reference in evidence.evidence_references
    )


def select_organization(evidence: OrganizationFitEvidence) -> OrganizationSelectionResult:
    """Select strong SOLO unless complete typed evidence permits an experiment.

    The input is immutable fit evidence only.  The result is advisory and has
    no admission, dispatch, mutation, or automatic-reuse behavior.
    """

    if not isinstance(evidence, OrganizationFitEvidence):
        raise TypeError("evidence must be OrganizationFitEvidence")

    profile = evidence.profile
    if any(
        getattr(profile, dimension) is OrganizationFitLevel.UNKNOWN
        for dimension in _CORE_PROFILE_DIMENSIONS
    ):
        return OrganizationSelectionResult(
            selection=OrganizationSelection.DIRECT_OR_STRONG_SOLO,
            reason_codes=(OrganizationSelectionReason.CORE_EVIDENCE_MISSING,),
            profile_digest=profile.digest,
        )

    matched_reasons: list[OrganizationSelectionReason] = []
    for reason, dimension, source_type, source_field in _INDEPENDENT_EVIDENCE:
        if (
            getattr(profile, dimension) is OrganizationFitLevel.HIGH
            and _has_exact_reference(evidence, dimension, source_type, source_field)
            and _has_exact_reference(evidence, "team_candidate", source_type, source_field)
        ):
            matched_reasons.append(reason)

    if not matched_reasons:
        return OrganizationSelectionResult(
            selection=OrganizationSelection.DIRECT_OR_STRONG_SOLO,
            reason_codes=(OrganizationSelectionReason.NO_INDEPENDENT_VALUE_EVIDENCE,),
            profile_digest=profile.digest,
        )

    return OrganizationSelectionResult(
        selection=OrganizationSelection.TEAM_EXPERIMENT_CANDIDATE,
        reason_codes=tuple(matched_reasons),
        profile_digest=profile.digest,
    )


__all__ = [
    "OrganizationSelection",
    "OrganizationSelectionReason",
    "OrganizationSelectionResult",
    "select_organization",
]
