"""Bounded Home Assistant REST tools behind the Noruct ActionPolicy.

Adapted from the registered Hermes Home Assistant source surface, but keeps no
websocket listener, gateway identity, event subscription, or broad instance
enumeration.  The operator provides explicit entity/service allowlists and a
user-managed long-lived token environment name.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from dynamic_firm.runtime.models import IdempotencyMode, ToolEffect, ToolRisk
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolDefinition, ToolExecutionError, ToolValidationError


_HEADER = re.compile(r"(?m)^\[home_assistant\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_ENTITY = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BLOCKED_DOMAINS = frozenset({"shell_command", "command_line", "python_script", "pyscript", "hassio", "rest_command"})


def _base_url(value: object) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    allowed_loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not ((parsed.scheme == "https" and parsed.hostname) or allowed_loopback) or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Home Assistant URL must be HTTPS, or explicit loopback HTTP, without a path or embedded credentials")
    return raw


@dataclass(frozen=True, slots=True)
class HomeAssistantConfig:
    base_url: str
    token_env: str = "HASS_TOKEN"
    allowed_entities: tuple[str, ...] = ()
    allowed_services: tuple[str, ...] = ()
    timeout_seconds: float = 15.0
    max_result_bytes: int = 32_000

    def validate(self) -> None:
        _base_url(self.base_url)
        if not _ENV.fullmatch(self.token_env): raise ValueError("Home Assistant token environment variable name is invalid")
        if not 1 <= len(self.allowed_entities) <= 64 or len(set(self.allowed_entities)) != len(self.allowed_entities): raise ValueError("Configure one to sixty-four unique Home Assistant entity patterns")
        if any(not _ENTITY.fullmatch(item.replace("*", "x")) and not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z0-9_*]+", item) for item in self.allowed_entities): raise ValueError("Home Assistant entity pattern is invalid")
        if len(self.allowed_services) > 128 or len(set(self.allowed_services)) != len(self.allowed_services): raise ValueError("Home Assistant service allowlist is invalid")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", item) or item.split(".", 1)[0] in _BLOCKED_DOMAINS for item in self.allowed_services): raise ValueError("Home Assistant service allowlist contains an invalid or blocked service")
        if not 1 <= self.timeout_seconds <= 45: raise ValueError("Home Assistant timeout must be between 1 and 45 seconds")
        if not 1_024 <= self.max_result_bytes <= 128_000: raise ValueError("Home Assistant result limit must be between 1024 and 128000 bytes")

    def allows_entity(self, entity_id: str) -> bool:
        return bool(_ENTITY.fullmatch(entity_id)) and any(fnmatch.fnmatchcase(entity_id, pattern) for pattern in self.allowed_entities)

    def allows_service(self, domain: str, service: str) -> bool:
        return f"{domain}.{service}" in self.allowed_services


def _without_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None: return text.strip()
    following = _TABLE.search(text, match.end())
    return (text[:match.start()] + (text[following.start():] if following else "")).strip()


def _atomic_write(path: Path, text: str) -> Path:
    import tempfile
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


def write_home_assistant_settings(path: Path, config: HomeAssistantConfig) -> Path:
    config.validate(); target = path.expanduser().resolve(); old = target.read_text(encoding="utf-8") if target.is_file() else ""; q = lambda value: json.dumps(value, ensure_ascii=False)
    table = "\n".join(("[home_assistant]", "enabled = true", f"base_url = {q(_base_url(config.base_url))}", f"token_env = {q(config.token_env)}", "allowed_entities = [" + ", ".join(q(item) for item in config.allowed_entities) + "]", "allowed_services = [" + ", ".join(q(item) for item in config.allowed_services) + "]", f"timeout_seconds = {config.timeout_seconds:g}", f"max_result_bytes = {config.max_result_bytes}", ""))
    rest = _without_table(old); return _atomic_write(target, (rest + "\n\n" if rest else "") + table)


def remove_home_assistant_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file(): return False
    old = target.read_text(encoding="utf-8")
    if _HEADER.search(old) is None: return False
    rest = _without_table(old); _atomic_write(target, rest + ("\n" if rest else "")); return True


def config_from_settings(settings: Mapping[str, Any]) -> HomeAssistantConfig | None:
    raw = settings.get("home_assistant")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True: return None
    required = {"enabled", "base_url", "token_env", "allowed_entities", "allowed_services", "timeout_seconds", "max_result_bytes"}
    if set(raw) != required or not isinstance(raw["base_url"], str) or not isinstance(raw["token_env"], str) or not isinstance(raw["allowed_entities"], list) or not isinstance(raw["allowed_services"], list): raise ValueError("Home Assistant configuration is malformed")
    config = HomeAssistantConfig(str(raw["base_url"]), str(raw["token_env"]), tuple(str(item) for item in raw["allowed_entities"]), tuple(str(item) for item in raw["allowed_services"]), float(raw["timeout_seconds"]), int(raw["max_result_bytes"]))
    config.validate(); return config


def status(config: HomeAssistantConfig | None) -> Mapping[str, object]:
    if config is None: return {"enabled": False, "authority": "no_home_assistant_capability", "next_action": "noruct home-assistant configure --base-url HTTPS_OR_LOOPBACK_URL --allow-entity light.example"}
    ready = bool(os.environ.get(config.token_env))
    return {"enabled": True, "base_url": _base_url(config.base_url), "token_environment": config.token_env, "allowed_entities": list(config.allowed_entities), "allowed_services": list(config.allowed_services), "ready": ready, "authority": "allowlisted_home_assistant_read_and_individually_approved_service_calls", "next_action": None if ready else f"Set {config.token_env} in the operator shell."}


class HomeAssistantTools:
    def __init__(self, config: HomeAssistantConfig) -> None: config.validate(); self.config = config

    def definitions(self) -> tuple[ToolDefinition, ...]:
        read = (
            self._definition("list_home_assistant_entities", "List configured Home Assistant entities and their current state.", {"type": "object", "properties": {"domain": {"type": "string", "maxLength": 64}}, "required": [], "additionalProperties": False}, ToolEffect.NETWORK, ToolRisk.MEDIUM, self._list_entities),
            self._definition("get_home_assistant_state", "Read the current state of one configured Home Assistant entity.", {"type": "object", "properties": {"entity_id": {"type": "string", "maxLength": 128}}, "required": ["entity_id"], "additionalProperties": False}, ToolEffect.NETWORK, ToolRisk.MEDIUM, self._get_state),
            self._definition("list_home_assistant_services", "List only configured Home Assistant services.", {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, ToolEffect.NETWORK, ToolRisk.MEDIUM, self._list_services),
        )
        write = () if not self.config.allowed_services else (self._definition("call_home_assistant_service", "Call one configured Home Assistant service for one configured entity.", {"type": "object", "properties": {"domain": {"type": "string", "maxLength": 64}, "service": {"type": "string", "maxLength": 64}, "entity_id": {"type": "string", "maxLength": 128}, "data": {"type": "object", "properties": {}, "additionalProperties": True}}, "required": ["domain", "service", "entity_id"], "additionalProperties": False}, ToolEffect.EXECUTE, ToolRisk.HIGH, self._call_service),)
        return read + write

    def _definition(self, name: str, description: str, schema: Mapping[str, Any], effect: ToolEffect, risk: ToolRisk, operation):
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not isinstance(arguments, Mapping): raise ToolValidationError("Home Assistant arguments must be an object")
            expected = schema["properties"]
            if set(arguments) - set(expected) or any(key not in arguments for key in schema.get("required", ())): raise ToolValidationError("Home Assistant arguments do not match the declared schema")
            for key, value in arguments.items():
                if key == "data":
                    if not isinstance(value, Mapping) or len(value) > 24: raise ToolValidationError("Home Assistant service data must be a bounded object")
                elif not isinstance(value, str) or len(value.encode("utf-8")) > int(expected[key].get("maxLength", 64)): raise ToolValidationError("Home Assistant text argument is invalid")
            return dict(arguments)
        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            if cancellation.cancelled: raise ToolExecutionError(cancellation.reason or "Home Assistant action cancelled")
            return await asyncio.to_thread(operation, dict(arguments))
        return ToolDefinition(name=name, description=description, input_schema=schema, effect=effect, risk=risk, idempotency_mode=IdempotencyMode.CALL_KEY, validator=validate, resource_key=lambda values: f"home-assistant:{name}:{values.get('entity_id', '*')}", handler=handle, timeout_ms=int(self.config.timeout_seconds * 1000), output_limit_bytes=self.config.max_result_bytes, requires_approval=effect == ToolEffect.EXECUTE, approval_preview=(lambda values: f"Call Home Assistant {values.get('domain')}.{values.get('service')} for {values.get('entity_id')}") if effect == ToolEffect.EXECUTE else None, allow_session_approval=False, parallel_safe=False)

    def _request(self, method: str, suffix: str, payload: Mapping[str, Any] | None = None) -> Any:
        token = os.environ.get(self.config.token_env)
        if not token: raise ToolExecutionError("Home Assistant token environment variable is not set")
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else None
        request = Request(f"{_base_url(self.config.base_url)}{suffix}", data=data, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(self.config.max_result_bytes + 1)
        except HTTPError as exc: raise ToolExecutionError(f"Home Assistant HTTP {exc.code}") from exc
        except URLError as exc: raise ToolExecutionError("Home Assistant connection failed") from exc
        if len(raw) > self.config.max_result_bytes: raise ToolExecutionError("Home Assistant response exceeds the output limit")
        try: return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ToolExecutionError("Home Assistant response is not valid JSON") from exc

    def _list_entities(self, arguments: Mapping[str, Any]) -> str:
        domain = arguments.get("domain")
        if domain is not None and (not isinstance(domain, str) or not _NAME.fullmatch(domain)): raise ToolExecutionError("Home Assistant domain is invalid")
        states = self._request("GET", "/api/states")
        if not isinstance(states, list): raise ToolExecutionError("Home Assistant states response is malformed")
        items = [{"entity_id": item.get("entity_id"), "state": item.get("state"), "friendly_name": item.get("attributes", {}).get("friendly_name", "")} for item in states if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str) and self.config.allows_entity(item["entity_id"]) and (domain is None or item["entity_id"].startswith(f"{domain}."))][:64]
        return json.dumps({"count": len(items), "entities": items}, ensure_ascii=False, sort_keys=True)

    def _get_state(self, arguments: Mapping[str, Any]) -> str:
        entity_id = str(arguments["entity_id"])
        if not self.config.allows_entity(entity_id): raise ToolExecutionError("Home Assistant entity is outside the configured allowlist")
        data = self._request("GET", "/api/states/" + quote(entity_id, safe="."))
        if not isinstance(data, Mapping): raise ToolExecutionError("Home Assistant state response is malformed")
        return json.dumps({"entity_id": data.get("entity_id"), "state": data.get("state"), "attributes": data.get("attributes", {}), "last_changed": data.get("last_changed")}, ensure_ascii=False, sort_keys=True)

    def _list_services(self, arguments: Mapping[str, Any]) -> str:
        del arguments
        services = self._request("GET", "/api/services")
        if not isinstance(services, list): raise ToolExecutionError("Home Assistant services response is malformed")
        allowed = set(self.config.allowed_services)
        result = []
        for item in services:
            if not isinstance(item, Mapping) or not isinstance(item.get("domain"), str): continue
            domain = item["domain"]; names = item.get("services", {})
            selected = sorted(name for name in (names.keys() if isinstance(names, Mapping) else names if isinstance(names, list) else []) if isinstance(name, str) and f"{domain}.{name}" in allowed)
            if selected: result.append({"domain": domain, "services": selected})
        return json.dumps({"domains": result}, ensure_ascii=False, sort_keys=True)

    def _call_service(self, arguments: Mapping[str, Any]) -> str:
        domain, service, entity_id = str(arguments["domain"]), str(arguments["service"]), str(arguments["entity_id"])
        if not _NAME.fullmatch(domain) or not _NAME.fullmatch(service) or not self.config.allows_service(domain, service): raise ToolExecutionError("Home Assistant service is outside the configured allowlist")
        if not self.config.allows_entity(entity_id): raise ToolExecutionError("Home Assistant entity is outside the configured allowlist")
        data = dict(arguments.get("data", {})); data["entity_id"] = entity_id
        result = self._request("POST", f"/api/services/{quote(domain, safe='')}/{quote(service, safe='')}", data)
        changed = [{"entity_id": item.get("entity_id"), "state": item.get("state")} for item in result[:32] if isinstance(item, Mapping) and self.config.allows_entity(str(item.get("entity_id", "")))] if isinstance(result, list) else []
        return json.dumps({"service": f"{domain}.{service}", "affected_entities": changed}, ensure_ascii=False, sort_keys=True)
