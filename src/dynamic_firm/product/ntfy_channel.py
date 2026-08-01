"""One operator-confirmed ntfy publish channel owned by Noruct.

This is the outbound half of the registered Hermes ntfy platform reference.
It intentionally remains a single, explicit HTTP publish operation: no
subscription stream, background reconnect loop, automatic Job delivery or
employee-tool authority is introduced here.
"""

from __future__ import annotations

import base64
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


_HEADER = re.compile(r"(?m)^\[ntfy_channel\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DEFAULT_SERVER_URL = "https://ntfy.sh"


def _validated_server_url(value: object) -> str:
    raw = str(value or _DEFAULT_SERVER_URL).strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    ):
        return raw
    if (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    ):
        return raw
    raise ValueError("ntfy server URL must be HTTPS, or an explicit loopback HTTP URL")


@dataclass(frozen=True, slots=True)
class NtfyChannelConfig:
    topic: str
    token_env: str | None = None
    server_url: str = _DEFAULT_SERVER_URL
    max_message_bytes: int = 4_000
    timeout_seconds: float = 15.0
    markdown: bool = False

    def validate(self) -> None:
        if not _TOPIC.fullmatch(self.topic):
            raise ValueError("ntfy topic must use 1 through 128 ASCII letters, digits, underscores, or hyphens")
        if self.token_env is not None and not _ENV.fullmatch(self.token_env):
            raise ValueError("ntfy token environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 4_000:
            raise ValueError("ntfy message limit must be between 1 and 4000 bytes")
        if not 1 <= self.timeout_seconds <= 45:
            raise ValueError("ntfy timeout must be between 1 and 45 seconds")
        _validated_server_url(self.server_url)

    @property
    def normalized_server_url(self) -> str:
        return _validated_server_url(self.server_url)


@dataclass(frozen=True, slots=True)
class NtfyDeliveryResult:
    delivered: bool
    topic: str
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


def _atomic_write(path: Path, text: str) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
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


def write_ntfy_channel_settings(path: Path, config: NtfyChannelConfig) -> Path:
    config.validate()
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = [
        "[ntfy_channel]",
        "enabled = true",
        f"topic = {quote(config.topic)}",
        f"server_url = {quote(config.normalized_server_url)}",
        f"max_message_bytes = {config.max_message_bytes}",
        f"timeout_seconds = {config.timeout_seconds:g}",
        f"markdown = {'true' if config.markdown else 'false'}",
    ]
    if config.token_env is not None:
        lines.append(f"token_env = {quote(config.token_env)}")
    table = "\n".join((*lines, ""))
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + table)


def remove_ntfy_channel_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def ntfy_channel_config_from_settings(settings: Mapping[str, Any]) -> NtfyChannelConfig | None:
    raw = settings.get("ntfy_channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    topic = raw.get("topic")
    token_env = raw.get("token_env")
    maximum = raw.get("max_message_bytes", 4_000)
    timeout = raw.get("timeout_seconds", 15.0)
    markdown = raw.get("markdown", False)
    if (
        not isinstance(topic, str)
        or token_env is not None and not isinstance(token_env, str)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not isinstance(timeout, (int, float))
        or not isinstance(markdown, bool)
    ):
        raise ValueError("ntfy channel configuration is malformed")
    config = NtfyChannelConfig(
        topic=topic.strip(),
        token_env=token_env.strip() if isinstance(token_env, str) and token_env.strip() else None,
        server_url=str(raw.get("server_url", _DEFAULT_SERVER_URL)),
        max_message_bytes=maximum,
        timeout_seconds=float(timeout),
        markdown=markdown,
    )
    config.validate()
    return config


def ntfy_channel_status(config: NtfyChannelConfig | None) -> Mapping[str, object]:
    if config is None:
        return {"enabled": False, "authority": "no_ntfy_channel", "next_action": "noruct channel ntfy-configure --topic TOPIC"}
    return {
        "enabled": True,
        "server_url": config.normalized_server_url,
        "topic": config.topic,
        "token_environment": config.token_env,
        "ready": config.token_env is None or bool(os.environ.get(config.token_env)),
        "automatic_delivery": False,
        "authority": "operator_confirmed_single_ntfy_publish_not_an_employee_tool",
        "next_action": None if config.token_env is None or os.environ.get(config.token_env) else f"Set {config.token_env} in the operator shell.",
    }


def _authorization_header(token: str) -> str:
    value = token.strip()
    if not value or "\r" in value or "\n" in value:
        raise ValueError("ntfy token is empty or invalid")
    if ":" in value:
        return "Basic " + base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"Bearer {value}"


def deliver_ntfy_message(config: NtfyChannelConfig, *, message: str, title: str = "Noruct") -> NtfyDeliveryResult:
    config.validate()
    if not isinstance(message, str) or not message.strip() or "\x00" in message:
        raise ValueError("ntfy message must be non-empty text")
    if not isinstance(title, str) or not title.strip() or "\r" in title or "\n" in title:
        raise ValueError("ntfy title must be non-empty single-line text")
    body = message.strip().encode("utf-8")
    if len(body) > config.max_message_bytes:
        raise ValueError("ntfy message exceeds the configured byte limit")
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Accept": "application/json",
        "Title": title.strip()[:256],
        "Tags": "noruct",
    }
    if config.markdown:
        headers["Markdown"] = "yes"
    if config.token_env is not None:
        token = os.environ.get(config.token_env)
        if not token:
            raise ValueError(f"ntfy token environment variable is not set: {config.token_env}")
        headers["Authorization"] = _authorization_header(token)
    request = Request(
        f"{config.normalized_server_url}/{config.topic}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            raw = response.read(32_768)
    except HTTPError as exc:
        return NtfyDeliveryResult(False, config.topic, len(body), False, redact_terminal_output(f"ntfy HTTP {exc.code}", force=True))
    except URLError as exc:
        return NtfyDeliveryResult(False, config.topic, len(body), False, redact_terminal_output(f"ntfy connection failed: {exc.reason}", force=True))
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return NtfyDeliveryResult(False, config.topic, len(body), False, "ntfy returned an invalid response")
    if isinstance(value, Mapping) and value.get("event") == "message":
        return NtfyDeliveryResult(True, config.topic, len(body), False, "accepted")
    return NtfyDeliveryResult(False, config.topic, len(body), False, "ntfy rejected the message")
