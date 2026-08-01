"""Noruct-owned local lifecycle state for the explicit schedule daemon.

The child is only a process wrapper around ``noruct schedule daemon``.  The
ScheduleStore remains the authority for schedule claims and every dispatched
item still takes the ordinary Company Job path.  This record deliberately
adds neither boot persistence, automatic restart, nor learning/policy apply.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


_WINDOW_SECONDS = 60.0
_MAX_UNEXPECTED_STARTS = 3


@dataclass(frozen=True, slots=True)
class ScheduleServiceRecord:
    state: str
    pid: int | None
    run_id: str | None
    poll_seconds: float | None
    limit: int | None
    started_at: float | None
    log_path: Path | None
    unexpected_starts: int
    restart_blocked: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "pid": self.pid,
            "run_id": self.run_id,
            "poll_seconds": self.poll_seconds,
            "limit": self.limit,
            "started_at": self.started_at,
            "log_path": str(self.log_path) if self.log_path else None,
            "unexpected_starts": self.unexpected_starts,
            "restart_blocked": self.restart_blocked,
        }


def schedule_service_state_path(job_state_path: Path) -> Path:
    return job_state_path.expanduser().resolve().with_name(f"{job_state_path.stem}.schedule-service.sqlite3")


def _is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ScheduleServiceStore:
    """One persisted local child record with an unexpected-exit circuit breaker."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS schedule_service (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1), state TEXT NOT NULL,
            pid INTEGER, run_id TEXT, poll_seconds REAL, dispatch_limit INTEGER,
            started_at REAL, log_path TEXT, unexpected_starts_json TEXT NOT NULL)"""
        )
        self._conn.execute(
            """INSERT OR IGNORE INTO schedule_service
            (singleton, state, pid, run_id, poll_seconds, dispatch_limit, started_at, log_path, unexpected_starts_json)
            VALUES (1, 'stopped', NULL, NULL, NULL, NULL, NULL, NULL, '[]')"""
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ScheduleServiceStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def status(self, *, now: float | None = None) -> ScheduleServiceRecord:
        timestamp = time.time() if now is None else now
        row = self._conn.execute("SELECT * FROM schedule_service WHERE singleton = 1").fetchone()
        assert row is not None
        pid = int(row["pid"]) if row["pid"] is not None else None
        state = str(row["state"])
        starts = [float(item) for item in json.loads(str(row["unexpected_starts_json"])) if float(item) >= timestamp - _WINDOW_SECONDS]
        if state == "running" and not _is_alive(pid):
            starts.append(timestamp)
            state, pid = "stopped", None
            self._conn.execute(
                "UPDATE schedule_service SET state = ?, pid = NULL, unexpected_starts_json = ? WHERE singleton = 1",
                (state, json.dumps(starts)),
            )
            self._conn.commit()
        return self._record(row, state=state, pid=pid, unexpected_starts=len(starts))

    @staticmethod
    def _record(row: sqlite3.Row, *, state: str, pid: int | None, unexpected_starts: int) -> ScheduleServiceRecord:
        return ScheduleServiceRecord(
            state=state, pid=pid, run_id=str(row["run_id"]) if row["run_id"] else None,
            poll_seconds=float(row["poll_seconds"]) if row["poll_seconds"] is not None else None,
            limit=int(row["dispatch_limit"]) if row["dispatch_limit"] is not None else None,
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            log_path=Path(str(row["log_path"])) if row["log_path"] else None,
            unexpected_starts=unexpected_starts,
            restart_blocked=unexpected_starts >= _MAX_UNEXPECTED_STARTS,
        )

    def reserve_start(self, *, poll_seconds: float, limit: int, log_path: Path, now: float | None = None) -> ScheduleServiceRecord:
        current = self.status(now=now)
        if current.state == "running":
            raise ValueError("Schedule service is already running; stop it before starting another instance")
        if current.restart_blocked:
            raise ValueError("Schedule service restart circuit is open after three unexpected exits in 60 seconds; inspect the log, then run schedule service reset --confirm")
        run_id = f"schedule-{uuid.uuid4()}"
        timestamp = time.time() if now is None else now
        self._conn.execute(
            """UPDATE schedule_service SET state = 'starting', pid = NULL, run_id = ?, poll_seconds = ?, dispatch_limit = ?, started_at = ?, log_path = ?
            WHERE singleton = 1""",
            (run_id, poll_seconds, limit, timestamp, str(log_path)),
        )
        self._conn.commit()
        return ScheduleServiceRecord("starting", None, run_id, poll_seconds, limit, timestamp, log_path, current.unexpected_starts, False)

    def mark_started(self, *, run_id: str, pid: int) -> ScheduleServiceRecord:
        self._conn.execute("UPDATE schedule_service SET state = 'running', pid = ? WHERE singleton = 1 AND run_id = ?", (pid, run_id))
        self._conn.commit()
        row = self._conn.execute("SELECT * FROM schedule_service WHERE singleton = 1").fetchone()
        assert row is not None
        starts = len(json.loads(str(row["unexpected_starts_json"])))
        return self._record(row, state="running", pid=pid, unexpected_starts=starts)

    def stop(self) -> ScheduleServiceRecord:
        current = self.status()
        if current.pid is not None and _is_alive(current.pid):
            try:
                os.kill(current.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self._conn.execute("UPDATE schedule_service SET state = 'stopped', pid = NULL, unexpected_starts_json = '[]' WHERE singleton = 1")
        self._conn.commit()
        return self.status()

    def reset(self) -> ScheduleServiceRecord:
        current = self.status()
        if current.state == "running":
            raise ValueError("Stop the schedule service before resetting its restart circuit")
        self._conn.execute("UPDATE schedule_service SET unexpected_starts_json = '[]' WHERE singleton = 1")
        self._conn.commit()
        return self.status()
