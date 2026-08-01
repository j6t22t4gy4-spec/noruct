"""Pure projection of repeated typed exceptions into improvement candidates.

This module is deliberately content-free and proposal-only.  It keeps only
typed exception identity and opaque evidence references; it cannot approve,
apply, rollback, or alter a running Job.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping, Sequence


_HEX_DIGITS = frozenset("0123456789abcdef")
_REPEAT_THRESHOLD = 2
_TOKEN_LIMIT = 256


def _opaque_token(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _TOKEN_LIMIT
        or any(character.isspace() for character in value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} must be a non-empty opaque token")
    return value


def _digest(value: object, field: str) -> str:
    value = _opaque_token(value, field)
    if len(value) != 64 or any(character not in _HEX_DIGITS for character in value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _identity_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: dict(item),
    ).encode()
    return sha256(encoded).hexdigest()


class ExceptionCandidateKind(StrEnum):
    """The only bounded improvement surfaces this compiler can propose."""

    RULE = "RULE"
    TOOL = "TOOL"
    TEST = "TEST"
    SKILL = "SKILL"
    ROSTER = "ROSTER"


@dataclass(frozen=True, slots=True)
class ExceptionProvenance:
    """Opaque reference to one recorded exception occurrence."""

    source_id: str
    source_digest: str

    def __post_init__(self) -> None:
        _opaque_token(self.source_id, "source_id")
        _digest(self.source_digest, "source_digest")

    def payload(self) -> Mapping[str, str]:
        return MappingProxyType(
            {"source_id": self.source_id, "source_digest": self.source_digest}
        )


@dataclass(frozen=True, slots=True)
class TypedExceptionObservation:
    """Content-free typed exception evidence for one occurrence."""

    exception_type: str
    exception_code: str
    provenance: ExceptionProvenance

    def __post_init__(self) -> None:
        _opaque_token(self.exception_type, "exception_type")
        _opaque_token(self.exception_code, "exception_code")
        if type(self.provenance) is not ExceptionProvenance:
            raise TypeError("provenance must be ExceptionProvenance")


# Short alias for callers that already have a typed exception receipt.
TypedException = TypedExceptionObservation


@dataclass(frozen=True, slots=True)
class ExceptionCluster:
    """Immutable cluster of exact typed exceptions and opaque provenance."""

    exception_type: str
    exception_code: str
    occurrences: tuple[ExceptionProvenance, ...]

    def __post_init__(self) -> None:
        _opaque_token(self.exception_type, "exception_type")
        _opaque_token(self.exception_code, "exception_code")
        if type(self.occurrences) is not tuple or not self.occurrences:
            raise ValueError("exception cluster occurrences must be a non-empty tuple")
        if any(type(item) is not ExceptionProvenance for item in self.occurrences):
            raise TypeError("exception cluster occurrences must be opaque provenance")
        source_ids = [item.source_id for item in self.occurrences]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("exception cluster occurrences must be unique")

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[TypedExceptionObservation],
    ) -> "ExceptionCluster":
        """Project observations only when their typed identity is exact."""

        items = tuple(observations)
        if not items:
            raise ValueError("at least one typed exception observation is required")
        if any(type(item) is not TypedExceptionObservation for item in items):
            raise TypeError("observations must be TypedExceptionObservation records")
        identity = (items[0].exception_type, items[0].exception_code)
        if any((item.exception_type, item.exception_code) != identity for item in items):
            raise ValueError("exception observations are not an exact typed cluster")
        return cls(
            exception_type=identity[0],
            exception_code=identity[1],
            occurrences=tuple(item.provenance for item in items),
        )

    @property
    def occurrence_count(self) -> int:
        return len(self.occurrences)

    @property
    def repeat_qualified(self) -> bool:
        return self.occurrence_count >= _REPEAT_THRESHOLD

    @property
    def cluster_digest(self) -> str:
        return _identity_digest(self._identity_payload())

    def _identity_payload(self) -> Mapping[str, object]:
        return {
            "exception_type": self.exception_type,
            "exception_code": self.exception_code,
            "occurrences": tuple(item.payload() for item in self.occurrences),
        }

    def payload(self) -> Mapping[str, object]:
        """Return the cluster projection without exception messages or content."""

        return MappingProxyType(
            {
                "exception_type": self.exception_type,
                "exception_code": self.exception_code,
                "occurrence_count": self.occurrence_count,
                "repeat_qualified": self.repeat_qualified,
                "cluster_digest": self.cluster_digest,
                "provenance": tuple(item.payload() for item in self.occurrences),
            }
        )


def project_exception_cluster(
    observations: Sequence[TypedExceptionObservation],
) -> ExceptionCluster:
    """Create one immutable exact-typed exception cluster."""

    return ExceptionCluster.from_observations(observations)


@dataclass(frozen=True, slots=True)
class CandidateControls:
    """Required opaque controls for a future human-reviewed proposal."""

    baseline: str
    approval: str
    rollback: str
    retirement_condition: str

    def __post_init__(self) -> None:
        for field in ("baseline", "approval", "rollback", "retirement_condition"):
            _opaque_token(getattr(self, field), field)

    def payload(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "baseline": self.baseline,
                "approval": self.approval,
                "rollback": self.rollback,
                "retirement_condition": self.retirement_condition,
            }
        )


@dataclass(frozen=True, slots=True)
class ExceptionCandidate:
    """Read-only improvement candidate; no authority or execution is carried."""

    kind: ExceptionCandidateKind
    cluster_digest: str
    provenance: tuple[ExceptionProvenance, ...]
    controls: CandidateControls
    proposal_only: bool = True
    authority_expanding: bool = False
    running_job_changed: bool = False

    def __post_init__(self) -> None:
        if type(self.kind) is not ExceptionCandidateKind:
            raise TypeError("candidate kind must be ExceptionCandidateKind")
        _digest(self.cluster_digest, "cluster_digest")
        if type(self.provenance) is not tuple or not self.provenance:
            raise ValueError("candidate provenance must be a non-empty tuple")
        if any(type(item) is not ExceptionProvenance for item in self.provenance):
            raise TypeError("candidate provenance must be opaque")
        if type(self.controls) is not CandidateControls:
            raise TypeError("candidate controls must be CandidateControls")
        if self.proposal_only is not True:
            raise ValueError("exception candidates are proposal-only")
        if self.authority_expanding:
            raise ValueError("exception candidate cannot expand authority")
        if self.running_job_changed:
            raise ValueError("exception candidate cannot change a running Job")

    @property
    def candidate_id(self) -> str:
        return f"exception-candidate-{_identity_digest(self.payload())[:24]}"

    def payload(self) -> Mapping[str, object]:
        """Return only typed identity, opaque provenance, and required controls."""

        return MappingProxyType(
            {
                "kind": self.kind.value,
                "cluster_digest": self.cluster_digest,
                "provenance": tuple(item.payload() for item in self.provenance),
                "controls": self.controls.payload(),
                "proposal_only": True,
                "authority_expanding": False,
                "running_job_changed": False,
            }
        )


def compile_exception_candidate(
    cluster: ExceptionCluster,
    kind: ExceptionCandidateKind,
    controls: CandidateControls,
    *,
    authority_expanding: bool = False,
) -> ExceptionCandidate:
    """Compile a repeat-qualified cluster into a proposal-only candidate.

    The compiler does not accept success evidence, approval, rollback, or Job
    mutation inputs.  Those are controls or later human/runtime actions, never
    learning inputs to this pure projection.
    """

    if type(cluster) is not ExceptionCluster:
        raise TypeError("cluster must be ExceptionCluster")
    if type(kind) is not ExceptionCandidateKind:
        raise TypeError("kind must be ExceptionCandidateKind")
    if type(controls) is not CandidateControls:
        raise TypeError("controls must be CandidateControls")
    if not cluster.repeat_qualified:
        raise ValueError("single occurrence is not repeat-qualified")
    if authority_expanding:
        raise ValueError("authority-expanding candidate is rejected")
    return ExceptionCandidate(
        kind=kind,
        cluster_digest=cluster.cluster_digest,
        provenance=cluster.occurrences,
        controls=controls,
    )


compile_candidate = compile_exception_candidate


__all__ = [
    "CandidateControls",
    "ExceptionCandidate",
    "ExceptionCandidateKind",
    "ExceptionCluster",
    "ExceptionProvenance",
    "TypedException",
    "TypedExceptionObservation",
    "compile_candidate",
    "compile_exception_candidate",
    "project_exception_cluster",
]
