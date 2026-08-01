"""Persistent Employee-session state composed into the canonical Run Store.

All methods use the owning Store's SQLite connection and transaction boundary.
This module isolates compare-and-swap conversation state and its run-scoped
lease; it does not create an independent memory database or scheduler.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .models import RunStatus, to_primitive, utc_now
from .redaction import redact_runtime_value


EMPLOYEE_SESSION_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class EmployeeSessionSnapshot:
    namespace_hash: str
    employee_id: str
    revision: int
    messages: tuple[dict[str, Any], ...]
    message_count: int
    byte_length: int
    last_run_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EmployeeSessionUpdate:
    namespace_hash: str
    employee_id: str
    expected_revision: int
    messages: Sequence[Mapping[str, Any]]
    max_messages: int
    max_chars: int


class EmployeeSessionConflict(RuntimeError):
    """A second runtime tried to replace a newer employee-session revision."""


def _session_value(value: Any) -> Any:
    primitive = to_primitive(value)
    if isinstance(primitive, dict):
        return {
            str(key): _session_value(item)
            for key, item in primitive.items()
            if not str(key).startswith("_")
        }
    if isinstance(primitive, (list, tuple)):
        return [_session_value(item) for item in primitive]
    return primitive


def _bounded_session_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_messages: int,
    max_chars: int,
) -> tuple[tuple[dict[str, Any], ...], int]:
    if max_messages <= 0 or max_chars <= 0:
        raise ValueError("employee session bounds must be positive")
    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise ValueError("employee session messages must be objects")
        safe = redact_runtime_value(_session_value(message))
        if not isinstance(safe, dict):
            raise ValueError("employee session message projection failed")
        role = safe.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError("employee session messages require a role")
        projected.append(safe)
    projected = projected[-max_messages:]
    if projected and projected[0].get("role") != "user":
        first_user = next(
            (index for index, item in enumerate(projected) if item.get("role") == "user"),
            None,
        )
        projected = projected[first_user:] if first_user is not None else []
    while projected:
        encoded = self_json(projected)
        if len(encoded) <= max_chars:
            return tuple(projected), len(encoded.encode("utf-8"))
        next_user = next(
            (index for index, item in enumerate(projected[1:], start=1) if item.get("role") == "user"),
            None,
        )
        projected = projected[next_user:] if next_user is not None else []
    return (), len(self_json([]).encode("utf-8"))


def self_json(value: Any) -> str:
    """Canonical JSON supplied locally to avoid import cycle with ``store``."""

    import json

    return json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RunStoreEmployeeSessionMixin:
    """Bounded Employee-session read/write and live-run lease operations."""

    def load_employee_session(self, namespace_hash: str, employee_id: str) -> EmployeeSessionSnapshot | None:
        self._validate_employee_session_identity(namespace_hash, employee_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM employee_session_state WHERE namespace_hash = ? AND employee_id = ?",
                (namespace_hash, employee_id),
            ).fetchone()
        if row is None:
            return None
        if int(row["format_version"]) != EMPLOYEE_SESSION_FORMAT_VERSION:
            raise RuntimeError(f"Unsupported employee session format: {row['format_version']}")
        messages = self._loads_employee_session_history(row["history_json"])
        return EmployeeSessionSnapshot(
            namespace_hash=str(row["namespace_hash"]), employee_id=str(row["employee_id"]),
            revision=int(row["revision"]), messages=messages, message_count=int(row["message_count"]),
            byte_length=int(row["byte_length"]), last_run_id=str(row["last_run_id"]),
            created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _loads_employee_session_history(value: str) -> tuple[dict[str, Any], ...]:
        import json
        messages = json.loads(value) if value else []
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise RuntimeError("Employee session history is corrupt")
        return tuple(dict(item) for item in messages)

    @staticmethod
    def _validate_employee_session_identity(namespace_hash: str, employee_id: str) -> None:
        if (len(namespace_hash) != 64 or namespace_hash.lower() != namespace_hash or any(char not in "0123456789abcdef" for char in namespace_hash)):
            raise ValueError("employee session namespace must be a lowercase SHA-256 digest")
        if not employee_id.strip():
            raise ValueError("employee session employee_id must be non-empty")

    def _write_employee_session(self, conn: sqlite3.Connection, *, update: EmployeeSessionUpdate, run: Mapping[str, Any], updated_at: datetime) -> None:
        self._validate_employee_session_identity(update.namespace_hash, update.employee_id)
        if update.expected_revision < 0:
            raise ValueError("employee session expected_revision must be non-negative")
        if str(run["employee_id"]) != update.employee_id:
            raise ValueError("employee session update does not belong to the run employee")
        messages, byte_length = _bounded_session_messages(update.messages, max_messages=update.max_messages, max_chars=update.max_chars)
        history_json = self_json(messages)
        existing = conn.execute("SELECT * FROM employee_session_state WHERE namespace_hash = ?", (update.namespace_hash,)).fetchone()
        actual_revision = int(existing["revision"]) if existing else 0
        if actual_revision != update.expected_revision:
            raise EmployeeSessionConflict("employee session changed after this run loaded its history")
        if existing and str(existing["employee_id"]) != update.employee_id:
            raise EmployeeSessionConflict("employee session namespace ownership changed")
        next_revision = actual_revision + 1
        if existing:
            conn.execute("UPDATE employee_session_state SET format_version = ?, revision = ?, history_json = ?, message_count = ?, byte_length = ?, last_run_id = ?, updated_at = ? WHERE namespace_hash = ? AND revision = ?", (EMPLOYEE_SESSION_FORMAT_VERSION, next_revision, history_json, len(messages), byte_length, run["run_id"], updated_at.isoformat(), update.namespace_hash, actual_revision))
        else:
            conn.execute("INSERT INTO employee_session_state(namespace_hash, employee_id, format_version, revision, history_json, message_count, byte_length, last_run_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (update.namespace_hash, update.employee_id, EMPLOYEE_SESSION_FORMAT_VERSION, next_revision, history_json, len(messages), byte_length, run["run_id"], updated_at.isoformat(), updated_at.isoformat()))

    def acquire_employee_session_lease(self, *, namespace_hash: str, employee_id: str, run_id: str) -> bool:
        self._validate_employee_session_identity(namespace_hash, employee_id)
        if not run_id.strip():
            raise ValueError("employee session lease requires a run id")
        with self._transaction() as conn:
            run = conn.execute("SELECT employee_id, status FROM employee_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None: raise KeyError(f"Unknown run: {run_id}")
            if str(run["employee_id"]) != employee_id: raise ValueError("employee session lease does not belong to the run employee")
            if RunStatus(str(run["status"])).terminal: return False
            existing = conn.execute("SELECT * FROM employee_session_leases WHERE namespace_hash = ?", (namespace_hash,)).fetchone()
            if existing is not None:
                if str(existing["owner_run_id"]) == run_id: return True
                owner = conn.execute("SELECT status FROM employee_runs WHERE run_id = ?", (str(existing["owner_run_id"]),)).fetchone()
                if owner is not None and not RunStatus(str(owner["status"])).terminal: return False
                conn.execute("DELETE FROM employee_session_leases WHERE namespace_hash = ?", (namespace_hash,))
            conn.execute("INSERT INTO employee_session_leases(namespace_hash, employee_id, owner_run_id, acquired_at) VALUES (?, ?, ?, ?)", (namespace_hash, employee_id, run_id, utc_now().isoformat()))
            return True

    def release_employee_session_lease(self, *, namespace_hash: str, employee_id: str, run_id: str) -> bool:
        self._validate_employee_session_identity(namespace_hash, employee_id)
        with self._transaction() as conn:
            deleted = conn.execute("DELETE FROM employee_session_leases WHERE namespace_hash = ? AND employee_id = ? AND owner_run_id = ?", (namespace_hash, employee_id, run_id))
        return deleted.rowcount == 1
