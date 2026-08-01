"""Surface-neutral rendering of bounded execution-summary conclusions.

This module consumes only the existing execution-summary v1 payload or its
additive v2 envelope.  It does not inspect an execution, and unknown fields
are intentionally not carried into the view model or any renderer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Mapping

from .execution_summary import EXECUTION_SUMMARY_SCHEMA
from .execution_summary_v2 import EXECUTION_SUMMARY_V2_SCHEMA
from .terminal import FrameRow, frame_lines


_TEXT_LIMIT = 320
_ID_LIMIT = 192
_LIST_LIMIT = 3
_VERIFICATION_LIMIT = 5
def _text(value: object, *, limit: int = _TEXT_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit]


def _entry(value: object, *, fields: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        return ()
    output: list[tuple[str, str]] = []
    for name in fields:
        item = _text(value.get(name), limit=_ID_LIMIT if name.endswith("_id") else _TEXT_LIMIT)
        if item:
            output.append((name, item))
    return tuple(output)


@dataclass(frozen=True, slots=True)
class ConclusionEntry:
    """An immutable, allowlisted content-free projection entry."""

    fields: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, str]:
        return dict(self.fields)


@dataclass(frozen=True, slots=True)
class ConclusionRequest:
    purpose: str
    outcome: str


@dataclass(frozen=True, slots=True)
class ConclusionCompletion:
    terminal_status: str
    outcome_claim: str


@dataclass(frozen=True, slots=True)
class ConclusionApproach:
    company_work_mode: str
    planning_mode: str
    recorded_reasons: tuple[str, ...]
    assignment_rationale: tuple[ConclusionEntry, ...]


@dataclass(frozen=True, slots=True)
class ConclusionImprovement:
    status: str
    evidence_level: str


@dataclass(frozen=True, slots=True)
class ConclusionLimitation:
    status: str
    issue: str
    next_action: str


@dataclass(frozen=True, slots=True)
class ExecutionConclusionViewModel:
    """The single bounded model shared by CLI, Modern-TUI, and JSON."""

    source_schema: str
    job_id: str
    request: ConclusionRequest
    completion: ConclusionCompletion
    approach: ConclusionApproach
    contribution: tuple[ConclusionEntry, ...]
    review: tuple[ConclusionEntry, ...]
    verification: tuple[ConclusionEntry, ...]
    alternatives: tuple[ConclusionEntry, ...]
    improvement: ConclusionImprovement
    limitations_next: tuple[ConclusionLimitation, ...]


def _summary_parts(summary: Mapping[str, Any]) -> tuple[str, Mapping[str, Any], Mapping[str, Any]]:
    if not isinstance(summary, Mapping):
        raise TypeError("execution summary must be a mapping")
    schema = summary.get("schema_version")
    if schema == EXECUTION_SUMMARY_SCHEMA:
        return schema, summary, {}
    if schema == EXECUTION_SUMMARY_V2_SCHEMA:
        envelope = summary.get("v1")
        extensions = summary.get("extensions")
        if not isinstance(envelope, Mapping) or envelope.get("schema_version") != EXECUTION_SUMMARY_SCHEMA:
            raise ValueError("v2 execution summary must contain a v1 projection")
        if not isinstance(extensions, Mapping):
            raise ValueError("v2 execution summary must contain extensions")
        return schema, envelope, extensions
    raise ValueError("unsupported execution-summary projection")


def _entries(
    value: object,
    *,
    fields: tuple[str, ...],
    limit: int = _LIST_LIMIT,
) -> tuple[ConclusionEntry, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    output: list[ConclusionEntry] = []
    for item in value[:limit]:
        fields_value = _entry(item, fields=fields)
        if fields_value:
            output.append(ConclusionEntry(fields_value))
    return tuple(output)


def _extension_entries(
    extensions: Mapping[str, Any],
    name: str,
    *,
    fields: tuple[str, ...],
    limit: int = _LIST_LIMIT,
) -> tuple[ConclusionEntry, ...]:
    value = extensions.get(name)
    if isinstance(value, Mapping) and "items" in value:
        value = value.get("items")
    return _entries(value, fields=fields, limit=limit)


def build_execution_conclusion_view_model(
    summary: Mapping[str, Any],
) -> ExecutionConclusionViewModel:
    """Build one immutable conclusion model from a v1 or v2 projection."""

    source_schema, v1, extensions = _summary_parts(summary)
    result = v1.get("result") if isinstance(v1.get("result"), Mapping) else {}
    approach = v1.get("approach") if isinstance(v1.get("approach"), Mapping) else {}

    reasons_value = approach.get("recorded_reasons")
    reason_items = reasons_value[:2] if isinstance(reasons_value, (tuple, list)) else ()
    reasons = tuple(_text(item) for item in reason_items if _text(item))

    contribution = _extension_entries(
        extensions,
        "ai_contribution",
        fields=("employee_id", "task_id", "task_status", "responsibility", "summary"),
    )
    if not contribution:
        contribution = _entries(
            v1.get("contribution"),
            fields=("employee_id", "task_id", "task_status", "responsibility"),
        )

    review = _extension_entries(
        extensions,
        "review_focus",
        fields=("kind", "status", "reason", "summary"),
    )
    if not review:
        review = _entries(
            v1.get("review_focus"),
            fields=("kind", "status", "reason"),
        )

    verification = _entries(
        v1.get("verification"),
        fields=("name", "status", "evidence"),
        limit=_VERIFICATION_LIMIT,
    )
    limitations_raw = v1.get("limitations_next")
    limitations: list[ConclusionLimitation] = []
    if isinstance(limitations_raw, (tuple, list)):
        for item in limitations_raw[:_LIST_LIMIT]:
            if not isinstance(item, Mapping):
                continue
            issue = _text(item.get("issue"))
            next_action = _text(item.get("next_action"))
            status = _text(item.get("status"), limit=64)
            if issue or next_action or status:
                limitations.append(ConclusionLimitation(status, issue, next_action))

    return ExecutionConclusionViewModel(
        source_schema=source_schema,
        job_id=_text(v1.get("job_id"), limit=_ID_LIMIT),
        request=ConclusionRequest(
            purpose=_text(result.get("requested_purpose")) or "UNKNOWN",
            outcome=_text(result.get("requested_outcome")) or "UNKNOWN",
        ),
        completion=ConclusionCompletion(
            terminal_status=_text(result.get("terminal_status"), limit=64) or "NOT_RECORDED",
            outcome_claim=_text(result.get("outcome_claim"), limit=96) or "UNKNOWN",
        ),
        approach=ConclusionApproach(
            company_work_mode=_text(approach.get("company_work_mode"), limit=64) or "UNKNOWN",
            planning_mode=_text(approach.get("planning_mode"), limit=64) or "UNKNOWN",
            recorded_reasons=reasons,
            assignment_rationale=_extension_entries(
                extensions,
                "assignment_rationale",
                fields=(
                    "rationale_id",
                    "required_capability",
                    "material_difference_status",
                    "contribution_status",
                    "summary",
                ),
            ),
        ),
        contribution=contribution,
        review=review or (ConclusionEntry((("status", "NONE_RECORDED"),)),),
        verification=verification,
        alternatives=_extension_entries(
            extensions,
            "material_alternatives",
            fields=("alternative_id", "status", "exclusion_reason", "summary", "reason"),
        ),
        improvement=ConclusionImprovement(
            status=_text(extensions.get("improvement_status"), limit=96) or "NOT_RECORDED",
            evidence_level=_text(extensions.get("evidence_level"), limit=96) or "UNKNOWN",
        ),
        limitations_next=tuple(limitations),
    )


def _model_dict(model: ExecutionConclusionViewModel) -> dict[str, Any]:
    """Convert only model fields to JSON-compatible bounded data."""

    return {
        "source_schema": model.source_schema,
        "job_id": model.job_id,
        "request": asdict(model.request),
        "completion": asdict(model.completion),
        "approach": {
            "company_work_mode": model.approach.company_work_mode,
            "planning_mode": model.approach.planning_mode,
            "recorded_reasons": list(model.approach.recorded_reasons),
            "assignment_rationale": [item.as_dict() for item in model.approach.assignment_rationale],
        },
        "contribution": [item.as_dict() for item in model.contribution],
        "review": [item.as_dict() for item in model.review],
        "verification": [item.as_dict() for item in model.verification],
        "alternatives": [item.as_dict() for item in model.alternatives],
        "improvement": asdict(model.improvement),
        "limitations_next": [asdict(item) for item in model.limitations_next],
    }


def render_execution_conclusion_json(model: ExecutionConclusionViewModel) -> str:
    """Render the bounded model as plain, deterministic JSON."""

    if not isinstance(model, ExecutionConclusionViewModel):
        raise TypeError("JSON renderer requires an execution conclusion view model")
    return json.dumps(_model_dict(model), ensure_ascii=False, sort_keys=True)


def _entry_text(entry: ConclusionEntry) -> str:
    values = entry.as_dict()
    return "; ".join(f"{key}={value}" for key, value in values.items())


def _common_lines(model: ExecutionConclusionViewModel) -> list[str]:
    lines = [
        "Request",
        f"  purpose: {model.request.purpose}",
        f"  outcome: {model.request.outcome}",
        "Completion",
        f"  terminal status: {model.completion.terminal_status}",
        f"  outcome claim: {model.completion.outcome_claim}",
        "Approach",
        f"  work mode: {model.approach.company_work_mode}",
        f"  planning mode: {model.approach.planning_mode}",
        f"  reasons: {', '.join(model.approach.recorded_reasons) or 'NONE_RECORDED'}",
        "Contribution",
    ]
    lines.extend(f"  - {_entry_text(item)}" for item in model.contribution or (ConclusionEntry((("status", "NONE_RECORDED"),)),))
    lines.append("Review")
    lines.extend(f"  - {_entry_text(item)}" for item in model.review)
    lines.append("Verification")
    lines.extend(f"  - {_entry_text(item)}" for item in model.verification or (ConclusionEntry((("status", "NOT_RUN"),)),))
    lines.append("Alternatives")
    lines.extend(f"  - {_entry_text(item)}" for item in model.alternatives or (ConclusionEntry((("status", "NONE_RECORDED"),)),))
    lines.extend(
        [
            "Improvement",
            f"  status: {model.improvement.status}",
            f"  evidence level: {model.improvement.evidence_level}",
            "Limitations / next action",
        ]
    )
    lines.extend(
        f"  - status={item.status}; issue={item.issue}; next_action={item.next_action}"
        for item in model.limitations_next or (ConclusionLimitation("UNKNOWN", "NONE_RECORDED", "NO_ACTION_RECORDED"),)
    )
    return lines


def render_execution_conclusion_cli(model: ExecutionConclusionViewModel) -> str:
    """Render the shared model as concise CLI text."""

    if not isinstance(model, ExecutionConclusionViewModel):
        raise TypeError("CLI renderer requires an execution conclusion view model")
    title = "Execution conclusion" + (f" [{model.job_id}]" if model.job_id else "")
    return "\n".join((title, *_common_lines(model)))


def render_execution_conclusion_modern_tui(
    model: ExecutionConclusionViewModel,
    *,
    width: int = 80,
) -> str:
    """Render the shared model through the existing bounded terminal frame."""

    if not isinstance(model, ExecutionConclusionViewModel):
        raise TypeError("Modern-TUI renderer requires an execution conclusion view model")
    title = "Execution conclusion" + (f" [{model.job_id}]" if model.job_id else "")
    rows = tuple(FrameRow(text=line) for line in _common_lines(model))
    return "\n".join(frame_lines(title, rows, width))


# Short aliases keep surface names obvious to callers without creating a
# second projection or a second renderer-specific model.
execution_conclusion_view_model = build_execution_conclusion_view_model
render_cli = render_execution_conclusion_cli
render_modern_tui = render_execution_conclusion_modern_tui
render_plain_json = render_execution_conclusion_json


__all__ = [
    "ConclusionApproach",
    "ConclusionCompletion",
    "ConclusionEntry",
    "ConclusionImprovement",
    "ConclusionLimitation",
    "ConclusionRequest",
    "ExecutionConclusionViewModel",
    "build_execution_conclusion_view_model",
    "execution_conclusion_view_model",
    "render_cli",
    "render_execution_conclusion_cli",
    "render_execution_conclusion_json",
    "render_execution_conclusion_modern_tui",
    "render_modern_tui",
    "render_plain_json",
]
