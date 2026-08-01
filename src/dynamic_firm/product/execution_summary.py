"""Bounded, honest terminal Job-summary projection.

This is a read-only product projection over an already replayed ACTIVE JOB and
the optional user-local Work Order authority.  It does not persist a second
summary ledger and deliberately refuses to turn a terminal Job status into a
claim about validation, safety, or real-world effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .execution_delivery_evidence import delivery_evidence


EXECUTION_SUMMARY_SCHEMA = "noruct.execution-summary.v1"
_TEXT_LIMIT = 320
_CONTRIBUTION_LIMIT = 3
_REVIEW_FOCUS_LIMIT = 3
_VERIFICATION_LIMIT = 5
_LIMITATION_LIMIT = 3


def _text(value: object, *, limit: int = _TEXT_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.replace("\x00", "").split())
    return normalized[:limit]


def _inspection_value(inspection: object, name: str, default: object = "") -> object:
    return getattr(inspection, name, default)


def _terminal_status(inspection: object) -> str:
    status = _text(_inspection_value(inspection, "job_status"), limit=64)
    return status or "NOT_RECORDED"


def _contributions(inspection: object) -> tuple[dict[str, str], ...]:
    entries: list[dict[str, str]] = []
    for task in tuple(_inspection_value(inspection, "reconstructed_tasks", ())):
        if not isinstance(task, Mapping):
            continue
        task_id = _text(task.get("task_id"), limit=128)
        employee_id = _text(task.get("assignee_id"), limit=128)
        status = _text(task.get("status"), limit=64) or "UNKNOWN"
        if not task_id or not employee_id:
            continue
        entries.append(
            {
                "employee_id": employee_id,
                "task_id": task_id,
                "task_status": status,
                "responsibility": "TASK_EXECUTION",
            }
        )
        if len(entries) == _CONTRIBUTION_LIMIT:
            break
    return tuple(entries)


def _effect_verification(inspection: object) -> dict[str, str]:
    """Report only durable action state, never a real-world effect claim."""

    effectful = tuple(
        item
        for item in tuple(_inspection_value(inspection, "tool_receipts", ()))
        if isinstance(item, Mapping)
        and item.get("effect") in {"WRITE", "EXECUTE", "EXTERNAL_COMMUNICATION"}
    )
    if not effectful:
        return {
            "name": "EXTERNAL_EFFECT_RECEIPTS",
            "status": "NOT_RUN",
            "evidence": "no retained effectful tool action receipt",
        }
    statuses = {str(item.get("status", "")) for item in effectful}
    if statuses & {"INTENT_RECORDED", "STARTED", "INDETERMINATE"}:
        status = "UNKNOWN"
    else:
        # A durable terminal action receipt proves only the local action
        # lifecycle. It does not independently verify a file, command, or
        # remote-world outcome, so it cannot be PASSED here.
        status = "PARTIAL"
    return {
        "name": "EXTERNAL_EFFECT_RECEIPTS",
        "status": status,
        "evidence": f"{len(effectful)} content-free terminal action receipt(s); real-world outcome remains unverified",
    }


def _continuation_preflight_verification(inspection: object) -> dict[str, str]:
    """Surface a durable refusal without pretending a retry changed the Job."""

    refusals = tuple(
        item
        for item in tuple(_inspection_value(inspection, "continuation_preflight_receipts", ()))
        if isinstance(item, Mapping)
    )
    if not refusals:
        return {
            "name": "CONTINUATION_PREFLIGHT",
            "status": "NOT_RUN",
            "evidence": "no retained same-Job continuation refusal receipt",
        }
    codes = ", ".join(sorted({_text(item.get("code"), limit=96) for item in refusals if _text(item.get("code"), limit=96)}))
    return {
        "name": "CONTINUATION_PREFLIGHT",
        "status": "FAILED",
        "evidence": f"{len(refusals)} retained pre-dispatch refusal receipt(s): {codes or 'UNKNOWN'}",
    }


def execution_summary(
    inspection: object,
    *,
    work_order: object | None = None,
) -> dict[str, Any]:
    """Project a terminal report without widening the retained-data boundary."""

    audit_status = _text(
        _inspection_value(_inspection_value(inspection, "audit_status", None), "value"),
        limit=64,
    ) or "UNKNOWN"
    terminal_status = _terminal_status(inspection)
    objective = _text(getattr(work_order, "objective", ""))
    requested_outcome = _text(getattr(work_order, "requested_outcome", ""))
    operating_reason = _text(_inspection_value(inspection, "operating_reason"), limit=96)
    planning_reason = _text(_inspection_value(inspection, "planning_reason"), limit=96)
    reasons = tuple(
        reason for reason in (operating_reason, planning_reason) if reason
    )[:2]
    review_focus: list[dict[str, str]] = []
    requested_effect = _text(_inspection_value(inspection, "requested_effect"), limit=64)
    if requested_effect == "HOST_ACTION":
        review_focus.append(
            {
                "kind": "EXTERNAL_EFFECT_BOUNDARY",
                "status": "REVIEW_REQUIRED",
                "reason": "HOST_ACTION was requested; effect receipts require separate review.",
            }
        )
    if audit_status == "INVALID":
        review_focus.append(
            {
                "kind": "ACTIVE_JOB_AUDIT",
                "status": "REVIEW_REQUIRED",
                "reason": "The replayed ACTIVE JOB audit is invalid.",
            }
        )

    replay_matches = bool(_inspection_value(inspection, "replay_matches", False))
    audit_verification = (
        "PASSED" if audit_status == "TERMINAL" and replay_matches else "FAILED"
        if audit_status == "INVALID" else "UNKNOWN"
    )
    verification = (
        {
            "name": "ACTIVE_JOB_AUDIT_REPLAY",
            "status": audit_verification,
            "evidence": "append-only ledger replay",
        },
        {
            "name": "WORK_OUTCOME_VALIDATION",
            "status": "NOT_RUN",
            "evidence": "no named validation receipt retained in this summary projection",
        },
        _effect_verification(inspection),
        _continuation_preflight_verification(inspection),
    )[:_VERIFICATION_LIMIT]
    limitations: list[dict[str, str]] = []
    if not objective:
        limitations.append(
            {
                "status": "UNKNOWN",
                "issue": "Requested purpose is unavailable because the matching local Work Order was not read.",
                "next_action": "Inspect the user-local Work Order authority separately.",
            }
        )
    if terminal_status == "NOT_RECORDED":
        limitations.append(
            {
                "status": "UNKNOWN",
                "issue": "The Job has no terminal result in the retained audit.",
                "next_action": "Inspect Job lifecycle and recovery guidance; do not infer completion.",
            }
        )
    else:
        limitations.append(
            {
                "status": "UNKNOWN",
                "issue": "Terminal Job status is not evidence of real-world outcome success.",
                "next_action": "Review the named validation and effect evidence separately.",
            }
        )
    return {
        "schema_version": EXECUTION_SUMMARY_SCHEMA,
        "job_id": _text(_inspection_value(inspection, "job_id"), limit=192),
        "result": {
            "requested_purpose": objective or "UNKNOWN",
            "requested_outcome": requested_outcome or "UNKNOWN",
            "terminal_status": terminal_status,
            "outcome_claim": "NO_REAL_WORLD_OUTCOME_CLAIM",
        },
        "approach": {
            "company_work_mode": _text(_inspection_value(inspection, "company_work_mode"), limit=64) or "UNKNOWN",
            "planning_mode": _text(_inspection_value(inspection, "planning_mode"), limit=64) or "UNKNOWN",
            "recorded_reasons": reasons,
        },
        "contribution": _contributions(inspection),
        "review_focus": tuple(review_focus[:_REVIEW_FOCUS_LIMIT]) or (
            {
                "kind": "NONE_RECORDED",
                "status": "NONE_RECORDED",
                "reason": "No bounded review focus was retained.",
            },
        ),
        "verification": verification,
        "delivery": delivery_evidence(inspection),
        "limitations_next": tuple(limitations[:_LIMITATION_LIMIT]),
    }
