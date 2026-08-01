"""Read-only projection of bounded AI contribution evidence.

This module deliberately does not decide ownership, approve a proposal, run an
effect, or merge artifacts.  It only preserves the identity carried by the
typed facts it is given and exposes the evidence states that those facts
already record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from dynamic_firm.kernel.models import GraphPatchProposalEvent, GraphPatchProposalStatus


CONTRIBUTION_SCHEMA = "noruct.execution-contribution.v1"


class ContributionState(StrEnum):
    """A single, non-cumulative responsibility boundary."""

    AUTHORED = "AUTHORED"
    PROPOSED = "PROPOSED"
    SELECTED = "SELECTED"
    EXECUTED = "EXECUTED"
    INTEGRATED = "INTEGRATED"


class ProjectionStatus(StrEnum):
    """The state of the projection itself, not the state of a Job."""

    COMPLETE = "COMPLETE"
    UNKNOWN = "UNKNOWN"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    # Short aliases make the typed failure shape easy to consume without
    # weakening the explicit identity-conflict wording in serialized output.
    CONFLICT = "IDENTITY_CONFLICT"


class ProjectionIssueCode(StrEnum):
    NO_ARTIFACT_IDENTITY = "NO_ARTIFACT_IDENTITY"
    INVALID_FACT = "INVALID_FACT"
    MISSING_FACT_IDENTITY = "MISSING_FACT_IDENTITY"
    ARTIFACT_IDENTITY_CONFLICT = "ARTIFACT_IDENTITY_CONFLICT"
    PROPOSAL_DECISION_MISMATCH = "PROPOSAL_DECISION_MISMATCH"
    UNRESOLVED_EVIDENCE = "UNRESOLVED_EVIDENCE"


class ProposalRecordStatus(StrEnum):
    RECORDED = "RECORDED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class ApprovalDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class EffectIntentStatus(StrEnum):
    NOT_RECORDED = "NOT_RECORDED"
    RECORDED = "RECORDED"
    STARTED = "STARTED"
    UNKNOWN = "UNKNOWN"


class EffectOutcomeStatus(StrEnum):
    NOT_RECORDED = "NOT_RECORDED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"
    UNKNOWN = "UNKNOWN"


class IntegrationStatus(StrEnum):
    NOT_RECORDED = "NOT_RECORDED"
    INTEGRATED = "INTEGRATED"
    UNKNOWN = "UNKNOWN"


def _required_text(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _enum_value(value: object, enum_type: type[StrEnum], name: str) -> StrEnum:
    try:
        result = value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{name} must be one of {allowed}") from exc
    return result


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """Opaque identity of one artifact; no content or line range is stored."""

    kind: str
    artifact_id: str
    artifact_digest: str

    def __post_init__(self) -> None:
        _required_text(self.kind, "artifact kind")
        _required_text(self.artifact_id, "artifact id")
        _required_text(self.artifact_digest, "artifact digest")

    @property
    def digest(self) -> str:
        """Compatibility spelling for callers that use the shorter identity term."""

        return self.artifact_digest

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "artifact_digest": self.artifact_digest,
        }


@dataclass(frozen=True, slots=True)
class DeliveryFact:
    """Typed delivery identity used as a projection input."""

    artifact_identity: ArtifactIdentity
    delivery_kind: str
    receipt_status: str = "RECORDED"
    evidence_id: str = ""

    def __post_init__(self) -> None:
        _required_text(self.delivery_kind, "delivery kind")
        _required_text(self.receipt_status, "delivery receipt status")
        if type(self.evidence_id) is not str:
            raise ValueError("delivery evidence id must be a string")


@dataclass(frozen=True, slots=True)
class TaskOwnershipFact:
    """Recorded task-level responsibility, never file or line attribution."""

    task_id: str
    artifact_identity: ArtifactIdentity
    owner_kind: str = "AI"
    owner_id: str = ""

    def __post_init__(self) -> None:
        _required_text(self.task_id, "task id")
        _required_text(self.owner_kind, "owner kind")
        if type(self.owner_id) is not str:
            raise ValueError("owner id must be a string")


@dataclass(frozen=True, slots=True)
class ProposalFact:
    """A recorded proposal, intentionally separate from an approval decision."""

    proposal_id: str
    artifact_identity: ArtifactIdentity
    record_status: ProposalRecordStatus | str = ProposalRecordStatus.RECORDED
    graph_event: GraphPatchProposalEvent | None = None

    def __post_init__(self) -> None:
        _required_text(self.proposal_id, "proposal id")
        object.__setattr__(
            self,
            "record_status",
            _enum_value(self.record_status, ProposalRecordStatus, "proposal record status"),
        )
        if self.graph_event is not None and self.graph_event.proposal_id != self.proposal_id:
            raise ValueError("proposal id must match the Graph proposal event")

    @classmethod
    def from_graph_event(
        cls,
        event: GraphPatchProposalEvent,
        *,
        artifact_identity: ArtifactIdentity | None = None,
    ) -> "ProposalFact":
        """Adapt an existing proposal event without treating it as approval."""

        if not isinstance(event, GraphPatchProposalEvent):
            raise TypeError("event must be a GraphPatchProposalEvent")
        identity = artifact_identity or ArtifactIdentity(
            kind="GRAPH_PATCH",
            artifact_id=event.patch.patch_id,
            artifact_digest=event.content_hash,
        )
        status = (
            ProposalRecordStatus.UNAVAILABLE
            if event.status == GraphPatchProposalStatus.UNAVAILABLE
            else ProposalRecordStatus.RECORDED
        )
        return cls(event.proposal_id, identity, status, event)


@dataclass(frozen=True, slots=True)
class ApprovalFact:
    """A decision receipt for one proposal; it is not implied by ProposalFact."""

    approval_id: str
    proposal_id: str
    artifact_identity: ArtifactIdentity
    decision: ApprovalDecision | str

    def __post_init__(self) -> None:
        _required_text(self.approval_id, "approval id")
        _required_text(self.proposal_id, "approval proposal id")
        object.__setattr__(
            self,
            "decision",
            _enum_value(self.decision, ApprovalDecision, "approval decision"),
        )


@dataclass(frozen=True, slots=True)
class EffectFact:
    """Effect intent and observed outcome are retained as separate statuses."""

    effect_id: str
    artifact_identity: ArtifactIdentity
    intent_status: EffectIntentStatus | str
    outcome_status: EffectOutcomeStatus | str = EffectOutcomeStatus.NOT_RECORDED

    def __post_init__(self) -> None:
        _required_text(self.effect_id, "effect id")
        object.__setattr__(
            self,
            "intent_status",
            _enum_value(self.intent_status, EffectIntentStatus, "effect intent status"),
        )
        object.__setattr__(
            self,
            "outcome_status",
            _enum_value(self.outcome_status, EffectOutcomeStatus, "effect outcome status"),
        )


@dataclass(frozen=True, slots=True)
class FinalOwnerFact:
    """Explicit final-owner/integration evidence, without attribution detail."""

    owner_id: str
    artifact_identity: ArtifactIdentity
    integration_status: IntegrationStatus | str = IntegrationStatus.INTEGRATED
    owner_kind: str = "UNKNOWN"
    evidence_id: str = ""

    def __post_init__(self) -> None:
        _required_text(self.owner_id, "final owner id")
        _required_text(self.owner_kind, "final owner kind")
        if type(self.evidence_id) is not str:
            raise ValueError("integration evidence id must be a string")
        object.__setattr__(
            self,
            "integration_status",
            _enum_value(
                self.integration_status,
                IntegrationStatus,
                "integration status",
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectionIssue:
    code: ProjectionIssueCode
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "code",
            _enum_value(self.code, ProjectionIssueCode, "projection issue code"),
        )
        if type(self.detail) is not str:
            raise ValueError("projection issue detail must be a string")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ResponsibilityEntry:
    """One bounded state/evidence pair for one exact artifact identity."""

    state: ContributionState | str
    artifact_identity: ArtifactIdentity
    evidence_kind: str
    evidence_id: str
    responsibility_scope: str = "TASK_OR_ARTIFACT_LEVEL"
    evidence_status: str = "RECORDED"
    effect_intent_status: EffectIntentStatus | None = None
    effect_outcome_status: EffectOutcomeStatus | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "state",
            _enum_value(self.state, ContributionState, "contribution state"),
        )
        _required_text(self.evidence_kind, "evidence kind")
        _required_text(self.evidence_id, "evidence id")
        _required_text(self.responsibility_scope, "responsibility scope")
        _required_text(self.evidence_status, "evidence status")
        if self.effect_intent_status is not None:
            object.__setattr__(
                self,
                "effect_intent_status",
                _enum_value(
                    self.effect_intent_status,
                    EffectIntentStatus,
                    "effect intent status",
                ),
            )
        if self.effect_outcome_status is not None:
            object.__setattr__(
                self,
                "effect_outcome_status",
                _enum_value(
                    self.effect_outcome_status,
                    EffectOutcomeStatus,
                    "effect outcome status",
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.state.value,
            "artifact_identity": self.artifact_identity.to_dict(),
            "evidence": {
                "kind": self.evidence_kind,
                "id": self.evidence_id,
                "status": self.evidence_status,
            },
            "responsibility_scope": self.responsibility_scope,
        }
        if self.effect_intent_status is not None:
            result["evidence"]["effect_intent_status"] = self.effect_intent_status.value
        if self.effect_outcome_status is not None:
            result["evidence"]["effect_outcome_status"] = self.effect_outcome_status.value
        return result


@dataclass(frozen=True, slots=True)
class ContributionProjectionResult:
    """Typed read-only result; conflict and unknown never contain combined entries."""

    status: ProjectionStatus | str
    artifact_identity: ArtifactIdentity | None
    entries: tuple[ResponsibilityEntry, ...] = ()
    issue: ProjectionIssue | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _enum_value(self.status, ProjectionStatus, "projection status"),
        )
        if not isinstance(self.entries, tuple):
            raise ValueError("projection entries must be a tuple")
        if self.status != ProjectionStatus.COMPLETE and self.entries:
            raise ValueError("non-complete projection results cannot contain entries")
        if self.status == ProjectionStatus.IDENTITY_CONFLICT and self.artifact_identity is not None:
            raise ValueError("identity-conflict results cannot select an artifact identity")

    @property
    def is_unknown(self) -> bool:
        return self.status == ProjectionStatus.UNKNOWN

    @property
    def is_conflict(self) -> bool:
        return self.status == ProjectionStatus.IDENTITY_CONFLICT

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTRIBUTION_SCHEMA,
            "status": self.status.value,
            "artifact_identity": (
                self.artifact_identity.to_dict()
                if self.artifact_identity is not None
                else None
            ),
            "entries": tuple(entry.to_dict() for entry in self.entries),
            "issue": self.issue.to_dict() if self.issue is not None else None,
        }


def _unknown(code: ProjectionIssueCode, detail: str = "") -> ContributionProjectionResult:
    return ContributionProjectionResult(
        status=ProjectionStatus.UNKNOWN,
        artifact_identity=None,
        issue=ProjectionIssue(code, detail),
    )


def _conflict(identities: tuple[ArtifactIdentity, ...]) -> ContributionProjectionResult:
    kinds = ", ".join(
        f"{identity.kind}:{identity.artifact_id}:{identity.artifact_digest}"
        for identity in identities
    )
    return ContributionProjectionResult(
        status=ProjectionStatus.IDENTITY_CONFLICT,
        artifact_identity=None,
        issue=ProjectionIssue(ProjectionIssueCode.ARTIFACT_IDENTITY_CONFLICT, kinds),
    )


def project_execution_contribution(
    delivery: DeliveryFact | None = None,
    task: TaskOwnershipFact | None = None,
    proposal: ProposalFact | None = None,
    approval: ApprovalFact | None = None,
    effect: EffectFact | None = None,
    final_owner: FinalOwnerFact | None = None,
) -> ContributionProjectionResult:
    """Project recorded responsibility evidence without performing any action.

    An absent fact means that state is not recorded.  A present fact with an
    unresolved value makes the whole projection ``UNKNOWN``.  Every present
    fact must identify the same artifact; otherwise no entry is returned.
    """

    facts = (delivery, task, proposal, approval, effect, final_owner)
    expected_types = (
        DeliveryFact,
        TaskOwnershipFact,
        ProposalFact,
        ApprovalFact,
        EffectFact,
        FinalOwnerFact,
    )
    for fact, expected_type in zip(facts, expected_types):
        if fact is not None and not isinstance(fact, expected_type):
            return _unknown(
                ProjectionIssueCode.INVALID_FACT,
                f"expected {expected_type.__name__}",
            )

    identities = tuple(
        fact.artifact_identity
        for fact in facts
        if fact is not None
    )
    if not identities:
        return _unknown(ProjectionIssueCode.NO_ARTIFACT_IDENTITY)
    canonical_identity = identities[0]
    if any(identity != canonical_identity for identity in identities[1:]):
        return _conflict(identities)

    if task is not None and task.owner_kind == "UNKNOWN":
        return _unknown(ProjectionIssueCode.UNRESOLVED_EVIDENCE, "task owner")
    if proposal is not None:
        if proposal.record_status == ProposalRecordStatus.UNKNOWN:
            return _unknown(ProjectionIssueCode.UNRESOLVED_EVIDENCE, "proposal record")
        if (
            approval is not None
            and approval.proposal_id != proposal.proposal_id
        ):
            return _unknown(
                ProjectionIssueCode.PROPOSAL_DECISION_MISMATCH,
                "approval does not decide the recorded proposal",
            )
    if approval is not None and approval.decision == ApprovalDecision.UNKNOWN:
        return _unknown(ProjectionIssueCode.UNRESOLVED_EVIDENCE, "approval decision")
    if effect is not None and (
        effect.intent_status == EffectIntentStatus.UNKNOWN
        or effect.outcome_status == EffectOutcomeStatus.UNKNOWN
    ):
        return _unknown(ProjectionIssueCode.UNRESOLVED_EVIDENCE, "effect receipt")
    if final_owner is not None and final_owner.integration_status == IntegrationStatus.UNKNOWN:
        return _unknown(ProjectionIssueCode.UNRESOLVED_EVIDENCE, "final owner")

    entries: list[ResponsibilityEntry] = []
    if task is not None and task.owner_kind == "AI":
        entries.append(
            ResponsibilityEntry(
                state=ContributionState.AUTHORED,
                artifact_identity=canonical_identity,
                evidence_kind="TASK_OWNERSHIP",
                evidence_id=task.task_id,
            )
        )
    if proposal is not None and proposal.record_status == ProposalRecordStatus.RECORDED:
        entries.append(
            ResponsibilityEntry(
                state=ContributionState.PROPOSED,
                artifact_identity=canonical_identity,
                evidence_kind="GRAPH_PROPOSAL",
                evidence_id=proposal.proposal_id,
            )
        )
    if approval is not None and approval.decision == ApprovalDecision.APPROVED:
        entries.append(
            ResponsibilityEntry(
                state=ContributionState.SELECTED,
                artifact_identity=canonical_identity,
                evidence_kind="APPROVAL_DECISION",
                evidence_id=approval.approval_id,
                evidence_status=approval.decision.value,
            )
        )
    if effect is not None and effect.intent_status in {
        EffectIntentStatus.RECORDED,
        EffectIntentStatus.STARTED,
    }:
        entries.append(
            ResponsibilityEntry(
                state=ContributionState.EXECUTED,
                artifact_identity=canonical_identity,
                evidence_kind="EFFECT_RECEIPT",
                evidence_id=effect.effect_id,
                evidence_status=effect.outcome_status.value,
                effect_intent_status=effect.intent_status,
                effect_outcome_status=effect.outcome_status,
            )
        )
    if (
        final_owner is not None
        and final_owner.integration_status == IntegrationStatus.INTEGRATED
    ):
        entries.append(
            ResponsibilityEntry(
                state=ContributionState.INTEGRATED,
                artifact_identity=canonical_identity,
                evidence_kind="FINAL_OWNER",
                evidence_id=final_owner.evidence_id or final_owner.owner_id,
                evidence_status=final_owner.integration_status.value,
            )
        )

    if not entries:
        return _unknown(ProjectionIssueCode.UNRESOLVED_EVIDENCE, "no AI contribution state")
    return ContributionProjectionResult(
        status=ProjectionStatus.COMPLETE,
        artifact_identity=canonical_identity,
        entries=tuple(entries),
    )


# Concise aliases for callers that name the responsibility by its product
# surface rather than by the underlying execution-summary field.
project_ai_contribution = project_execution_contribution
execution_contribution = project_execution_contribution
DeliveryEvidenceFact = DeliveryFact
TaskFact = TaskOwnershipFact
GraphProposalFact = ProposalFact
ApprovalReceiptFact = ApprovalFact
EffectReceiptFact = EffectFact
ContributionEntry = ResponsibilityEntry


__all__ = [
    "ApprovalDecision",
    "ApprovalFact",
    "ApprovalReceiptFact",
    "ArtifactIdentity",
    "CONTRIBUTION_SCHEMA",
    "ContributionEntry",
    "ContributionProjectionResult",
    "ContributionState",
    "DeliveryEvidenceFact",
    "DeliveryFact",
    "EffectFact",
    "EffectIntentStatus",
    "EffectOutcomeStatus",
    "EffectReceiptFact",
    "FinalOwnerFact",
    "GraphProposalFact",
    "IntegrationStatus",
    "ProjectionIssue",
    "ProjectionIssueCode",
    "ProjectionStatus",
    "ProposalFact",
    "ProposalRecordStatus",
    "ResponsibilityEntry",
    "TaskFact",
    "TaskOwnershipFact",
    "execution_contribution",
    "project_ai_contribution",
    "project_execution_contribution",
]
