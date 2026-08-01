from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .global_settings import GlobalRuntimeSettings, write_global_runtime_settings
from dynamic_firm.providers.profiles import PROVIDER_KINDS, provider_profile


@dataclass(frozen=True, slots=True)
class SetupConfig:
    provider_kind: str = "openai_api"
    base_url: str = ""
    model: str = ""
    api_key_env: str = "NORUCT_API_KEY"
    no_auth: bool = False
    codex_command: str = "codex"
    external_command: str = ""
    request_timeout_seconds: float | None = None
    stale_timeout_seconds: float | None = None
    state_path: str = "~/.noruct/runtime.db"
    max_wall_time_seconds: float = 86_400.0
    max_model_calls: int = 2_048
    max_tool_calls: int = 8_192
    max_cost_usd: float = 1_000_000.0
    cost_mode: str = "standard"
    # An interactive company should expose the complete local tool surface
    # and ask immediately before each write/command.  This is not an
    # auto-approve mode: it merely avoids a first-run read-only shell that
    # makes the installed employee foundation look unavailable.
    permission_mode: str = "ask"
    capability_trust_mode: str = "trusted"
    external_read_mode: str = "allow"
    external_state_mode: str = "ask"
    agent_settings_mode: str = "ask"

    def validate(self) -> None:
        if self.provider_kind not in PROVIDER_KINDS:
            raise ValueError("Unsupported provider kind")
        if self.provider_kind == "external_exec":
            if not self.external_command.strip() or not self.model.strip():
                raise ValueError("external_command and model are required for external_exec")
        elif self.provider_kind != "openai_codex":
            if not self.base_url.strip() or not self.model.strip():
                raise ValueError("Provider base URL and model are required")
            if not self.no_auth and not self.api_key_env.strip():
                raise ValueError("api_key_env is required unless no_auth is enabled")
            if provider_profile(self.provider_kind).transport == "anthropic-messages" and self.no_auth:
                raise ValueError(f"{self.provider_kind} requires an environment-backed credential")
        elif not self.codex_command.strip():
            raise ValueError("codex_command is required for openai_codex")
        if self.request_timeout_seconds is not None and self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.stale_timeout_seconds is not None and self.stale_timeout_seconds <= 0:
            raise ValueError("stale_timeout_seconds must be positive")
        if self.max_wall_time_seconds <= 0 or self.max_model_calls <= 0 or self.max_tool_calls <= 0:
            raise ValueError("Run limits must be positive")
        if self.max_cost_usd < 0:
            raise ValueError("max_cost_usd cannot be negative")
        if self.cost_mode not in {"standard", "economy"}:
            raise ValueError("cost_mode must be standard or economy")
        if self.permission_mode not in {"read-only", "ask"}:
            raise ValueError("permission_mode must be read-only or ask")
        if self.capability_trust_mode not in {"strict", "trusted", "autonomous"}:
            raise ValueError("capability_trust_mode must be strict, trusted, or autonomous")
        if self.external_read_mode not in {"blocked", "ask", "allow"}:
            raise ValueError("external_read_mode must be blocked, ask, or allow")
        if self.external_state_mode not in {"blocked", "ask", "user-authorized-auto"}:
            raise ValueError("external_state_mode is invalid")
        if self.agent_settings_mode not in {"blocked", "ask"}:
            raise ValueError("agent_settings_mode must be blocked or ask")

    def render(self) -> str:
        self.validate()
        quote = lambda value: json.dumps(str(value), ensure_ascii=False)
        timeout = self.request_timeout_seconds
        if timeout is None:
            timeout = 1_800.0 if self.provider_kind == "openai_codex" else 30.0
        stale_timeout = self.stale_timeout_seconds if self.stale_timeout_seconds is not None else 90.0
        provider_lines = [
            "[provider]",
            f"kind = {quote(self.provider_kind)}",
        ]
        if self.provider_kind == "openai_codex":
            provider_lines.append(f"codex_command = {quote(self.codex_command.strip())}")
            if self.model.strip():
                provider_lines.append(f"model = {quote(self.model.strip())}")
        elif self.provider_kind == "external_exec":
            provider_lines.extend((f"external_command = {quote(self.external_command.strip())}", f"model = {quote(self.model.strip())}"))
        else:
            provider_lines.extend(
                (
                    f"base_url = {quote(self.base_url.strip())}",
                    f"model = {quote(self.model.strip())}",
                    f"api_key_env = {quote(self.api_key_env.strip())}",
                    f"no_auth = {'true' if self.no_auth else 'false'}",
                )
            )
        provider_lines.append(f"request_timeout = {timeout}")
        provider_lines.append(f"stale_timeout = {stale_timeout}")
        return (
            "\n".join(provider_lines)
            + "\n\n"
            "[run]\n"
            f"state = {quote(self.state_path)}\n"
            f"max_wall_time = {self.max_wall_time_seconds}\n"
            f"max_model_calls = {self.max_model_calls}\n"
            f"max_tool_calls = {self.max_tool_calls}\n"
            f"max_cost_usd = {self.max_cost_usd}\n"
            f"cost_mode = {quote(self.cost_mode)}\n"
            f"permission_mode = {quote(self.permission_mode)}\n"
            f"capability_trust_mode = {quote(self.capability_trust_mode)}\n"
            f"external_read_mode = {quote(self.external_read_mode)}\n"
            f"external_state_mode = {quote(self.external_state_mode)}\n"
            f"agent_settings_mode = {quote(self.agent_settings_mode)}\n"
        )


def write_setup_config(path: Path, config: SetupConfig, *, overwrite: bool = False) -> Path:
    target = path.expanduser().resolve()
    if target.exists() and not overwrite:
        raise FileExistsError(f"Configuration already exists: {target}")
    config.validate()
    # Setup used to reconstruct only provider/run plus two old tables.  A
    # provider change could therefore erase browser, plugin, gateway, media,
    # or execution-environment configuration.  The Global Settings owner
    # atomically replaces exactly provider/run and preserves every optional
    # capability table.
    timeout = config.request_timeout_seconds
    if timeout is None:
        timeout = 1_800.0 if config.provider_kind == "openai_codex" else 30.0
    stale_timeout = config.stale_timeout_seconds if config.stale_timeout_seconds is not None else 90.0
    return write_global_runtime_settings(
        target,
        GlobalRuntimeSettings(
            provider_kind=config.provider_kind,
            base_url=config.base_url,
            model=config.model,
            api_key_env=config.api_key_env,
            no_auth=config.no_auth,
            codex_command=config.codex_command,
            external_command=config.external_command,
            request_timeout=timeout,
            stale_timeout=stale_timeout,
            state_path=config.state_path,
            permission_mode=config.permission_mode,
            capability_trust_mode=config.capability_trust_mode,
            external_read_mode=config.external_read_mode,
            external_state_mode=config.external_state_mode,
            agent_settings_mode=config.agent_settings_mode,
            max_wall_time=config.max_wall_time_seconds,
            max_model_calls=config.max_model_calls,
            max_tool_calls=config.max_tool_calls,
            max_cost_usd=config.max_cost_usd,
            cost_mode=config.cost_mode,
            employee_runtime="noruct",
            runtime_python="",
        ),
    )
