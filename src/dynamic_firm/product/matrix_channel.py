"""One operator-confirmed Matrix Client-Server plaintext event send."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output

_HEADER = re.compile(r"(?m)^\[matrix_channel\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_ROOM = re.compile(r"^![A-Za-z0-9._=/-]{1,191}:[A-Za-z0-9.-]{1,253}$")

def _homeserver(value: object) -> str:
    raw = str(value or "").strip().rstrip("/"); parsed = urlsplit(raw)
    https = parsed.scheme == "https" and parsed.hostname
    loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not (https or loopback) or parsed.username or parsed.password or parsed.query or parsed.fragment: raise ValueError("Matrix homeserver URL must be HTTPS, or an explicit loopback HTTP URL")
    return raw

@dataclass(frozen=True, slots=True)
class MatrixChannelConfig:
    homeserver_url: str
    room_id: str
    token_env: str = "MATRIX_ACCESS_TOKEN"
    max_message_bytes: int = 16_000
    timeout_seconds: float = 15.0
    def validate(self) -> None:
        _homeserver(self.homeserver_url)
        if not _ROOM.fullmatch(self.room_id): raise ValueError("Matrix room ID must be one bounded canonical !room:server identifier")
        if not _ENV.fullmatch(self.token_env): raise ValueError("Matrix access-token environment variable name is invalid")
        if not 1 <= self.max_message_bytes <= 65_536: raise ValueError("Matrix message limit must be between 1 and 65536 bytes")
        if not 1 <= self.timeout_seconds <= 45: raise ValueError("Matrix timeout must be between 1 and 45 seconds")
    @property
    def endpoint(self) -> str: return f"{_homeserver(self.homeserver_url)}/_matrix/client/v3/rooms/{quote(self.room_id, safe='')}/send/m.room.message/{uuid.uuid4().hex}"

@dataclass(frozen=True, slots=True)
class MatrixDeliveryResult:
    delivered: bool; room_id: str; message_bytes: int; automatic_delivery: bool; output: str
    def to_dict(self) -> Mapping[str, object]: return asdict(self)

def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None: return text.strip()
    following = _TABLE.search(text, match.end()); return (text[:match.start()] + (text[following.start():] if following else "")).strip()

def _atomic_write(path: Path, text: str) -> Path:
    target = path.expanduser().resolve(); target.parent.mkdir(parents=True, exist_ok=True); descriptor, temporary = tempfile.mkstemp(prefix=".noruct-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle: handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, target)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
    return target

def write_matrix_channel_settings(path: Path, config: MatrixChannelConfig) -> Path:
    config.validate(); target = path.expanduser().resolve(); existing = target.read_text(encoding="utf-8") if target.is_file() else ""; q = lambda value: json.dumps(value, ensure_ascii=False)
    table = "\n".join(("[matrix_channel]", "enabled = true", f"homeserver_url = {q(_homeserver(config.homeserver_url))}", f"room_id = {q(config.room_id)}", f"token_env = {q(config.token_env)}", f"max_message_bytes = {config.max_message_bytes}", f"timeout_seconds = {config.timeout_seconds:g}", "")); remainder = _without_table(existing)
    return _atomic_write(target, (remainder + "\n\n" if remainder else "") + table)

def remove_matrix_channel_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file(): return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None: return False
    remainder = _without_table(existing); _atomic_write(target, remainder + ("\n" if remainder else "")); return True

def matrix_channel_config_from_settings(settings: Mapping[str, Any]) -> MatrixChannelConfig | None:
    raw = settings.get("matrix_channel")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True: return None
    url, room, token, maximum, timeout = raw.get("homeserver_url"), raw.get("room_id"), raw.get("token_env", "MATRIX_ACCESS_TOKEN"), raw.get("max_message_bytes", 16_000), raw.get("timeout_seconds", 15.0)
    if not isinstance(url, str) or not isinstance(room, str) or not isinstance(token, str) or not isinstance(maximum, int) or isinstance(maximum, bool) or not isinstance(timeout, (int, float)): raise ValueError("Matrix channel configuration is malformed")
    config = MatrixChannelConfig(url.strip(), room.strip(), token.strip(), maximum, float(timeout)); config.validate(); return config

def matrix_channel_status(config: MatrixChannelConfig | None) -> Mapping[str, object]:
    if config is None: return {"enabled": False, "authority": "no_matrix_channel", "next_action": "noruct channel matrix-configure"}
    return {"enabled": True, "homeserver_url": _homeserver(config.homeserver_url), "room_id": config.room_id, "token_environment": config.token_env, "ready": bool(os.environ.get(config.token_env)), "automatic_delivery": False, "authority": "operator_confirmed_single_matrix_plaintext_event_not_an_employee_tool", "next_action": None if os.environ.get(config.token_env) else f"Set {config.token_env} in the operator shell."}

def deliver_matrix_message(config: MatrixChannelConfig, *, message: str) -> MatrixDeliveryResult:
    config.validate(); content = str(message or "").strip()
    if not content or "\x00" in content: raise ValueError("Matrix message must be non-empty text")
    size = len(content.encode("utf-8"))
    if size > config.max_message_bytes: raise ValueError("Matrix message exceeds the configured byte limit")
    token = os.environ.get(config.token_env)
    if not token: raise ValueError(f"Matrix access-token environment variable is not set: {config.token_env}")
    request = Request(config.endpoint, data=json.dumps({"msgtype": "m.text", "body": content}, ensure_ascii=False).encode("utf-8"), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}, method="PUT")
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response: response.read(32_768)
    except HTTPError as exc: return MatrixDeliveryResult(False, config.room_id, size, False, redact_terminal_output(f"Matrix HTTP {exc.code}", force=True))
    except URLError as exc: return MatrixDeliveryResult(False, config.room_id, size, False, redact_terminal_output(f"Matrix connection failed: {exc.reason}", force=True))
    return MatrixDeliveryResult(True, config.room_id, size, False, "accepted")
