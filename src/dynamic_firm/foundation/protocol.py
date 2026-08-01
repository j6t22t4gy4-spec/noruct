"""Bounded first-party JSONL contract for an isolated employee foundation.

Only plain JSON values cross this boundary. Private-source/provider objects, secrets,
file descriptors, and executable callbacks never do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping


PROTOCOL_VERSION = "noruct.employee.v2"
MAX_FRAME_BYTES = 1_048_576
_FRAME_TYPES = frozenset(
    {
        "execute",
        "cancel",
        "model_request",
        "provider_response",
        "provider_error",
        "text_delta",
        "tool_intent",
        "tool_result",
        "terminal",
        "worker_error",
    }
)


class FoundationProtocolError(ValueError):
    """A malformed, oversized, or out-of-order worker frame."""


@dataclass(frozen=True, slots=True)
class FoundationFrame:
    type: str
    run_id: str
    seq: int
    payload: Mapping[str, Any] = field(default_factory=dict)


def encode_frame(frame: FoundationFrame) -> bytes:
    if frame.type not in _FRAME_TYPES:
        raise FoundationProtocolError(f"unsupported frame type: {frame.type}")
    if not frame.run_id or frame.seq < 1 or not isinstance(frame.payload, Mapping):
        raise FoundationProtocolError("frame requires run_id, positive seq, and object payload")
    try:
        raw = json.dumps(
            {
                "protocol": PROTOCOL_VERSION,
                "type": frame.type,
                "run_id": frame.run_id,
                "seq": frame.seq,
                "payload": dict(frame.payload),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FoundationProtocolError("frame payload is not JSON serializable") from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise FoundationProtocolError("frame exceeds the byte limit")
    return raw + b"\n"


def decode_frame(raw: bytes | str) -> FoundationFrame:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(encoded) > MAX_FRAME_BYTES + 1:
        raise FoundationProtocolError("frame exceeds the byte limit")
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FoundationProtocolError("frame is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise FoundationProtocolError("frame must be a JSON object")
    if value.get("protocol") != PROTOCOL_VERSION:
        raise FoundationProtocolError("frame protocol version mismatch")
    frame_type = value.get("type")
    run_id = value.get("run_id")
    seq = value.get("seq")
    payload = value.get("payload")
    if frame_type not in _FRAME_TYPES:
        raise FoundationProtocolError(f"unsupported frame type: {frame_type}")
    if not isinstance(run_id, str) or not run_id:
        raise FoundationProtocolError("frame run_id must be a non-empty string")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise FoundationProtocolError("frame seq must be a positive integer")
    if not isinstance(payload, dict):
        raise FoundationProtocolError("frame payload must be an object")
    return FoundationFrame(frame_type, run_id, seq, payload)


class FrameSequence:
    """Track one direction's monotonically increasing sequence per run."""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def accept(self, frame: FoundationFrame) -> None:
        expected = self._last.get(frame.run_id, 0) + 1
        if frame.seq != expected:
            raise FoundationProtocolError(
                f"out-of-order frame for {frame.run_id}: expected {expected}, got {frame.seq}"
            )
        self._last[frame.run_id] = frame.seq

    def next(self, run_id: str) -> int:
        value = self._last.get(run_id, 0) + 1
        self._last[run_id] = value
        return value
