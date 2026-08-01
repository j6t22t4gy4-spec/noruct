"""Bounded foreground IMAP text intake for read-only Company Jobs.

The registered email adapter supplied the IMAP/SMTP protocol shape.  This
module only activates IMAP intake with Noruct-owned state and authority: each
foreground run reads at most a configured number of unseen, allowlisted plain
text messages, deduplicates by mailbox UID locally, and sends the body to the
ordinary read-only Company Job path supplied by the CLI.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol

from dynamic_firm.product.email_channel import _ENV, _HOST, _email
from dynamic_firm.product.inbound_channel import InboundMessage, InboundMessageStore


_HEADER = re.compile(r"(?m)^\[email_inbound\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")


@dataclass(frozen=True, slots=True)
class EmailInboundConfig:
    workspace: Path
    mailbox: str
    imap_host: str
    allowed_senders: tuple[str, ...]
    imap_port: int = 993
    password_env: str = "EMAIL_PASSWORD"
    username_env: str | None = None
    folder: str = "INBOX"
    max_message_bytes: int = 16_000
    max_messages_per_run: int = 4
    timeout_seconds: float = 30.0

    def validate(self) -> None:
        workspace = self.workspace.expanduser()
        if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Email inbound workspace must be an existing absolute non-symbolic-link directory")
        _email(self.mailbox, label="Email inbound mailbox")
        if not _HOST.fullmatch(self.imap_host.strip()):
            raise ValueError("IMAP host must be a DNS hostname or localhost")
        if not 1 <= self.imap_port <= 65535:
            raise ValueError("IMAP port must be between 1 and 65535")
        if not 1 <= len(self.allowed_senders) <= 64:
            raise ValueError("Email inbound requires 1 through 64 allowed sender addresses")
        normalized = tuple(_email(item, label="Email allowed sender") for item in self.allowed_senders)
        if len({item.lower() for item in normalized}) != len(normalized):
            raise ValueError("Email inbound allowed senders must not contain duplicates")
        if not _ENV.fullmatch(self.password_env):
            raise ValueError("Email inbound password environment variable name is invalid")
        if self.username_env is not None and not _ENV.fullmatch(self.username_env):
            raise ValueError("Email inbound username environment variable name is invalid")
        if not self.folder or any(item in self.folder for item in "\r\n\x00") or len(self.folder.encode("utf-8")) > 128:
            raise ValueError("Email inbound folder is invalid")
        if not 1 <= self.max_message_bytes <= 16_000 or not 1 <= self.max_messages_per_run <= 16:
            raise ValueError("Email inbound limits are invalid")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("Email inbound timeout must be between 1 and 60 seconds")


@dataclass(frozen=True, slots=True)
class EmailInboundMessage:
    uid: str
    sender: str
    text: str


@dataclass(frozen=True, slots=True)
class EmailInboundReceipt:
    accepted_count: int
    duplicate_count: int
    ignored_count: int
    dispatches: tuple[dict[str, str | None], ...]


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE.search(text, match.end())
    return (text[:match.start()] + (text[following.start():] if following else "")).strip()


def _atomic_write(path: Path, text: str) -> Path:
    target = path.expanduser().resolve(); target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, target)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
    return target


def write_email_inbound_settings(path: Path, config: EmailInboundConfig) -> Path:
    config.validate(); target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""; quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = ["[email_inbound]", "enabled = true", f"workspace = {quote(str(config.workspace.expanduser().resolve()))}", f"mailbox = {quote(config.mailbox)}", f"imap_host = {quote(config.imap_host.strip())}", f"imap_port = {config.imap_port}", "allowed_senders = [" + ", ".join(quote(item) for item in config.allowed_senders) + "]", f"password_env = {quote(config.password_env)}", f"folder = {quote(config.folder)}", f"max_message_bytes = {config.max_message_bytes}", f"max_messages_per_run = {config.max_messages_per_run}", f"timeout_seconds = {config.timeout_seconds:g}"]
    if config.username_env is not None: lines.append(f"username_env = {quote(config.username_env)}")
    return _atomic_write(target, (_without_table(existing) + "\n\n" if _without_table(existing) else "") + "\n".join((*lines, "")))


def remove_email_inbound_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file(): return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None: return False
    remainder = _without_table(existing); _atomic_write(target, remainder + ("\n" if remainder else "")); return True


def email_inbound_config_from_settings(settings: Mapping[str, object]) -> EmailInboundConfig | None:
    raw = settings.get("email_inbound")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True: return None
    workspace, mailbox, host, allowed = raw.get("workspace"), raw.get("mailbox"), raw.get("imap_host"), raw.get("allowed_senders")
    if not isinstance(workspace, str) or not isinstance(mailbox, str) or not isinstance(host, str) or not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed): raise ValueError("Email inbound configuration is malformed")
    password, username, port, maximum, per_run, timeout, folder = raw.get("password_env", "EMAIL_PASSWORD"), raw.get("username_env"), raw.get("imap_port", 993), raw.get("max_message_bytes", 16_000), raw.get("max_messages_per_run", 4), raw.get("timeout_seconds", 30.0), raw.get("folder", "INBOX")
    if not isinstance(password, str) or username is not None and not isinstance(username, str) or not isinstance(port, int) or isinstance(port, bool) or not isinstance(maximum, int) or isinstance(maximum, bool) or not isinstance(per_run, int) or isinstance(per_run, bool) or not isinstance(timeout, (int, float)) or not isinstance(folder, str): raise ValueError("Email inbound configuration is malformed")
    config = EmailInboundConfig(Path(workspace), mailbox.strip(), host.strip(), tuple(item.strip() for item in allowed), port, password.strip(), username.strip() if isinstance(username, str) and username.strip() else None, folder, maximum, per_run, float(timeout)); config.validate(); return config


def email_inbound_status(config: EmailInboundConfig | None) -> Mapping[str, object]:
    if config is None: return {"enabled": False, "ready": False, "authority": "no_email_inbound", "next_action": "noruct channel email-inbox-configure --workspace PATH --mailbox ADDRESS --imap-host HOST --allow-sender ADDRESS"}
    password_ready = bool(os.environ.get(config.password_env)); username_ready = config.username_env is None or bool(os.environ.get(config.username_env))
    return {"enabled": True, "ready": password_ready and username_ready, "workspace": str(config.workspace.expanduser().resolve()), "mailbox": config.mailbox, "imap_host": config.imap_host, "imap_port": config.imap_port, "folder": config.folder, "allowed_sender_count": len(config.allowed_senders), "password_environment": config.password_env, "username_environment": config.username_env, "authority": "foreground_allowlisted_imap_plaintext_read_only_jobs", "next_action": None if password_ready and username_ready else "Set the configured IMAP credential environment variable(s) in the operator shell."}


def _plain_text(message: Message) -> str | None:
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_type() != "text/plain" or "attachment" in str(part.get("Content-Disposition", "")).lower(): continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes): continue
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace").strip()
    return None


class _ImapClient(Protocol):
    def unread(self, limit: int) -> tuple[EmailInboundMessage, ...]: ...


class ImapUnreadClient:
    def __init__(self, config: EmailInboundConfig) -> None: self.config = config

    def unread(self, limit: int) -> tuple[EmailInboundMessage, ...]:
        password = os.environ.get(self.config.password_env)
        if not password: raise ValueError(f"Email inbound password environment variable is not set: {self.config.password_env}")
        username = os.environ.get(self.config.username_env) if self.config.username_env else self.config.mailbox
        if not username: raise ValueError(f"Email inbound username environment variable is not set: {self.config.username_env}")
        client = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port, timeout=self.config.timeout_seconds)
        values: list[EmailInboundMessage] = []
        try:
            client.login(username, password)
            status, _data = client.select(self.config.folder, readonly=False)
            if status != "OK": raise RuntimeError("IMAP folder could not be selected")
            status, data = client.uid("search", None, "UNSEEN")
            if status != "OK" or not data or not data[0]: return ()
            for raw_uid in data[0].split()[:limit]:
                uid = raw_uid.decode("ascii", errors="ignore")
                if not uid or not uid.isdigit(): continue
                status, fetched = client.uid("fetch", raw_uid, "(BODY.PEEK[])")
                raw = fetched[0][1] if status == "OK" and fetched and isinstance(fetched[0], tuple) and isinstance(fetched[0][1], bytes) else None
                if raw is None: continue
                parsed = email.message_from_bytes(raw)
                _display, sender = parseaddr(str(parsed.get("From", "")))
                text = _plain_text(parsed)
                # A malformed/unallowed/attachment-only unseen message should not keep
                # being parsed on every foreground run; mark it consumed locally.
                client.uid("store", raw_uid, "+FLAGS.SILENT", "(\\Seen)")
                if not sender or text is None: continue
                values.append(EmailInboundMessage(uid, sender.lower(), text))
        except (OSError, imaplib.IMAP4.error) as exc:
            raise RuntimeError("IMAP inbound read failed") from exc
        finally:
            try: client.logout()
            except (OSError, imaplib.IMAP4.error): pass
        return tuple(values)


EmailInboundDispatch = Callable[[EmailInboundMessage], Awaitable[tuple[str, str]]]
async def run_email_inbound(config: EmailInboundConfig, *, store: InboundMessageStore, dispatch: EmailInboundDispatch, maximum_seconds: float, maximum_messages: int | None = None, client: _ImapClient | None = None) -> EmailInboundReceipt:
    config.validate()
    if not 1 <= maximum_seconds <= 3600: raise ValueError("Email inbound maximum seconds must be between 1 and 3600")
    limit = config.max_messages_per_run if maximum_messages is None else maximum_messages
    if not 1 <= limit <= config.max_messages_per_run: raise ValueError("Email inbound maximum messages exceeds configured limit")
    messages = await asyncio.wait_for(asyncio.to_thread((client or ImapUnreadClient(config)).unread, limit * 4), timeout=maximum_seconds)
    accepted = duplicate = ignored = 0; dispatches: list[dict[str, str | None]] = []; allowed = {item.lower() for item in config.allowed_senders}
    for incoming in messages:
        if accepted >= limit: break
        if incoming.sender.lower() not in allowed or not incoming.text.strip() or "\x00" in incoming.text or len(incoming.text.encode("utf-8")) > config.max_message_bytes: ignored += 1; continue
        message = InboundMessage(source_id=f"email:{config.mailbox.lower()}", message_id=incoming.uid, sender=incoming.sender.lower(), text=incoming.text.strip())
        if not store.claim(message): duplicate += 1; continue
        accepted += 1
        try:
            job_id, status = await dispatch(incoming); store.complete(message, job_id=job_id, job_status=status); dispatches.append({"message_id": incoming.uid, "job_id": job_id, "job_status": status, "outcome": "DISPATCHED"})
        except Exception: dispatches.append({"message_id": incoming.uid, "job_id": None, "job_status": None, "outcome": "FAILED"})
    return EmailInboundReceipt(accepted, duplicate, ignored, tuple(dispatches))
