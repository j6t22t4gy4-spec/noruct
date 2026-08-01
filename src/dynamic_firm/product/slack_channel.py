"""Explicit, user-managed Slack Web API outbound channel.

This is a named convenience adapter for one operator-confirmed outbound
message.  It never becomes an employee tool, inbound gateway, or credential
store; the bot token remains only in a named operator environment variable.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output


_HEADER = re.compile(r"(?m)^\[slack_channel\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_CHANNEL = re.compile(r"^[A-Za-z0-9]{1,128}$")
_API_URL = "https://slack.com/api/chat.postMessage"


@dataclass(frozen=True, slots=True)
class SlackChannelConfig:
    channel_id: str
    token_env: str = "SLACK_BOT_TOKEN"
    max_message_bytes: int = 12_000
    timeout_seconds: float = 15.0

    def validate(self) -> None:
        if not _CHANNEL.fullmatch(self.channel_id):
            raise ValueError("Slack channel ID must be a bounded opaque identifier")
        if not _ENV.fullmatch(self.token_env):
            raise ValueError("Slack token environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 40_000:
            raise ValueError("Slack message limit must be between 1 and 40000 bytes")
        if not 1 <= self.timeout_seconds <= 45:
            raise ValueError("Slack timeout must be between 1 and 45 seconds")


@dataclass(frozen=True, slots=True)
class SlackDeliveryResult:
    delivered: bool
    channel_id: str
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


def write_slack_channel_settings(path: Path, config: SlackChannelConfig) -> Path:
    config.validate()
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    table = "\n".join((
        "[slack_channel]", "enabled = true", f"channel_id = {quote(config.channel_id)}",
        f"token_env = {quote(config.token_env)}", f"max_message_bytes = {config.max_message_bytes}",
        f"timeout_seconds = {config.timeout_seconds:g}", "",
    ))
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + table)


def remove_slack_channel_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def slack_channel_config_from_settings(settings: Mapping[str, Any]) -> SlackChannelConfig | None:
    raw = settings.get("slack_channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    channel_id, token_env = raw.get("channel_id"), raw.get("token_env", "SLACK_BOT_TOKEN")
    if not isinstance(channel_id, str) or not isinstance(token_env, str):
        raise ValueError("Slack channel configuration is malformed")
    max_message_bytes, timeout_seconds = raw.get("max_message_bytes", 12_000), raw.get("timeout_seconds", 15.0)
    if not isinstance(max_message_bytes, int) or isinstance(max_message_bytes, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("Slack channel configuration is malformed")
    config = SlackChannelConfig(channel_id=channel_id.strip(), token_env=token_env.strip(), max_message_bytes=max_message_bytes, timeout_seconds=float(timeout_seconds))
    config.validate()
    return config


def slack_channel_status(config: SlackChannelConfig | None) -> Mapping[str, object]:
    if config is None:
        return {"enabled": False, "authority": "no_slack_channel", "next_action": "noruct channel slack-configure"}
    return {
        "enabled": True,
        "channel_id": config.channel_id,
        "token_environment": config.token_env,
        "ready": bool(os.environ.get(config.token_env)),
        "automatic_delivery": False,
        "authority": "operator_confirmed_single_slack_chat_post_message_not_an_employee_tool",
        "next_action": None if os.environ.get(config.token_env) else f"Set {config.token_env} in the operator shell.",
    }


def deliver_slack_message(config: SlackChannelConfig, *, message: str) -> SlackDeliveryResult:
    config.validate()
    if not isinstance(message, str) or not message.strip() or "\x00" in message:
        raise ValueError("Slack message must be non-empty text")
    encoded_message = message.strip().encode("utf-8")
    if len(encoded_message) > config.max_message_bytes:
        raise ValueError("Slack message exceeds the configured byte limit")
    token = os.environ.get(config.token_env)
    if not token:
        raise ValueError(f"Slack token environment variable is not set: {config.token_env}")
    payload = json.dumps({"channel": config.channel_id, "text": message.strip()}, ensure_ascii=False).encode("utf-8")
    request = Request(_API_URL, data=payload, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:  # noqa: S310 - fixed HTTPS API endpoint
            body = response.read(32_768)
    except HTTPError as exc:
        return SlackDeliveryResult(False, config.channel_id, len(encoded_message), False, redact_terminal_output(f"Slack HTTP {exc.code}", force=True))
    except URLError as exc:
        return SlackDeliveryResult(False, config.channel_id, len(encoded_message), False, redact_terminal_output(f"Slack connection failed: {exc.reason}", force=True))
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SlackDeliveryResult(False, config.channel_id, len(encoded_message), False, "Slack returned an invalid response")
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        error = result.get("error") if isinstance(result, Mapping) else "unknown_error"
        return SlackDeliveryResult(False, config.channel_id, len(encoded_message), False, redact_terminal_output(f"Slack rejected the message: {error}", force=True))
    return SlackDeliveryResult(True, config.channel_id, len(encoded_message), False, "accepted")
