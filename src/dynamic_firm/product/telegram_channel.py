"""A bounded, foreground Telegram Bot API channel owned by Noruct.

This is deliberately a small public-HTTP protocol adapter, not a generic
messenger gateway.  It supports text messages from an explicit sender allowlist
and sends one bounded terminal Job summary back to the same chat.  Token values
remain in the operator environment and raw incoming text is never persisted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output


_HEADER = re.compile(r"(?m)^\[telegram_channel\][ \t]*(?:\r?\n|$)")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")
_DEFAULT_API_BASE_URL = "https://api.telegram.org"
_MAX_REPLY_BYTES = 4_000
_INLINE_SECRET = re.compile(
    r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|authorization)\s*[:=]\s*[^\s,;]+"
)


def _validated_api_base_url(value: object) -> str:
    raw = str(value or _DEFAULT_API_BASE_URL).strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
        return raw
    if (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username
        and not parsed.password
    ):
        return raw
    raise ValueError("Telegram API base URL must be HTTPS, or an explicit loopback HTTP URL")


@dataclass(frozen=True, slots=True)
class TelegramChannelConfig:
    workspace: Path
    allowed_senders: tuple[str, ...]
    token_env: str = "TELEGRAM_BOT_TOKEN"
    api_base_url: str = _DEFAULT_API_BASE_URL
    max_message_bytes: int = 12_000
    max_messages_per_run: int = 8
    poll_timeout_seconds: int = 15

    def __post_init__(self) -> None:
        workspace = self.workspace.expanduser()
        if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Telegram channel workspace must be an existing absolute non-symbolic-link directory")
        if not _ENVIRONMENT_NAME.fullmatch(self.token_env):
            raise ValueError("Telegram channel token environment name is invalid")
        senders = tuple(dict.fromkeys(str(item).strip() for item in self.allowed_senders))
        if not senders or len(senders) > 64 or any(not _OPAQUE_ID.fullmatch(item) for item in senders):
            raise ValueError("Telegram channel requires 1 through 64 bounded allowed sender identities")
        if not 1 <= self.max_message_bytes <= 16_000:
            raise ValueError("Telegram channel message limit must be between 1 and 16000 bytes")
        if not 1 <= self.max_messages_per_run <= 32:
            raise ValueError("Telegram channel run message limit must be between 1 and 32")
        if not 1 <= self.poll_timeout_seconds <= 30:
            raise ValueError("Telegram channel poll timeout must be between 1 and 30 seconds")
        object.__setattr__(self, "workspace", workspace.resolve())
        object.__setattr__(self, "allowed_senders", senders)
        object.__setattr__(self, "api_base_url", _validated_api_base_url(self.api_base_url))


@dataclass(frozen=True, slots=True)
class TelegramInboundMessage:
    update_id: int
    message_id: str
    sender_id: str
    chat_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TelegramDispatchReceipt:
    update_id: int
    message_id: str
    sender_id: str
    job_id: str | None
    job_status: str | None
    outcome: str
    replied: bool


@dataclass(frozen=True, slots=True)
class TelegramRunReceipt:
    accepted_count: int
    rejected_count: int
    duplicate_count: int
    ignored_count: int
    highest_offset: int
    dispatches: tuple[TelegramDispatchReceipt, ...]


class TelegramChannelStore:
    """Stores only update identity, hash and terminal dispatch metadata."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS telegram_channel_offsets (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                next_update_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_channel_messages (
                update_id INTEGER PRIMARY KEY,
                message_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
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

    def __enter__(self) -> "TelegramChannelStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def offset(self) -> int | None:
        row = self._conn.execute(
            "SELECT next_update_id FROM telegram_channel_offsets WHERE singleton = 1"
        ).fetchone()
        return int(row[0]) if row is not None else None

    def advance_offset(self, next_update_id: int) -> None:
        if next_update_id < 1:
            raise ValueError("Telegram next update id must be positive")
        self._conn.execute(
            """
            INSERT INTO telegram_channel_offsets(singleton, next_update_id) VALUES(1, ?)
            ON CONFLICT(singleton) DO UPDATE SET next_update_id =
                MAX(telegram_channel_offsets.next_update_id, excluded.next_update_id)
            """,
            (next_update_id,),
        )

    def claim(self, message: TelegramInboundMessage) -> bool:
        digest = hashlib.sha256(message.text.encode("utf-8")).hexdigest()
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO telegram_channel_messages(
                update_id, message_id, sender_id, content_sha256, status, created_at
            ) VALUES (?, ?, ?, ?, 'CLAIMED', datetime('now'))
            """,
            (message.update_id, message.message_id, message.sender_id, digest),
        )
        return cursor.rowcount == 1

    def complete(self, message: TelegramInboundMessage, *, job_id: str, job_status: str) -> None:
        self._conn.execute(
            """
            UPDATE telegram_channel_messages
            SET status = 'COMPLETED', job_id = ?, job_status = ?, completed_at = datetime('now')
            WHERE update_id = ?
            """,
            (job_id, job_status, message.update_id),
        )


def telegram_state_path(runtime_state_path: str | Path) -> Path:
    target = Path(runtime_state_path).expanduser().resolve()
    return target.with_name(f"{target.stem}.telegram-channel.sqlite3")


def telegram_channel_table_text(config: TelegramChannelConfig) -> str:
    values = [
        "[telegram_channel]",
        "enabled = true",
        f"workspace = {json.dumps(str(config.workspace))}",
        f"allowed_senders = {json.dumps(list(config.allowed_senders))}",
        f"token_env = {json.dumps(config.token_env)}",
        f"api_base_url = {json.dumps(config.api_base_url)}",
        f"max_message_bytes = {config.max_message_bytes}",
        f"max_messages_per_run = {config.max_messages_per_run}",
        f"poll_timeout_seconds = {config.poll_timeout_seconds}",
    ]
    return "\n".join(values) + "\n"


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.rstrip()
    next_table = re.search(r"(?m)^\[[^\n]+\][ \t]*(?:\r?\n|$)", text[match.end() :])
    end = match.end() + next_table.start() if next_table else len(text)
    return (text[: match.start()] + text[end:]).strip()


def _atomic_write(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)
    return path


def write_telegram_channel_settings(path: str | Path, config: TelegramChannelConfig) -> Path:
    target = Path(path).expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + telegram_channel_table_text(config))


def remove_telegram_channel_settings(path: str | Path) -> bool:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    _atomic_write(target, _without_table(existing) + ("\n" if _without_table(existing) else ""))
    return True


def telegram_channel_config_from_settings(settings: Mapping[str, object]) -> TelegramChannelConfig | None:
    raw = settings.get("telegram_channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    allowed = raw.get("allowed_senders")
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise ValueError("Telegram channel allowed_senders is malformed")
    workspace = raw.get("workspace")
    if not isinstance(workspace, str):
        raise ValueError("Telegram channel workspace is malformed")
    return TelegramChannelConfig(
        workspace=Path(workspace),
        allowed_senders=tuple(allowed),
        token_env=str(raw.get("token_env") or "TELEGRAM_BOT_TOKEN"),
        api_base_url=str(raw.get("api_base_url") or _DEFAULT_API_BASE_URL),
        max_message_bytes=int(raw.get("max_message_bytes") or 12_000),
        max_messages_per_run=int(raw.get("max_messages_per_run") or 8),
        poll_timeout_seconds=int(raw.get("poll_timeout_seconds") or 15),
    )


def telegram_channel_status(config: TelegramChannelConfig | None) -> Mapping[str, object]:
    if config is None:
        return {
            "enabled": False,
            "ready": False,
            "authority": "no_telegram_channel",
            "next_action": "noruct channel telegram-configure",
        }
    return {
        "enabled": True,
        "ready": bool(os.environ.get(config.token_env, "").strip()),
        "workspace": str(config.workspace),
        "allowed_sender_count": len(config.allowed_senders),
        "token_environment": config.token_env,
        "api_base_url": config.api_base_url,
        "authority": "foreground_confirmed_telegram_text_channel",
        "next_action": None if os.environ.get(config.token_env, "").strip() else f"Set {config.token_env} in the operator environment.",
    }


class TelegramBotApiClient:
    """Small standard-library HTTPS client; token values never enter records."""

    def __init__(self, config: TelegramChannelConfig, *, timeout_seconds: float = 35.0) -> None:
        token = os.environ.get(config.token_env, "").strip()
        if not token:
            raise ValueError(f"Telegram channel environment variable is not set: {config.token_env}")
        if len(token) > 512 or any(character.isspace() for character in token):
            raise ValueError("Telegram bot token is invalid")
        self._base_url = config.api_base_url
        self._token = token
        self._timeout_seconds = timeout_seconds

    def _request(self, method: str, payload: Mapping[str, object]) -> object:
        endpoint = f"{self._base_url}/bot{self._token}/{method}"
        encoded = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        request = Request(
            endpoint,
            data=encoded,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # nosec B310: validated HTTPS/loopback endpoint
                body = response.read(1_000_000)
        except (HTTPError, URLError, OSError) as exc:
            raise RuntimeError("Telegram API request failed") from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Telegram API returned invalid JSON") from exc
        if not isinstance(decoded, Mapping) or decoded.get("ok") is not True:
            raise RuntimeError("Telegram API rejected the request")
        return decoded.get("result")

    async def get_updates(self, *, offset: int | None, timeout_seconds: int) -> tuple[Mapping[str, object], ...]:
        payload: dict[str, object] = {
            "limit": 32,
            "timeout": timeout_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await asyncio.to_thread(self._request, "getUpdates", payload)
        if not isinstance(result, list) or not all(isinstance(item, Mapping) for item in result):
            raise RuntimeError("Telegram API returned malformed updates")
        return tuple(result)

    async def send_message(self, *, chat_id: str, text: str, reply_to_message_id: str) -> None:
        await asyncio.to_thread(
            self._request,
            "sendMessage",
            {"chat_id": chat_id, "text": text, "reply_parameters": {"message_id": int(reply_to_message_id)}},
        )


def _message_from_update(
    update: Mapping[str, object],
    config: TelegramChannelConfig,
) -> TelegramInboundMessage | None:
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int) or update_id < 1 or not isinstance(message, Mapping):
        return None
    raw_text = message.get("text")
    sender = message.get("from")
    chat = message.get("chat")
    message_id = message.get("message_id")
    if not isinstance(raw_text, str) or not raw_text.strip() or not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
        return None
    sender_id = str(sender.get("id") or "")
    chat_id = str(chat.get("id") or "")
    if not _OPAQUE_ID.fullmatch(sender_id) or not _OPAQUE_ID.fullmatch(chat_id) or not isinstance(message_id, int) or message_id < 1:
        return None
    if sender_id not in config.allowed_senders:
        return None
    text = raw_text.strip()
    if len(text.encode("utf-8")) > config.max_message_bytes:
        return None
    return TelegramInboundMessage(
        update_id=update_id,
        message_id=str(message_id),
        sender_id=sender_id,
        chat_id=chat_id,
        text=text,
    )


def _reply_text(summary: str, status: str) -> str:
    value = redact_terminal_output(summary or "", force=True)
    # Terminal redaction intentionally avoids broad source-code heuristics;
    # a third-party chat reply is a stricter disclosure boundary.  Mask the
    # common inline assignment shapes that a model can echo in a prose final.
    value = _INLINE_SECRET.sub("[REDACTED]", value)
    if not value:
        value = f"Noruct job {status.lower()}."
    encoded = value.encode("utf-8")[:_MAX_REPLY_BYTES]
    return encoded.decode("utf-8", errors="ignore")


TelegramDispatch = Callable[[TelegramInboundMessage], Awaitable[tuple[str, str, str]]]


async def run_telegram_channel(
    config: TelegramChannelConfig,
    *,
    store: TelegramChannelStore,
    dispatch: TelegramDispatch,
    maximum_seconds: float,
    maximum_messages: int | None = None,
    client: TelegramBotApiClient | None = None,
) -> TelegramRunReceipt:
    """Foreground long-poll and reply loop with monotonic Telegram offsets."""

    if not 1 <= maximum_seconds <= 3_600:
        raise ValueError("Telegram channel maximum seconds must be between 1 and 3600")
    limit = config.max_messages_per_run if maximum_messages is None else maximum_messages
    if not 1 <= limit <= config.max_messages_per_run:
        raise ValueError("Telegram channel maximum messages exceeds configured limit")
    api = client or TelegramBotApiClient(config)
    deadline = time.monotonic() + maximum_seconds
    accepted = rejected = duplicate = ignored = 0
    dispatches: list[TelegramDispatchReceipt] = []
    while time.monotonic() < deadline and accepted < limit:
        remaining = max(1, int(deadline - time.monotonic()))
        poll_timeout = min(config.poll_timeout_seconds, remaining)
        updates = await api.get_updates(offset=store.offset(), timeout_seconds=poll_timeout)
        if not updates:
            continue
        for update in sorted(updates, key=lambda item: int(item.get("update_id", 0) or 0)):
            raw_update_id = update.get("update_id")
            if isinstance(raw_update_id, int) and raw_update_id > 0:
                store.advance_offset(raw_update_id + 1)
            message = _message_from_update(update, config)
            if message is None:
                ignored += 1
                continue
            if not store.claim(message):
                duplicate += 1
                continue
            if accepted >= limit:
                rejected += 1
                continue
            accepted += 1
            try:
                job_id, job_status, summary = await dispatch(message)
                store.complete(message, job_id=job_id, job_status=job_status)
                reply = _reply_text(summary, job_status)
                await api.send_message(
                    chat_id=message.chat_id,
                    text=reply,
                    reply_to_message_id=message.message_id,
                )
                dispatches.append(
                    TelegramDispatchReceipt(
                        message.update_id, message.message_id, message.sender_id,
                        job_id, job_status, "DISPATCHED", True,
                    )
                )
            except Exception:
                # The update offset is intentionally retained: this bridge is
                # at-most-once after a claim, so a transport/model fault never
                # silently duplicates a Company Job or an external reply.
                dispatches.append(
                    TelegramDispatchReceipt(
                        message.update_id, message.message_id, message.sender_id,
                        None, None, "FAILED", False,
                    )
                )
            if accepted >= limit or time.monotonic() >= deadline:
                break
    return TelegramRunReceipt(
        accepted_count=accepted,
        rejected_count=rejected,
        duplicate_count=duplicate,
        ignored_count=ignored,
        highest_offset=store.offset() or 0,
        dispatches=tuple(dispatches),
    )
