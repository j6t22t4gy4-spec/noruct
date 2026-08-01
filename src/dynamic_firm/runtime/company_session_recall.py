"""Bounded employee tools for a user's own local Company session ledger.

The registered Hermes ``session_search_tool`` is a useful interaction shape:
discover sessions, then read a small scrollable window.  Its profile scans,
FTS index, raw transcript messages and foreign state authority are deliberately
not a Noruct runtime dependency.  This adapter exposes only locally persisted
Company goals and terminal summaries through ordinary parent-authorized READ
tools.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol, Sequence

from .models import IdempotencyMode, ToolEffect, ToolRisk
from .ports import CancellationToken
from .tools import ToolDefinition, ToolValidationError


_CATALOG_RESOURCE = "company:session:catalog"
_MAX_DISCOVERY_PREVIEW_BYTES = 1_024


class CompanySessionListingRowPort(Protocol):
    """Content-free fields exposed by the Product-owned session catalog."""

    session_id: str
    title: str
    preview: str
    turn_count: int


class CompanySessionTurnRowPort(Protocol):
    """One bounded terminal-summary row from the Product-owned session ledger."""

    position: int
    status: str
    goal: str
    summary: str


class CompanySessionRecallPort(Protocol):
    """Read-only Product session projection injected into Runtime tools.

    The Product component remains the SQLite/schema owner. Runtime knows only
    the capped listing and turn windows needed by the parent-authorized tool.
    """

    def recall_listing_rows(
        self,
        *,
        search_query: str | None,
        exclude_session_id: str | None,
        limit: int,
    ) -> Sequence[CompanySessionListingRowPort]: ...

    def recall_turns(
        self,
        *,
        session_id: str,
        after_position: int,
        limit: int,
    ) -> Sequence[CompanySessionTurnRowPort]: ...


def _required_string(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_limit(arguments: Mapping[str, Any], *, default: int) -> int:
    value = arguments.get("limit", default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 8:
        raise ToolValidationError("limit must be an integer between 1 and 8")
    return value


def _preview(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_DISCOVERY_PREVIEW_BYTES:
        return value, False
    return encoded[:_MAX_DISCOVERY_PREVIEW_BYTES].decode("utf-8", errors="ignore"), True


class CompanySessionRecallTools:
    """One frozen Job's private, read-only local Company-memory surface."""

    def __init__(
        self,
        store: CompanySessionRecallPort,
        *,
        current_session_id: str,
    ) -> None:
        self._store = store
        self._current_session_id = current_session_id

    def definitions(self) -> tuple[ToolDefinition, ToolDefinition]:
        return self._search_definition(), self._read_definition()

    def _search_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not set(arguments).issubset({"query", "limit"}):
                raise ToolValidationError("search_company_session_memory received unknown fields")
            query = arguments.get("query", "")
            if not isinstance(query, str):
                raise ToolValidationError("query must be a string")
            query = query.strip()
            if len(query.encode("utf-8")) > 160:
                raise ToolValidationError("query exceeds the 160-byte local bound")
            return {"query": query, "limit": _optional_limit(arguments, default=5)}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            rows = self._store.recall_listing_rows(
                search_query=str(arguments["query"]) or None,
                exclude_session_id=self._current_session_id or None,
                limit=int(arguments["limit"]),
            )
            sessions = []
            for row in rows:
                preview, truncated = _preview(row.preview)
                sessions.append(
                    {
                        "session_id": row.session_id,
                        "title": row.title,
                        "summary_preview": preview,
                        "preview_truncated": truncated,
                        "turn_count": row.turn_count,
                    }
                )
            return json.dumps(
                {
                    "scope": "local_company_ledger",
                    "query": str(arguments["query"]),
                    "sessions": sessions,
                    "read_tool": "read_company_session_memory",
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        return ToolDefinition(
            name="search_company_session_memory",
            description=(
                "Search or browse prior local Company session summaries. This returns only session "
                "metadata and a bounded terminal-summary preview; it never reads raw transcripts, "
                "other profiles, remote sessions, credentials, or tool output."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 160},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5},
                },
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=lambda _arguments: _CATALOG_RESOURCE,
            handler=handle,
            output_limit_bytes=12_000,
            parallel_safe=True,
        )

    def _read_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not set(arguments).issubset({"session_id", "after_position", "limit"}):
                raise ToolValidationError("read_company_session_memory received unknown fields")
            session_id = _required_string(arguments, "session_id")
            if len(session_id.encode("utf-8")) > 128:
                raise ToolValidationError("session_id exceeds the local bound")
            if self._current_session_id and session_id == self._current_session_id:
                raise ToolValidationError("Current session is already available in the active Company context")
            position = arguments.get("after_position", 0)
            if not isinstance(position, int) or isinstance(position, bool) or position < 0:
                raise ToolValidationError("after_position must be a non-negative integer")
            return {
                "session_id": session_id,
                "after_position": position,
                "limit": _optional_limit(arguments, default=4),
            }

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            try:
                rows = self._store.recall_turns(
                    session_id=str(arguments["session_id"]),
                    after_position=int(arguments["after_position"]),
                    limit=int(arguments["limit"]),
                )
            except KeyError as exc:
                raise ToolValidationError(str(exc)) from exc
            return json.dumps(
                {
                    "scope": "local_company_ledger",
                    "session_id": str(arguments["session_id"]),
                    "turns": [
                        {
                            "position": row.position,
                            "status": row.status,
                            "goal": row.goal,
                            "summary": row.summary,
                        }
                        for row in rows
                    ],
                    "next_after_position": rows[-1].position if rows else int(arguments["after_position"]),
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        return ToolDefinition(
            name="read_company_session_memory",
            description=(
                "Read the next bounded window of goal and terminal-summary records from one session "
                "returned by search_company_session_memory. The session must be a prior local Company "
                "session; raw transcript, tool output, provider identity, and remote data are unavailable."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "after_position": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8, "default": 4},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=lambda arguments: f"company:session:{arguments['session_id']}",
            handler=handle,
            output_limit_bytes=12_000,
            parallel_safe=True,
        )
