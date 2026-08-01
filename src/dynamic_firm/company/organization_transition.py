"""Immutable organization-transition proposals and Kernel receipt binding.

This module is deliberately a proposal/projection boundary.  It does not
approve, apply, or otherwise mutate a Graph, budget, lease, or authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from dynamic_firm.kernel.models import (
    GraphMutationLease,
    GraphPatch,
    GraphPatchProposalEvent,
    GraphPatchProposalStatus,
)
from dynamic_firm.kernel.mutation import content_digest


class OrganizationTransition(StrEnum):
    """The bounded transition union exposed by the organization layer."""

    SOLO_TO_SPLIT = "SOLO_TO_SPLIT"
    SPLIT_TO_INTEGRATE = "SPLIT_TO_INTEGRATE"
    INTEGRATE_TO_VERIFY = "INTEGRATE_TO_VERIFY"
    VERIFY_TO_COMMIT = "VERIFY_TO_COMMIT"
    VERIFY_TO_REPAIR = "VERIFY_TO_REPAIR"
    VERIFY_TO_REPLACE = "VERIFY_TO_REPLACE"
    VERIFY_TO_STOP = "VERIFY_TO_STOP"


_TEXT_LIMIT = 1000


def _validate_text(label: str, value: object) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _TEXT_LIMIT
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"Organization transition {label} is invalid")


@dataclass(frozen=True, slots=True)
class OrganizationTransitionProposal:
    """One advisory organization transition, with no execution authority."""

    transition: OrganizationTransition
    reason: str
    expected_benefit: str
    incremental_lease: GraphMutationLease
    verification_plan: str
    stop_condition: str
    rollback_or_replacement_boundary: str

    def __post_init__(self) -> None:
        if type(self.transition) is not OrganizationTransition:
            raise TypeError("Organization transition must be typed")
        if type(self.incremental_lease) is not GraphMutationLease:
            raise TypeError("Organization transition lease must be a Kernel lease")
        for label, value in (
            ("reason", self.reason),
            ("expected benefit", self.expected_benefit),
            ("verification plan", self.verification_plan),
            ("stop condition", self.stop_condition),
            ("rollback/replacement boundary", self.rollback_or_replacement_boundary),
        ):
            _validate_text(label, value)


@dataclass(frozen=True, slots=True)
class OrganizationTransitionReceipt:
    """A read-only binding to one exact existing Kernel proposal event."""

    proposal: OrganizationTransitionProposal
    kernel_proposal: GraphPatchProposalEvent

    def __post_init__(self) -> None:
        if type(self.proposal) is not OrganizationTransitionProposal:
            raise TypeError("Organization transition receipt proposal is malformed")
        if type(self.kernel_proposal) is not GraphPatchProposalEvent:
            raise TypeError("Organization transition Kernel receipt is malformed")


def _is_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_kernel_proposal(event: GraphPatchProposalEvent) -> None:
    """Reject a forged or malformed event before it crosses this boundary."""

    if type(event) is not GraphPatchProposalEvent:
        raise TypeError("Only an exact GraphPatchProposalEvent is accepted")
    if type(event.patch) is not GraphPatch:
        raise TypeError("Kernel Graph proposal patch is malformed")
    if type(event.proposed_lease) is not GraphMutationLease:
        raise TypeError("Kernel Graph proposal lease is malformed")
    if type(event.status) is not GraphPatchProposalStatus:
        raise TypeError("Kernel Graph proposal status is malformed")
    if (
        type(event.proposal_id) is not str
        or type(event.event_id) is not str
        or not event.proposal_id
        or not event.event_id
        or not _is_digest(event.before_graph_digest)
        or not _is_digest(event.after_graph_digest)
        or not _is_digest(event.content_hash)
    ):
        raise ValueError("Kernel Graph proposal identity is malformed")

    proposal_identity = {
        "patch": event.patch,
        "before_graph_digest": event.before_graph_digest,
        "after_graph_digest": event.after_graph_digest,
        "proposed_lease": event.proposed_lease,
    }
    expected_proposal_id = f"graph-proposal-{content_digest(proposal_identity)[:24]}"
    expected_event_id = f"graph-proposal-event-{content_digest({**proposal_identity, 'status': event.status.value})[:24]}"
    expected_content_hash = content_digest(replace(event, content_hash=""))
    if (
        event.proposal_id != expected_proposal_id
        or event.event_id != expected_event_id
        or event.content_hash != expected_content_hash
    ):
        raise ValueError("Kernel Graph proposal content identity mismatch")


def adapt_kernel_graph_proposal_receipt(
    proposal: OrganizationTransitionProposal,
    kernel_proposal: GraphPatchProposalEvent,
) -> OrganizationTransitionReceipt:
    """Bind a typed proposal to an exact Kernel receipt without applying it."""

    if type(proposal) is not OrganizationTransitionProposal:
        raise TypeError("Organization transition proposal is malformed")
    _validate_kernel_proposal(kernel_proposal)
    if proposal.incremental_lease != kernel_proposal.proposed_lease:
        raise ValueError("Organization transition lease does not match Kernel proposal")
    return OrganizationTransitionReceipt(
        proposal=proposal,
        kernel_proposal=kernel_proposal,
    )
