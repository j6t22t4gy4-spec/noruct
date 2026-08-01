"""Immutable, bounded projection of explicit material alternatives.

The ledger records only a caller-named choice, its fixed evidence status, and
the small amount of source identity needed to explain where that fact came
from.  It does not retain candidate searches, reasoning, estimates, or make a
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence

from ..company.assignment_rationale import (
    AssignmentAlternative,
    AssignmentExclusionReason,
    AssignmentRationale,
)
from ..kernel.models import GraphPatchProposalEvent, GraphPatchProposalStatus


MATERIAL_ALTERNATIVES_SCHEMA = "noruct.material-alternatives.v1"
_MAX_ENTRIES = 3


class MaterialAlternativeStatus(StrEnum):
    """The only statuses a material-alternative entry may expose."""

    EVALUATED = "EVALUATED"
    REJECTED = "REJECTED"
    NOT_EVALUATED = "NOT_EVALUATED"


# B06 and the material-alternative ledger intentionally share the fixed
# exclusion taxonomy.  The alias keeps the product contract from inventing a
# second set of reasons.
MaterialAlternativeReason = AssignmentExclusionReason
MaterialAlternativeExclusionReason = AssignmentExclusionReason


def _required_token(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or any(
        character.isspace() for character in value
    ):
        raise ValueError(f"{field} must be a non-empty token")
    return value


def _status(value: object) -> MaterialAlternativeStatus:
    try:
        return value if isinstance(value, MaterialAlternativeStatus) else MaterialAlternativeStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "status must be EVALUATED, REJECTED, or NOT_EVALUATED"
        ) from exc


def _reason(value: object) -> AssignmentExclusionReason | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, AssignmentExclusionReason) else AssignmentExclusionReason(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("reason must be a fixed assignment exclusion reason") from exc


@dataclass(frozen=True, slots=True)
class MaterialAlternativeEntry:
    """One explicit final-choice-impacting alternative.

    ``compared`` is a caller-supplied fact.  It is never inferred from a
    choice name or status.  A not-evaluated entry is useful only when that
    state itself was explicitly recorded; it is not a retained hidden
    candidate.
    """

    choice: str
    status: MaterialAlternativeStatus | str
    reason: AssignmentExclusionReason | str | None = None
    compared: bool | None = None
    source: str = "EXPLICIT"
    source_id: str = ""
    decision: str = ""

    def __post_init__(self) -> None:
        _required_token(self.choice, "choice")
        normalized_status = _status(self.status)
        object.__setattr__(self, "status", normalized_status)
        normalized_reason = _reason(self.reason)
        object.__setattr__(self, "reason", normalized_reason)

        if self.compared is None:
            normalized_compared = normalized_status is not MaterialAlternativeStatus.NOT_EVALUATED
        elif type(self.compared) is bool:
            normalized_compared = self.compared
        else:
            raise ValueError("compared must be a boolean when supplied")
        if normalized_status is MaterialAlternativeStatus.NOT_EVALUATED:
            if normalized_compared:
                raise ValueError("NOT_EVALUATED must have compared=False")
            if normalized_reason is not None:
                raise ValueError("NOT_EVALUATED cannot have an exclusion reason")
        elif not normalized_compared:
            raise ValueError("evaluated or rejected alternatives must be compared")
        if normalized_status is MaterialAlternativeStatus.REJECTED and normalized_reason is None:
            raise ValueError("REJECTED must have a fixed exclusion reason")
        if normalized_status is MaterialAlternativeStatus.EVALUATED and normalized_reason is not None:
            raise ValueError("EVALUATED cannot have an exclusion reason")
        object.__setattr__(self, "compared", normalized_compared)

        _required_token(self.source, "source")
        if type(self.source_id) is not str:
            raise ValueError("source_id must be a string")
        if type(self.decision) is not str:
            raise ValueError("decision must be a string")

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "MaterialAlternativeEntry":
        """Reconstitute one entry without adding fields or inferred facts."""

        if not isinstance(payload, Mapping):
            raise TypeError("material alternative entry must be a mapping")
        allowed = {"choice", "status", "reason", "compared", "source", "source_id", "decision"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown material alternative field(s): {sorted(unknown)!r}")
        required = {"choice", "status", "compared"}
        missing = required - set(payload)
        if missing:
            raise ValueError(f"missing material alternative field(s): {sorted(missing)!r}")
        return cls(
            choice=payload["choice"],
            status=payload["status"],
            reason=payload.get("reason"),
            compared=payload["compared"],
            source=payload.get("source", "EXPLICIT"),
            source_id=payload.get("source_id", ""),
            decision=payload.get("decision", ""),
        )

    def payload(self) -> Mapping[str, object]:
        """Return the complete bounded, content-free representation."""

        return {
            "choice": self.choice,
            "status": self.status.value,
            "reason": self.reason.value if self.reason is not None else None,
            "compared": self.compared,
            "source": self.source,
            "source_id": self.source_id,
            "decision": self.decision,
        }

    to_payload = payload


@dataclass(frozen=True, slots=True)
class MaterialAlternativeLedger:
    """An immutable ledger containing at most three explicit entries."""

    entries: tuple[MaterialAlternativeEntry, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise ValueError("entries must be an immutable tuple")
        if len(self.entries) > _MAX_ENTRIES:
            raise ValueError("at most three material alternatives may be recorded")
        if any(not isinstance(entry, MaterialAlternativeEntry) for entry in self.entries):
            raise ValueError("entries must contain MaterialAlternativeEntry records")
        choices = [entry.choice for entry in self.entries]
        if len(choices) != len(set(choices)):
            raise ValueError("material alternative choices must be unique")

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "MaterialAlternativeLedger":
        if not isinstance(payload, Mapping):
            raise TypeError("material alternative ledger must be a mapping")
        if payload.get("schema_version") != MATERIAL_ALTERNATIVES_SCHEMA:
            raise ValueError("payload is not noruct.material-alternatives.v1")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes, bytearray)):
            raise TypeError("material alternative entries must be a sequence")
        return cls(tuple(MaterialAlternativeEntry.from_payload(item) for item in raw_entries))

    @classmethod
    def from_b06(cls, rationale: AssignmentRationale) -> "MaterialAlternativeLedger":
        """Read only the compared alternatives already recorded by B06."""

        if not isinstance(rationale, AssignmentRationale):
            raise TypeError("rationale must be an AssignmentRationale")
        entries = tuple(_entry_from_b06(item) for item in rationale.alternatives)
        return cls(entries)

    @classmethod
    def from_graph_proposal(cls, event: GraphPatchProposalEvent) -> "MaterialAlternativeLedger":
        """Project one exact Graph proposal decision without approving or applying it."""

        if not isinstance(event, GraphPatchProposalEvent):
            raise TypeError("event must be a GraphPatchProposalEvent")
        status = _status_from_graph(event.status)
        entry = MaterialAlternativeEntry(
            choice=event.patch.patch_id,
            status=status,
            reason=(
                AssignmentExclusionReason.UNKNOWN
                if status is MaterialAlternativeStatus.REJECTED
                else None
            ),
            compared=status is not MaterialAlternativeStatus.NOT_EVALUATED,
            source="GRAPH_PROPOSAL",
            source_id=event.proposal_id,
            decision=event.status.value,
        )
        return cls((entry,))

    def payload(self) -> Mapping[str, object]:
        return {
            "schema_version": MATERIAL_ALTERNATIVES_SCHEMA,
            "entries": tuple(entry.payload() for entry in self.entries),
        }

    to_payload = payload


def _entry_from_b06(alternative: AssignmentAlternative) -> MaterialAlternativeEntry:
    if not isinstance(alternative, AssignmentAlternative):
        raise TypeError("B06 alternatives must be AssignmentAlternative records")
    if alternative.compared is not True:
        raise ValueError("a B06 material alternative must have compared=True")
    return MaterialAlternativeEntry(
        choice=alternative.alternative_id,
        status=MaterialAlternativeStatus.REJECTED,
        reason=alternative.exclusion_reason,
        compared=True,
        source="B06",
        source_id=alternative.alternative_id,
    )


def _status_from_graph(status: GraphPatchProposalStatus) -> MaterialAlternativeStatus:
    if status is GraphPatchProposalStatus.APPROVED:
        return MaterialAlternativeStatus.EVALUATED
    if status is GraphPatchProposalStatus.REJECTED:
        return MaterialAlternativeStatus.REJECTED
    return MaterialAlternativeStatus.NOT_EVALUATED


def material_alternatives_from_b06(
    rationale: AssignmentRationale,
) -> MaterialAlternativeLedger:
    return MaterialAlternativeLedger.from_b06(rationale)


def material_alternatives_from_graph_proposal(
    event: GraphPatchProposalEvent,
) -> MaterialAlternativeLedger:
    return MaterialAlternativeLedger.from_graph_proposal(event)


# Descriptive aliases keep both adapter names usable without adding another
# public package boundary.
ledger_from_b06 = material_alternatives_from_b06
ledger_from_graph_proposal = material_alternatives_from_graph_proposal


__all__ = [
    "MATERIAL_ALTERNATIVES_SCHEMA",
    "MaterialAlternativeEntry",
    "MaterialAlternativeExclusionReason",
    "MaterialAlternativeLedger",
    "MaterialAlternativeReason",
    "MaterialAlternativeStatus",
    "ledger_from_b06",
    "ledger_from_graph_proposal",
    "material_alternatives_from_b06",
    "material_alternatives_from_graph_proposal",
]
