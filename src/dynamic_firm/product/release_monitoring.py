"""Bounded, local, content-free release observation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ObservationState(Enum):
    """The explicitly supplied state of the observation window."""

    NOT_RUN = "NOT_RUN"
    INSUFFICIENT = "INSUFFICIENT"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class ReleaseObservation:
    """Content-free aggregate counters for one observation window."""

    install_attempts: int
    install_failures: int
    crash_safe_receipts: int
    approval_effect_incidents: int
    migration_failures: int
    review_burden_signals: int
    observation_state: ObservationState

    def __post_init__(self) -> None:
        counters = (
            self.install_attempts,
            self.install_failures,
            self.crash_safe_receipts,
            self.approval_effect_incidents,
            self.migration_failures,
            self.review_burden_signals,
        )
        if not isinstance(self.observation_state, ObservationState):
            raise ValueError("invalid observation state")
        if any(type(counter) is not int or counter < 0 for counter in counters):
            raise ValueError("aggregate counters must be non-negative integers")
        if self.install_failures > self.install_attempts:
            raise ValueError("install failures exceed install attempts")


@dataclass(frozen=True, slots=True)
class H4EscalationNotice:
    """Opaque notice used by the local H4 delivery seam."""

    code: str = "G11_THRESHOLD_BREACH"


H4_ESCALATION_NOTICE = H4EscalationNotice()


@dataclass(frozen=True, slots=True)
class ObservationAssessment:
    """A non-release-decisive result of evaluating one observation."""

    observation_sufficient: bool
    threshold_breached: bool
    h4_escalation_required: bool


class H4AlertDelivery(Protocol):
    def deliver(self, notice: H4EscalationNotice) -> None:
        """Deliver one local, opaque H4 notice."""


class InMemoryH4AlertDelivery:
    """Test/local delivery seam retaining only immutable opaque notices."""

    __slots__ = ("_notices",)

    def __init__(self) -> None:
        self._notices: list[H4EscalationNotice] = []

    def deliver(self, notice: H4EscalationNotice) -> None:
        if not isinstance(notice, H4EscalationNotice):
            raise ValueError("invalid H4 notice")
        self._notices.append(notice)

    @property
    def notices(self) -> tuple[H4EscalationNotice, ...]:
        return tuple(self._notices)


# G11 is intentionally conservative: any observed failure/incident signal
# escalates, while one completed receipt is required before a clean window is
# considered sufficient.  These values are fixed and are not caller input.
_MIN_INSTALL_ATTEMPTS = 1
_MIN_CRASH_SAFE_RECEIPTS = 1
_MAX_INSTALL_FAILURES = 0
_MAX_APPROVAL_EFFECT_INCIDENTS = 0
_MAX_MIGRATION_FAILURES = 0
_MAX_REVIEW_BURDEN_SIGNALS = 0


def observe_release(
    observation: ReleaseObservation,
    delivery: H4AlertDelivery,
) -> ObservationAssessment:
    """Evaluate bounded aggregates and optionally deliver one local H4 notice."""

    if not isinstance(observation, ReleaseObservation):
        return ObservationAssessment(False, False, False)

    sufficient = (
        observation.observation_state is ObservationState.COMPLETE
        and observation.install_attempts >= _MIN_INSTALL_ATTEMPTS
        and observation.crash_safe_receipts >= _MIN_CRASH_SAFE_RECEIPTS
    )
    if not sufficient:
        return ObservationAssessment(False, False, False)

    breached = (
        observation.install_failures > _MAX_INSTALL_FAILURES
        or observation.approval_effect_incidents > _MAX_APPROVAL_EFFECT_INCIDENTS
        or observation.migration_failures > _MAX_MIGRATION_FAILURES
        or observation.review_burden_signals > _MAX_REVIEW_BURDEN_SIGNALS
    )
    if breached:
        delivery.deliver(H4_ESCALATION_NOTICE)
    return ObservationAssessment(True, breached, breached)
