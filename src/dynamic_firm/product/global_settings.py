"""Secret-free global Noruct settings stored in the user's TOML config.

The interactive terminal must not keep a second, ephemeral copy of provider and
runtime defaults.  This module owns the small provider/run rewrite needed by
the Settings Center while preserving every capability-specific TOML table.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dynamic_firm.providers.profiles import PROVIDER_KINDS, provider_profile


@dataclass(frozen=True, slots=True)
class GlobalRuntimeSettings:
    provider_kind: str
    base_url: str
    model: str
    api_key_env: str
    no_auth: bool
    codex_command: str
    external_command: str
    request_timeout: float
    stale_timeout: float
    state_path: str
    permission_mode: str
    capability_trust_mode: str
    external_read_mode: str
    external_state_mode: str
    agent_settings_mode: str
    max_wall_time: float
    max_model_calls: int
    max_tool_calls: int
    max_cost_usd: float
    cost_mode: str
    employee_runtime: str
    runtime_python: str

    @classmethod
    def from_mapping(cls, settings: Mapping[str, object]) -> "GlobalRuntimeSettings":
        provider = settings.get("provider", {})
        run = settings.get("run", {})
        provider = provider if isinstance(provider, Mapping) else {}
        run = run if isinstance(run, Mapping) else {}
        kind = str(provider.get("kind", "openai_api")).strip().replace("-", "_")
        return cls(
            provider_kind=kind,
            base_url=str(provider.get("base_url", "")).strip(),
            model=str(provider.get("model", "")).strip(),
            api_key_env=str(provider.get("api_key_env", "NORUCT_API_KEY")).strip(),
            no_auth=bool(provider.get("no_auth", False)),
            codex_command=str(provider.get("codex_command", "codex")).strip(),
            external_command=str(provider.get("external_command", "")).strip(),
            request_timeout=float(provider.get("request_timeout", 1_800.0 if kind == "openai_codex" else 120.0)),
            stale_timeout=float(provider.get("stale_timeout", 90.0)),
            state_path=str(run.get("state", "~/.noruct/runtime.db")).strip(),
            permission_mode=str(run.get("permission_mode", "ask")).strip(),
            capability_trust_mode=str(run.get("capability_trust_mode", "trusted")).strip(),
            external_read_mode=str(run.get("external_read_mode", "allow")).strip(),
            external_state_mode=str(run.get("external_state_mode", "ask")).strip(),
            agent_settings_mode=str(run.get("agent_settings_mode", "ask")).strip(),
            max_wall_time=float(run.get("max_wall_time", 86_400.0)),
            max_model_calls=int(run.get("max_model_calls", 2_048)),
            max_tool_calls=int(run.get("max_tool_calls", 8_192)),
            max_cost_usd=float(run.get("max_cost_usd", 1_000_000.0)),
            cost_mode=str(run.get("cost_mode", "standard")).strip(),
            employee_runtime=str(run.get("employee_runtime", "noruct")).strip(),
            runtime_python=str(run.get("runtime_python", "")).strip(),
        )

    def validate(self) -> None:
        if self.provider_kind not in PROVIDER_KINDS:
            raise ValueError("Unsupported provider kind")
        if self.permission_mode not in {"read-only", "ask"}:
            raise ValueError("Permission mode must be read-only or ask")
        if self.capability_trust_mode not in {"strict", "trusted", "autonomous"}:
            raise ValueError("Capability trust mode must be strict, trusted, or autonomous")
        if self.external_read_mode not in {"blocked", "ask", "allow"}:
            raise ValueError("External read mode must be blocked, ask, or allow")
        if self.external_state_mode not in {"blocked", "ask", "user-authorized-auto"}:
            raise ValueError(
                "External state mode must be blocked, ask, or user-authorized-auto"
            )
        if self.agent_settings_mode not in {"blocked", "ask"}:
            raise ValueError("Agent settings mode must be blocked or ask")
        if self.cost_mode not in {"standard", "economy"}:
            raise ValueError("Cost mode must be standard or economy")
        if self.employee_runtime != "noruct":
            raise ValueError("Employee runtime must be noruct; the legacy runtime was removed")
        if self.request_timeout <= 0 or self.stale_timeout <= 0 or self.max_wall_time <= 0:
            raise ValueError("Timeouts must be positive")
        if self.max_model_calls <= 0 or self.max_tool_calls <= 0 or self.max_cost_usd < 0:
            raise ValueError("Run limits must be bounded positive values")
        if self.provider_kind == "openai_codex" and not self.codex_command:
            raise ValueError("Codex command is required for openai_codex")
        if self.provider_kind == "external_exec" and (not self.external_command or not self.model):
            raise ValueError("external_command and model are required for external_exec")
        if self.provider_kind not in {"openai_codex", "external_exec"}:
            if not self.base_url or not self.model:
                raise ValueError("Provider base URL and model are required")
            if not self.no_auth and not self.api_key_env:
                raise ValueError("api_key_env is required unless no_auth is enabled")
            if provider_profile(self.provider_kind).transport == "anthropic-messages" and self.no_auth:
                raise ValueError(f"{self.provider_kind} requires an environment-backed credential")

    def render(self) -> str:
        self.validate()
        quote = lambda value: json.dumps(str(value), ensure_ascii=False)
        provider = ["[provider]", f"kind = {quote(self.provider_kind)}"]
        if self.provider_kind == "openai_codex":
            provider.append(f"codex_command = {quote(self.codex_command)}")
            if self.model:
                provider.append(f"model = {quote(self.model)}")
        elif self.provider_kind == "external_exec":
            provider.extend((f"external_command = {quote(self.external_command)}", f"model = {quote(self.model)}"))
        else:
            provider.extend((
                f"base_url = {quote(self.base_url)}",
                f"model = {quote(self.model)}",
                f"api_key_env = {quote(self.api_key_env)}",
                f"no_auth = {'true' if self.no_auth else 'false'}",
            ))
        provider.append(f"request_timeout = {self.request_timeout}")
        provider.append(f"stale_timeout = {self.stale_timeout}")
        run = [
            "[run]",
            f"state = {quote(self.state_path)}",
            f"max_wall_time = {self.max_wall_time}",
            f"max_model_calls = {self.max_model_calls}",
            f"max_tool_calls = {self.max_tool_calls}",
            f"max_cost_usd = {self.max_cost_usd}",
            f"cost_mode = {quote(self.cost_mode)}",
            f"permission_mode = {quote(self.permission_mode)}",
            f"capability_trust_mode = {quote(self.capability_trust_mode)}",
            f"external_read_mode = {quote(self.external_read_mode)}",
            f"external_state_mode = {quote(self.external_state_mode)}",
            f"agent_settings_mode = {quote(self.agent_settings_mode)}",
            f"employee_runtime = {quote(self.employee_runtime)}",
        ]
        if self.runtime_python:
            run.append(f"runtime_python = {quote(self.runtime_python)}")
        return "\n".join(provider) + "\n\n" + "\n".join(run) + "\n"


def _without_named_table(text: str, name: str) -> str:
    """Remove exactly one top-level TOML table without touching other settings."""

    pattern = re.compile(
        rf"(?ms)^\[{re.escape(name)}\]\s*\n.*?(?=^\[[^\n]+\]\s*\n|\Z)"
    )
    return pattern.sub("", text).strip()


def write_global_runtime_settings(path: Path, settings: GlobalRuntimeSettings) -> Path:
    """Atomically replace only `[provider]` and `[run]`, retaining all integrations."""

    rendered = settings.render()
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    retained = _without_named_table(_without_named_table(existing, "provider"), "run")
    content = (rendered + ("\n" + retained.strip() + "\n" if retained.strip() else ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-global-settings-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
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


def remove_optional_settings_table(path: Path, table_name: str) -> Path:
    """Atomically remove one optional top-level capability table.

    This is deliberately narrower than a generic TOML editor: the Settings
    Center may turn off a configured integration or channel, but it cannot
    rewrite credentials or invent arbitrary configuration.
    """

    if not re.fullmatch(r"[A-Za-z0-9_-]+", table_name):
        raise ValueError("Invalid optional settings table name")
    target = path.expanduser().resolve()
    if not target.is_file():
        raise ValueError("Settings file does not exist")
    existing = target.read_text(encoding="utf-8")
    updated = _without_named_table(existing, table_name)
    if updated == existing.strip():
        raise ValueError("Configured capability was not found")
    content = updated + ("\n" if updated else "")
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-disable-settings-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
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
