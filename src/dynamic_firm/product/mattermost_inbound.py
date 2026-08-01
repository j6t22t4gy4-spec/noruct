"""Bounded foreground Mattermost v4 plaintext-post intake.

This is deliberately not a Mattermost gateway.  It reads plaintext posts from
one configured channel, only accepts explicitly allowlisted user IDs, and
turns each accepted post into a read-only Company Job.  Its first successful
poll only establishes the cursor: historical posts are never dispatched.
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
from dynamic_firm.product.mattermost_channel import _CHANNEL, _ENV, _atomic_write, _base_url, _without_table


_HEADER = re.compile(r"(?m)^\[mattermost_inbound\][ \t]*(?:\r?\n|$)")
_POST = re.compile(r"^[A-Za-z0-9]{1,128}$")
_MAX_RESPONSE_BYTES = 256_000


@dataclass(frozen=True, slots=True)
class MattermostInboundConfig:
    workspace: Path
    base_url: str
    channel_id: str
    allowed_senders: tuple[str, ...]
    token_env: str = "MATTERMOST_TOKEN"
    max_message_bytes: int = 12_000
    max_messages_per_run: int = 8
    timeout_seconds: float = 20.0

    def validate(self) -> None:
        workspace = self.workspace.expanduser()
        if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Mattermost inbound workspace must be an existing absolute non-symbolic-link directory")
        _base_url(self.base_url)
        if not _CHANNEL.fullmatch(self.channel_id):
            raise ValueError("Mattermost inbound channel is invalid")
        if not self.allowed_senders or len(set(self.allowed_senders)) != len(self.allowed_senders) or any(not _POST.fullmatch(sender) for sender in self.allowed_senders):
            raise ValueError("Mattermost inbound sender allowlist is invalid")
        if not _ENV.fullmatch(self.token_env):
            raise ValueError("Mattermost inbound token environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 16_000 or not 1 <= self.max_messages_per_run <= 32:
            raise ValueError("Mattermost inbound limits are invalid")
        if not 1 <= self.timeout_seconds <= 45:
            raise ValueError("Mattermost inbound timeout must be between 1 and 45 seconds")

    @property
    def normalized_base_url(self) -> str:
        return _base_url(self.base_url)


@dataclass(frozen=True, slots=True)
class MattermostInboundMessage:
    post_id: str
    sender_id: str
    channel_id: str
    text: str


@dataclass(frozen=True, slots=True)
class MattermostInboundReceipt:
    primed: bool
    accepted_count: int
    duplicate_count: int
    ignored_count: int
    dispatches: tuple[dict[str, str | None], ...]

    def to_dict(self) -> Mapping[str, object]:
        return asdict(self)


def mattermost_inbound_table_text(config: MattermostInboundConfig) -> str:
    config.validate()
    return "\n".join((
        "[mattermost_inbound]", "enabled = true", f"workspace = {json.dumps(str(config.workspace.resolve()))}",
        f"base_url = {json.dumps(config.normalized_base_url)}", f"channel_id = {json.dumps(config.channel_id)}",
        f"allowed_senders = {json.dumps(list(config.allowed_senders))}", f"token_env = {json.dumps(config.token_env)}",
        f"max_message_bytes = {config.max_message_bytes}", f"max_messages_per_run = {config.max_messages_per_run}",
        f"timeout_seconds = {config.timeout_seconds:g}", "",
    ))


def write_mattermost_inbound_settings(path: Path, config: MattermostInboundConfig) -> Path:
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    return _atomic_write(target, (_without_table(existing) + "\n\n" if _without_table(existing) else "") + mattermost_inbound_table_text(config))


def remove_mattermost_inbound_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file(): return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None: return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def mattermost_inbound_config_from_settings(settings: Mapping[str, object]) -> MattermostInboundConfig | None:
    raw = settings.get("mattermost_inbound")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True: return None
    workspace, base_url, channel_id, senders = raw.get("workspace"), raw.get("base_url"), raw.get("channel_id"), raw.get("allowed_senders")
    if not isinstance(workspace, str) or not isinstance(base_url, str) or not isinstance(channel_id, str) or not isinstance(senders, list):
        raise ValueError("Mattermost inbound configuration is malformed")
    config = MattermostInboundConfig(Path(workspace), base_url, channel_id, tuple(str(item) for item in senders), str(raw.get("token_env") or "MATTERMOST_TOKEN"), int(raw.get("max_message_bytes") or 12_000), int(raw.get("max_messages_per_run") or 8), float(raw.get("timeout_seconds") or 20.0))
    config.validate()
    return config


def mattermost_inbound_status(config: MattermostInboundConfig | None) -> Mapping[str, object]:
    if config is None:
        return {"enabled": False, "ready": False, "authority": "no_mattermost_inbound", "next_action": "noruct channel mattermost-inbox-configure --workspace PATH --base-url HTTPS_URL --channel-id ID --allow-sender USER_ID"}
    return {"enabled": True, "ready": bool(os.environ.get(config.token_env)), "workspace": str(config.workspace.resolve()), "base_url": config.normalized_base_url, "channel_id": config.channel_id, "allowed_senders": list(config.allowed_senders), "token_environment": config.token_env, "authority": "foreground_mattermost_rest_allowlisted_plaintext_posts_first_poll_primes_cursor", "next_action": None if os.environ.get(config.token_env) else f"Set {config.token_env} in the operator shell."}


class MattermostInboundCursorStore:
    def __init__(self, path: Path) -> None:
        target = path.expanduser().resolve(); target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(target), isolation_level=None)
        self.connection.execute("CREATE TABLE IF NOT EXISTS mattermost_inbound_cursor (id INTEGER PRIMARY KEY CHECK (id = 1), since INTEGER NOT NULL)")

    def __enter__(self) -> "MattermostInboundCursorStore": return self
    def __exit__(self, *_: object) -> None: self.connection.close()
    def get(self) -> int | None:
        row = self.connection.execute("SELECT since FROM mattermost_inbound_cursor WHERE id = 1").fetchone()
        return int(row[0]) if row else None
    def set(self, value: int) -> None:
        if not isinstance(value, int) or value < 0: raise ValueError("Mattermost inbound cursor is invalid")
        self.connection.execute("INSERT INTO mattermost_inbound_cursor(id,since) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET since=excluded.since", (value,))


def mattermost_inbound_state_path(runtime_state_path: str | Path) -> Path:
    target = Path(runtime_state_path).expanduser().resolve()
    return target.with_name(f"{target.stem}.mattermost-inbound.sqlite3")


class MattermostPostsClient:
    def __init__(self, config: MattermostInboundConfig) -> None: self.config = config

    def read(self, *, since: int | None) -> Mapping[str, object]:
        token = os.environ.get(self.config.token_env)
        if not token: raise ValueError(f"Mattermost token environment variable is not set: {self.config.token_env}")
        query = urlencode({"since": str(since or 0), "per_page": str(min(32, self.config.max_messages_per_run * 4))})
        request = Request(f"{self.config.normalized_base_url}/api/v4/channels/{self.config.channel_id}/posts?{query}", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, method="GET")
        with urlopen(request, timeout=self.config.timeout_seconds) as response: raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES: raise RuntimeError("Mattermost posts response exceeds the configured bound")
        try: value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise RuntimeError("Mattermost posts response is not valid JSON") from exc
        if not isinstance(value, Mapping): raise RuntimeError("Mattermost posts response is invalid")
        return value


MattermostDispatch = Callable[[MattermostInboundMessage], Awaitable[tuple[str, str]]]


async def run_mattermost_inbound(config: MattermostInboundConfig, *, cursor_store: MattermostInboundCursorStore, message_store: InboundMessageStore, dispatch: MattermostDispatch, client: MattermostPostsClient | None = None, maximum_messages: int | None = None) -> MattermostInboundReceipt:
    config.validate()
    limit = config.max_messages_per_run if maximum_messages is None else maximum_messages
    if not 1 <= limit <= config.max_messages_per_run: raise ValueError("Mattermost inbound maximum messages exceeds configured limit")
    since = cursor_store.get()
    payload = await asyncio.to_thread((client or MattermostPostsClient(config)).read, since=since)
    raw_posts = payload.get("posts")
    posts = [item for item in raw_posts.values() if isinstance(item, Mapping)] if isinstance(raw_posts, Mapping) else []
    newest = max((int(item["create_at"]) for item in posts if isinstance(item.get("create_at"), int)), default=since or 0)
    if since is None:
        cursor_store.set(newest)
        return MattermostInboundReceipt(True, 0, 0, 0, ())
    cursor_store.set(max(newest, since))
    accepted = duplicates = ignored = 0
    dispatches: list[dict[str, str | None]] = []
    for post in sorted(posts, key=lambda item: int(item.get("create_at", 0))):
        if accepted >= limit: break
        post_id, sender, channel_id, text = post.get("id"), post.get("user_id"), post.get("channel_id"), post.get("message")
        if not isinstance(post_id, str) or not _POST.fullmatch(post_id) or not isinstance(sender, str) or sender not in config.allowed_senders or channel_id != config.channel_id or not isinstance(text, str) or not text.strip() or len(text.encode()) > config.max_message_bytes:
            ignored += 1; continue
        message = InboundMessage(source_id=f"mattermost:{config.channel_id}", message_id=post_id, sender=sender, text=text.strip())
        if not message_store.claim(message):
            duplicates += 1; continue
        accepted += 1
        try:
            job_id, status = await dispatch(MattermostInboundMessage(post_id, sender, config.channel_id, text.strip()))
            message_store.complete(message, job_id=job_id, job_status=status)
            dispatches.append({"post_id": post_id, "job_id": job_id, "job_status": status, "outcome": "DISPATCHED"})
        except Exception:
            dispatches.append({"post_id": post_id, "job_id": None, "job_status": None, "outcome": "FAILED"})
    return MattermostInboundReceipt(False, accepted, duplicates, ignored, tuple(dispatches))
