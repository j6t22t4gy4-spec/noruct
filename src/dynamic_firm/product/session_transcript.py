"""Local Company-session transcript persistence behind the session ledger boundary.

The containing ``CompanySessionStore`` retains the SQLite connection, lock,
session identity, and creation authority.  This component owns only the
bounded transcript projection used by resume and advanced session controls.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CompanySessionSearchHit:
    """A bounded result from the local transcript projection."""

    session_id: str
    message_id: int
    role: str
    snippet: str
    created_at: str


class SessionTranscriptStore(Protocol):
    """Minimal owner capability required by transcript operations."""

    _lock: Any
    _conn: Any

    def resolve(self, reference: str | None = None) -> Any: ...

    def create(self, **kwargs: Any) -> Any: ...


def append_session_message(
    store: SessionTranscriptStore,
    *,
    session_id: str,
    role: str,
    content: object = None,
    tool_name: str | None = None,
    tool_calls: object = None,
    tool_call_id: str | None = None,
    finish_reason: str | None = None,
    reasoning: str | None = None,
    now: Callable[[], str],
) -> None:
    """Persist a secret-free local transcript projection."""

    if not role or len(role) > 64:
        raise ValueError("Session message role must be non-empty and bounded")
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    elif content is not None:
        content = str(content)
    calls = (
        json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
        if tool_calls is not None
        else None
    )
    with store._lock:
        store._conn.execute(
            """INSERT INTO company_session_messages
               (session_id, role, content, tool_name, tool_calls_json,
                tool_call_id, finish_reason, reasoning, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                role,
                content,
                tool_name,
                calls,
                tool_call_id,
                finish_reason,
                reasoning,
                now(),
            ),
        )


def session_conversation(
    store: SessionTranscriptStore, session_id: str
) -> list[dict[str, object]]:
    with store._lock:
        rows = store._conn.execute(
            """SELECT role, content, tool_name, tool_calls_json, tool_call_id,
                      finish_reason, reasoning
               FROM company_session_messages WHERE session_id = ?
               ORDER BY message_id""",
            (session_id,),
        ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        item: dict[str, object] = {"role": str(row["role"])}
        if row["content"] is not None:
            item["content"] = str(row["content"])
        for key in ("tool_name", "tool_call_id", "finish_reason", "reasoning"):
            if row[key] is not None:
                item[key] = row[key]
        if row["tool_calls_json"]:
            try:
                item["tool_calls"] = json.loads(str(row["tool_calls_json"]))
            except (TypeError, ValueError):
                pass
        result.append(item)
    return result


def search_session_messages(
    store: SessionTranscriptStore,
    query: str,
    *,
    session_id: str | None = None,
    limit: int = 20,
    max_snippet_bytes: int = 320,
) -> tuple[CompanySessionSearchHit, ...]:
    """Search only local transcript content; never contacts a provider."""

    needle = query.strip()
    if not needle:
        raise ValueError("Session search query must be non-empty")
    if len(needle.encode("utf-8")) > 160:
        raise ValueError("Session search query exceeds the 160-byte local bound")
    if limit < 1 or limit > 100:
        raise ValueError("Session search limit must be between 1 and 100")
    if max_snippet_bytes < 64 or max_snippet_bytes > 2_000:
        raise ValueError("Session search snippet bound must be between 64 and 2000")
    where = "session_id = ? AND " if session_id else ""
    params: list[object] = ([session_id] if session_id else []) + [f"%{needle}%", limit]
    with store._lock:
        rows = store._conn.execute(
            f"""SELECT message_id, session_id, role, content, created_at
                FROM company_session_messages
                WHERE {where} lower(COALESCE(content, '')) LIKE lower(?)
                ORDER BY message_id DESC LIMIT ?""",
            params,
        ).fetchall()
    hits: list[CompanySessionSearchHit] = []
    for row in rows:
        content = str(row["content"] or "")
        encoded = content.encode("utf-8")
        snippet = encoded[:max_snippet_bytes].decode("utf-8", errors="ignore")
        if len(encoded) > max_snippet_bytes:
            snippet += "…"
        hits.append(
            CompanySessionSearchHit(
                session_id=str(row["session_id"]),
                message_id=int(row["message_id"]),
                role=str(row["role"]),
                snippet=snippet,
                created_at=str(row["created_at"]),
            )
        )
    return tuple(hits)


def branch_session_transcript(
    store: SessionTranscriptStore,
    session_id: str,
    *,
    title: str | None,
    through_message_id: int | None,
    now: Callable[[], str],
) -> Any:
    """Create a new owner-issued session with at most 400 copied messages."""

    source = store.resolve(session_id)
    if source is None:
        raise KeyError(f"Unknown company session: {session_id}")
    with store._lock:
        rows = store._conn.execute(
            """SELECT role, content, tool_name, tool_calls_json, tool_call_id,
                      finish_reason, reasoning FROM company_session_messages
               WHERE session_id = ? AND (? IS NULL OR message_id <= ?)
               ORDER BY message_id LIMIT 400""",
            (session_id, through_message_id, through_message_id),
        ).fetchall()
    branched = store.create(
        workspace=Path(source.workspace),
        model=source.model,
        title=(title or f"Branch of {source.title}")[:100],
        provider_kind=source.provider_kind,
        provider_base_url=source.provider_base_url,
        provider_api_key_env=source.provider_api_key_env,
        mcp_binding_digest=source.mcp_binding_digest,
        cost_efficiency_mode=source.cost_efficiency_mode,
    )
    for row in rows:
        calls = None
        if row["tool_calls_json"]:
            try:
                calls = json.loads(str(row["tool_calls_json"]))
            except (TypeError, ValueError):
                calls = None
        append_session_message(
            store,
            session_id=branched.session_id,
            role=str(row["role"]),
            content=row["content"],
            tool_name=row["tool_name"],
            tool_calls=calls,
            tool_call_id=row["tool_call_id"],
            finish_reason=row["finish_reason"],
            reasoning=row["reasoning"],
            now=now,
        )
    return branched


def rewind_session_messages(
    store: SessionTranscriptStore,
    session_id: str,
    through_message_id: int,
    *,
    now: Callable[[], str],
) -> int:
    """Remove transcript rows after a checkpoint; the turn ledger is immutable."""

    if through_message_id < 0:
        raise ValueError("Session rewind checkpoint must be non-negative")
    if store.resolve(session_id) is None:
        raise KeyError(f"Unknown company session: {session_id}")
    with store._lock:
        cursor = store._conn.execute(
            "DELETE FROM company_session_messages WHERE session_id = ? AND message_id > ?",
            (session_id, through_message_id),
        )
        store._conn.execute(
            "UPDATE company_sessions SET updated_at = ? WHERE session_id = ?",
            (now(), session_id),
        )
    return int(cursor.rowcount)
