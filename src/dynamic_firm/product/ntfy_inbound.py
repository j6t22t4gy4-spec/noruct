"""Bounded foreground ntfy JSON-stream intake for read-only Company Jobs."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dynamic_firm.product.inbound_channel import InboundMessage, InboundMessageStore
from dynamic_firm.product.ntfy_channel import _TOPIC, _authorization_header, _validated_server_url

_HEADER = re.compile(r"(?m)^\[ntfy_inbound\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class NtfyInboundConfig:
    workspace: Path
    topic: str
    token_env: str | None = None
    server_url: str = "https://ntfy.sh"
    max_message_bytes: int = 4_000
    max_messages_per_run: int = 4

    def validate(self) -> None:
        workspace = self.workspace.expanduser()
        if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("ntfy inbound workspace must be an existing absolute non-symbolic-link directory")
        if not _TOPIC.fullmatch(self.topic):
            raise ValueError("ntfy inbound topic is invalid")
        if self.token_env is not None and not _ENV.fullmatch(self.token_env):
            raise ValueError("ntfy inbound token environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 16_000 or not 1 <= self.max_messages_per_run <= 16:
            raise ValueError("ntfy inbound limits are invalid")
        _validated_server_url(self.server_url)

    @property
    def normalized_server_url(self) -> str:
        return _validated_server_url(self.server_url)


@dataclass(frozen=True, slots=True)
class NtfyInboundMessage:
    message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class NtfyInboundReceipt:
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


def write_ntfy_inbound_settings(path: Path, config: NtfyInboundConfig) -> Path:
    config.validate(); target = path.expanduser().resolve(); target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""; q = json.dumps
    lines = ["[ntfy_inbound]", "enabled = true", f"workspace = {q(str(config.workspace.resolve()))}", f"topic = {q(config.topic)}", f"server_url = {q(config.normalized_server_url)}", f"max_message_bytes = {config.max_message_bytes}", f"max_messages_per_run = {config.max_messages_per_run}"]
    if config.token_env: lines.append(f"token_env = {q(config.token_env)}")
    temporary = target.with_name(f".{target.name}.ntfy.tmp")
    temporary.write_text((_without_table(existing) + "\n\n" if _without_table(existing) else "") + "\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600); temporary.replace(target); return target


def remove_ntfy_inbound_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file() or _HEADER.search(target.read_text(encoding="utf-8")) is None: return False
    text = _without_table(target.read_text(encoding="utf-8")); target.write_text(text + ("\n" if text else ""), encoding="utf-8"); return True


def ntfy_inbound_config_from_settings(settings: Mapping[str, object]) -> NtfyInboundConfig | None:
    raw = settings.get("ntfy_inbound")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True: return None
    workspace, topic = raw.get("workspace"), raw.get("topic")
    if not isinstance(workspace, str) or not isinstance(topic, str): raise ValueError("ntfy inbound configuration is malformed")
    config = NtfyInboundConfig(Path(workspace), topic.strip(), str(raw["token_env"]).strip() if isinstance(raw.get("token_env"), str) and str(raw["token_env"]).strip() else None, str(raw.get("server_url") or "https://ntfy.sh"), int(raw.get("max_message_bytes") or 4_000), int(raw.get("max_messages_per_run") or 4))
    config.validate(); return config


def ntfy_inbound_status(config: NtfyInboundConfig | None) -> Mapping[str, object]:
    if config is None: return {"enabled": False, "ready": False, "authority": "no_ntfy_inbound", "next_action": "noruct channel ntfy-inbox-configure --workspace PATH --topic PRIVATE_TOPIC"}
    return {"enabled": True, "ready": config.token_env is None or bool(os.environ.get(config.token_env)), "workspace": str(config.workspace.resolve()), "topic": config.topic, "token_environment": config.token_env, "authority": "foreground_ntfy_json_stream_topic_trusted_read_only_jobs", "next_action": None if config.token_env is None or os.environ.get(config.token_env) else f"Set {config.token_env} in the operator shell."}


class NtfyJsonClient:
    def __init__(self, config: NtfyInboundConfig) -> None: self.config = config
    def read(self, seconds: float, limit: int) -> tuple[Mapping[str, object], ...]:
        headers = {"Accept": "application/x-ndjson"}
        if self.config.token_env:
            token = os.environ.get(self.config.token_env)
            if not token: raise ValueError(f"ntfy token environment variable is not set: {self.config.token_env}")
            headers["Authorization"] = _authorization_header(token)
        request = Request(f"{self.config.normalized_server_url}/{self.config.topic}/json?poll=false", headers=headers, method="GET")
        values: list[Mapping[str, object]] = []; deadline = time.monotonic() + seconds
        try:
            with urlopen(request, timeout=min(seconds + 2, 60)) as response:
                for raw in response:
                    if time.monotonic() >= deadline or len(values) >= limit: break
                    try: value = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError): continue
                    if isinstance(value, Mapping): values.append(value)
        except (HTTPError, URLError, OSError) as exc: raise RuntimeError("ntfy inbound stream failed") from exc
        return tuple(values)


NtfyDispatch = Callable[[NtfyInboundMessage], Awaitable[tuple[str, str]]]
async def run_ntfy_inbound(config: NtfyInboundConfig, *, store: InboundMessageStore, dispatch: NtfyDispatch, maximum_seconds: float, maximum_messages: int | None = None, client: NtfyJsonClient | None = None) -> NtfyInboundReceipt:
    if not 1 <= maximum_seconds <= 3600: raise ValueError("ntfy inbound maximum seconds must be between 1 and 3600")
    limit = config.max_messages_per_run if maximum_messages is None else maximum_messages
    if not 1 <= limit <= config.max_messages_per_run: raise ValueError("ntfy inbound maximum messages exceeds configured limit")
    events = await asyncio.to_thread((client or NtfyJsonClient(config)).read, maximum_seconds, limit * 4)
    accepted = duplicate = ignored = 0; dispatches: list[dict[str, str | None]] = []
    for event in events:
        if accepted >= limit: break
        if event.get("event") != "message" or event.get("topic") != config.topic: ignored += 1; continue
        identifier, text = event.get("id"), event.get("message")
        if not isinstance(identifier, str) or not identifier or not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > config.max_message_bytes: ignored += 1; continue
        message = InboundMessage(source_id=f"ntfy:{config.topic}", message_id=identifier, sender=config.topic, text=text.strip())
        if not store.claim(message): duplicate += 1; continue
        accepted += 1
        try:
            job_id, status = await dispatch(NtfyInboundMessage(identifier, text.strip())); store.complete(message, job_id=job_id, job_status=status); dispatches.append({"message_id": identifier, "job_id": job_id, "job_status": status, "outcome": "DISPATCHED"})
        except Exception: dispatches.append({"message_id": identifier, "job_id": None, "job_status": None, "outcome": "FAILED"})
    return NtfyInboundReceipt(accepted, duplicate, ignored, tuple(dispatches))
