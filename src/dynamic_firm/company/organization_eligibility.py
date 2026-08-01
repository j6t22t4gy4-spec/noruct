"""Pure compatibility rules for organization reuse eligibility.

This module does not participate in admission or mutate organization state.  It
only names the four post-migration states, translates the legacy decision
values, and validates an explicitly proposed transition.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping


class OrganizationReuseEligibility(StrEnum):
    """The only states available to the reuse-eligibility migration."""

    SOLO_REQUIRED = "SOLO_REQUIRED"
    EXPERIMENT_ELIGIBLE = "EXPERIMENT_ELIGIBLE"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    AUTO_REUSE_ELIGIBLE = "AUTO_REUSE_ELIGIBLE"


# Legacy qualification is deliberately never migrated directly to automatic
# reuse.  The table is immutable so callers cannot turn migration into policy.
LEGACY_MIGRATION_TABLE: Final[Mapping[str, OrganizationReuseEligibility]] = MappingProxyType(
    {
        "INSUFFICIENT_EVIDENCE": OrganizationReuseEligibility.SOLO_REQUIRED,
        "SOLO_REQUIRED": OrganizationReuseEligibility.SOLO_REQUIRED,
        "TEAM_ELIGIBLE": OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
        "REPLICA_ELIGIBLE": OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE,
    }
)


def migrate_legacy_eligibility(
    legacy_value: str | OrganizationReuseEligibility,
) -> OrganizationReuseEligibility:
    """Migrate one legacy value without granting automatic reuse."""

    if isinstance(legacy_value, OrganizationReuseEligibility):
        return legacy_value
    try:
        return LEGACY_MIGRATION_TABLE[legacy_value]
    except (KeyError, TypeError) as exc:
        raise ValueError("UNKNOWN_LEGACY_ELIGIBILITY") from exc


def negative_transfer_eligibility(*, observe_only: bool = False) -> OrganizationReuseEligibility:
    """Return the conservative state for a negative-transfer observation."""

    if observe_only:
        return OrganizationReuseEligibility.OBSERVE_ONLY
    return OrganizationReuseEligibility.SOLO_REQUIRED


def validate_eligibility_transition(
    current: OrganizationReuseEligibility,
    proposed: OrganizationReuseEligibility,
    *,
    matched_cohort_evidence: bool = False,
    negative_transfer: bool = False,
) -> bool:
    """Return whether an explicitly proposed state transition is admissible.

    Automatic reuse is the only promotion requiring positive evidence: an
    explicit matched cohort is mandatory.  Negative transfer is fail-closed
    to SOLO or observation and cannot be hidden by a promotion request.
    """

    if not isinstance(current, OrganizationReuseEligibility):
        raise TypeError("CURRENT_STATE_REQUIRED")
    if not isinstance(proposed, OrganizationReuseEligibility):
        raise TypeError("PROPOSED_STATE_REQUIRED")
    if current is proposed:
        return True
    if negative_transfer:
        return proposed in {
            OrganizationReuseEligibility.SOLO_REQUIRED,
            OrganizationReuseEligibility.OBSERVE_ONLY,
        }
    if proposed is OrganizationReuseEligibility.AUTO_REUSE_ELIGIBLE:
        return matched_cohort_evidence
    return True


def explain_eligibility_transition(
    current: OrganizationReuseEligibility,
    proposed: OrganizationReuseEligibility,
    *,
    matched_cohort_evidence: bool = False,
    negative_transfer: bool = False,
) -> str:
    """Return a fixed, content-free operator explanation code."""

    if not validate_eligibility_transition(
        current,
        proposed,
        matched_cohort_evidence=matched_cohort_evidence,
        negative_transfer=negative_transfer,
    ):
        return "TRANSITION_REJECTED"
    if current is proposed:
        return "STATE_UNCHANGED"
    if negative_transfer:
        return "NEGATIVE_TRANSFER_CONSERVATIVE_STATE"
    if proposed is OrganizationReuseEligibility.AUTO_REUSE_ELIGIBLE:
        return "MATCHED_COHORT_EVIDENCE"
    if proposed is OrganizationReuseEligibility.EXPERIMENT_ELIGIBLE:
        return "EXPLICIT_EXPERIMENT_ONLY"
    if proposed is OrganizationReuseEligibility.OBSERVE_ONLY:
        return "OBSERVATION_ONLY"
    return "SOLO_REQUIRED"
