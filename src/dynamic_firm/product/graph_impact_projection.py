"""Pure, content-free projection of a retained Graph revision assessment."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from re import fullmatch
from typing import Mapping

from dynamic_firm.company.graph_revision_attribution import (
    GraphRevisionImpactAssessment,
    GraphRevisionImpactDisposition,
)
from dynamic_firm.kernel.models import GraphPatchObservedOutcome


GRAPH_IMPACT_PROJECTION_SCHEMA = "noruct.graph-impact-projection.v1"
_OPAQUE_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"


class GraphImpactTruthState(StrEnum):
    """Whether the projection contains only structure or a matched result."""

    STRUCTURAL_ONLY = "STRUCTURAL_ONLY"
    MATCHED_OUTCOME = "MATCHED_OUTCOME"


class GraphImpactOutcomeStatus(StrEnum):
    """Explicitly prevents absent outcome evidence from reading as success."""

    OUTCOME_NOT_ESTABLISHED = "OUTCOME_NOT_ESTABLISHED"
    MATCHED_OUTCOME = "MATCHED_OUTCOME"


def _opaque_identifier(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or fullmatch(_OPAQUE_IDENTIFIER, value) is None:
        raise ValueError(f"{label} must be an opaque identifier")
    return value


def _graph_digest(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a SHA-256 digest")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return normalized


def _lease_delta(value: int | float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError("accepted_lease_delta must be a finite number")
    return value


@dataclass(frozen=True, slots=True)
class GraphImpactProjection:
    """Immutable read-only view of one accepted revision and its evidence links.

    The optional assessment is retained only as a typed provenance input.  Its
    content-free scalar fields are copied into the matched fields below; no
    goal, artifact, prompt, transcript, provider output, or action token can
    enter this view.
    """

    initial_graph_digest: str
    final_graph_digest: str
    accepted_revision_sequence: int
    accepted_operation: str
    accepted_lease_delta: int | float
    organization_selection_evidence_id: str | None = None
    alternative_evidence_id: str | None = None
    impact_assessment: GraphRevisionImpactAssessment | None = None
    outcome_evidence_id: str | None = None
    truth_state: GraphImpactTruthState = field(init=False)
    outcome_status: GraphImpactOutcomeStatus = field(init=False)
    baseline_terminal_outcome: GraphPatchObservedOutcome | None = field(init=False)
    candidate_terminal_outcome: GraphPatchObservedOutcome | None = field(init=False)
    quality_delta: float | None = field(init=False)
    model_call_delta: int | None = field(init=False)
    disposition: GraphRevisionImpactDisposition | None = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "initial_graph_digest", _graph_digest(self.initial_graph_digest, "initial_graph_digest"))
        object.__setattr__(self, "final_graph_digest", _graph_digest(self.final_graph_digest, "final_graph_digest"))
        if type(self.accepted_revision_sequence) is not int or self.accepted_revision_sequence < 1:
            raise ValueError("accepted_revision_sequence must be a positive integer")
        if not isinstance(self.accepted_operation, str) or fullmatch(_OPAQUE_IDENTIFIER, self.accepted_operation) is None:
            raise ValueError("accepted_operation must be an opaque identifier")
        object.__setattr__(self, "accepted_lease_delta", _lease_delta(self.accepted_lease_delta))
        object.__setattr__(
            self,
            "organization_selection_evidence_id",
            _opaque_identifier(self.organization_selection_evidence_id, "organization_selection_evidence_id"),
        )
        object.__setattr__(
            self,
            "alternative_evidence_id",
            _opaque_identifier(self.alternative_evidence_id, "alternative_evidence_id"),
        )
        supplied_outcome_id = _opaque_identifier(self.outcome_evidence_id, "outcome_evidence_id")
        if self.impact_assessment is not None and not isinstance(
            self.impact_assessment, GraphRevisionImpactAssessment
        ):
            raise TypeError("impact_assessment must be GraphRevisionImpactAssessment")

        assessment = self.impact_assessment
        if assessment is None:
            if supplied_outcome_id is not None:
                raise ValueError("outcome evidence requires an exact impact assessment")
            object.__setattr__(self, "outcome_evidence_id", None)
            object.__setattr__(self, "truth_state", GraphImpactTruthState.STRUCTURAL_ONLY)
            object.__setattr__(self, "outcome_status", GraphImpactOutcomeStatus.OUTCOME_NOT_ESTABLISHED)
            object.__setattr__(self, "baseline_terminal_outcome", None)
            object.__setattr__(self, "candidate_terminal_outcome", None)
            object.__setattr__(self, "quality_delta", None)
            object.__setattr__(self, "model_call_delta", None)
            object.__setattr__(self, "disposition", None)
            return

        if supplied_outcome_id is not None and supplied_outcome_id != assessment.evidence_digest:
            raise ValueError("outcome_evidence_id must match the exact assessment evidence digest")
        object.__setattr__(self, "outcome_evidence_id", assessment.evidence_digest)
        object.__setattr__(self, "truth_state", GraphImpactTruthState.MATCHED_OUTCOME)
        object.__setattr__(self, "outcome_status", GraphImpactOutcomeStatus.MATCHED_OUTCOME)
        object.__setattr__(self, "baseline_terminal_outcome", assessment.baseline_terminal_outcome)
        object.__setattr__(self, "candidate_terminal_outcome", assessment.candidate_terminal_outcome)
        object.__setattr__(self, "quality_delta", assessment.quality_delta)
        object.__setattr__(self, "model_call_delta", assessment.model_call_delta)
        object.__setattr__(self, "disposition", assessment.disposition)

    def canonical_payload(self) -> Mapping[str, object]:
        """Return only safe scalar identities and bounded outcome facts."""

        payload: dict[str, object] = {
            "schema": GRAPH_IMPACT_PROJECTION_SCHEMA,
            "initial_graph_digest": self.initial_graph_digest,
            "final_graph_digest": self.final_graph_digest,
            "accepted_revision_sequence": self.accepted_revision_sequence,
            "accepted_operation": self.accepted_operation,
            "accepted_lease_delta": self.accepted_lease_delta,
            "organization_selection_evidence_id": self.organization_selection_evidence_id,
            "alternative_evidence_id": self.alternative_evidence_id,
            "outcome_evidence_id": self.outcome_evidence_id,
            "truth_state": self.truth_state.value,
            "outcome_status": self.outcome_status.value,
        }
        if self.impact_assessment is not None:
            payload.update(
                {
                    "baseline_terminal_outcome": self.baseline_terminal_outcome.value,
                    "candidate_terminal_outcome": self.candidate_terminal_outcome.value,
                    "quality_delta": self.quality_delta,
                    "model_call_delta": self.model_call_delta,
                    "disposition": self.disposition.value,
                }
            )
        return payload


def project_graph_impact(
    *,
    initial_graph_digest: str,
    final_graph_digest: str,
    accepted_revision_sequence: int,
    accepted_operation: str,
    accepted_lease_delta: int | float,
    organization_selection_evidence_id: str | None = None,
    alternative_evidence_id: str | None = None,
    impact_assessment: GraphRevisionImpactAssessment | None = None,
) -> GraphImpactProjection:
    """Build a pure projection without changing any Company or audit state."""

    return GraphImpactProjection(
        initial_graph_digest=initial_graph_digest,
        final_graph_digest=final_graph_digest,
        accepted_revision_sequence=accepted_revision_sequence,
        accepted_operation=accepted_operation,
        accepted_lease_delta=accepted_lease_delta,
        organization_selection_evidence_id=organization_selection_evidence_id,
        alternative_evidence_id=alternative_evidence_id,
        impact_assessment=impact_assessment,
    )


__all__ = [
    "GRAPH_IMPACT_PROJECTION_SCHEMA",
    "GraphImpactOutcomeStatus",
    "GraphImpactProjection",
    "GraphImpactTruthState",
    "project_graph_impact",
]
