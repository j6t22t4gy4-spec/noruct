"""Shared bounded-value helpers for the canonical Knowledge Store components."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Mapping


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def _bounded_text(value: object, label: str, maximum_bytes: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{label} must be non-empty")
    if len(normalized.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its {maximum_bytes} byte limit")
    return normalized


def _truncated_text(value: object, label: str, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    payload = value.strip().encode("utf-8")
    if len(payload) <= maximum_bytes:
        return payload.decode("utf-8")
    clipped = payload[:maximum_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8").rstrip()
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def _normalized_scope(value: object) -> str:
    return _bounded_text(value, "Knowledge access scope", 256)


def _bounded_mapping(
    value: Mapping[str, object] | None,
    label: str,
    maximum_bytes: int,
) -> dict[str, object]:
    try:
        normalized = dict(value or {})
        encoded = _json(normalized).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON data") from exc
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{label} exceeds its {maximum_bytes} byte limit")
    return normalized


def _normalized_timestamp(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Knowledge timestamps must include an explicit timezone")
    return parsed.astimezone(UTC).isoformat()
