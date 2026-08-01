"""Deterministic, bounded projection of caller-recorded review focus facts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypedDict


REVIEW_FOCUS_SCHEMA = "noruct.review-focus.v1"
NONE_RECORDED = "NONE_RECORDED"
_LIMIT = 3


class ReviewFocusItem(TypedDict):
    """The single surface item schema; ordering facts are not projected."""

    subject: str
    reason: str
    evidence_id: str
    failure_impact: str


# These are fixed precedence tables, not a severity or evidence assessment.
# Values outside the table are rejected so the projection never invents a
# meaning for a caller fact it does not understand.
_SEVERITY_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "UNKNOWN": 4,
}
_EVIDENCE_STATUS_ORDER = {
    "FAILED": 0,
    "UNKNOWN": 1,
    "NOT_RUN": 2,
    "PARTIAL": 3,
    "PASSED": 4,
}


def _required_text(candidate: Mapping[str, object], name: str) -> str:
    value = candidate.get(name)
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _candidate(candidate: Mapping[str, object]) -> tuple[ReviewFocusItem, tuple[int, int, str]]:
    if not isinstance(candidate, Mapping):
        raise TypeError("review-focus candidates must be mappings")

    item: ReviewFocusItem = {
        "subject": _required_text(candidate, "subject"),
        "reason": _required_text(candidate, "reason"),
        "evidence_id": _required_text(candidate, "evidence_id"),
        "failure_impact": _required_text(candidate, "failure_impact"),
    }
    severity = _required_text(candidate, "severity")
    evidence_status = _required_text(candidate, "evidence_status")
    try:
        order = (
            _SEVERITY_ORDER[severity],
            _EVIDENCE_STATUS_ORDER[evidence_status],
            item["evidence_id"],
        )
    except KeyError as exc:
        raise ValueError(f"unsupported review-focus ordering fact: {exc.args[0]}") from exc
    return item, order


def project_review_focus(
    candidates: Iterable[Mapping[str, object]],
) -> tuple[ReviewFocusItem, ...] | str:
    """Return the stable top-three projection or ``NONE_RECORDED``.

    ``severity`` and ``evidence_status`` are consumed only as fixed ordering
    facts.  They are intentionally absent from the returned item schema.
    """

    prepared = [_candidate(candidate) for candidate in candidates]
    evidence_ids = [item["evidence_id"] for item, _ in prepared]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("review-focus evidence_id values must be unique")
    if not prepared:
        return NONE_RECORDED
    prepared.sort(key=lambda entry: entry[1])
    return tuple(item for item, _ in prepared[:_LIMIT])


review_focus = project_review_focus


__all__ = [
    "NONE_RECORDED",
    "REVIEW_FOCUS_SCHEMA",
    "ReviewFocusItem",
    "project_review_focus",
    "review_focus",
]
