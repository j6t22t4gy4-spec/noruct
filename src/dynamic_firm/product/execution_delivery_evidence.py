"""One bounded delivery-evidence projection for code and non-code Jobs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DELIVERY_EVIDENCE_SCHEMA = "noruct.delivery-evidence.v1"
_ITEM_LIMIT = 3


def _text(value: object, *, limit: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit]


def _effect_status(receipts: object) -> str:
    effectful = tuple(
        item
        for item in tuple(receipts) if isinstance(item, Mapping)
        and item.get("effect") in {"WRITE", "EXECUTE", "EXTERNAL_COMMUNICATION"}
    )
    if not effectful:
        return "NOT_RUN"
    statuses = {str(item.get("status", "")) for item in effectful}
    if statuses & {"INTENT_RECORDED", "STARTED", "INDETERMINATE"}:
        return "UNKNOWN"
    return "PARTIAL"


def _code_validation(inspection: object) -> tuple[dict[str, str], ...]:
    receipts = tuple(
        item
        for item in tuple(getattr(inspection, "validation_receipts", ()))
        if isinstance(item, Mapping)
    )[:_ITEM_LIMIT]
    if not receipts:
        return (
            {
                "name": "TEST_EXECUTION",
                "status": "NOT_RUN",
                "evidence": "no retained named coding validation receipt",
            },
        )
    return tuple(
        {
            "name": _text(item.get("name"), limit=128) or "UNNAMED_VALIDATION",
            "status": "PASSED" if item.get("status") == "PASSED" else "FAILED",
            "evidence": "retained coding validation receipt",
        }
        for item in receipts
    )


def delivery_evidence(inspection: object) -> dict[str, Any]:
    """Describe delivery facts without treating terminal state as outcome proof.

    The outer execution summary owns user purpose and planning reason. This
    nested schema owns only delivery kind, AI responsibility boundary, review
    focus, retained verification and the explicit limits of that evidence.
    """

    final_task_id = _text(getattr(inspection, "final_task_id", ""), limit=128)
    capabilities = frozenset(
        _text(value, limit=64)
        for value in tuple(getattr(inspection, "final_task_capabilities", ()))
    )
    is_code = "implementation" in capabilities
    effect_status = _effect_status(getattr(inspection, "tool_receipts", ()))
    if is_code:
        return {
            "schema_version": DELIVERY_EVIDENCE_SCHEMA,
            "kind": "CODE",
            "subject": {
                "kind": "WORKSPACE_CHANGESET",
                "status": effect_status,
            },
            "ai_responsibility": {
                "task_id": final_task_id or "UNKNOWN",
                "scope": "IMPLEMENTATION_TASK",
            },
            "review_focus": (
                {
                    "kind": "CHANGESET_AND_VALIDATION",
                    "status": "REVIEW_REQUIRED",
                },
            ),
            "verification": _code_validation(inspection),
            "limitations": (
                {
                    "status": "UNKNOWN",
                    "issue": "A coding validation receipt does not prove a real-workspace or user outcome.",
                },
            ),
        }

    requested_effect = _text(getattr(inspection, "requested_effect", ""), limit=64)
    subject_kind = (
        "EXTERNAL_EFFECT"
        if requested_effect == "HOST_ACTION" or effect_status != "NOT_RUN"
        else "ARTIFACT_OR_RESPONSE"
    )
    return {
        "schema_version": DELIVERY_EVIDENCE_SCHEMA,
        "kind": "NON_CODE",
        "subject": {"kind": subject_kind, "status": effect_status},
        "ai_responsibility": {
            "task_id": final_task_id or "UNKNOWN",
            "scope": "TASK_RESULT",
        },
        "review_focus": (
            {
                "kind": "EFFECT_OR_ARTIFACT_BOUNDARY",
                "status": "REVIEW_REQUIRED" if subject_kind == "EXTERNAL_EFFECT" else "NONE_RECORDED",
            },
        ),
        "verification": (
            {
                "name": "ARTIFACT_OR_EFFECT_OUTCOME",
                "status": effect_status,
                "evidence": "retained action receipt only; no real-world outcome verification",
            },
        ),
        "limitations": (
            {
                "status": "UNKNOWN",
                "issue": "No retained artifact or external-effect receipt proves a user outcome.",
            },
        ),
    }
