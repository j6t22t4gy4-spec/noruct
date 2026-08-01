"""Bounded foreground Matrix Client-Server plaintext intake.

This is not a Matrix gateway: it reads only `m.text` events from one canonical
room, from explicitly allowlisted senders, and dispatches them as ordinary
read-only Company Jobs. The first successful sync is deliberately a cursor
prime and never dispatches historical messages.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dynamic_firm.product.inbound_channel import InboundMessage, InboundMessageStore
from dynamic_firm.product.matrix_channel import _ENV, _ROOM, _atomic_write, _homeserver, _without_table

_HEADER = re.compile(r"(?m)^\[matrix_inbound\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_USER = re.compile(r"^@[A-Za-z0-9._=/-]{1,191}:[A-Za-z0-9.-]{1,253}$")
_EVENT = re.compile(r"^\$[A-Za-z0-9._=/-]{1,255}$")
_MAX_RESPONSE_BYTES = 512_000


def _unique_users(values: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(str(value).strip() for value in values))
    if not 1 <= len(result) <= 64 or any(not _USER.fullmatch(value) for value in result):
        raise ValueError("Matrix inbound requires one through 64 canonical allowed user IDs")
    return result


@dataclass(frozen=True, slots=True)
class MatrixInboundConfig:
    workspace: Path
    homeserver_url: str
    room_id: str
    allowed_senders: tuple[str, ...]
    token_env: str = "MATRIX_ACCESS_TOKEN"
    max_message_bytes: int = 12_000
    max_messages_per_run: int = 8
    timeout_seconds: float = 30.0

    def validate(self) -> None:
        workspace = self.workspace.expanduser()
        if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Matrix inbound workspace must be an existing absolute non-symbolic-link directory")
        _homeserver(self.homeserver_url)
        if not _ROOM.fullmatch(self.room_id):
            raise ValueError("Matrix inbound room ID is invalid")
        _unique_users(self.allowed_senders)
        if not _ENV.fullmatch(self.token_env):
            raise ValueError("Matrix inbound token environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 16_000 or not 1 <= self.max_messages_per_run <= 32:
            raise ValueError("Matrix inbound limits are invalid")
        if not 1 <= self.timeout_seconds <= 45:
            raise ValueError("Matrix inbound timeout must be between 1 and 45 seconds")

    @property
    def normalized_homeserver_url(self) -> str:
        return _homeserver(self.homeserver_url)


@dataclass(frozen=True, slots=True)
class MatrixInboundMessage:
    event_id: str
    sender_id: str
    room_id: str
    text: str


@dataclass(frozen=True, slots=True)
class MatrixInboundReceipt:
    primed: bool
    accepted_count: int
    duplicate_count: int
    ignored_count: int
    dispatches: tuple[dict[str, str | None], ...]

    def to_dict(self) -> Mapping[str, object]:
        return asdict(self)


def matrix_inbound_table_text(config: MatrixInboundConfig) -> str:
    config.validate()
    return "\n".join((
        "[matrix_inbound]", "enabled = true", f"workspace = {json.dumps(str(config.workspace.resolve()))}",
        f"homeserver_url = {json.dumps(config.normalized_homeserver_url)}", f"room_id = {json.dumps(config.room_id)}",
        f"allowed_senders = {json.dumps(list(_unique_users(config.allowed_senders)))}", f"token_env = {json.dumps(config.token_env)}",
        f"max_message_bytes = {config.max_message_bytes}", f"max_messages_per_run = {config.max_messages_per_run}",
        f"timeout_seconds = {config.timeout_seconds:g}", "",
    ))


def write_matrix_inbound_settings(path: Path, config: MatrixInboundConfig) -> Path:
    target = path.expanduser().resolve(); existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    match = _HEADER.search(existing)
    if match is None:
        remainder = existing.strip()
    else:
        following = _TABLE.search(existing, match.end())
        remainder = (existing[:match.start()] + (existing[following.start():] if following else "")).strip()
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + matrix_inbound_table_text(config))


def remove_matrix_inbound_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file(): return False
    existing = target.read_text(encoding="utf-8")
    match = _HEADER.search(existing)
    if match is None: return False
    following = _TABLE.search(existing, match.end())
    remainder = (existing[:match.start()] + (existing[following.start():] if following else "")).strip()
    _atomic_write(target, remainder + ("\n" if remainder else "")); return True


def matrix_inbound_config_from_settings(settings: Mapping[str, object]) -> MatrixInboundConfig | None:
    raw = settings.get("matrix_inbound")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True: return None
    workspace, url, room, senders = raw.get("workspace"), raw.get("homeserver_url"), raw.get("room_id"), raw.get("allowed_senders")
    if not isinstance(workspace, str) or not isinstance(url, str) or not isinstance(room, str) or not isinstance(senders, list):
        raise ValueError("Matrix inbound configuration is malformed")
    config = MatrixInboundConfig(Path(workspace), url, room, tuple(str(item) for item in senders), str(raw.get("token_env") or "MATRIX_ACCESS_TOKEN"), int(raw.get("max_message_bytes") or 12_000), int(raw.get("max_messages_per_run") or 8), float(raw.get("timeout_seconds") or 30.0))
    config.validate(); return config


def matrix_inbound_status(config: MatrixInboundConfig | None) -> Mapping[str, object]:
    if config is None: return {"enabled": False, "ready": False, "authority": "no_matrix_inbound", "next_action": "noruct channel matrix-inbox-configure --workspace PATH --homeserver-url HTTPS_URL --room-id !ROOM:SERVER --allow-sender @USER:SERVER"}
    return {"enabled": True, "ready": bool(os.environ.get(config.token_env)), "workspace": str(config.workspace.resolve()), "homeserver_url": config.normalized_homeserver_url, "room_id": config.room_id, "allowed_senders": list(config.allowed_senders), "token_environment": config.token_env, "authority": "foreground_matrix_sync_plaintext_allowlist_read_only_jobs_first_sync_primes_cursor", "next_action": None if os.environ.get(config.token_env) else f"Set {config.token_env} in the operator shell."}


class MatrixInboundCursorStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.execute("CREATE TABLE IF NOT EXISTS matrix_inbound_cursor (id INTEGER PRIMARY KEY CHECK (id = 1), next_batch TEXT NOT NULL)")
    def close(self) -> None: self.conn.close()
    def __enter__(self) -> "MatrixInboundCursorStore": return self
    def __exit__(self, *_: object) -> None: self.close()
    def get(self) -> str | None:
        row = self.conn.execute("SELECT next_batch FROM matrix_inbound_cursor WHERE id = 1").fetchone(); return str(row[0]) if row else None
    def set(self, token: str) -> None:
        if not isinstance(token, str) or not token or len(token.encode()) > 4096: raise ValueError("Matrix sync cursor is invalid")
        self.conn.execute("INSERT INTO matrix_inbound_cursor(id,next_batch) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET next_batch=excluded.next_batch", (token,))


def matrix_inbound_state_path(runtime_state_path: str | Path) -> Path:
    target = Path(runtime_state_path).expanduser().resolve(); return target.with_name(f"{target.stem}.matrix-inbound.sqlite3")


class MatrixSyncClient:
    def __init__(self, config: MatrixInboundConfig) -> None: self.config = config
    def read(self, *, since: str | None) -> Mapping[str, object]:
        token = os.environ.get(self.config.token_env)
        if not token: raise ValueError(f"Matrix access-token environment variable is not set: {self.config.token_env}")
        params: dict[str, str] = {"timeout": str(int(self.config.timeout_seconds * 1000)), "filter": json.dumps({"room": {"timeline": {"limit": min(32, self.config.max_messages_per_run * 4), "types": ["m.room.message"]}}}, separators=(",", ":"))}
        if since: params["since"] = since
        request = Request(f"{self.config.normalized_homeserver_url}/_matrix/client/v3/sync?{urlencode(params)}", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, method="GET")
        with urlopen(request, timeout=self.config.timeout_seconds + 5) as response: raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES: raise RuntimeError("Matrix sync response exceeds the configured bound")
        try: value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise RuntimeError("Matrix sync response is not valid JSON") from exc
        if not isinstance(value, Mapping) or not isinstance(value.get("next_batch"), str): raise RuntimeError("Matrix sync response has no next_batch cursor")
        return value


MatrixDispatch = Callable[[MatrixInboundMessage], Awaitable[tuple[str, str]]]


async def run_matrix_inbound(config: MatrixInboundConfig, *, cursor_store: MatrixInboundCursorStore, message_store: InboundMessageStore, dispatch: MatrixDispatch, client: MatrixSyncClient | None = None, maximum_messages: int | None = None) -> MatrixInboundReceipt:
    config.validate(); limit = config.max_messages_per_run if maximum_messages is None else maximum_messages
    if not 1 <= limit <= config.max_messages_per_run: raise ValueError("Matrix inbound maximum messages exceeds configured limit")
    cursor = cursor_store.get(); payload = await asyncio.to_thread((client or MatrixSyncClient(config)).read, since=cursor)
    next_batch = payload["next_batch"]
    if cursor is None:
        cursor_store.set(next_batch); return MatrixInboundReceipt(True, 0, 0, 0, ())
    cursor_store.set(next_batch)
    rooms = payload.get("rooms"); joined = rooms.get("join") if isinstance(rooms, Mapping) else None; room = joined.get(config.room_id) if isinstance(joined, Mapping) else None
    timeline = room.get("timeline") if isinstance(room, Mapping) else None; events = timeline.get("events") if isinstance(timeline, Mapping) else []
    accepted = duplicates = ignored = 0; dispatches: list[dict[str, str | None]] = []
    for event in events if isinstance(events, list) else []:
        if accepted >= limit: break
        if not isinstance(event, Mapping): ignored += 1; continue
        event_id, sender, kind, content = event.get("event_id"), event.get("sender"), event.get("type"), event.get("content")
        text = content.get("body") if isinstance(content, Mapping) and content.get("msgtype") == "m.text" else None
        if not isinstance(event_id, str) or not _EVENT.fullmatch(event_id) or not isinstance(sender, str) or sender not in config.allowed_senders or kind != "m.room.message" or not isinstance(text, str) or not text.strip() or len(text.encode()) > config.max_message_bytes: ignored += 1; continue
        message = InboundMessage(source_id=f"matrix:{config.room_id}", message_id=event_id, sender=sender, text=text.strip())
        if not message_store.claim(message): duplicates += 1; continue
        accepted += 1
        try:
            job_id, status = await dispatch(MatrixInboundMessage(event_id, sender, config.room_id, text.strip())); message_store.complete(message, job_id=job_id, job_status=status); dispatches.append({"event_id": event_id, "job_id": job_id, "job_status": status, "outcome": "DISPATCHED"})
        except Exception: dispatches.append({"event_id": event_id, "job_id": None, "job_status": None, "outcome": "FAILED"})
    return MatrixInboundReceipt(False, accepted, duplicates, ignored, tuple(dispatches))
