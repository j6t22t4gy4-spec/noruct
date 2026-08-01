"""One operator-confirmed Mattermost REST post owned by Noruct."""

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


_HEADER = re.compile(r"(?m)^\[mattermost_channel\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_CHANNEL = re.compile(r"^[A-Za-z0-9]{1,128}$")


def _base_url(value: object) -> str:
    raw = str(value or "").strip().rstrip("/"); parsed = urlsplit(raw)
    valid_https = parsed.scheme == "https" and parsed.hostname
    valid_loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not (valid_https or valid_loopback) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Mattermost base URL must be HTTPS, or an explicit loopback HTTP URL")
    return raw


@dataclass(frozen=True, slots=True)
class MattermostChannelConfig:
    base_url: str
    channel_id: str
    token_env: str = "MATTERMOST_TOKEN"
    max_message_bytes: int = 4_000
    timeout_seconds: float = 15.0

    def validate(self) -> None:
        _base_url(self.base_url)
        if not _CHANNEL.fullmatch(self.channel_id): raise ValueError("Mattermost channel ID must be a bounded opaque identifier")
        if not _ENV.fullmatch(self.token_env): raise ValueError("Mattermost token environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 16_383: raise ValueError("Mattermost message limit must be between 1 and 16383 bytes")
        if not 1 <= self.timeout_seconds <= 45: raise ValueError("Mattermost timeout must be between 1 and 45 seconds")

    @property
    def endpoint(self) -> str: return f"{_base_url(self.base_url)}/api/v4/posts"


@dataclass(frozen=True, slots=True)
class MattermostDeliveryResult:
    delivered: bool
    channel_id: str
    message_bytes: int
    automatic_delivery: bool
    output: str
    def to_dict(self) -> Mapping[str, object]: return asdict(self)


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None: return text.strip()
    following = _TABLE.search(text, match.end())
    return (text[:match.start()] + (text[following.start():] if following else "")).strip()


def _atomic_write(path: Path, text: str) -> Path:
    target = path.expanduser().resolve(); target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle: handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, target)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
    return target


def write_mattermost_channel_settings(path: Path, config: MattermostChannelConfig) -> Path:
    config.validate(); target = path.expanduser().resolve(); existing = target.read_text(encoding="utf-8") if target.is_file() else ""; q = lambda v: json.dumps(v, ensure_ascii=False)
    table = "\n".join(("[mattermost_channel]", "enabled = true", f"base_url = {q(_base_url(config.base_url))}", f"channel_id = {q(config.channel_id)}", f"token_env = {q(config.token_env)}", f"max_message_bytes = {config.max_message_bytes}", f"timeout_seconds = {config.timeout_seconds:g}", ""))
    remainder = _without_table(existing); return _atomic_write(target, (remainder + "\n\n" if remainder else "") + table)


def remove_mattermost_channel_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file(): return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None: return False
    remainder = _without_table(existing); _atomic_write(target, remainder + ("\n" if remainder else "")); return True


def mattermost_channel_config_from_settings(settings: Mapping[str, Any]) -> MattermostChannelConfig | None:
    raw = settings.get("mattermost_channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True: return None
    base_url, channel_id, token_env, maximum, timeout = raw.get("base_url"), raw.get("channel_id"), raw.get("token_env", "MATTERMOST_TOKEN"), raw.get("max_message_bytes", 4_000), raw.get("timeout_seconds", 15.0)
    if not isinstance(base_url, str) or not isinstance(channel_id, str) or not isinstance(token_env, str) or not isinstance(maximum, int) or isinstance(maximum, bool) or not isinstance(timeout, (int, float)): raise ValueError("Mattermost channel configuration is malformed")
    config = MattermostChannelConfig(base_url.strip(), channel_id.strip(), token_env.strip(), maximum, float(timeout)); config.validate(); return config


def mattermost_channel_status(config: MattermostChannelConfig | None) -> Mapping[str, object]:
    if config is None: return {"enabled": False, "authority": "no_mattermost_channel", "next_action": "noruct channel mattermost-configure"}
    return {"enabled": True, "base_url": _base_url(config.base_url), "channel_id": config.channel_id, "token_environment": config.token_env, "ready": bool(os.environ.get(config.token_env)), "automatic_delivery": False, "authority": "operator_confirmed_single_mattermost_rest_post_not_an_employee_tool", "next_action": None if os.environ.get(config.token_env) else f"Set {config.token_env} in the operator shell."}


def deliver_mattermost_message(config: MattermostChannelConfig, *, message: str) -> MattermostDeliveryResult:
    config.validate()
    content = str(message or "").strip()
    if not content or "\x00" in content: raise ValueError("Mattermost message must be non-empty text")
    size = len(content.encode("utf-8"))
    if size > config.max_message_bytes: raise ValueError("Mattermost message exceeds the configured byte limit")
    token = os.environ.get(config.token_env)
    if not token: raise ValueError(f"Mattermost token environment variable is not set: {config.token_env}")
    request = Request(config.endpoint, data=json.dumps({"channel_id": config.channel_id, "message": content}, ensure_ascii=False).encode("utf-8"), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response: response.read(32_768)
    except HTTPError as exc: return MattermostDeliveryResult(False, config.channel_id, size, False, redact_terminal_output(f"Mattermost HTTP {exc.code}", force=True))
    except URLError as exc: return MattermostDeliveryResult(False, config.channel_id, size, False, redact_terminal_output(f"Mattermost connection failed: {exc.reason}", force=True))
    return MattermostDeliveryResult(True, config.channel_id, size, False, "accepted")
