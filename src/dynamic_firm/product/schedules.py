"""Durable, manually-dispatched schedules for ordinary Noruct Jobs.

The store deliberately has no daemon, script runner, provider credential, or
delivery target.  A due schedule is claimed once and handed back to the
existing Company Job path by the CLI.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .cron_expression import CronExpression


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    schedule_id: str
    name: str
    goal: str
    workspace: Path
    interval_minutes: int
    schedule_type: str
    cron_expression: str | None
    enabled: bool
    next_run_at: datetime
    last_run_at: datetime | None
    last_job_id: str | None
    last_status: str | None
    run_count: int


class ScheduleStore:
    """SQLite schedule lifecycle with one atomic due/forced-run claim."""

    def __init__(self, path: Path) -> None:
        path.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
              schedule_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              goal TEXT NOT NULL,
              workspace TEXT NOT NULL,
              interval_minutes INTEGER NOT NULL,
              enabled INTEGER NOT NULL,
              next_run_at TEXT NOT NULL,
              last_run_at TEXT,
              last_job_id TEXT,
              last_status TEXT,
              run_count INTEGER NOT NULL DEFAULT 0,
              schedule_type TEXT NOT NULL DEFAULT 'interval',
              cron_expression TEXT,
              created_at TEXT NOT NULL
            )
            """
        )
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(scheduled_jobs)")}
        if "schedule_type" not in columns: self._conn.execute("ALTER TABLE scheduled_jobs ADD COLUMN schedule_type TEXT NOT NULL DEFAULT 'interval'")
        if "cron_expression" not in columns: self._conn.execute("ALTER TABLE scheduled_jobs ADD COLUMN cron_expression TEXT")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ScheduleStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create(
        self, *, name: str, goal: str, workspace: Path, interval_minutes: int
    ) -> ScheduledJob:
        if not name.strip() or not goal.strip():
            raise ValueError("Schedule name and goal must be non-empty")
        if interval_minutes < 1 or interval_minutes > 43_200:
            raise ValueError("Schedule interval must be between 1 minute and 30 days")
        if not workspace.is_dir():
            raise ValueError(f"Schedule workspace is not a directory: {workspace}")
        now = _now()
        record = ScheduledJob(
            schedule_id=f"schedule-{uuid.uuid4()}",
            name=name.strip(),
            goal=goal.strip(),
            workspace=workspace.resolve(),
            interval_minutes=interval_minutes,
            schedule_type="interval",
            cron_expression=None,
            enabled=True,
            next_run_at=now + timedelta(minutes=interval_minutes),
            last_run_at=None,
            last_job_id=None,
            last_status=None,
            run_count=0,
        )
        self._conn.execute(
            """INSERT INTO scheduled_jobs (
                 schedule_id, name, goal, workspace, interval_minutes, enabled,
                 next_run_at, last_run_at, last_job_id, last_status, run_count, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?)""",
            (
                record.schedule_id, record.name, record.goal, str(record.workspace),
                record.interval_minutes, 1, _timestamp(record.next_run_at), _timestamp(now),
            ),
        )
        self._conn.commit()
        return record

    def create_cron(self, *, name: str, goal: str, workspace: Path, expression: str) -> ScheduledJob:
        if not name.strip() or not goal.strip(): raise ValueError("Schedule name and goal must be non-empty")
        if not workspace.is_dir(): raise ValueError(f"Schedule workspace is not a directory: {workspace}")
        cron = CronExpression.parse(expression); now = _now()
        record = ScheduledJob(f"schedule-{uuid.uuid4()}", name.strip(), goal.strip(), workspace.resolve(), 1, "cron", cron.value, True, cron.next_after(now), None, None, None, 0)
        self._conn.execute("""INSERT INTO scheduled_jobs (schedule_id,name,goal,workspace,interval_minutes,enabled,next_run_at,last_run_at,last_job_id,last_status,run_count,schedule_type,cron_expression,created_at) VALUES (?,?,?,?,?,1,?,NULL,NULL,NULL,0,?,?,?)""", (record.schedule_id,record.name,record.goal,str(record.workspace),1,_timestamp(record.next_run_at),"cron",cron.value,_timestamp(now)))
        self._conn.commit(); return record

    def list(self, *, include_disabled: bool = False) -> tuple[ScheduledJob, ...]:
        where = "" if include_disabled else "WHERE enabled = 1"
        rows = self._conn.execute(
            f"SELECT * FROM scheduled_jobs {where} ORDER BY next_run_at, schedule_id"
        ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def get(self, schedule_id: str) -> ScheduledJob | None:
        row = self._conn.execute(
            "SELECT * FROM scheduled_jobs WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        return self._decode(row) if row else None

    def set_enabled(self, schedule_id: str, *, enabled: bool) -> ScheduledJob:
        if self.get(schedule_id) is None:
            raise ValueError(f"Schedule was not found: {schedule_id}")
        self._conn.execute(
            "UPDATE scheduled_jobs SET enabled = ? WHERE schedule_id = ?",
            (1 if enabled else 0, schedule_id),
        )
        self._conn.commit()
        return self.get(schedule_id)  # type: ignore[return-value]

    def remove(self, schedule_id: str) -> bool:
        result = self._conn.execute(
            "DELETE FROM scheduled_jobs WHERE schedule_id = ?", (schedule_id,)
        )
        self._conn.commit()
        return result.rowcount == 1

    def claim_due(self, *, now: datetime | None = None, limit: int | None = None) -> tuple[ScheduledJob, ...]:
        if limit is not None and not 1 <= limit <= 32:
            raise ValueError("Schedule due-claim limit must be between 1 and 32")
        return self._claim(now=now or _now(), force_id=None, limit=limit)

    def claim_one(self, schedule_id: str, *, now: datetime | None = None) -> ScheduledJob:
        claimed = self._claim(now=now or _now(), force_id=schedule_id, limit=None)
        if not claimed:
            raise ValueError(f"Enabled schedule was not found: {schedule_id}")
        return claimed[0]

    def _claim(self, *, now: datetime, force_id: str | None, limit: int | None) -> tuple[ScheduledJob, ...]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if force_id is None:
                query = "SELECT * FROM scheduled_jobs WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at, schedule_id"
                parameters: tuple[object, ...] = (_timestamp(now),)
                if limit is not None:
                    query += " LIMIT ?"
                    parameters += (limit,)
                rows = self._conn.execute(query, parameters).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE schedule_id = ? AND enabled = 1",
                    (force_id,),
                ).fetchall()
            claimed = tuple(self._decode(row) for row in rows)
            for item in claimed:
                self._conn.execute(
                    "UPDATE scheduled_jobs SET next_run_at = ?, last_run_at = ? WHERE schedule_id = ?",
                    (
                        _timestamp(CronExpression.parse(item.cron_expression).next_after(now) if item.schedule_type == "cron" and item.cron_expression else now + timedelta(minutes=item.interval_minutes)),
                        _timestamp(now), item.schedule_id,
                    ),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return claimed

    def complete(self, schedule_id: str, *, job_id: str, status: str) -> ScheduledJob:
        self._conn.execute(
            """UPDATE scheduled_jobs
               SET last_job_id = ?, last_status = ?, run_count = run_count + 1
               WHERE schedule_id = ?""",
            (job_id, status, schedule_id),
        )
        self._conn.commit()
        item = self.get(schedule_id)
        assert item is not None
        return item

    @staticmethod
    def _decode(row: sqlite3.Row) -> ScheduledJob:
        return ScheduledJob(
            schedule_id=str(row["schedule_id"]), name=str(row["name"]),
            goal=str(row["goal"]), workspace=Path(str(row["workspace"])),
            interval_minutes=int(row["interval_minutes"]), enabled=bool(row["enabled"]),
            schedule_type=str(row["schedule_type"] or "interval"), cron_expression=str(row["cron_expression"]) if row["cron_expression"] else None,
            next_run_at=_parse_timestamp(str(row["next_run_at"])),
            last_run_at=_parse_timestamp(str(row["last_run_at"])) if row["last_run_at"] else None,
            last_job_id=str(row["last_job_id"]) if row["last_job_id"] else None,
            last_status=str(row["last_status"]) if row["last_status"] else None,
            run_count=int(row["run_count"]),
        )
