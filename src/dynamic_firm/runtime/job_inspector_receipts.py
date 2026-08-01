"""Content-free receipt validation used by ACTIVE JOB inspection."""

from __future__ import annotations

from typing import Any, Mapping


def safe_tool_receipts(
    records: list[Mapping[str, Any]],
    *,
    errors: list[str],
) -> tuple[Mapping[str, str], ...]:
    """Project only tool identity/effect/terminal state for a Job summary."""

    if len(records) > 128:
        errors.append("Job tool receipt count exceeds retained projection limit")
        records = records[:128]
    effects = {"READ", "WRITE", "EXECUTE", "NETWORK", "EXTERNAL_COMMUNICATION"}
    statuses = {"INTENT_RECORDED", "STARTED", "SUCCEEDED", "FAILED", "INDETERMINATE"}
    receipts: list[Mapping[str, str]] = []
    for record in records:
        if not isinstance(record, Mapping):
            errors.append("Job tool receipt malformed")
            continue
        receipt = {
            key: str(record.get(key, ""))
            for key in ("action_id", "task_id", "tool_name", "status")
        }
        effect = record.get("effect")
        receipt["effect"] = effect if isinstance(effect, str) and effect else "UNKNOWN"
        if (
            not receipt["action_id"]
            or not receipt["task_id"]
            or not receipt["tool_name"]
            or receipt["effect"] not in {*effects, "UNKNOWN"}
            or receipt["status"] not in statuses
            or any(len(value.encode("utf-8")) > 192 for value in receipt.values())
        ):
            errors.append("Job tool receipt invalid")
            continue
        receipts.append(receipt)
    return tuple(receipts)


def safe_continuation_preflight_receipts(
    records: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    errors: list[str],
) -> tuple[Mapping[str, str], ...]:
    """Project immutable refusal class only; configuration details stay private."""

    if len(records) > 64:
        errors.append("Job continuation preflight receipt count exceeds retained projection limit")
        records = records[:64]
    kinds = {"READ_ONLY_PARTIAL", "GRAPH_PROPOSAL"}
    receipts: list[Mapping[str, str]] = []
    for record in records:
        if not isinstance(record, Mapping):
            errors.append("Job continuation preflight receipt malformed")
            continue
        receipt = {
            key: str(record.get(key, ""))
            for key in ("receipt_id", "continuation_kind", "code", "created_at")
        }
        if (
            not receipt["receipt_id"].startswith("continuation-preflight:")
            or receipt["continuation_kind"] not in kinds
            or not receipt["code"].isupper()
            or len(receipt["code"]) > 96
            or not receipt["created_at"]
            or any(len(value.encode("utf-8")) > 192 for value in receipt.values())
        ):
            errors.append("Job continuation preflight receipt invalid")
            continue
        receipts.append(receipt)
    return tuple(receipts)
