"""Content-free, non-authoritative assignment rationale records.

The records in this module explain exercised capability only.  They do not
choose an Employee, rank candidates, or retain Employee identity, role, or
profile contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence

from ..runtime.models import EmployeeCapabilityProfile


class AssignmentExclusionReason(StrEnum):
    """The finite reasons allowed for a compared alternative."""

    NOT_NEEDED = "NOT_NEEDED"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    AUTHORITY_EXCEEDED = "AUTHORITY_EXCEEDED"
    EVIDENCE_WEAK = "EVIDENCE_WEAK"
    COST_OR_DELAY = "COST_OR_DELAY"
    SAFETY_RISK = "SAFETY_RISK"
    SOURCE_REUSE_PREFERRED = "SOURCE_REUSE_PREFERRED"
    USER_CONSTRAINT = "USER_CONSTRAINT"
    UNKNOWN = "UNKNOWN"


_HEX_DIGITS = frozenset("0123456789abcdef")


def _require_token(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a non-empty content-free token")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field} must not contain whitespace")
    return value


def _require_digest(value: str, field: str) -> str:
    value = _require_token(value, field)
    if len(value) != 64 or any(character not in _HEX_DIGITS for character in value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class AssignmentAlternative:
    """One explicitly compared, excluded alternative.

    ``alternative_id`` is intentionally opaque.  In particular, this record
    has no employee/name/role field and cannot represent an unexamined
    candidate: ``compared`` must be explicitly true.
    """

    alternative_id: str
    material_profile_digest: str
    exclusion_reason: AssignmentExclusionReason
    compared: bool

    def __post_init__(self) -> None:
        _require_token(self.alternative_id, "alternative_id")
        _require_digest(self.material_profile_digest, "material_profile_digest")
        if not isinstance(self.exclusion_reason, AssignmentExclusionReason):
            raise ValueError("exclusion_reason must be a fixed AssignmentExclusionReason")
        if self.compared is not True:
            raise ValueError("an assignment alternative must be explicitly compared")

    @classmethod
    def compared_candidate(
        cls,
        *,
        alternative_id: str,
        material_profile_digest: str,
        exclusion_reason: AssignmentExclusionReason,
    ) -> "AssignmentAlternative":
        return cls(
            alternative_id=alternative_id,
            material_profile_digest=material_profile_digest,
            exclusion_reason=exclusion_reason,
            compared=True,
        )

    def payload(self) -> Mapping[str, object]:
        """Return the complete content-free representation."""

        return {
            "alternative_id": self.alternative_id,
            "material_profile_digest": self.material_profile_digest,
            "exclusion_reason": self.exclusion_reason.value,
            "compared": True,
        }


@dataclass(frozen=True, slots=True)
class AssignmentRationale:
    """Bounded evidence for one already-made assignment.

    This is a projection and never makes or changes an assignment.  The
    direct constructor is content-free; ``from_profile`` additionally proves
    that the exercised capability exists in the selected material profile.
    """

    required_capability: str
    selected_material_profile_digest: str
    exercised_capability: str
    alternatives: tuple[AssignmentAlternative, ...] = ()

    def __post_init__(self) -> None:
        _require_token(self.required_capability, "required_capability")
        _require_digest(
            self.selected_material_profile_digest,
            "selected_material_profile_digest",
        )
        _require_token(self.exercised_capability, "exercised_capability")
        if not isinstance(self.alternatives, tuple):
            raise ValueError("alternatives must be an immutable tuple")
        if len(self.alternatives) > 3:
            raise ValueError("at most three assignment alternatives may be recorded")
        if any(not isinstance(item, AssignmentAlternative) for item in self.alternatives):
            raise ValueError("alternatives must contain AssignmentAlternative records")
        if len({item.alternative_id for item in self.alternatives}) != len(self.alternatives):
            raise ValueError("assignment alternative ids must be unique")

    @classmethod
    def from_profile(
        cls,
        *,
        required_capability: str,
        selected_profile: EmployeeCapabilityProfile,
        exercised_capability: str,
        alternatives: Sequence[AssignmentAlternative] = (),
    ) -> "AssignmentRationale":
        """Create a rationale after checking exercised capability evidence."""

        if not isinstance(selected_profile, EmployeeCapabilityProfile):
            raise TypeError("selected_profile must be EmployeeCapabilityProfile")
        selected_profile.verify()
        if exercised_capability not in selected_profile.capability_ids:
            raise ValueError("exercised_capability is not present in the selected profile")
        return cls(
            required_capability=required_capability,
            selected_material_profile_digest=selected_profile.material_digest,
            exercised_capability=exercised_capability,
            alternatives=tuple(alternatives),
        )

    def payload(self) -> Mapping[str, object]:
        """Return only the fields allowed in the content-free record."""

        return {
            "required_capability": self.required_capability,
            "selected_material_profile_digest": self.selected_material_profile_digest,
            "exercised_capability": self.exercised_capability,
            "alternatives": tuple(item.payload() for item in self.alternatives),
        }


__all__ = [
    "AssignmentAlternative",
    "AssignmentExclusionReason",
    "AssignmentRationale",
]
