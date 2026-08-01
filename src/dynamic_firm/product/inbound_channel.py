"""Foreground, user-managed inbound message bridge for ordinary Company Jobs.

An adapter selected by the operator owns the external platform, login and
webhook/polling implementation.  It writes one bounded JSON object per stdout
line.  Noruct accepts only messages from explicitly configured opaque sender
identities, deduplicates each source/message identity locally, and hands an
accepted text to the ordinary read-only Company Job path supplied by the CLI.

There is intentionally no platform SDK, gateway state, credential persistence,
reply delivery, detached daemon or automatic start in this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output


INBOUND_CHANNEL_SCHEMA = "noruct.user-managed-inbound-channel.v1"
_HEADER = re.compile(r"(?m)^\[inbound_channel\][ \t]*(?:\r?\n|$)")
_TABLE_HEADER = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_MAX_STDERR_BYTES = 4_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True, slots=True)
class InboundChannelConfig:
    source_id: str
    command: Path
    workspace: Path
    allowed_senders: tuple[str, ...]
    args: tuple[str, ...] = ()
    environment_names: tuple[str, ...] = ()
    max_message_bytes: int = 4_000
    max_messages_per_run: int = 4

    def validate(self) -> None:
        if not _IDENTITY.fullmatch(self.source_id):
            raise ValueError("Inbound channel source_id is invalid")
        command = self.command.expanduser()
        if command.is_symlink():
            raise ValueError("Inbound channel command must be an absolute regular non-symbolic-link executable")
        command = command.resolve()
        if not command.is_absolute() or not command.is_file() or not os.access(command, os.X_OK):
            raise ValueError("Inbound channel command must be an absolute regular non-symbolic-link executable")
        workspace = self.workspace.expanduser()
        if workspace.is_symlink():
            raise ValueError("Inbound channel workspace must be an absolute non-symbolic-link directory")
        workspace = workspace.resolve()
        if not workspace.is_absolute() or not workspace.is_dir():
            raise ValueError("Inbound channel workspace must be an absolute non-symbolic-link directory")
        if not 1 <= len(self.allowed_senders) <= 64 or len(set(self.allowed_senders)) != len(self.allowed_senders):
            raise ValueError("Inbound channel requires 1 through 64 unique allowed sender identities")
        if any(not _IDENTITY.fullmatch(item) for item in self.allowed_senders):
            raise ValueError("Inbound channel allowed sender identity is invalid")
        if len(self.args) > 12:
            raise ValueError("Inbound channel accepts at most 12 fixed non-secret arguments")
        if any(not isinstance(item, str) or not item or "\x00" in item or len(item.encode("utf-8")) > 512 for item in self.args):
            raise ValueError("Inbound channel argument must be a bounded non-empty string")
        if len(self.environment_names) > 12 or len(set(self.environment_names)) != len(self.environment_names):
            raise ValueError("Inbound channel environment names must be unique and bounded")
        if any(not _ENVIRONMENT_NAME.fullmatch(item) for item in self.environment_names):
            raise ValueError("Inbound channel environment name is invalid")
        if not 1 <= self.max_message_bytes <= 16_000:
            raise ValueError("Inbound channel message limit must be between 1 and 16000 bytes")
        if not 1 <= self.max_messages_per_run <= 16:
            raise ValueError("Inbound channel per-run message limit must be between 1 and 16")


@dataclass(frozen=True, slots=True)
class InboundMessage:
    source_id: str
    message_id: str
    sender: str
    text: str

    @classmethod
    def parse(cls, raw: object, *, config: InboundChannelConfig) -> "InboundMessage":
        if not isinstance(raw, Mapping):
            raise ValueError("Inbound channel line must be a JSON object")
        if raw.get("schema") != INBOUND_CHANNEL_SCHEMA:
            raise ValueError("Inbound channel schema is not accepted")
        source_id = raw.get("source_id")
        message_id = raw.get("message_id")
        sender = raw.get("sender")
        text = raw.get("text")
        if source_id != config.source_id:
            raise ValueError("Inbound channel source identity does not match the configured source")
        if not isinstance(message_id, str) or not _IDENTITY.fullmatch(message_id):
            raise ValueError("Inbound channel message identity is invalid")
        if not isinstance(sender, str) or not _IDENTITY.fullmatch(sender):
            raise ValueError("Inbound channel sender identity is invalid")
        if sender not in config.allowed_senders:
            raise ValueError("Inbound channel sender is not allowed")
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise ValueError("Inbound channel message text is empty or invalid")
        if len(text.encode("utf-8")) > config.max_message_bytes:
            raise ValueError("Inbound channel message exceeds the configured byte limit")
        return cls(source_id=source_id, message_id=message_id, sender=sender, text=text.strip())


@dataclass(frozen=True, slots=True)
class InboundDispatchReceipt:
    message_id: str
    sender: str
    outcome: str
    job_id: str | None = None
    job_status: str | None = None


@dataclass(frozen=True, slots=True)
class InboundConsumeResult:
    source_id: str
    command: str
    maximum_seconds: float
    maximum_messages: int
    received_count: int
    accepted_count: int
    rejected_count: int
    duplicate_count: int
    dispatches: tuple[InboundDispatchReceipt, ...]
    process_exit_code: int | None
    timed_out: bool
    process_output: str

    def to_dict(self) -> Mapping[str, object]:
        return asdict(self)


class InboundMessageStore:
    """Content-minimizing, at-most-once claim store for inbound messages."""

    def __init__(self, path: Path) -> None:
        target = path.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(target)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound_channel_messages (
              source_id TEXT NOT NULL,
              message_id TEXT NOT NULL,
              sender TEXT NOT NULL,
              content_sha256 TEXT NOT NULL,
              status TEXT NOT NULL,
              job_id TEXT,
              job_status TEXT,
              claimed_at TEXT NOT NULL,
              completed_at TEXT,
              PRIMARY KEY (source_id, message_id)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "InboundMessageStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def claim(self, message: InboundMessage) -> bool:
        digest = hashlib.sha256(message.text.encode("utf-8")).hexdigest()
        try:
            self._conn.execute(
                """INSERT INTO inbound_channel_messages (
                     source_id, message_id, sender, content_sha256, status, claimed_at
                   ) VALUES (?, ?, ?, ?, 'CLAIMED', ?)""",
                (message.source_id, message.message_id, message.sender, digest, _utc_now()),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def complete(self, message: InboundMessage, *, job_id: str, job_status: str) -> None:
        self._conn.execute(
            """UPDATE inbound_channel_messages
               SET status = 'COMPLETED', job_id = ?, job_status = ?, completed_at = ?
               WHERE source_id = ? AND message_id = ?""",
            (job_id, job_status, _utc_now(), message.source_id, message.message_id),
        )
        self._conn.commit()


def inbound_state_path(company_state_path: Path) -> Path:
    target = company_state_path.expanduser().resolve()
    return target.with_name(f"{target.stem}.inbound-channel.sqlite3")


def inbound_channel_table_text(config: InboundChannelConfig) -> str:
    config.validate()
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    return "\n".join((
        "[inbound_channel]",
        "enabled = true",
        f"source_id = {quote(config.source_id)}",
        f"command = {quote(str(config.command.expanduser().resolve()))}",
        f"workspace = {quote(str(config.workspace.expanduser().resolve()))}",
        "allowed_senders = [" + ", ".join(quote(item) for item in config.allowed_senders) + "]",
        "args = [" + ", ".join(quote(item) for item in config.args) + "]",
        "environment = [" + ", ".join(quote(item) for item in config.environment_names) + "]",
        f"max_message_bytes = {config.max_message_bytes}",
        f"max_messages_per_run = {config.max_messages_per_run}",
        "",
    ))


def _extract_table(text: str) -> str | None:
    match = _HEADER.search(text)
    if match is None:
        return None
    following = _TABLE_HEADER.search(text, match.end())
    return text[match.start():following.start() if following else len(text)].strip() + "\n"


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE_HEADER.search(text, match.end())
    return (text[:match.start()] + text[following.start() if following else len(text):]).strip()


def _atomic_write(path: Path, value: str) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def write_inbound_channel_settings(path: Path, config: InboundChannelConfig) -> Path:
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + inbound_channel_table_text(config))


def remove_inbound_channel_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _extract_table(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def inbound_channel_config_from_settings(settings: Mapping[str, object]) -> InboundChannelConfig | None:
    raw = settings.get("inbound_channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    source_id = raw.get("source_id")
    command = raw.get("command")
    workspace = raw.get("workspace")
    allowed_senders = raw.get("allowed_senders")
    args = raw.get("args", ())
    environment = raw.get("environment", ())
    if not all(isinstance(value, str) for value in (source_id, command, workspace)):
        raise ValueError("Inbound channel configuration is malformed")
    if not all(isinstance(value, list) for value in (allowed_senders, args, environment)):
        raise ValueError("Inbound channel configuration is malformed")
    config = InboundChannelConfig(
        source_id=source_id,
        command=Path(command),
        workspace=Path(workspace),
        allowed_senders=tuple(str(item) for item in allowed_senders),
        args=tuple(str(item) for item in args),
        environment_names=tuple(str(item) for item in environment),
        max_message_bytes=int(raw.get("max_message_bytes", 4_000)),
        max_messages_per_run=int(raw.get("max_messages_per_run", 4)),
    )
    config.validate()
    return config


def inbound_channel_status(config: InboundChannelConfig | None) -> Mapping[str, object]:
    if config is None:
        return {
            "enabled": False,
            "authority": "no_external_inbound_channel",
            "next_action": "noruct channel inbox-configure",
        }
    missing = tuple(name for name in config.environment_names if name not in os.environ)
    return {
        "enabled": True,
        "source_id": config.source_id,
        "command": str(config.command.expanduser().resolve()),
        "workspace": str(config.workspace.expanduser().resolve()),
        "allowed_sender_count": len(config.allowed_senders),
        "environment_names": list(config.environment_names),
        "missing_environment_names": list(missing),
        "ready": not missing,
        "lifecycle": "foreground_operator_confirmed_only",
        "automatic_start": False,
        "automatic_reply_delivery": False,
        "next_action": None if not missing else "Set each named inbound channel environment variable in the operator shell.",
    }


def _safe_environment(config: InboundChannelConfig) -> Mapping[str, str]:
    missing = tuple(name for name in config.environment_names if name not in os.environ)
    if missing:
        raise ValueError("Inbound channel environment variable is not set: " + ", ".join(missing))
    return {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"} or key in config.environment_names
    }


async def consume_inbound_channel(
    config: InboundChannelConfig,
    *,
    store: InboundMessageStore,
    maximum_seconds: float,
    maximum_messages: int,
    dispatch: Callable[[InboundMessage], Awaitable[tuple[str, str]]],
) -> InboundConsumeResult:
    """Run an operator-selected adapter in the foreground and dispatch its lines.

    The callback returns ``(job_id, terminal_status)`` after it has gone
    through Noruct's ordinary Company Job execution path.  The raw text stays
    out of this module's receipt and SQLite store; only its SHA-256 is stored.
    """

    config.validate()
    if not 1 <= maximum_seconds <= 3_600:
        raise ValueError("Inbound channel maximum seconds must be between 1 and 3600")
    if not 1 <= maximum_messages <= config.max_messages_per_run:
        raise ValueError("Inbound channel maximum messages must be within the configured per-run limit")
    command = str(config.command.expanduser().resolve())
    process = await asyncio.create_subprocess_exec(
        command,
        *config.args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=tempfile.gettempdir(),
        env=dict(_safe_environment(config)),
        limit=config.max_message_bytes + 2_048,
    )
    assert process.stdout is not None and process.stderr is not None

    async def capture_stderr() -> str:
        value = await process.stderr.read(_MAX_STDERR_BYTES + 1)
        return redact_terminal_output(value[:_MAX_STDERR_BYTES].decode("utf-8", errors="replace"), force=True).strip()

    stderr_task = asyncio.create_task(capture_stderr())
    started = time.monotonic()
    received = accepted = rejected = duplicates = 0
    dispatches: list[InboundDispatchReceipt] = []
    timed_out = False
    try:
        while accepted < maximum_messages:
            remaining = maximum_seconds - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                break
            try:
                raw = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            except ValueError:
                rejected += 1
                break
            if not raw:
                break
            if len(raw) > config.max_message_bytes + 2_048 or not raw.endswith(b"\n"):
                rejected += 1
                continue
            received += 1
            try:
                decoded = json.loads(raw.decode("utf-8"))
                message = InboundMessage.parse(decoded, config=config)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                rejected += 1
                continue
            if not store.claim(message):
                duplicates += 1
                dispatches.append(InboundDispatchReceipt(message.message_id, message.sender, "duplicate"))
                continue
            accepted += 1
            try:
                job_id, job_status = await dispatch(message)
            except Exception:
                dispatches.append(InboundDispatchReceipt(message.message_id, message.sender, "dispatch_failed"))
                continue
            store.complete(message, job_id=job_id, job_status=job_status)
            dispatches.append(InboundDispatchReceipt(message.message_id, message.sender, "dispatched", job_id, job_status))
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        output = await stderr_task
    return InboundConsumeResult(
        source_id=config.source_id,
        command=command,
        maximum_seconds=maximum_seconds,
        maximum_messages=maximum_messages,
        received_count=received,
        accepted_count=accepted,
        rejected_count=rejected,
        duplicate_count=duplicates,
        dispatches=tuple(dispatches),
        process_exit_code=process.returncode,
        timed_out=timed_out,
        process_output=output,
    )
