"""Pure, bounded verification policy for Organization Fit evidence.

This module is an advisory projection only.  It does not own a Work Order,
budget, approval, or execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .organization_fit import OrganizationFitLevel, OrganizationFitProfile


class VerificationTier(StrEnum):
    """The bounded verification tier selected by the three relevant facts."""

    DETERMINISTIC = "DETERMINISTIC"
    INDEPENDENT = "INDEPENDENT"


class VerificationRequirement(StrEnum):
    """The verification requirement projected for an advisory policy."""

    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"
    ADDITIONAL_INDEPENDENT_EVIDENCE_OR_REVIEWER = (
        "ADDITIONAL_INDEPENDENT_EVIDENCE_OR_REVIEWER"
    )


@dataclass(frozen=True, slots=True)
class VerificationCeiling:
    """A bounded ceiling that cannot alter Work Order authority or caps."""

    max_verification_rounds: int
    max_additional_independent_requirements: int
    authority_scope: str = "ADVISORY_ONLY"
    work_order_cap_effect: str = "UNCHANGED"


@dataclass(frozen=True, slots=True)
class VerificationBudgetPolicy:
    """Immutable policy keyed only by the relevant fit-profile dimensions."""

    risk_irreversibility: OrganizationFitLevel
    verifiability: OrganizationFitLevel
    error_correlation: OrganizationFitLevel
    tier: VerificationTier
    requirement: VerificationRequirement
    ceiling: VerificationCeiling

    @classmethod
    def from_profile(cls, profile: OrganizationFitProfile) -> "VerificationBudgetPolicy":
        """Project only the three verification-relevant profile facts."""

        if not isinstance(profile, OrganizationFitProfile):
            raise TypeError("Verification policy requires an OrganizationFitProfile")
        risk = profile.risk_irreversibility
        verifiability = profile.verifiability
        correlation = profile.error_correlation
        deterministic = (
            risk is OrganizationFitLevel.LOW
            and verifiability is OrganizationFitLevel.HIGH
            and correlation is OrganizationFitLevel.LOW
        )
        if deterministic:
            return cls(
                risk_irreversibility=risk,
                verifiability=verifiability,
                error_correlation=correlation,
                tier=VerificationTier.DETERMINISTIC,
                requirement=VerificationRequirement.DETERMINISTIC_ONLY,
                ceiling=VerificationCeiling(
                    max_verification_rounds=1,
                    max_additional_independent_requirements=0,
                ),
            )
        return cls(
            risk_irreversibility=risk,
            verifiability=verifiability,
            error_correlation=correlation,
            tier=VerificationTier.INDEPENDENT,
            requirement=VerificationRequirement.ADDITIONAL_INDEPENDENT_EVIDENCE_OR_REVIEWER,
            ceiling=VerificationCeiling(
                max_verification_rounds=2,
                max_additional_independent_requirements=1,
            ),
        )


VerificationTierPolicy = VerificationBudgetPolicy


def verification_budget_policy(
    profile: OrganizationFitProfile,
) -> VerificationBudgetPolicy:
    """Return the pure advisory policy for ``profile``."""

    return VerificationBudgetPolicy.from_profile(profile)


def policy_for(profile: OrganizationFitProfile) -> VerificationBudgetPolicy:
    """Short alias for callers projecting a policy from a fit profile."""

    return verification_budget_policy(profile)
