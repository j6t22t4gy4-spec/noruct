"""Noruct-owned local lifecycle state for the foreground gateway runner.

The service wrapper intentionally starts the existing ``noruct gateway run``
command in a child process.  It does not import Hermes' gateway/profile/session
state, invent a platform router, or grant a receiver additional authority.
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
from typing import Sequence


_WINDOW_SECONDS = 60.0
_MAX_UNEXPECTED_STARTS = 3


@dataclass(frozen=True, slots=True)
class GatewayServiceRecord:
    state: str
    pid: int | None
    run_id: str | None
    receivers: tuple[str, ...]
    started_at: float | None
    log_path: Path | None
    receiver_config_digest: str | None
    unexpected_starts: int
    restart_blocked: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "pid": self.pid,
            "run_id": self.run_id,
            "receivers": list(self.receivers),
            "started_at": self.started_at,
            "log_path": str(self.log_path) if self.log_path else None,
            "receiver_config_digest": self.receiver_config_digest,
            "unexpected_starts": self.unexpected_starts,
            "restart_blocked": self.restart_blocked,
        }


def gateway_service_state_path(job_state_path: Path) -> Path:
    return job_state_path.expanduser().resolve().with_name(f"{job_state_path.stem}.gateway-service.sqlite3")


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


class GatewayServiceStore:
    """Small SQLite control record with a persisted restart-loop circuit breaker."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gateway_service (
              singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
              state TEXT NOT NULL,
              pid INTEGER,
              run_id TEXT,
              receivers_json TEXT NOT NULL,
              started_at REAL,
              log_path TEXT,
              receiver_config_digest TEXT,
              unexpected_starts_json TEXT NOT NULL
            )
            """
        )
        # Existing local service records predate configuration attestation.
        # Keep the migration deliberately additive so a status inspection never
        # requires a service reset or loses its restart-circuit history.
        columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(gateway_service)")}
        if "receiver_config_digest" not in columns:
            self._conn.execute("ALTER TABLE gateway_service ADD COLUMN receiver_config_digest TEXT")
        self._conn.execute(
            """INSERT OR IGNORE INTO gateway_service
               (singleton, state, pid, run_id, receivers_json, started_at, log_path, receiver_config_digest, unexpected_starts_json)
               VALUES (1, 'stopped', NULL, NULL, '[]', NULL, NULL, NULL, '[]')"""
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "GatewayServiceStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def status(self, *, now: float | None = None) -> GatewayServiceRecord:
        timestamp = time.time() if now is None else now
        row = self._conn.execute("SELECT * FROM gateway_service WHERE singleton = 1").fetchone()
        assert row is not None
        pid = int(row["pid"]) if row["pid"] is not None else None
        state = str(row["state"])
        starts = [float(item) for item in json.loads(str(row["unexpected_starts_json"])) if float(item) >= timestamp - _WINDOW_SECONDS]
        if state == "running" and not _is_alive(pid):
            starts.append(timestamp)
            state = "stopped"
            self._conn.execute(
                "UPDATE gateway_service SET state = ?, pid = NULL, unexpected_starts_json = ? WHERE singleton = 1",
                (state, json.dumps(starts),),
            )
            self._conn.commit()
            pid = None
        receivers = tuple(str(item) for item in json.loads(str(row["receivers_json"])))
        return GatewayServiceRecord(
            state=state,
            pid=pid,
            run_id=str(row["run_id"]) if row["run_id"] else None,
            receivers=receivers,
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            log_path=Path(str(row["log_path"])) if row["log_path"] else None,
            receiver_config_digest=str(row["receiver_config_digest"]) if row["receiver_config_digest"] else None,
            unexpected_starts=len(starts),
            restart_blocked=len(starts) >= _MAX_UNEXPECTED_STARTS,
        )

    def reserve_start(
        self,
        *,
        receivers: Sequence[str],
        log_path: Path,
        receiver_config_digest: str | None = None,
        now: float | None = None,
    ) -> GatewayServiceRecord:
        current = self.status(now=now)
        if current.state == "running":
            raise ValueError("Gateway service is already running; stop it before starting another instance")
        if current.restart_blocked:
            raise ValueError("Gateway service restart circuit is open after three unexpected exits in 60 seconds; inspect the log, then run gateway service reset --confirm")
        run_id = f"gateway-{uuid.uuid4()}"
        timestamp = time.time() if now is None else now
        if receiver_config_digest is not None and (len(receiver_config_digest) != 64 or any(character not in "0123456789abcdef" for character in receiver_config_digest)):
            raise ValueError("Gateway receiver configuration digest must be a lowercase SHA-256 hex digest")
        self._conn.execute(
            """UPDATE gateway_service SET state = 'starting', pid = NULL, run_id = ?, receivers_json = ?, started_at = ?, log_path = ?, receiver_config_digest = ?
               WHERE singleton = 1""",
            (run_id, json.dumps(list(receivers)), timestamp, str(log_path), receiver_config_digest),
        )
        self._conn.commit()
        return GatewayServiceRecord("starting", None, run_id, tuple(receivers), timestamp, log_path, receiver_config_digest, current.unexpected_starts, False)

    def mark_started(self, *, run_id: str, pid: int) -> GatewayServiceRecord:
        self._conn.execute("UPDATE gateway_service SET state = 'running', pid = ? WHERE singleton = 1 AND run_id = ?", (pid, run_id))
        self._conn.commit()
        row = self._conn.execute("SELECT * FROM gateway_service WHERE singleton = 1").fetchone()
        assert row is not None
        starts = [float(item) for item in json.loads(str(row["unexpected_starts_json"]))]
        return GatewayServiceRecord(
            state="running",
            pid=pid,
            run_id=run_id,
            receivers=tuple(str(item) for item in json.loads(str(row["receivers_json"]))),
            started_at=float(row["started_at"]),
            log_path=Path(str(row["log_path"])),
            receiver_config_digest=str(row["receiver_config_digest"]) if row["receiver_config_digest"] else None,
            unexpected_starts=len(starts),
            restart_blocked=False,
        )

    def stop(self) -> GatewayServiceRecord:
        current = self.status()
        if current.pid is not None and _is_alive(current.pid):
            try:
                os.kill(current.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self._conn.execute("UPDATE gateway_service SET state = 'stopped', pid = NULL, unexpected_starts_json = '[]' WHERE singleton = 1")
        self._conn.commit()
        return self.status()

    def reset(self) -> GatewayServiceRecord:
        current = self.status()
        if current.state == "running":
            raise ValueError("Stop the gateway service before resetting its restart circuit")
        self._conn.execute("UPDATE gateway_service SET unexpected_starts_json = '[]' WHERE singleton = 1")
        self._conn.commit()
        return self.status()
