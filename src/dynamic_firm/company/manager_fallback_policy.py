"""Pure, fail-closed fallback policy for bounded Manager coordination.

The policy is only a projection of supplied evidence.  It does not run a
Manager, retry work, replan a Graph, or mutate any state.  A fallback is
terminal so callers cannot turn a failed Manager path into a review or retry
loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ManagerFallbackDecision(StrEnum):
    CONTINUE_MANAGER = "CONTINUE_MANAGER"
    STRONG_SOLO = "STRONG_SOLO"


class ManagerFallbackReason(StrEnum):
    NONE = "NONE"
    NEGATIVE_TRANSFER_STRONG_SOLO = "NEGATIVE_TRANSFER_STRONG_SOLO"
    BOUND_EXHAUSTED_STRONG_SOLO = "BOUND_EXHAUSTED_STRONG_SOLO"


GRAPH_UNCHANGED_EVIDENCE: Final[str] = "VIABLE_GRAPH_NOT_CHANGED"


@dataclass(frozen=True, slots=True)
class ManagerFallbackPolicy:
    """Immutable hard bounds for one Manager planning/supervision path."""

    max_planning_calls: int = 2
    max_supervision_calls: int = 2
    max_integration_calls: int = 2
    max_review_loops: int = 1
    max_reassignments: int = 1
    max_replans: int = 1
    max_wall_time_seconds: int = 300

    def __post_init__(self) -> None:
        for name, value in self._bounds():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name.upper()}_MUST_BE_POSITIVE_INTEGER")

    def _bounds(self) -> tuple[tuple[str, int], ...]:
        return (
            ("planning_calls", self.max_planning_calls),
            ("supervision_calls", self.max_supervision_calls),
            ("integration_calls", self.max_integration_calls),
            ("review_loops", self.max_review_loops),
            ("reassignments", self.max_reassignments),
            ("replans", self.max_replans),
            ("wall_time_seconds", self.max_wall_time_seconds),
        )


@dataclass(frozen=True, slots=True)
class ManagerTerminalEvidence:
    """Content-free result that a caller can record without executing control."""

    decision: ManagerFallbackDecision
    reason: ManagerFallbackReason
    terminal: bool
    exhausted_bound: str | None
    retry_allowed: bool
    loop_allowed: bool
    graph_changed: bool
    graph_evidence: str = GRAPH_UNCHANGED_EVIDENCE


def project_manager_fallback(
    policy: ManagerFallbackPolicy = ManagerFallbackPolicy(),
    *,
    negative_transfer: bool = False,
    planning_calls: int = 0,
    supervision_calls: int = 0,
    integration_calls: int = 0,
    review_loops: int = 0,
    reassignments: int = 0,
    replans: int = 0,
    wall_time_seconds: int = 0,
) -> ManagerTerminalEvidence:
    """Project evidence into either continued supervision or terminal SOLO.

    Negative transfer has priority over bounds and uses a fixed reason.  When
    no negative-transfer evidence exists, the first exhausted bound produces
    the same terminal SOLO contract and reports only the fixed bound name.
    """

    observations = {
        "planning_calls": planning_calls,
        "supervision_calls": supervision_calls,
        "integration_calls": integration_calls,
        "review_loops": review_loops,
        "reassignments": reassignments,
        "replans": replans,
        "wall_time_seconds": wall_time_seconds,
    }
    for name, value in observations.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name.upper()}_MUST_BE_NON_NEGATIVE_INTEGER")

    if negative_transfer:
        return _terminal(ManagerFallbackReason.NEGATIVE_TRANSFER_STRONG_SOLO)

    for name, limit in policy._bounds():
        if observations[name] >= limit:
            return _terminal(
                ManagerFallbackReason.BOUND_EXHAUSTED_STRONG_SOLO,
                exhausted_bound=name,
            )

    return ManagerTerminalEvidence(
        decision=ManagerFallbackDecision.CONTINUE_MANAGER,
        reason=ManagerFallbackReason.NONE,
        terminal=False,
        exhausted_bound=None,
        retry_allowed=True,
        loop_allowed=True,
        graph_changed=False,
    )


def _terminal(
    reason: ManagerFallbackReason,
    *,
    exhausted_bound: str | None = None,
) -> ManagerTerminalEvidence:
    return ManagerTerminalEvidence(
        decision=ManagerFallbackDecision.STRONG_SOLO,
        reason=reason,
        terminal=True,
        exhausted_bound=exhausted_bound,
        retry_allowed=False,
        loop_allowed=False,
        graph_changed=False,
    )


# The longer name reads naturally at call sites that treat this as a policy
# evaluation rather than a projection.
evaluate_manager_fallback_policy = project_manager_fallback
