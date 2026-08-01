"""Explicit user-managed outbound notification channel bridge.

The channel is intentionally outside the Employee ToolRegistry.  It is an
operator-triggered process bridge, disabled by default, and never receives a
Company transcript, workspace content, credential value, or automatic Job
event.  A user-managed executable can adapt stdin to Slack, Discord, Telegram,
email, or an internal notification service without making any external vendor
the Noruct product boundary.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output


CHANNEL_SCHEMA = "noruct.user-managed-channel.v1"
_HEADER = re.compile(r"(?m)^\[channel\][ \t]*(?:\r?\n|$)")
_TABLE_HEADER = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_JOB_STATUS = re.compile(r"^[A-Z][A-Z_]{0,63}$")


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    command: Path
    args: tuple[str, ...] = ()
    environment_names: tuple[str, ...] = ()
    timeout_seconds: float = 10.0
    max_message_bytes: int = 4_000

    def validate(self) -> None:
        declared_command = self.command.expanduser()
        # Check the declared path before resolving it: Path.resolve() erases
        # the evidence that the operator selected a symlink.
        if declared_command.is_symlink():
            raise ValueError("Channel command must be an absolute regular non-symbolic-link executable")
        command = declared_command.resolve()
        if not command.is_absolute() or not command.is_file():
            raise ValueError("Channel command must be an absolute regular non-symbolic-link executable")
        if not os.access(command, os.X_OK):
            raise ValueError("Channel command is not executable")
        if len(self.args) > 12:
            raise ValueError("Channel accepts at most 12 fixed non-secret arguments")
        for argument in self.args:
            if not isinstance(argument, str) or not argument or "\x00" in argument or len(argument.encode("utf-8")) > 512:
                raise ValueError("Channel argument must be a bounded non-empty string")
        if len(self.environment_names) > 12 or len(set(self.environment_names)) != len(self.environment_names):
            raise ValueError("Channel environment names must be unique and bounded")
        if any(not _ENVIRONMENT_NAME.fullmatch(name) for name in self.environment_names):
            raise ValueError("Channel environment name is invalid")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("Channel timeout must be between 1 and 60 seconds")
        if not 1 <= self.max_message_bytes <= 16_000:
            raise ValueError("Channel message limit must be between 1 and 16000 bytes")


@dataclass(frozen=True, slots=True)
class ChannelDeliveryResult:
    schema: str
    delivered: bool
    command: str
    environment_names: tuple[str, ...]
    message_bytes: int
    automatic_delivery: bool
    payload_kind: str
    job_id: str | None
    output: str

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChannelJobSummary:
    """The only terminal Job facts eligible for an explicit channel delivery."""

    job_id: str
    job_status: str
    audit_status: str
    attempt_count: int
    mutation_count: int
    final_graph_version: int

    def validate(self) -> None:
        if not _JOB_ID.fullmatch(self.job_id):
            raise ValueError("Channel job summary has an invalid job identity")
        if self.audit_status != "TERMINAL" or not _JOB_STATUS.fullmatch(self.job_status):
            raise ValueError("Only a terminal audited Job summary may be delivered")
        if any(not isinstance(value, int) or value < 0 for value in (self.attempt_count, self.mutation_count, self.final_graph_version)):
            raise ValueError("Channel job summary counts must be bounded non-negative integers")


def channel_table_text(config: ChannelConfig) -> str:
    config.validate()
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    return "\n".join(
        (
            "[channel]",
            "enabled = true",
            f"command = {quote(str(config.command.expanduser().resolve()))}",
            "args = [" + ", ".join(quote(item) for item in config.args) + "]",
            "environment = [" + ", ".join(quote(item) for item in config.environment_names) + "]",
            f"timeout_seconds = {config.timeout_seconds:g}",
            f"max_message_bytes = {config.max_message_bytes}",
            "",
        )
    )


def extract_channel_table(text: str) -> str | None:
    match = _HEADER.search(text)
    if match is None:
        return None
    following = _TABLE_HEADER.search(text, match.end())
    return text[match.start() : following.start() if following else len(text)].strip() + "\n"


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE_HEADER.search(text, match.end())
    return (text[: match.start()] + text[following.start() if following else len(text) :]).strip()


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


def write_channel_settings(path: Path, config: ChannelConfig) -> Path:
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + channel_table_text(config))


def remove_channel_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if extract_channel_table(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def channel_config_from_settings(settings: Mapping[str, Any]) -> ChannelConfig | None:
    raw = settings.get("channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    command = raw.get("command")
    args = raw.get("args", ())
    environment = raw.get("environment", ())
    if not isinstance(command, str) or not isinstance(args, list) or not isinstance(environment, list):
        raise ValueError("Channel configuration is malformed")
    config = ChannelConfig(
        command=Path(command),
        args=tuple(str(item) for item in args),
        environment_names=tuple(str(item) for item in environment),
        timeout_seconds=float(raw.get("timeout_seconds", 10.0)),
        max_message_bytes=int(raw.get("max_message_bytes", 4_000)),
    )
    config.validate()
    return config


def channel_status(config: ChannelConfig | None) -> Mapping[str, Any]:
    if config is None:
        return {
            "enabled": False,
            "automatic_delivery": False,
            "authority": "no_external_communication_channel",
            "next_action": "noruct channel configure",
        }
    missing = tuple(name for name in config.environment_names if name not in os.environ)
    return {
        "enabled": True,
        "command": str(config.command.expanduser().resolve()),
        "environment_names": list(config.environment_names),
        "missing_environment_names": list(missing),
        "ready": not missing,
        "automatic_delivery": False,
        "authority": "operator_confirmed_test_only_not_an_employee_tool",
        "next_action": None if not missing else "Set each named channel environment variable in the operator shell.",
    }


def deliver_channel_test(config: ChannelConfig, *, message: str, title: str = "Noruct channel test") -> ChannelDeliveryResult:
    return _deliver_channel_message(
        config,
        message=message,
        title=title,
        payload_kind="operator_test",
        job_id=None,
    )


def deliver_terminal_job_summary(
    config: ChannelConfig,
    *,
    summary: ChannelJobSummary,
) -> ChannelDeliveryResult:
    """Deliver a terminal audit projection after one operator confirmation.

    This deliberately carries no goal, prompt, workspace path, employee/task
    identities, model output, tool output, approval detail, or ledger hashes.
    It is an operator action and must never be called from a Job/event sink.
    """

    summary.validate()
    message = "\n".join(
        (
            f"Job: {summary.job_id}",
            f"Status: {summary.job_status}",
            "Audit: TERMINAL",
            f"Attempts: {summary.attempt_count}",
            f"Workflow mutations: {summary.mutation_count}",
            f"Final graph version: {summary.final_graph_version}",
        )
    )
    return _deliver_channel_message(
        config,
        message=message,
        title="Noruct terminal Job summary",
        payload_kind="operator_confirmed_terminal_job_summary",
        job_id=summary.job_id,
    )


def _deliver_channel_message(
    config: ChannelConfig,
    *,
    message: str,
    title: str,
    payload_kind: str,
    job_id: str | None,
) -> ChannelDeliveryResult:
    config.validate()
    encoded = message.encode("utf-8")
    if not message.strip() or len(encoded) > config.max_message_bytes:
        raise ValueError("Channel test message is empty or exceeds the configured byte limit")
    missing = tuple(name for name in config.environment_names if name not in os.environ)
    if missing:
        raise ValueError("Channel environment variable is not set: " + ", ".join(missing))
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"} or key in config.environment_names
    }
    try:
        completed = subprocess.run(
            [str(config.command.expanduser().resolve()), *config.args],
            input=json.dumps({"schema": CHANNEL_SCHEMA, "title": title, "message": message}, ensure_ascii=False) + "\n",
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            cwd=tempfile.gettempdir(),
            env=environment,
            check=False,
        )
        output = redact_terminal_output((completed.stdout or "") + (completed.stderr or ""), force=True).strip()
        delivered = completed.returncode == 0
    except subprocess.TimeoutExpired:
        output = "Channel test timed out"
        delivered = False
    return ChannelDeliveryResult(
        schema=CHANNEL_SCHEMA,
        delivered=delivered,
        command=str(config.command.expanduser().resolve()),
        environment_names=config.environment_names,
        message_bytes=len(encoded),
        automatic_delivery=False,
        payload_kind=payload_kind,
        job_id=job_id,
        output=output[:1_000] if output else ("Channel command failed" if not delivered else ""),
    )
