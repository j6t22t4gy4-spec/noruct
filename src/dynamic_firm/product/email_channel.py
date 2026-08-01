"""One operator-confirmed SMTP email delivery path owned by Noruct.

This borrows the registered employee-foundation adapter's useful transport
invariant: SMTP port 465 is implicit TLS and every other supported port uses
STARTTLS.  The product boundary is deliberately narrower: one configured
sender and recipient allowlist, no mailbox read, reply loop, attachment,
background delivery, or employee-tool authority.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import tempfile
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output


_HEADER = re.compile(r"(?m)^\[email_channel\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_HOST = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$|^localhost$")


def _email(value: object, *, label: str) -> str:
    raw = str(value or "").strip()
    if "\r" in raw or "\n" in raw:
        raise ValueError(f"{label} must not contain line breaks")
    _name, address = parseaddr(raw)
    if not address or address != raw or address.count("@") != 1:
        raise ValueError(f"{label} must be one plain email address")
    local, domain = address.rsplit("@", 1)
    if not local or not domain or any(char.isspace() for char in address):
        raise ValueError(f"{label} must be one plain email address")
    return address


@dataclass(frozen=True, slots=True)
class EmailChannelConfig:
    sender: str
    recipients: tuple[str, ...]
    smtp_host: str
    smtp_port: int = 587
    password_env: str = "EMAIL_PASSWORD"
    username_env: str | None = None
    max_message_bytes: int = 40_000
    timeout_seconds: float = 20.0

    def validate(self) -> None:
        _email(self.sender, label="Email sender")
        if not self.recipients or len(self.recipients) > 16:
            raise ValueError("Email recipient allowlist must contain 1 through 16 addresses")
        normalized = tuple(_email(item, label="Email recipient") for item in self.recipients)
        if len(set(address.lower() for address in normalized)) != len(normalized):
            raise ValueError("Email recipient allowlist must not contain duplicates")
        if not _HOST.fullmatch(self.smtp_host.strip()):
            raise ValueError("SMTP host must be a DNS hostname or localhost")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP port must be between 1 and 65535")
        if not _ENV.fullmatch(self.password_env):
            raise ValueError("Email password environment variable name is invalid")
        if self.username_env is not None and not _ENV.fullmatch(self.username_env):
            raise ValueError("Email username environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 100_000:
            raise ValueError("Email message limit must be between 1 and 100000 bytes")
        if not 1 <= self.timeout_seconds <= 45:
            raise ValueError("Email timeout must be between 1 and 45 seconds")


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    delivered: bool
    recipients: tuple[str, ...]
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


def write_email_channel_settings(path: Path, config: EmailChannelConfig) -> Path:
    config.validate()
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = [
        "[email_channel]", "enabled = true", f"sender = {quote(config.sender)}",
        f"recipients = {quote(list(config.recipients))}", f"smtp_host = {quote(config.smtp_host.strip())}",
        f"smtp_port = {config.smtp_port}", f"password_env = {quote(config.password_env)}",
        f"max_message_bytes = {config.max_message_bytes}", f"timeout_seconds = {config.timeout_seconds:g}",
    ]
    if config.username_env is not None:
        lines.append(f"username_env = {quote(config.username_env)}")
    remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + "\n".join((*lines, "")))


def remove_email_channel_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    remainder = _without_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def email_channel_config_from_settings(settings: Mapping[str, Any]) -> EmailChannelConfig | None:
    raw = settings.get("email_channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    recipients = raw.get("recipients")
    if not isinstance(recipients, list) or not all(isinstance(item, str) for item in recipients):
        raise ValueError("Email recipient allowlist configuration is malformed")
    sender, host, password_env, username_env = raw.get("sender"), raw.get("smtp_host"), raw.get("password_env", "EMAIL_PASSWORD"), raw.get("username_env")
    maximum, timeout, port = raw.get("max_message_bytes", 40_000), raw.get("timeout_seconds", 20.0), raw.get("smtp_port", 587)
    if not isinstance(sender, str) or not isinstance(host, str) or not isinstance(password_env, str) or username_env is not None and not isinstance(username_env, str) or not isinstance(port, int) or isinstance(port, bool) or not isinstance(maximum, int) or isinstance(maximum, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("Email channel configuration is malformed")
    config = EmailChannelConfig(sender=sender.strip(), recipients=tuple(item.strip() for item in recipients), smtp_host=host.strip(), smtp_port=port, password_env=password_env.strip(), username_env=username_env.strip() if isinstance(username_env, str) and username_env.strip() else None, max_message_bytes=maximum, timeout_seconds=float(timeout))
    config.validate()
    return config


def email_channel_status(config: EmailChannelConfig | None) -> Mapping[str, object]:
    if config is None:
        return {"enabled": False, "authority": "no_email_channel", "next_action": "noruct channel email-configure"}
    password_ready = bool(os.environ.get(config.password_env))
    username_ready = config.username_env is None or bool(os.environ.get(config.username_env))
    return {
        "enabled": True, "sender": config.sender, "recipient_count": len(config.recipients),
        "smtp_host": config.smtp_host, "smtp_port": config.smtp_port,
        "password_environment": config.password_env, "username_environment": config.username_env,
        "ready": password_ready and username_ready, "automatic_delivery": False,
        "authority": "operator_confirmed_allowlisted_smtp_message_not_an_employee_tool",
        "next_action": None if password_ready and username_ready else "Set the configured SMTP credential environment variable(s) in the operator shell.",
    }


def _safe_header(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or "\r" in text or "\n" in text or "\x00" in text:
        raise ValueError(f"Email {label} must be non-empty single-line text")
    return text


def deliver_email_message(config: EmailChannelConfig, *, subject: str, message: str) -> EmailDeliveryResult:
    config.validate()
    normalized_message = str(message or "").strip()
    if not normalized_message or "\x00" in normalized_message:
        raise ValueError("Email message must be non-empty text")
    message_bytes = len(normalized_message.encode("utf-8"))
    if message_bytes > config.max_message_bytes:
        raise ValueError("Email message exceeds the configured byte limit")
    password = os.environ.get(config.password_env)
    if not password:
        raise ValueError(f"Email password environment variable is not set: {config.password_env}")
    username = os.environ.get(config.username_env) if config.username_env else config.sender
    if not username:
        raise ValueError(f"Email username environment variable is not set: {config.username_env}")
    email = EmailMessage()
    email["From"] = _email(config.sender, label="Email sender")
    email["To"] = ", ".join(_email(item, label="Email recipient") for item in config.recipients)
    email["Subject"] = _safe_header(subject, label="subject")
    email.set_content(normalized_message)
    context = ssl.create_default_context()
    client: smtplib.SMTP | smtplib.SMTP_SSL
    try:
        if config.smtp_port == 465:
            client = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=config.timeout_seconds, context=context)
        else:
            client = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout_seconds)
            client.starttls(context=context)
        try:
            client.login(username, password)
            client.send_message(email, from_addr=config.sender, to_addrs=list(config.recipients))
        finally:
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()
    except (OSError, smtplib.SMTPException) as exc:
        return EmailDeliveryResult(False, config.recipients, message_bytes, False, redact_terminal_output(f"SMTP delivery failed: {exc}", force=True))
    return EmailDeliveryResult(True, config.recipients, message_bytes, False, "accepted")
