"""Bounded foreground Discord text intake behind Noruct Company authority.

The optional ``discord.py`` dependency owns only the user-authenticated Discord
Gateway connection.  Noruct owns the allowlists, deduplication, persistence and
ordinary read-only Company Job dispatch.  It can be selected by the Noruct
gateway supervisor/service, but does not itself reply, manage an application,
accept media, route sessions, or restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_HEADER = re.compile(r"(?m)^\[discord_inbound\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_SNOWFLAKE = re.compile(r"^[0-9]{1,32}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class DiscordInboundConfig:
    workspace: Path
    allowed_senders: tuple[str, ...]
    allowed_channels: tuple[str, ...]
    token_env: str = "DISCORD_BOT_TOKEN"
    max_message_bytes: int = 12_000
    max_messages_per_run: int = 8

    def __post_init__(self) -> None:
        workspace = self.workspace.expanduser()
        if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Discord inbound workspace must be an existing absolute non-symbolic-link directory")
        if not _ENVIRONMENT_NAME.fullmatch(self.token_env):
            raise ValueError("Discord inbound token environment name is invalid")
        senders = _unique_snowflakes(self.allowed_senders, "sender")
        channels = _unique_snowflakes(self.allowed_channels, "channel")
        if not 1 <= self.max_message_bytes <= 16_000:
            raise ValueError("Discord inbound message limit must be between 1 and 16000 bytes")
        if not 1 <= self.max_messages_per_run <= 32:
            raise ValueError("Discord inbound run message limit must be between 1 and 32")
        object.__setattr__(self, "workspace", workspace.resolve())
        object.__setattr__(self, "allowed_senders", senders)
        object.__setattr__(self, "allowed_channels", channels)


def _unique_snowflakes(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(str(item).strip() for item in values))
    if not result or len(result) > 64 or any(not _SNOWFLAKE.fullmatch(item) for item in result):
        raise ValueError(f"Discord inbound requires 1 through 64 numeric allowed {label} identities")
    return result


@dataclass(frozen=True, slots=True)
class DiscordInboundMessage:
    message_id: str
    sender_id: str
    channel_id: str
    text: str


@dataclass(frozen=True, slots=True)
class DiscordInboundDispatchReceipt:
    message_id: str
    sender_id: str
    channel_id: str
    job_id: str | None
    job_status: str | None
    outcome: str


@dataclass(frozen=True, slots=True)
class DiscordInboundRunReceipt:
    accepted_count: int
    duplicate_count: int
    ignored_count: int
    dispatches: tuple[DiscordInboundDispatchReceipt, ...]


class DiscordInboundStore:
    """Stores message identity, content digest and terminal Job metadata only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discord_inbound_messages (
                message_id TEXT PRIMARY KEY,
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

    def __enter__(self) -> "DiscordInboundStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def claim(self, message: DiscordInboundMessage) -> bool:
        digest = hashlib.sha256(message.text.encode("utf-8")).hexdigest()
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO discord_inbound_messages(
                message_id, sender_id, channel_id, content_sha256, status, created_at
            ) VALUES (?, ?, ?, ?, 'CLAIMED', datetime('now'))
            """,
            (message.message_id, message.sender_id, message.channel_id, digest),
        )
        return cursor.rowcount == 1

    def complete(self, message: DiscordInboundMessage, *, job_id: str, job_status: str) -> None:
        self._conn.execute(
            """
            UPDATE discord_inbound_messages
            SET status = 'COMPLETED', job_id = ?, job_status = ?, completed_at = datetime('now')
            WHERE message_id = ?
            """,
            (job_id, job_status, message.message_id),
        )


def discord_inbound_state_path(runtime_state_path: str | Path) -> Path:
    target = Path(runtime_state_path).expanduser().resolve()
    return target.with_name(f"{target.stem}.discord-inbound.sqlite3")


def discord_inbound_table_text(config: DiscordInboundConfig) -> str:
    values = (
        "[discord_inbound]",
        "enabled = true",
        f"workspace = {json.dumps(str(config.workspace))}",
        f"allowed_senders = {json.dumps(list(config.allowed_senders))}",
        f"allowed_channels = {json.dumps(list(config.allowed_channels))}",
        f"token_env = {json.dumps(config.token_env)}",
        f"max_message_bytes = {config.max_message_bytes}",
        f"max_messages_per_run = {config.max_messages_per_run}",
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
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-discord-inbound-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path


def write_discord_inbound_settings(path: str | Path, config: DiscordInboundConfig) -> Path:
    target = Path(path).expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + discord_inbound_table_text(config))


def remove_discord_inbound_settings(path: str | Path) -> bool:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    value = _without_table(existing)
    _atomic_write(target, value + ("\n" if value else ""))
    return True


def discord_inbound_config_from_settings(settings: Mapping[str, object]) -> DiscordInboundConfig | None:
    raw = settings.get("discord_inbound")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    senders, channels, workspace = raw.get("allowed_senders"), raw.get("allowed_channels"), raw.get("workspace")
    if not isinstance(senders, list) or not all(isinstance(item, str) for item in senders):
        raise ValueError("Discord inbound allowed_senders is malformed")
    if not isinstance(channels, list) or not all(isinstance(item, str) for item in channels):
        raise ValueError("Discord inbound allowed_channels is malformed")
    if not isinstance(workspace, str):
        raise ValueError("Discord inbound workspace is malformed")
    return DiscordInboundConfig(
        workspace=Path(workspace), allowed_senders=tuple(senders), allowed_channels=tuple(channels),
        token_env=str(raw.get("token_env") or "DISCORD_BOT_TOKEN"),
        max_message_bytes=int(raw.get("max_message_bytes") or 12_000),
        max_messages_per_run=int(raw.get("max_messages_per_run") or 8),
    )


def discord_inbound_status(config: DiscordInboundConfig | None) -> Mapping[str, object]:
    if config is None:
        return {"enabled": False, "ready": False, "authority": "no_discord_inbound_channel", "next_action": "noruct channel discord-inbox-configure"}
    dependency_available = importlib.util.find_spec("discord") is not None
    token_available = bool(os.environ.get(config.token_env, "").strip())
    next_action = None
    if not dependency_available:
        next_action = "Install the optional discord.py==2.7.1 environment before running this channel."
    elif not token_available:
        next_action = f"Set {config.token_env} in the operator environment."
    return {
        "enabled": True,
        "ready": dependency_available and token_available,
        "dependency_available": dependency_available,
        "token_environment": config.token_env,
        "allowed_sender_count": len(config.allowed_senders),
        "allowed_channel_count": len(config.allowed_channels),
        "authority": "foreground_allowlisted_discord_text_receiver",
        "next_action": next_action,
    }


DiscordInboundDispatch = Callable[[DiscordInboundMessage], Awaitable[tuple[str, str]]]


def _message_from_discord(raw: Any, config: DiscordInboundConfig) -> DiscordInboundMessage | None:
    author = getattr(raw, "author", None)
    if author is None or bool(getattr(author, "bot", False)):
        return None
    message_id, sender_id = str(getattr(raw, "id", "")), str(getattr(author, "id", ""))
    channel_id = str(getattr(getattr(raw, "channel", None), "id", ""))
    text = getattr(raw, "content", "")
    if not _SNOWFLAKE.fullmatch(message_id) or sender_id not in config.allowed_senders or channel_id not in config.allowed_channels:
        return None
    if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text.encode("utf-8")) > config.max_message_bytes:
        return None
    return DiscordInboundMessage(message_id=message_id, sender_id=sender_id, channel_id=channel_id, text=text.strip())


async def run_discord_inbound_channel(
    config: DiscordInboundConfig,
    *,
    store: DiscordInboundStore,
    dispatch: DiscordInboundDispatch,
    maximum_seconds: float,
    maximum_messages: int | None = None,
    discord_module: Any | None = None,
    client_factory: Callable[[Any], Any] | None = None,
) -> DiscordInboundRunReceipt:
    """Run one bounded Discord Gateway session in the foreground.

    ``discord_module`` and ``client_factory`` exist solely to make the protocol
    boundary testable without a live token, network connection or dependency.
    """
    if not 1 <= maximum_seconds <= 3_600:
        raise ValueError("Discord inbound maximum seconds must be between 1 and 3600")
    limit = config.max_messages_per_run if maximum_messages is None else maximum_messages
    if not 1 <= limit <= config.max_messages_per_run:
        raise ValueError("Discord inbound maximum messages exceeds configured limit")
    token = os.environ.get(config.token_env, "").strip()
    if not token:
        raise ValueError("Discord inbound token environment variable is not set")
    if discord_module is None:
        try:
            import discord as discord_module  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ValueError("Discord inbound requires the optional discord.py==2.7.1 package") from exc
    intents = discord_module.Intents.default()
    intents.message_content = True
    intents.guild_messages = True
    intents.dm_messages = True
    client = client_factory(intents) if client_factory is not None else discord_module.Client(intents=intents)
    accepted = duplicate = ignored = 0
    dispatches: list[DiscordInboundDispatchReceipt] = []
    lock = asyncio.Lock()

    @client.event
    async def on_message(raw: Any) -> None:
        nonlocal accepted, duplicate, ignored
        message = _message_from_discord(raw, config)
        async with lock:
            if message is None or accepted >= limit:
                ignored += 1
                return
            if not store.claim(message):
                duplicate += 1
                return
            accepted += 1
            try:
                job_id, job_status = await dispatch(message)
                store.complete(message, job_id=job_id, job_status=job_status)
                dispatches.append(DiscordInboundDispatchReceipt(message.message_id, message.sender_id, message.channel_id, job_id, job_status, "DISPATCHED"))
            except Exception:
                dispatches.append(DiscordInboundDispatchReceipt(message.message_id, message.sender_id, message.channel_id, None, None, "FAILED"))
            if accepted >= limit:
                await client.close()

    async def close_after_deadline() -> None:
        await asyncio.sleep(maximum_seconds)
        await client.close()

    deadline = asyncio.create_task(close_after_deadline())
    try:
        await client.start(token)
    finally:
        deadline.cancel()
        try:
            await deadline
        except asyncio.CancelledError:
            pass
        if not bool(getattr(client, "is_closed", lambda: True)()):
            await client.close()
    return DiscordInboundRunReceipt(accepted, duplicate, ignored, tuple(dispatches))
