"""Bounded, content-free projection of assignment-rationale records.

This module only projects ``AssignmentRationale`` records.  It does not make
an assignment, identify an Employee, or infer a reason that is absent from a
record.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from enum import StrEnum
from typing import Mapping, Sequence

from ..company.assignment_rationale import AssignmentRationale


class AssignmentRationaleSummaryStatus(StrEnum):
    """The status of the bounded summary section."""

    RECORDED = "RECORDED"
    NO_RECORDED_ASSIGNMENT_RATIONALE = "NO_RECORDED_ASSIGNMENT_RATIONALE"


class AssignmentMaterialDifference(StrEnum):
    """Fixed-template material-difference values."""

    EXERCISED = "MATERIAL_DIFFERENCE_EXERCISED"
    NOT_EXERCISED = "MATERIAL_DIFFERENCE_NOT_EXERCISED"


class AssignmentContribution(StrEnum):
    """Whether the required capability is the exercised capability."""

    EXERCISED = "CAPABILITY_EXERCISED"
    NOT_EXERCISED = "CAPABILITY_NOT_EXERCISED"


_MAX_ENTRIES = 3
_ENTRY_TEMPLATE = "ASSIGNMENT_RATIONALE_ENTRY_V1"


def _rationale_drill_down_id(record: AssignmentRationale, ordinal: int) -> str:
    """Derive a stable opaque id without putting record content in the id."""

    payload = json.dumps(
        record.payload(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"rr-{sha256(payload).hexdigest()}-{ordinal}"


@dataclass(frozen=True, slots=True)
class AssignmentRationaleSummaryEntry:
    """One fixed-template, content-free summary entry."""

    rationale_drill_down_id: str
    required_capability: str
    material_difference: AssignmentMaterialDifference
    contribution: AssignmentContribution
    alternative_drill_down_ids: tuple[str, ...]
    alternative_exclusion_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rationale_drill_down_id, str) or not self.rationale_drill_down_id:
            raise ValueError("rationale_drill_down_id must be a non-empty opaque id")
        if not isinstance(self.required_capability, str) or not self.required_capability:
            raise ValueError("required_capability must be a non-empty token")
        if not isinstance(self.material_difference, AssignmentMaterialDifference):
            raise ValueError("material_difference must be a fixed value")
        if not isinstance(self.contribution, AssignmentContribution):
            raise ValueError("contribution must be a fixed value")
        if not isinstance(self.alternative_drill_down_ids, tuple):
            raise ValueError("alternative_drill_down_ids must be an immutable tuple")
        if not isinstance(self.alternative_exclusion_reasons, tuple):
            raise ValueError("alternative_exclusion_reasons must be an immutable tuple")
        if len(self.alternative_drill_down_ids) != len(self.alternative_exclusion_reasons):
            raise ValueError("alternative ids and exclusion reasons must align")
        if len(self.alternative_drill_down_ids) > 3:
            raise ValueError("at most three alternatives may be projected")

    def payload(self) -> Mapping[str, object]:
        """Return the stable fixed-template representation."""

        return {
            "template": _ENTRY_TEMPLATE,
            "rationale_drill_down_id": self.rationale_drill_down_id,
            "required_capability": self.required_capability,
            "material_difference": self.material_difference.value,
            "contribution": self.contribution.value,
            "alternatives": tuple(
                {
                    "alternative_drill_down_id": alternative_id,
                    "exclusion_reason": reason,
                }
                for alternative_id, reason in zip(
                    self.alternative_drill_down_ids,
                    self.alternative_exclusion_reasons,
                )
            ),
        }


@dataclass(frozen=True, slots=True)
class AssignmentRationaleSummary:
    """The single bounded assignment-rationale summary section."""

    status: AssignmentRationaleSummaryStatus
    entries: tuple[AssignmentRationaleSummaryEntry, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, AssignmentRationaleSummaryStatus):
            raise ValueError("status must be a fixed summary status")
        if not isinstance(self.entries, tuple):
            raise ValueError("entries must be an immutable tuple")
        if len(self.entries) > _MAX_ENTRIES:
            raise ValueError("at most three summary entries may be projected")
        if any(not isinstance(entry, AssignmentRationaleSummaryEntry) for entry in self.entries):
            raise ValueError("entries must contain summary entries")
        if self.status is AssignmentRationaleSummaryStatus.NO_RECORDED_ASSIGNMENT_RATIONALE and self.entries:
            raise ValueError("a missing summary cannot contain entries")
        if self.status is AssignmentRationaleSummaryStatus.RECORDED and not self.entries:
            raise ValueError("a recorded summary must contain entries")

    @classmethod
    def from_records(
        cls,
        records: Sequence[AssignmentRationale],
    ) -> "AssignmentRationaleSummary":
        """Project only the first three B06 records in source order."""

        if isinstance(records, (str, bytes)):
            raise TypeError("records must contain AssignmentRationale records")
        bounded_records = tuple(records)[:_MAX_ENTRIES]
        if any(not isinstance(record, AssignmentRationale) for record in bounded_records):
            raise TypeError("records must contain AssignmentRationale records")
        if not bounded_records:
            return cls(AssignmentRationaleSummaryStatus.NO_RECORDED_ASSIGNMENT_RATIONALE)

        entries = tuple(
            _project_record(record, _index + 1)
            for _index, record in enumerate(bounded_records)
        )
        return cls(AssignmentRationaleSummaryStatus.RECORDED, entries)

    def payload(self) -> Mapping[str, object]:
        """Return one bounded summary section for a consumer."""

        return {
            "section": "assignment-rationale-summary",
            "status": self.status.value,
            "entries": tuple(entry.payload() for entry in self.entries),
        }


def _project_record(record: AssignmentRationale, ordinal: int) -> AssignmentRationaleSummaryEntry:
    exercised = record.required_capability == record.exercised_capability
    difference = (
        AssignmentMaterialDifference.EXERCISED
        if exercised
        else AssignmentMaterialDifference.NOT_EXERCISED
    )
    contribution = (
        AssignmentContribution.EXERCISED
        if exercised
        else AssignmentContribution.NOT_EXERCISED
    )
    return AssignmentRationaleSummaryEntry(
        rationale_drill_down_id=_rationale_drill_down_id(record, ordinal),
        required_capability=record.required_capability,
        material_difference=difference,
        contribution=contribution,
        alternative_drill_down_ids=tuple(item.alternative_id for item in record.alternatives),
        alternative_exclusion_reasons=tuple(
            item.exclusion_reason.value for item in record.alternatives
        ),
    )


def summarize_assignment_rationale(
    records: Sequence[AssignmentRationale],
) -> AssignmentRationaleSummary:
    """Return the bounded summary for B06 assignment-rationale records."""

    return AssignmentRationaleSummary.from_records(records)


__all__ = [
    "AssignmentContribution",
    "AssignmentMaterialDifference",
    "AssignmentRationaleSummary",
    "AssignmentRationaleSummaryEntry",
    "AssignmentRationaleSummaryStatus",
    "summarize_assignment_rationale",
]
