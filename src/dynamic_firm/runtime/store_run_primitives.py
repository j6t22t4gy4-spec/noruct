"""Shared privacy-preserving request identity helpers for RunStore components."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import to_primitive
from .redaction import redact_runtime_value


def _json(value: Any) -> str:
    return json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def employee_session_namespace(employee_id: str, continuity_key: str) -> str:
    """Return the opaque first-party namespace for one employee conversation."""

    employee = employee_id.strip()
    continuity = continuity_key.strip()
    if not employee or not continuity:
        raise ValueError("employee_id and continuity_key must be non-empty")
    return hashlib.sha256(
        _json(["noruct.employee-session.v1", employee, continuity]).encode("utf-8")
    ).hexdigest()


def safe_request_json(value: Any, employee_id: str) -> str:
    """Persist idempotency identity without retaining a caller session key."""

    projected = to_primitive(value)
    if not isinstance(projected, dict):
        raise ValueError("employee run request snapshot must be an object")
    session_key = projected.get("session_key")
    if isinstance(session_key, str) and session_key.strip():
        projected["session_key"] = {
            "namespace_hash": employee_session_namespace(employee_id, session_key)
        }
    context = projected.get("context")
    if isinstance(context, dict):
        evidence = context.get("task_evidence")
        if isinstance(evidence, dict):
            raw_items = evidence.get("items")
            items = raw_items if isinstance(raw_items, list) else []
            context["task_evidence"] = {
                "pack_id": evidence.get("pack_id"),
                "revision": evidence.get("revision"),
                "pack_digest": evidence.get("pack_digest"),
                "delivery_digest": evidence.get("delivery_digest"),
                "access_scope": evidence.get("access_scope"),
                "item_count": len(items),
                "selected_bytes": sum(
                    len(str(item.get("content") or "").encode("utf-8"))
                    for item in items
                    if isinstance(item, dict)
                ),
                "content_retained": False,
            }
    return _json(redact_runtime_value(projected))
