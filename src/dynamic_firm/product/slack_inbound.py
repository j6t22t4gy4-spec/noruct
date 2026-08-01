"""A bounded foreground Slack Events API receiver owned by Noruct.

This is deliberately a narrow HTTP receiver, not a Slack gateway.  It verifies
the official request signature before accepting only allowlisted text messages,
acknowledges Slack before Company work starts, and dispatches accepted input as
ordinary read-only Jobs.  The receiver binds loopback only: an operator who
wants public delivery must own and configure the separate HTTPS reverse proxy
or tunnel.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import queue
import re
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


_HEADER = re.compile(r"(?m)^\[slack_inbound\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}$")
_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/%-]{1,160}$")
_MAX_REQUEST_BYTES = 48_000
_MAX_CHALLENGE_BYTES = 4_000


@dataclass(frozen=True, slots=True)
class SlackInboundConfig:
    workspace: Path
    allowed_senders: tuple[str, ...]
    allowed_channels: tuple[str, ...]
    signing_secret_env: str = "SLACK_SIGNING_SECRET"
    port: int = 3001
    request_path: str = "/slack/events"
    max_message_bytes: int = 12_000
    max_messages_per_run: int = 8
    timestamp_skew_seconds: int = 300

    def __post_init__(self) -> None:
        workspace = self.workspace.expanduser()
        if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Slack inbound workspace must be an existing absolute non-symbolic-link directory")
        if not _ENVIRONMENT_NAME.fullmatch(self.signing_secret_env):
            raise ValueError("Slack inbound signing-secret environment name is invalid")
        senders = _unique_ids(self.allowed_senders, "sender")
        channels = _unique_ids(self.allowed_channels, "channel")
        if not 1 <= self.port <= 65_535:
            raise ValueError("Slack inbound port must be between 1 and 65535")
        if not _PATH.fullmatch(self.request_path) or "//" in self.request_path:
            raise ValueError("Slack inbound request path is invalid")
        if not 1 <= self.max_message_bytes <= 16_000:
            raise ValueError("Slack inbound message limit must be between 1 and 16000 bytes")
        if not 1 <= self.max_messages_per_run <= 32:
            raise ValueError("Slack inbound run message limit must be between 1 and 32")
        if not 30 <= self.timestamp_skew_seconds <= 900:
            raise ValueError("Slack inbound timestamp skew must be between 30 and 900 seconds")
        object.__setattr__(self, "workspace", workspace.resolve())
        object.__setattr__(self, "allowed_senders", senders)
        object.__setattr__(self, "allowed_channels", channels)


def _unique_ids(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(str(item).strip() for item in values))
    if not result or len(result) > 64 or any(not _OPAQUE_ID.fullmatch(item) for item in result):
        raise ValueError(f"Slack inbound requires 1 through 64 bounded allowed {label} identities")
    return result


@dataclass(frozen=True, slots=True)
class SlackInboundMessage:
    event_id: str
    message_id: str
    sender_id: str
    channel_id: str
    text: str


@dataclass(frozen=True, slots=True)
class SlackInboundDispatchReceipt:
    event_id: str
    message_id: str
    sender_id: str
    channel_id: str
    job_id: str | None
    job_status: str | None
    outcome: str


@dataclass(frozen=True, slots=True)
class SlackInboundRunReceipt:
    accepted_count: int
    duplicate_count: int
    ignored_count: int
    rejected_request_count: int
    bound_port: int
    dispatches: tuple[SlackInboundDispatchReceipt, ...]


class SlackInboundStore:
    """Stores event identity, content hash and terminal Job metadata only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS slack_inbound_messages (
                event_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                job_id TEXT,
                job_status TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SlackInboundStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def claim(self, message: SlackInboundMessage) -> bool:
        digest = hashlib.sha256(message.text.encode("utf-8")).hexdigest()
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO slack_inbound_messages(
                event_id, message_id, sender_id, channel_id, content_sha256, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'CLAIMED', datetime('now'))
            """,
            (message.event_id, message.message_id, message.sender_id, message.channel_id, digest),
        )
        return cursor.rowcount == 1

    def complete(self, message: SlackInboundMessage, *, job_id: str, job_status: str) -> None:
        self._conn.execute(
            """
            UPDATE slack_inbound_messages
            SET status = 'COMPLETED', job_id = ?, job_status = ?, completed_at = datetime('now')
            WHERE event_id = ?
            """,
            (job_id, job_status, message.event_id),
        )


def slack_inbound_state_path(runtime_state_path: str | Path) -> Path:
    target = Path(runtime_state_path).expanduser().resolve()
    return target.with_name(f"{target.stem}.slack-inbound.sqlite3")


def slack_inbound_table_text(config: SlackInboundConfig) -> str:
    values = (
        "[slack_inbound]",
        "enabled = true",
        f"workspace = {json.dumps(str(config.workspace))}",
        f"allowed_senders = {json.dumps(list(config.allowed_senders))}",
        f"allowed_channels = {json.dumps(list(config.allowed_channels))}",
        f"signing_secret_env = {json.dumps(config.signing_secret_env)}",
        f"port = {config.port}",
        f"request_path = {json.dumps(config.request_path)}",
        f"max_message_bytes = {config.max_message_bytes}",
        f"max_messages_per_run = {config.max_messages_per_run}",
        f"timestamp_skew_seconds = {config.timestamp_skew_seconds}",
    )
    return "\n".join(values) + "\n"


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.rstrip()
    following = _TABLE.search(text, match.end())
    return (text[:match.start()] + (text[following.start():] if following else "")).strip()


def _atomic_write(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)
    return path


def write_slack_inbound_settings(path: str | Path, config: SlackInboundConfig) -> Path:
    target = Path(path).expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + slack_inbound_table_text(config))


def remove_slack_inbound_settings(path: str | Path) -> bool:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    value = _without_table(existing)
    _atomic_write(target, value + ("\n" if value else ""))
    return True


def slack_inbound_config_from_settings(settings: Mapping[str, object]) -> SlackInboundConfig | None:
    raw = settings.get("slack_inbound")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    senders, channels, workspace = raw.get("allowed_senders"), raw.get("allowed_channels"), raw.get("workspace")
    if not isinstance(senders, list) or not all(isinstance(item, str) for item in senders):
        raise ValueError("Slack inbound allowed_senders is malformed")
    if not isinstance(channels, list) or not all(isinstance(item, str) for item in channels):
        raise ValueError("Slack inbound allowed_channels is malformed")
    if not isinstance(workspace, str):
        raise ValueError("Slack inbound workspace is malformed")
    return SlackInboundConfig(
        workspace=Path(workspace), allowed_senders=tuple(senders), allowed_channels=tuple(channels),
        signing_secret_env=str(raw.get("signing_secret_env") or "SLACK_SIGNING_SECRET"),
        port=int(raw.get("port") or 3001), request_path=str(raw.get("request_path") or "/slack/events"),
        max_message_bytes=int(raw.get("max_message_bytes") or 12_000),
        max_messages_per_run=int(raw.get("max_messages_per_run") or 8),
        timestamp_skew_seconds=int(raw.get("timestamp_skew_seconds") or 300),
    )


def slack_inbound_status(config: SlackInboundConfig | None) -> Mapping[str, object]:
    if config is None:
        return {"enabled": False, "ready": False, "authority": "no_slack_inbound_channel", "next_action": "noruct channel slack-inbox-configure"}
    ready = bool(os.environ.get(config.signing_secret_env, "").strip())
    return {
        "enabled": True, "ready": ready, "workspace": str(config.workspace),
        "allowed_sender_count": len(config.allowed_senders), "allowed_channel_count": len(config.allowed_channels),
        "signing_secret_environment": config.signing_secret_env, "loopback_url": f"http://127.0.0.1:{config.port}{config.request_path}",
        "authority": "foreground_confirmed_signed_slack_event_receiver",
        "next_action": None if ready else f"Set {config.signing_secret_env} in the operator environment.",
    }


def verify_slack_signature(*, secret: str, timestamp: str | None, signature: str | None, body: bytes, skew_seconds: int, now: float | None = None) -> bool:
    if not timestamp or not signature or not timestamp.isdecimal() or len(secret) > 1_024:
        return False
    issued_at = int(timestamp)
    if abs((time.time() if now is None else now) - issued_at) > skew_seconds:
        return False
    expected = "v0=" + hmac.new(secret.encode("utf-8"), b"v0:" + timestamp.encode("ascii") + b":" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _message_from_envelope(payload: Mapping[str, object], config: SlackInboundConfig) -> SlackInboundMessage | None:
    if payload.get("type") != "event_callback":
        return None
    event_id, event = payload.get("event_id"), payload.get("event")
    if not isinstance(event_id, str) or not _OPAQUE_ID.fullmatch(event_id) or not isinstance(event, Mapping):
        return None
    if event.get("type") != "message" or event.get("subtype") is not None or event.get("bot_id") is not None:
        return None
    sender_id, channel_id, message_id, text = event.get("user"), event.get("channel"), event.get("ts"), event.get("text")
    if not all(isinstance(item, str) for item in (sender_id, channel_id, message_id, text)):
        return None
    if not _OPAQUE_ID.fullmatch(sender_id) or not _OPAQUE_ID.fullmatch(channel_id) or not _OPAQUE_ID.fullmatch(message_id):
        return None
    value = text.strip()
    if not value or len(value.encode("utf-8")) > config.max_message_bytes:
        return None
    if sender_id not in config.allowed_senders or channel_id not in config.allowed_channels:
        return None
    return SlackInboundMessage(event_id=event_id, message_id=message_id, sender_id=sender_id, channel_id=channel_id, text=value)


class SlackEventReceiver:
    """Loopback receiver which verifies then queues minimal accepted messages."""

    def __init__(self, config: SlackInboundConfig) -> None:
        secret = os.environ.get(config.signing_secret_env, "").strip()
        if not secret or len(secret) > 1_024:
            raise ValueError(f"Slack signing secret environment variable is not set or invalid: {config.signing_secret_env}")
        self.config, self._secret = config, secret
        self._messages: queue.Queue[SlackInboundMessage] = queue.Queue(maxsize=config.max_messages_per_run)
        self._lock = threading.Lock()
        self._ignored = 0
        self._rejected = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        return int(self._server.server_port) if self._server is not None else self.config.port

    def counters(self) -> tuple[int, int]:
        with self._lock:
            return self._ignored, self._rejected

    def _ignored_event(self) -> None:
        with self._lock:
            self._ignored += 1

    def _rejected_request(self) -> None:
        with self._lock:
            self._rejected += 1

    def accept(self, *, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes, str]:
        if path != self.config.request_path or len(body) > _MAX_REQUEST_BYTES:
            self._rejected_request()
            return 404, b"not found", "text/plain; charset=utf-8"
        if not verify_slack_signature(secret=self._secret, timestamp=headers.get("X-Slack-Request-Timestamp"), signature=headers.get("X-Slack-Signature"), body=body, skew_seconds=self.config.timestamp_skew_seconds):
            self._rejected_request()
            return 401, b"invalid signature", "text/plain; charset=utf-8"
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._rejected_request()
            return 400, b"invalid JSON", "text/plain; charset=utf-8"
        if not isinstance(payload, Mapping):
            self._rejected_request()
            return 400, b"invalid event", "text/plain; charset=utf-8"
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or not challenge or len(challenge.encode("utf-8")) > _MAX_CHALLENGE_BYTES:
                self._rejected_request()
                return 400, b"invalid challenge", "text/plain; charset=utf-8"
            return 200, challenge.encode("utf-8"), "text/plain; charset=utf-8"
        message = _message_from_envelope(payload, self.config)
        if message is None:
            self._ignored_event()
            return 200, b'{"ok":true}', "application/json"
        try:
            self._messages.put_nowait(message)
        except queue.Full:
            self._rejected_request()
            return 503, b"receiver busy", "text/plain; charset=utf-8"
        return 200, b'{"ok":true}', "application/json"

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("Slack event receiver is already running")
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                try:
                    raw_length = self.headers.get("Content-Length", "")
                    length = int(raw_length)
                    if length < 0 or length > _MAX_REQUEST_BYTES:
                        raise ValueError
                    body = self.rfile.read(length)
                except (TypeError, ValueError):
                    receiver._rejected_request()
                    status, response, content_type = 400, b"invalid request", "text/plain; charset=utf-8"
                else:
                    status, response, content_type = receiver.accept(path=self.path.split("?", 1)[0], headers=dict(self.headers.items()), body=body)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format: str, *_args: object) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", self.config.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="noruct-slack-events", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server, self._thread = None, None

    def get(self, timeout_seconds: float) -> SlackInboundMessage:
        return self._messages.get(timeout=timeout_seconds)


SlackInboundDispatch = Callable[[SlackInboundMessage], Awaitable[tuple[str, str]]]


async def run_slack_inbound_channel(config: SlackInboundConfig, *, store: SlackInboundStore, dispatch: SlackInboundDispatch, maximum_seconds: float, maximum_messages: int | None = None, receiver: SlackEventReceiver | None = None) -> SlackInboundRunReceipt:
    """Run the signed loopback receiver in the foreground and dispatch accepted text."""
    if not 1 <= maximum_seconds <= 3_600:
        raise ValueError("Slack inbound maximum seconds must be between 1 and 3600")
    limit = config.max_messages_per_run if maximum_messages is None else maximum_messages
    if not 1 <= limit <= config.max_messages_per_run:
        raise ValueError("Slack inbound maximum messages exceeds configured limit")
    active_receiver = receiver or SlackEventReceiver(config)
    accepted = duplicate = 0
    dispatches: list[SlackInboundDispatchReceipt] = []
    active_receiver.start()
    try:
        deadline = time.monotonic() + maximum_seconds
        while time.monotonic() < deadline and accepted < limit:
            try:
                message = await asyncio.to_thread(active_receiver.get, min(0.25, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if not store.claim(message):
                duplicate += 1
                continue
            accepted += 1
            try:
                job_id, job_status = await dispatch(message)
                store.complete(message, job_id=job_id, job_status=job_status)
                dispatches.append(SlackInboundDispatchReceipt(message.event_id, message.message_id, message.sender_id, message.channel_id, job_id, job_status, "DISPATCHED"))
            except Exception:
                dispatches.append(SlackInboundDispatchReceipt(message.event_id, message.message_id, message.sender_id, message.channel_id, None, None, "FAILED"))
    finally:
        ignored, rejected = active_receiver.counters()
        bound_port = active_receiver.bound_port
        active_receiver.close()
    return SlackInboundRunReceipt(accepted, duplicate, ignored, rejected, bound_port, tuple(dispatches))
