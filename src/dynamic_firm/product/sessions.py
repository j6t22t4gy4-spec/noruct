from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dynamic_firm._vendor.session_shell.session_listing import (
    parse_session_listing_args as _parse_session_listing_args,
    query_session_listing as _query_session_listing,
)
from dynamic_firm.runtime.models import Usage, to_primitive
from dynamic_firm.providers.profiles import PROVIDER_KINDS
from .session_transcript import (
    CompanySessionSearchHit,
    append_session_message,
    branch_session_transcript,
    rewind_session_messages,
    search_session_messages,
    session_conversation,
)


_LOCAL_SESSION_SOURCE = "noruct-local"
_SESSION_PROVIDER_KINDS = frozenset(PROVIDER_KINDS)
_SHA256_HEX = frozenset("0123456789abcdef")
_COST_EFFICIENCY_MODES = frozenset({"standard", "economy"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validated_provider_binding(
    *,
    provider_kind: str,
    provider_base_url: str,
    provider_api_key_env: str | None,
) -> tuple[str, str, str | None]:
    """Validate secret-free transport identity persisted with a Company session.

    The binding deliberately stores neither credential values nor an external
    provider session/thread identity.  A completely empty tuple denotes a
    pre-binding legacy session.  Any partial tuple is rejected so a damaged DB
    row cannot silently route a resumed conversation to an arbitrary endpoint.
    """

    kind = provider_kind.strip().lower()
    base_url = provider_base_url.strip()
    api_key_env = provider_api_key_env.strip() if provider_api_key_env else None
    if not kind and not base_url and api_key_env is None:
        return "", "", None
    if kind not in _SESSION_PROVIDER_KINDS:
        raise ValueError("Company session has an unsupported provider binding")
    if api_key_env is not None and (
        len(api_key_env) > 128
        or not (api_key_env[0].isalpha() or api_key_env[0] == "_")
        or not all(character.isalnum() or character == "_" for character in api_key_env)
    ):
        raise ValueError("Company session has an invalid credential environment name")
    if kind == "openai_codex":
        if base_url or api_key_env is not None:
            raise ValueError("Company session has an invalid Codex provider binding")
        return kind, "", None
    if not base_url:
        raise ValueError("Company session provider binding requires a base URL")
    parsed = urlsplit(base_url)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Company session provider binding has an invalid base URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Company session provider binding has an unsafe base URL")
    return kind, base_url, api_key_env


def _validated_mcp_binding_digest(value: str) -> str:
    """Accept only an opaque, versioned-policy digest or a legacy empty value."""

    digest = value.strip().lower()
    if not digest:
        return ""
    if len(digest) != 64 or any(character not in _SHA256_HEX for character in digest):
        raise ValueError("Company session has an invalid MCP configuration binding")
    return digest


def _validated_cost_efficiency_mode(value: object) -> str:
    mode = str(value or "standard").strip().lower()
    if mode not in _COST_EFFICIENCY_MODES:
        raise ValueError("Company session has an invalid cost efficiency mode")
    return mode


@dataclass(frozen=True, slots=True)
class CompanySession:
    session_id: str
    title: str
    workspace: str
    model: str
    created_at: str
    updated_at: str
    provider_kind: str = ""
    provider_base_url: str = ""
    provider_api_key_env: str | None = None
    mcp_binding_digest: str = ""
    cost_efficiency_mode: str = "standard"
    turn_count: int = 0

    @property
    def has_provider_binding(self) -> bool:
        """Whether this session can restore its original provider transport.

        Empty bindings are intentionally reserved for rows created before the
        additive migration.  They remain locally resumable, but must use the
        current operator configuration rather than pretending a historical
        transport is known.
        """

        return bool(self.provider_kind)

    @property
    def has_mcp_binding(self) -> bool:
        """Whether this row records an exact external-read policy identity."""

        return bool(self.mcp_binding_digest)


@dataclass(frozen=True, slots=True)
class CompanyTurn:
    turn_id: str
    session_id: str
    position: int
    goal: str
    job_id: str
    status: str
    summary: str
    usage: Usage
    created_at: str


@dataclass(frozen=True, slots=True)
class CompanySessionListItem:
    """First-party session projection selected by the private browse policy."""

    session_id: str
    title: str
    preview: str
    workspace: str
    model: str
    turn_count: int


@dataclass(frozen=True, slots=True)
class CompanySessionBrowse:
    """Parsed local session browse or resume intent without state mutation."""

    target: str
    search_query: str | None
    include_unnamed: bool
    items: tuple[CompanySessionListItem, ...]


@dataclass(frozen=True, slots=True)
class CompanySessionRecallTurn:
    """A deliberately small local-memory projection for an employee tool call.

    This is not a conversation transcript.  It contains only the user goal and
    terminal Company summary which the product already stores as durable local
    session metadata.  Tool output, provider wire messages, credentials and
    external session identities never enter this projection.
    """

    session_id: str
    position: int
    status: str
    goal: str
    summary: str


@dataclass(frozen=True, slots=True)
class CompanySessionMessage:
    """Bounded transcript projection used by advanced session controls."""

    message_id: int
    session_id: str
    role: str
    content: str
    created_at: str


class CompanySessionStore:
    """Small company-conversation ledger kept separate from employee run state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS company_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    provider_kind TEXT NOT NULL DEFAULT '',
                    provider_base_url TEXT NOT NULL DEFAULT '',
                    provider_api_key_env TEXT,
                    mcp_binding_digest TEXT NOT NULL DEFAULT '',
                    cost_efficiency_mode TEXT NOT NULL DEFAULT 'standard'
                );

                CREATE TABLE IF NOT EXISTS company_turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES company_sessions(session_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    goal TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, position)
                );

                CREATE INDEX IF NOT EXISTS company_turns_session_position_idx
                    ON company_turns(session_id, position);

                CREATE TABLE IF NOT EXISTS company_session_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES company_sessions(session_id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_name TEXT,
                    tool_calls_json TEXT,
                    tool_call_id TEXT,
                    finish_reason TEXT,
                    reasoning TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS company_session_messages_idx
                    ON company_session_messages(session_id, message_id);
                """
            )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(company_sessions)").fetchall()
            }
            migrations = (
                ("provider_kind", "TEXT NOT NULL DEFAULT ''"),
                ("provider_base_url", "TEXT NOT NULL DEFAULT ''"),
                ("provider_api_key_env", "TEXT"),
                ("mcp_binding_digest", "TEXT NOT NULL DEFAULT ''"),
                ("cost_efficiency_mode", "TEXT NOT NULL DEFAULT 'standard'"),
            )
            for name, declaration in migrations:
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE company_sessions ADD COLUMN {name} {declaration}"
                    )

    def create(
        self,
        *,
        workspace: Path,
        model: str,
        title: str = "New session",
        provider_kind: str = "",
        provider_base_url: str = "",
        provider_api_key_env: str | None = None,
        mcp_binding_digest: str = "",
        cost_efficiency_mode: str = "standard",
    ) -> CompanySession:
        binding = _validated_provider_binding(
            provider_kind=provider_kind,
            provider_base_url=provider_base_url,
            provider_api_key_env=provider_api_key_env,
        )
        mcp_digest = _validated_mcp_binding_digest(mcp_binding_digest)
        cost_mode = _validated_cost_efficiency_mode(cost_efficiency_mode)
        session_id = str(uuid.uuid4())
        now = _now()
        resolved = str(workspace.expanduser().resolve())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO company_sessions (
                    session_id, title, workspace, model, created_at, updated_at,
                    provider_kind, provider_base_url, provider_api_key_env, mcp_binding_digest,
                    cost_efficiency_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    title,
                    resolved,
                    model,
                    now,
                    now,
                    binding[0],
                    binding[1],
                    binding[2],
                    mcp_digest,
                    cost_mode,
                ),
            )
        return CompanySession(
            session_id,
            title,
            resolved,
            model,
            now,
            now,
            binding[0],
            binding[1],
            binding[2],
            mcp_digest,
            cost_mode,
            0,
        )

    def create_with_id(self, session_id: str, *, workspace: Path, model: str,
                       title: str = "New session") -> CompanySession:
        """Create a Company session with an externally supplied local id.

        The fork CLI already allocates a session id before constructing its
        agent.  Keeping that id avoids a second identity namespace while the
        Company store remains the sole persistence authority.
        """
        if not session_id or len(session_id) > 128:
            raise ValueError("Company session id is invalid")
        now = _now()
        resolved = str(workspace.expanduser().resolve())
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO company_sessions
                   (session_id, title, workspace, model, created_at, updated_at,
                    provider_kind, provider_base_url, provider_api_key_env,
                    mcp_binding_digest, cost_efficiency_mode)
                   VALUES (?, ?, ?, ?, ?, ?, '', '', NULL, '', 'standard')""",
                (session_id, title, resolved, model, now, now),
            )
        session = self.resolve(session_id)
        if session is None:
            raise RuntimeError("Company session could not be created")
        return session

    @staticmethod
    def _row(row: sqlite3.Row) -> CompanySession:
        binding = _validated_provider_binding(
            provider_kind=str(row["provider_kind"] or ""),
            provider_base_url=str(row["provider_base_url"] or ""),
            provider_api_key_env=(
                str(row["provider_api_key_env"])
                if row["provider_api_key_env"] is not None
                else None
            ),
        )
        mcp_digest = _validated_mcp_binding_digest(str(row["mcp_binding_digest"] or ""))
        cost_mode = _validated_cost_efficiency_mode(row["cost_efficiency_mode"])
        return CompanySession(
            session_id=str(row["session_id"]),
            title=str(row["title"]),
            workspace=str(row["workspace"]),
            model=str(row["model"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            provider_kind=binding[0],
            provider_base_url=binding[1],
            provider_api_key_env=binding[2],
            mcp_binding_digest=mcp_digest,
            cost_efficiency_mode=cost_mode,
            turn_count=int(row["turn_count"]),
        )

    def list(self, limit: int = 20) -> tuple[CompanySession, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("Session list limit must be between 1 and 200")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.*, COUNT(t.turn_id) AS turn_count
                FROM company_sessions s
                LEFT JOIN company_turns t ON t.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def list_listing_rows(
        self,
        *,
        source: str | None,
        exclude_sources: list[str] | None,
        limit: int,
        search_query: str | None,
        order_by_last_active: bool,
    ) -> list[dict[str, Any]]:
        """Project company-owned rows for the private upstream selection policy.

        The private policy only receives dictionaries. SQLite, title semantics,
        query bounds and every returned field remain Noruct-owned.  There is one
        local source today; accepting ``None`` preserves the policy contract for
        a future explicitly approved shared-session source without adding one.
        """

        if limit < 1 or limit > 200:
            raise ValueError("Session listing limit must be between 1 and 200")
        if source not in (None, _LOCAL_SESSION_SOURCE):
            return []
        if exclude_sources and _LOCAL_SESSION_SOURCE in exclude_sources:
            return []
        search = (search_query or "").strip().lower()
        where = ""
        parameters: list[object] = []
        if search:
            if len(search.encode("utf-8")) > 160:
                raise ValueError("Session search query exceeds the 160-byte local bound")
            where = (
                "WHERE instr(lower(session_id), ?) > 0 "
                "OR instr(lower(title), ?) > 0 "
                "OR EXISTS ("
                "SELECT 1 FROM company_turns searched "
                "WHERE searched.session_id = session_rows.session_id "
                "AND (instr(lower(searched.goal), ?) > 0 "
                "OR instr(lower(searched.summary), ?) > 0)"
                ")"
            )
            parameters.extend((search, search, search, search))
        parameters.append(limit)
        # Both normal browsing and search are newest-first in the local ledger.
        # The named flag is retained to keep the upstream policy adapter narrow.
        del order_by_last_active
        with self._lock:
            rows = self._conn.execute(
                f"""
                WITH session_rows AS (
                    SELECT
                        s.session_id,
                        s.title,
                        s.workspace,
                        s.model,
                        s.updated_at,
                        COUNT(t.turn_id) AS turn_count,
                        (
                            SELECT latest.summary
                            FROM company_turns latest
                            WHERE latest.session_id = s.session_id
                            ORDER BY latest.position DESC
                            LIMIT 1
                        ) AS preview
                    FROM company_sessions s
                    LEFT JOIN company_turns t ON t.session_id = s.session_id
                    GROUP BY s.session_id
                )
                SELECT * FROM session_rows
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            raw_title = str(row["title"])
            title = "" if raw_title == "New session" and int(row["turn_count"]) == 0 else raw_title
            result.append(
                {
                    "id": str(row["session_id"]),
                    "title": title,
                    "preview": str(row["preview"] or ""),
                    "source": _LOCAL_SESSION_SOURCE,
                    "workspace": str(row["workspace"]),
                    "model": str(row["model"]),
                    "turn_count": int(row["turn_count"]),
                }
            )
        return result

    def recall_listing_rows(
        self,
        *,
        search_query: str | None,
        exclude_session_id: str | None,
        limit: int = 8,
    ) -> tuple[CompanySessionListItem, ...]:
        """Return a bounded local Company-memory discovery projection.

        The source session-search design separates discovery from reading a
        selected record.  Keep that useful shape, while making this a local
        Company-ledger operation: no FTS extension, profile scan, transcript,
        remote source or provider call is involved.
        """

        if limit < 1 or limit > 8:
            raise ValueError("Company session recall limit must be between 1 and 8")
        # Read one extra row when a current session is supplied, so exclusion
        # does not unnecessarily make a short browse appear empty.
        rows = self.list_listing_rows(
            source=_LOCAL_SESSION_SOURCE,
            exclude_sources=None,
            limit=min(9, limit + (1 if exclude_session_id else 0)),
            search_query=search_query,
            order_by_last_active=True,
        )
        result: list[CompanySessionListItem] = []
        for row in rows:
            session_id = str(row["id"])
            if exclude_session_id and session_id == exclude_session_id:
                continue
            result.append(
                CompanySessionListItem(
                    session_id=session_id,
                    title=str(row.get("title") or "New session"),
                    preview=str(row.get("preview") or ""),
                    workspace=str(row.get("workspace") or ""),
                    model=str(row.get("model") or ""),
                    turn_count=int(row.get("turn_count") or 0),
                )
            )
            if len(result) >= limit:
                break
        return tuple(result)

    def recall_turns(
        self,
        *,
        session_id: str,
        after_position: int = 0,
        limit: int = 4,
        max_bytes: int = 8_000,
    ) -> tuple[CompanySessionRecallTurn, ...]:
        """Read a bounded, forward-scrollable local summary window.

        ``session_id`` must be the exact opaque identifier emitted by the
        discovery projection.  Prefix/title lookup is intentionally not
        accepted here: model-selected recall must not turn an ambiguous name
        into another session's data.
        """

        if not session_id or len(session_id.encode("utf-8")) > 128:
            raise ValueError("Company session recall requires a bounded session_id")
        if not isinstance(after_position, int) or isinstance(after_position, bool) or after_position < 0:
            raise ValueError("Company session recall after_position must be a non-negative integer")
        if limit < 1 or limit > 8:
            raise ValueError("Company session recall limit must be between 1 and 8")
        if max_bytes < 512 or max_bytes > 16_000:
            raise ValueError("Company session recall byte bound is outside the allowed range")
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM company_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if exists is None:
                raise KeyError("Unknown local Company session")
            rows = self._conn.execute(
                """
                SELECT position, status, goal, summary
                FROM company_turns
                WHERE session_id = ? AND position > ?
                ORDER BY position ASC
                LIMIT ?
                """,
                (session_id, after_position, limit),
            ).fetchall()
        remaining = max_bytes
        result: list[CompanySessionRecallTurn] = []
        for row in rows:
            goal = str(row["goal"])
            summary = str(row["summary"])
            # A single pathological historical row should never make the
            # current employee context unbounded.  Omit it rather than return
            # a silently altered goal/summary.
            encoded_size = len(goal.encode("utf-8")) + len(summary.encode("utf-8"))
            if encoded_size > remaining:
                break
            result.append(
                CompanySessionRecallTurn(
                    session_id=session_id,
                    position=int(row["position"]),
                    status=str(row["status"]),
                    goal=goal,
                    summary=summary,
                )
            )
            remaining -= encoded_size
        return tuple(result)

    def resolve(self, reference: str | None = None) -> CompanySession | None:
        with self._lock:
            if not reference:
                row = self._conn.execute(
                    """
                    SELECT s.*, COUNT(t.turn_id) AS turn_count
                    FROM company_sessions s
                    LEFT JOIN company_turns t ON t.session_id = s.session_id
                    GROUP BY s.session_id
                    ORDER BY s.updated_at DESC LIMIT 1
                    """
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT s.*, COUNT(t.turn_id) AS turn_count
                    FROM company_sessions s
                    LEFT JOIN company_turns t ON t.session_id = s.session_id
                    WHERE s.session_id = ? OR s.session_id LIKE ? OR s.title = ?
                    GROUP BY s.session_id
                    ORDER BY CASE WHEN s.session_id = ? THEN 0 ELSE 1 END, s.updated_at DESC
                    LIMIT 1
                    """,
                    (reference, f"{reference}%", reference, reference),
                ).fetchone()
        return self._row(row) if row else None

    def update_model(self, session_id: str, model: str) -> CompanySession:
        selected = model.strip()
        if not selected:
            raise ValueError("Session model must be non-empty")
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE company_sessions SET model = ?, updated_at = ? WHERE session_id = ?",
                (selected, now, session_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown company session: {session_id}")
        updated = self.resolve(session_id)
        if updated is None:  # pragma: no cover - guarded by the update row count
            raise KeyError(f"Unknown company session: {session_id}")
        return updated

    def update_cost_efficiency_mode(self, session_id: str, mode: str) -> CompanySession:
        selected = _validated_cost_efficiency_mode(mode)
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE company_sessions SET cost_efficiency_mode = ?, updated_at = ? WHERE session_id = ?",
                (selected, now, session_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown company session: {session_id}")
        updated = self.resolve(session_id)
        if updated is None:  # pragma: no cover - guarded by the update row count
            raise KeyError(f"Unknown company session: {session_id}")
        return updated

    def append_turn(
        self,
        *,
        session_id: str,
        goal: str,
        job_id: str,
        status: str,
        summary: str,
        usage: Usage,
    ) -> CompanyTurn:
        now = _now()
        turn_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                session = self._conn.execute(
                    "SELECT title FROM company_sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if not session:
                    raise KeyError(f"Unknown company session: {session_id}")
                position = int(
                    self._conn.execute(
                        "SELECT COALESCE(MAX(position), 0) + 1 FROM company_turns "
                        "WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                )
                self._conn.execute(
                    "INSERT INTO company_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        turn_id,
                        session_id,
                        position,
                        goal,
                        job_id,
                        status,
                        summary,
                        json.dumps(to_primitive(usage), ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                title = str(session["title"])
                if title == "New session":
                    title = " ".join(goal.split())[:80] or title
                self._conn.execute(
                    "UPDATE company_sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                    (title, now, session_id),
                )
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
        return CompanyTurn(turn_id, session_id, position, goal, job_id, status, summary, usage, now)

    def recent_context(
        self,
        session_id: str,
        *,
        max_turns: int = 6,
        max_bytes: int = 12_000,
    ) -> tuple[str, ...]:
        if max_turns < 1 or max_bytes < 1:
            raise ValueError("Session context bounds must be positive")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT position, goal, status, summary
                FROM company_turns
                WHERE session_id = ? AND status = 'SUCCEEDED'
                ORDER BY position DESC
                LIMIT ?
                """,
                (session_id, max_turns),
            ).fetchall()
        remaining = max_bytes
        selected: list[str] = []
        for row in reversed(rows):
            value = (
                f"Prior company turn {row['position']} [{row['status']}]: "
                f"goal={row['goal']}\nresult={row['summary']}"
            )
            encoded = value.encode("utf-8")
            if len(encoded) > remaining:
                if remaining < 64:
                    break
                value = encoded[:remaining].decode("utf-8", errors="ignore")
                selected.append(value)
                break
            selected.append(value)
            remaining -= len(encoded)
        return tuple(selected)

    def input_history(
        self,
        session_id: str,
        *,
        limit: int = 100,
        max_goal_bytes: int = 8_000,
    ) -> tuple[str, ...]:
        """Return bounded, complete prior goals for one local composer only.

        This is deliberately separate from ``recent_context``.  The latter is
        provider context and contains summaries; input history contains only
        the user's own complete goal text, is never cross-session, and is
        never sent anywhere merely because a terminal editor preloads it.
        Oversized entries are omitted rather than truncated so recalling one
        cannot silently turn it into a different goal.
        """

        if limit < 1 or limit > 200:
            raise ValueError("Input history limit must be between 1 and 200")
        if max_goal_bytes < 1 or max_goal_bytes > 32_000:
            raise ValueError("Input history goal-byte bound must be between 1 and 32000")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT goal
                FROM company_turns
                WHERE session_id = ?
                ORDER BY position DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        newest_first: list[str] = []
        seen: set[str] = set()
        for row in rows:
            goal = str(row["goal"]).strip()
            if (
                not goal
                or len(goal.encode("utf-8")) > max_goal_bytes
                or goal in seen
            ):
                continue
            seen.add(goal)
            newest_first.append(goal)
        return tuple(reversed(newest_first))

    def usage(self, session_id: str) -> Usage:
        with self._lock:
            rows = self._conn.execute(
                "SELECT usage_json FROM company_turns WHERE session_id = ? ORDER BY position",
                (session_id,),
            ).fetchall()
        total = Usage()
        for row in rows:
            try:
                value = json.loads(str(row["usage_json"]))
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            total = total.plus(
                Usage(
                    model_calls=int(value.get("model_calls", 0)),
                    tool_calls=int(value.get("tool_calls", 0)),
                    input_tokens=int(value.get("input_tokens", 0)),
                    cached_input_tokens=int(value.get("cached_input_tokens", 0)),
                    output_tokens=int(value.get("output_tokens", 0)),
                    cost_usd=float(value.get("cost_usd", 0.0)),
                )
            )
        return total

    def append_message(self, *, session_id: str, role: str, content: object = None,
                       tool_name: str | None = None, tool_calls: object = None,
                       tool_call_id: str | None = None, finish_reason: str | None = None,
                       reasoning: str | None = None, **_: object) -> None:
        append_session_message(
            self, session_id=session_id, role=role, content=content,
            tool_name=tool_name, tool_calls=tool_calls, tool_call_id=tool_call_id,
            finish_reason=finish_reason, reasoning=reasoning, now=_now,
        )

    def conversation(self, session_id: str) -> list[dict[str, object]]:
        return session_conversation(self, session_id)

    def search_messages(
        self, query: str, *, session_id: str | None = None, limit: int = 20,
        max_snippet_bytes: int = 320,
    ) -> tuple[CompanySessionSearchHit, ...]:
        return search_session_messages(
            self, query, session_id=session_id, limit=limit,
            max_snippet_bytes=max_snippet_bytes,
        )

    def branch(self, session_id: str, *, title: str | None = None,
               through_message_id: int | None = None) -> CompanySession:
        return branch_session_transcript(
            self, session_id, title=title, through_message_id=through_message_id,
            now=_now,
        )

    def rewind_messages(self, session_id: str, through_message_id: int) -> int:
        return rewind_session_messages(
            self, session_id, through_message_id, now=_now,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def browse_company_sessions(
    store: CompanySessionStore,
    raw_args: str,
    *,
    current_session_id: str | None,
    limit: int = 10,
) -> CompanySessionBrowse:
    """Use the vendored session policy through a first-party local projection.

    No remote/session-network path exists here. ``all`` is deliberately a
    harmless compatibility flag until a separately consented shared source is
    introduced; it never changes state or enables a transport.
    """

    include_all, include_unnamed, target, search_query = _parse_session_listing_args(raw_args)
    if target:
        return CompanySessionBrowse(target, search_query, include_unnamed, ())
    adapter = _CompanySessionListingAdapter(store)
    rows = _query_session_listing(
        adapter,
        source=_LOCAL_SESSION_SOURCE,
        current_session_id=current_session_id,
        include_all_sources=include_all,
        include_unnamed=include_unnamed,
        search_query=search_query,
        limit=limit,
    )
    items = tuple(
        CompanySessionListItem(
            session_id=str(row["id"]),
            title=str(row.get("title") or "New session"),
            preview=str(row.get("preview") or ""),
            workspace=str(row.get("workspace") or ""),
            model=str(row.get("model") or ""),
            turn_count=int(row.get("turn_count") or 0),
        )
        for row in rows
    )
    return CompanySessionBrowse(target, search_query, include_unnamed, items)


class _CompanySessionListingAdapter:
    """Private compatibility seam; never exposes a foreign session authority."""

    def __init__(self, store: CompanySessionStore) -> None:
        self._store = store

    def list_sessions_rich(
        self,
        *,
        source: str | None,
        exclude_sources: list[str] | None,
        limit: int,
        search_query: str | None,
        order_by_last_active: bool,
    ) -> list[dict[str, Any]]:
        return self._store.list_listing_rows(
            source=source,
            exclude_sources=exclude_sources,
            limit=limit,
            search_query=search_query,
            order_by_last_active=order_by_last_active,
        )
