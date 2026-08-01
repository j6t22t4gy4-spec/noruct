"""One operator-confirmed Microsoft Teams workflow-webhook message delivery.

This is intentionally smaller than the registered Hermes Teams bot adapter:
it owns neither a bot identity nor an inbound listener.  A user creates a
Teams Workflow/Incoming Webhook and keeps its secret URL in their shell.
Noruct can then POST exactly one bounded plaintext envelope only when an
operator explicitly invokes the test command.
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
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output


_HEADER = re.compile(r"(?m)^\[teams_channel\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


def _webhook(value: str) -> str:
    raw = value.strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or not parsed.path
        or len(raw.encode("utf-8")) > 4_096
    ):
        raise ValueError("Teams webhook must be a bounded HTTPS URL without embedded credentials")
    return raw


@dataclass(frozen=True, slots=True)
class TeamsChannelConfig:
    webhook_env: str = "TEAMS_WEBHOOK_URL"
    max_message_bytes: int = 4_000
    timeout_seconds: float = 15.0

    def validate(self) -> None:
        if not _ENV.fullmatch(self.webhook_env):
            raise ValueError("Teams webhook environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 28_000:
            raise ValueError("Teams message limit must be between 1 and 28000 bytes")
        if not 1 <= self.timeout_seconds <= 45:
            raise ValueError("Teams timeout must be between 1 and 45 seconds")


@dataclass(frozen=True, slots=True)
class TeamsDeliveryResult:
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


def write_teams_channel_settings(path: Path, config: TeamsChannelConfig) -> Path:
    config.validate()
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    table = "\n".join((
        "[teams_channel]", "enabled = true",
        f"webhook_env = {quote(config.webhook_env)}",
        f"max_message_bytes = {config.max_message_bytes}",
        f"timeout_seconds = {config.timeout_seconds:g}", "",
    ))
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + table)


def remove_teams_channel_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def teams_channel_config_from_settings(settings: Mapping[str, Any]) -> TeamsChannelConfig | None:
    raw = settings.get("teams_channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    environment = raw.get("webhook_env", "TEAMS_WEBHOOK_URL")
    maximum = raw.get("max_message_bytes", 4_000)
    timeout = raw.get("timeout_seconds", 15.0)
    if not isinstance(environment, str) or not isinstance(maximum, int) or isinstance(maximum, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("Teams channel configuration is malformed")
    config = TeamsChannelConfig(environment.strip(), maximum, float(timeout))
    config.validate()
    return config


def teams_channel_status(config: TeamsChannelConfig | None) -> Mapping[str, object]:
    if config is None:
        return {"enabled": False, "authority": "no_teams_channel", "next_action": "noruct channel teams-configure"}
    ready = bool(os.environ.get(config.webhook_env))
    return {
        "enabled": True,
        "webhook_environment": config.webhook_env,
        "ready": ready,
        "automatic_delivery": False,
        "authority": "operator_confirmed_single_teams_workflow_webhook_not_an_employee_tool",
        "next_action": None if ready else f"Set {config.webhook_env} in the operator shell.",
    }


def deliver_teams_message(config: TeamsChannelConfig, *, message: str) -> TeamsDeliveryResult:
    config.validate()
    body = str(message or "").strip()
    if not body or "\x00" in body:
        raise ValueError("Teams message must be non-empty safe text")
    size = len(body.encode("utf-8"))
    if size > config.max_message_bytes:
        raise ValueError("Teams message exceeds the configured byte limit")
    endpoint = os.environ.get(config.webhook_env)
    if not endpoint:
        raise ValueError(f"Teams webhook environment variable is not set: {config.webhook_env}")
    request = Request(
        _webhook(endpoint),
        data=json.dumps({"text": body}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            response.read(32_768)
    except HTTPError as exc:
        return TeamsDeliveryResult(False, size, False, redact_terminal_output(f"Teams HTTP {exc.code}", force=True))
    except URLError as exc:
        return TeamsDeliveryResult(False, size, False, redact_terminal_output(f"Teams connection failed: {exc.reason}", force=True))
    return TeamsDeliveryResult(True, size, False, "accepted")
