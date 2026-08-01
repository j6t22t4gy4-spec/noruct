"""Private Hermes session facade backed by Noruct's Company ledger.

The upstream CLI expects a SessionDB-shaped object.  This facade deliberately
implements only that narrow surface and keeps all durable state in
``CompanySessionStore``; the upstream SQLite schema is never opened by the
fork path.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dynamic_firm.product.sessions import CompanySessionStore


def _store() -> CompanySessionStore:
    path = os.environ.get("NORUCT_STATE_PATH")
    if path:
        return CompanySessionStore(path)
    return CompanySessionStore(Path.home() / ".noruct" / "state.db")


class CompanySessionDB:
    """SessionDB-compatible projection for the fork CLI."""

    def __init__(self) -> None:
        self._store = _store()
        self._conn = self  # compatibility for the upstream reopen call

    def create_session(self, session_id: str, source: str, **kwargs: Any) -> str:
        self._store.create_with_id(
            session_id,
            workspace=Path(kwargs.get("cwd") or Path.cwd()),
            model=str(kwargs.get("model") or "unknown"),
        )
        return session_id

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        session = self._store.resolve(session_id)
        if session is None:
            return None
        return {
            "id": session.session_id,
            "title": session.title,
            "cwd": session.workspace,
            "model": session.model,
            "source": "noruct-company",
        }

    def resolve_resume_session_id(self, session_id: str) -> str:
        return session_id if self._store.resolve(session_id) else ""

    def get_messages_as_conversation(self, session_id: str) -> list[dict[str, Any]]:
        return self._store.conversation(session_id)

    def append_message(self, **kwargs: Any) -> None:
        self._store.append_message(**kwargs)

    def set_session_title(self, session_id: str, title: str) -> None:
        # Titles are presentation metadata; the Company store owns their
        # lifecycle.  Keep this compatibility operation intentionally bounded.
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE company_sessions SET title = ?, updated_at = datetime('now') WHERE session_id = ?",
                (str(title)[:100], session_id),
            )

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def commit(self) -> None:
        return None

    def close(self) -> None:
        self._store.close()


def open_company_session_db() -> CompanySessionDB:
    return CompanySessionDB()
