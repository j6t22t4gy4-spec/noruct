"""One operator-confirmed Discord incoming-webhook delivery path."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output

_HEADER = re.compile(r"(?m)^\[discord_channel\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class DiscordChannelConfig:
    webhook_env: str = "DISCORD_WEBHOOK_URL"
    max_message_bytes: int = 8_000
    timeout_seconds: float = 15.0

    def validate(self) -> None:
        if not _ENV.fullmatch(self.webhook_env):
            raise ValueError("Discord webhook environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 8_000:
            raise ValueError("Discord message limit must be between 1 and 8000 bytes")
        if not 1 <= self.timeout_seconds <= 45:
            raise ValueError("Discord timeout must be between 1 and 45 seconds")


@dataclass(frozen=True, slots=True)
class DiscordDeliveryResult:
    delivered: bool
    message_bytes: int
    automatic_delivery: bool
    output: str

    def to_dict(self) -> Mapping[str, object]:
        return asdict(self)


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE.search(text, match.end())
    return (text[:match.start()] + (text[following.start():] if following else "")).strip()


def _write(path: Path, text: str) -> Path:
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


def write_discord_channel_settings(path: Path, config: DiscordChannelConfig) -> Path:
    config.validate(); target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    table = "\n".join(("[discord_channel]", "enabled = true", f"webhook_env = {quote(config.webhook_env)}", f"max_message_bytes = {config.max_message_bytes}", f"timeout_seconds = {config.timeout_seconds:g}", ""))
    remainder = _without_table(existing)
    return _write(target, (remainder + "\n\n" if remainder else "") + table)


def remove_discord_channel_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file(): return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None: return False
    remainder = _without_table(existing); _write(target, remainder + ("\n" if remainder else "")); return True


def discord_channel_config_from_settings(settings: Mapping[str, Any]) -> DiscordChannelConfig | None:
    raw = settings.get("discord_channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True: return None
    webhook_env, maximum, timeout = raw.get("webhook_env", "DISCORD_WEBHOOK_URL"), raw.get("max_message_bytes", 8_000), raw.get("timeout_seconds", 15.0)
    if not isinstance(webhook_env, str) or not isinstance(maximum, int) or isinstance(maximum, bool) or not isinstance(timeout, (int, float)): raise ValueError("Discord channel configuration is malformed")
    config = DiscordChannelConfig(webhook_env=webhook_env.strip(), max_message_bytes=maximum, timeout_seconds=float(timeout)); config.validate(); return config


def discord_channel_status(config: DiscordChannelConfig | None) -> Mapping[str, object]:
    if config is None: return {"enabled": False, "authority": "no_discord_channel", "next_action": "noruct channel discord-configure"}
    return {"enabled": True, "webhook_environment": config.webhook_env, "ready": bool(os.environ.get(config.webhook_env)), "automatic_delivery": False, "authority": "operator_confirmed_single_discord_webhook_not_an_employee_tool", "next_action": None if os.environ.get(config.webhook_env) else f"Set {config.webhook_env} in the operator shell."}


def deliver_discord_message(config: DiscordChannelConfig, *, message: str) -> DiscordDeliveryResult:
    config.validate()
    if not isinstance(message, str) or not message.strip() or "\x00" in message: raise ValueError("Discord message must be non-empty text")
    content = message.strip(); content_bytes = content.encode("utf-8")
    if len(content_bytes) > config.max_message_bytes or len(content) > 2_000: raise ValueError("Discord message exceeds the configured or API content limit")
    endpoint = os.environ.get(config.webhook_env)
    if not endpoint: raise ValueError(f"Discord webhook environment variable is not set: {config.webhook_env}")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"} or not parsed.path.startswith("/api/webhooks/"):
        raise ValueError("Discord webhook URL must be an HTTPS discord.com/api/webhooks endpoint")
    request = Request(endpoint, data=json.dumps({"content": content, "allowed_mentions": {"parse": []}}, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response: response.read(32_768)
    except HTTPError as exc:
        return DiscordDeliveryResult(False, len(content_bytes), False, redact_terminal_output(f"Discord HTTP {exc.code}", force=True))
    except URLError as exc:
        return DiscordDeliveryResult(False, len(content_bytes), False, redact_terminal_output(f"Discord connection failed: {exc.reason}", force=True))
    return DiscordDeliveryResult(True, len(content_bytes), False, "accepted")
