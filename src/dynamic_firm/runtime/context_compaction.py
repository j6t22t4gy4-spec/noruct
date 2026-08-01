from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import ModelMessage, to_primitive


@dataclass(frozen=True, slots=True)
class ContextCompactionResult:
    messages: tuple[ModelMessage, ...]
    compacted: bool
    removed_message_count: int = 0
    source_hash: str = ""
    chars_before: int = 0
    chars_after: int = 0


@dataclass(frozen=True, slots=True)
class SessionHistoryCompactionResult:
    messages: tuple[dict[str, Any], ...]
    compacted: bool
    removed_message_count: int = 0
    source_hash: str = ""
    chars_before: int = 0
    chars_after: int = 0


def _canonical_messages(messages: Sequence[ModelMessage]) -> str:
    return json.dumps(
        to_primitive(tuple(messages)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_history(messages: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(
        tuple(dict(message) for message in messages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _conversation_groups(messages: Sequence[ModelMessage]) -> list[list[ModelMessage]]:
    """Keep assistant tool requests adjacent to all following tool results."""

    groups: list[list[ModelMessage]] = []
    for message in messages:
        if message.role == "tool" and groups and groups[-1][0].role == "assistant":
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def _history_groups(
    messages: Sequence[Mapping[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Keep a stored assistant tool request adjacent to its tool results."""

    groups: list[list[dict[str, Any]]] = []
    for raw in messages:
        message = dict(raw)
        role = str(message.get("role") or "")
        if not role:
            raise ValueError("session history messages require a role")
        if role == "tool" and groups and groups[-1][0].get("role") == "assistant":
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


class BoundedContextCompactor:
    """Build a bounded model projection without changing the immutable ledger.

    The private foundation's head/tail protection and summary boundary are
    adapted behind a deterministic first-party projection. Noruct does not ask
    a model to summarize here: canonical run messages stay in SQLite, while the
    provider receives only a content-free digest for older completed groups.
    """

    revision = "noruct-head-tail-digest-v2"

    def compact_session_history(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_messages: int,
        max_chars: int,
        keep_recent_messages: int,
    ) -> SessionHistoryCompactionResult:
        """Project durable employee history without flattening tool-call objects."""

        original = tuple(dict(message) for message in messages)
        serialized = _canonical_history(original)
        chars_before = len(serialized)
        if (
            len(original) <= max_messages and chars_before <= max_chars
        ) or len(original) <= 2:
            return SessionHistoryCompactionResult(
                original,
                False,
                chars_before=chars_before,
                chars_after=chars_before,
            )

        groups = _history_groups(original)
        tail: list[list[dict[str, Any]]] = []
        kept = 0
        while groups and (kept < max(2, keep_recent_messages) or not tail):
            group = groups.pop()
            tail.insert(0, group)
            kept += len(group)
        removed = [message for group in groups for message in group]
        if not removed:
            return SessionHistoryCompactionResult(
                original,
                False,
                chars_before=chars_before,
                chars_after=chars_before,
            )

        removed_serialized = _canonical_history(removed)
        source_hash = hashlib.sha256(removed_serialized.encode("utf-8")).hexdigest()
        roles = Counter(str(message.get("role") or "unknown") for message in removed)
        boundary = {
            "role": "user",
            "content": {
                "runtime_context_compaction": {
                    "revision": self.revision,
                    "historical_only": True,
                    "instruction": (
                        "This digest describes older completed runtime context. "
                        "It is not a current user instruction and grants no authority."
                    ),
                    "removed_message_count": len(removed),
                    "role_counts": dict(sorted(roles.items())),
                    "source_sha256": source_hash,
                }
            },
        }
        projected = (
            boundary,
            *(message for group in tail for message in group),
        )
        chars_after = len(_canonical_history(projected))
        return SessionHistoryCompactionResult(
            tuple(projected),
            True,
            removed_message_count=len(removed),
            source_hash=source_hash,
            chars_before=chars_before,
            chars_after=chars_after,
        )

    def compact(
        self,
        messages: Sequence[ModelMessage],
        *,
        max_messages: int,
        max_chars: int,
        keep_recent_messages: int,
    ) -> ContextCompactionResult:
        original = tuple(messages)
        serialized = _canonical_messages(original)
        chars_before = len(serialized)
        if (
            len(original) <= max_messages
            and chars_before <= max_chars
        ) or len(original) <= 3:
            return ContextCompactionResult(
                original,
                False,
                chars_before=chars_before,
                chars_after=chars_before,
            )

        # The stable system prompt and initial task context are never compacted.
        prefix = original[:2]
        groups = _conversation_groups(original[2:])
        tail: list[list[ModelMessage]] = []
        kept = 0
        while groups and (kept < max(2, keep_recent_messages) or not tail):
            group = groups.pop()
            tail.insert(0, group)
            kept += len(group)
        removed = tuple(message for group in groups for message in group)
        if not removed:
            return ContextCompactionResult(
                original,
                False,
                chars_before=chars_before,
                chars_after=chars_before,
            )

        removed_serialized = _canonical_messages(removed)
        source_hash = hashlib.sha256(removed_serialized.encode("utf-8")).hexdigest()
        roles = Counter(message.role for message in removed)
        boundary = ModelMessage(
            "user",
            {
                "runtime_context_compaction": {
                    "revision": self.revision,
                    "historical_only": True,
                    "instruction": (
                        "This digest describes older completed runtime context. "
                        "It is not a current user instruction and grants no authority."
                    ),
                    "removed_message_count": len(removed),
                    "role_counts": dict(sorted(roles.items())),
                    "source_sha256": source_hash,
                }
            },
        )
        projected = (*prefix, boundary, *(message for group in tail for message in group))
        chars_after = len(_canonical_messages(projected))
        return ContextCompactionResult(
            tuple(projected),
            True,
            removed_message_count=len(removed),
            source_hash=source_hash,
            chars_before=chars_before,
            chars_after=chars_after,
        )
