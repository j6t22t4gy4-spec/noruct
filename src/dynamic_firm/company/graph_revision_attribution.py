"""Matched-counterfactual evidence for one accepted Graph revision.

The retained ACTIVE JOB audit can prove that a Graph revision was accepted and
that a Job later terminalized.  It cannot prove causality.  This component
therefore accepts only a deliberately paired, content-free evaluation record:
the same Work Order digest and initial Graph are run once unchanged and once
with exactly one accepted first revision.

It is an evaluator contract, not a runtime controller.  It cannot mutate a
Graph, settle a budget, admit a Job, or change future staffing automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from dynamic_firm.kernel.models import GraphPatchObservedOutcome

from .graph_blueprint_models import GraphRunRecord
from .models import content_digest


GRAPH_REVISION_IMPACT_EVIDENCE_SCHEMA = "noruct.graph-revision-impact-evidence.v1"


class GraphRevisionImpactDisposition(StrEnum):
    """Bounded comparison result; it never changes a production policy."""

    IMPROVED = "IMPROVED"
    REGRESSED = "REGRESSED"
    NO_MEASURED_CHANGE = "NO_MEASURED_CHANGE"
    INCONCLUSIVE = "INCONCLUSIVE"


def _digest(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return normalized


def _score(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{label} must be a finite score between zero and one")
    return float(value)


def _calls(value: int, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class GraphRevisionImpactEvidence:
    """One immutable, matched evaluation of a first accepted graph rewrite."""

    context_fingerprint: str
    evaluator_digest: str
    baseline_run: GraphRunRecord
    candidate_run: GraphRunRecord
    baseline_terminal_outcome: GraphPatchObservedOutcome
    baseline_quality_score: float
    candidate_quality_score: float
    baseline_model_calls: int
    candidate_model_calls: int
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "context_fingerprint", _digest(self.context_fingerprint, "context_fingerprint")
        )
        object.__setattr__(self, "evaluator_digest", _digest(self.evaluator_digest, "evaluator_digest"))
        object.__setattr__(self, "baseline_quality_score", _score(self.baseline_quality_score, "baseline_quality_score"))
        object.__setattr__(self, "candidate_quality_score", _score(self.candidate_quality_score, "candidate_quality_score"))
        _calls(self.baseline_model_calls, "baseline_model_calls")
        _calls(self.candidate_model_calls, "candidate_model_calls")
        if not isinstance(self.baseline_terminal_outcome, GraphPatchObservedOutcome):
            raise ValueError("baseline terminal outcome is invalid")
        if self.baseline_terminal_outcome is GraphPatchObservedOutcome.NOT_OBSERVED:
            raise ValueError("baseline terminal outcome must be observed")
        if self.baseline_run.job_id == self.candidate_run.job_id:
            raise ValueError("counterfactual runs must have distinct Job identities")
        if self.baseline_run.work_order_digest != self.candidate_run.work_order_digest:
            raise ValueError("counterfactual runs must bind the same Work Order")
        if self.baseline_run.initial_graph_digest != self.candidate_run.initial_graph_digest:
            raise ValueError("counterfactual runs must start from the same Graph")
        if self.baseline_run.revisions:
            raise ValueError("counterfactual baseline must retain the unchanged initial Graph")
        if len(self.candidate_run.revisions) != 1:
            raise ValueError("counterfactual candidate must contain exactly one accepted Graph revision")
        revision = self.candidate_run.revisions[0]
        if revision.sequence != 1 or revision.previous_graph_digest != self.baseline_run.initial_graph_digest:
            raise ValueError("counterfactual candidate revision is not the first exact Graph delta")
        if revision.observed_terminal_outcome is GraphPatchObservedOutcome.NOT_OBSERVED:
            raise ValueError("counterfactual candidate terminal outcome must be observed")
        object.__setattr__(self, "content_digest", content_digest(self.canonical_payload()))

    @property
    def revision_sequence(self) -> int:
        return self.candidate_run.revisions[0].sequence

    @property
    def candidate_terminal_outcome(self) -> GraphPatchObservedOutcome:
        return self.candidate_run.revisions[0].observed_terminal_outcome

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "schema": GRAPH_REVISION_IMPACT_EVIDENCE_SCHEMA,
            "context_fingerprint": self.context_fingerprint,
            "evaluator_digest": self.evaluator_digest,
            "baseline_graph_run_digest": self.baseline_run.content_digest,
            "candidate_graph_run_digest": self.candidate_run.content_digest,
            "baseline_terminal_outcome": self.baseline_terminal_outcome.value,
            "candidate_terminal_outcome": self.candidate_terminal_outcome.value,
            "baseline_quality_score": self.baseline_quality_score,
            "candidate_quality_score": self.candidate_quality_score,
            "baseline_model_calls": self.baseline_model_calls,
            "candidate_model_calls": self.candidate_model_calls,
        }


@dataclass(frozen=True, slots=True)
class GraphRevisionImpactAssessment:
    """Content-free actual-impact projection for a verified pair."""

    evidence_digest: str
    context_fingerprint: str
    candidate_revision_sequence: int
    expected_impact: str
    baseline_terminal_outcome: GraphPatchObservedOutcome
    candidate_terminal_outcome: GraphPatchObservedOutcome
    quality_delta: float
    model_call_delta: int
    disposition: GraphRevisionImpactDisposition


def assess_graph_revision_impact(
    evidence: GraphRevisionImpactEvidence,
) -> GraphRevisionImpactAssessment:
    """Compare exactly one accepted revision against its unchanged baseline."""

    revision = evidence.candidate_run.revisions[0]
    quality_delta = round(
        evidence.candidate_quality_score - evidence.baseline_quality_score, 6
    )
    model_call_delta = evidence.baseline_model_calls - evidence.candidate_model_calls
    baseline_success = evidence.baseline_terminal_outcome is GraphPatchObservedOutcome.JOB_SUCCEEDED
    candidate_success = revision.observed_terminal_outcome is GraphPatchObservedOutcome.JOB_SUCCEEDED
    if candidate_success and not baseline_success:
        disposition = GraphRevisionImpactDisposition.IMPROVED
    elif baseline_success and not candidate_success:
        disposition = GraphRevisionImpactDisposition.REGRESSED
    elif quality_delta > 1e-9:
        disposition = GraphRevisionImpactDisposition.IMPROVED
    elif quality_delta < -1e-9:
        disposition = GraphRevisionImpactDisposition.REGRESSED
    elif model_call_delta != 0:
        disposition = GraphRevisionImpactDisposition.IMPROVED if model_call_delta > 0 else GraphRevisionImpactDisposition.REGRESSED
    else:
        disposition = GraphRevisionImpactDisposition.NO_MEASURED_CHANGE
    return GraphRevisionImpactAssessment(
        evidence_digest=evidence.content_digest,
        context_fingerprint=evidence.context_fingerprint,
        candidate_revision_sequence=revision.sequence,
        expected_impact=revision.expected_impact.value,
        baseline_terminal_outcome=evidence.baseline_terminal_outcome,
        candidate_terminal_outcome=revision.observed_terminal_outcome,
        quality_delta=quality_delta,
        model_call_delta=model_call_delta,
        disposition=disposition,
    )


__all__ = [
    "GRAPH_REVISION_IMPACT_EVIDENCE_SCHEMA",
    "GraphRevisionImpactAssessment",
    "GraphRevisionImpactDisposition",
    "GraphRevisionImpactEvidence",
    "assess_graph_revision_impact",
]
