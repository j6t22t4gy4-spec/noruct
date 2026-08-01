"""Atomic settings lifecycle for one approval-gated MCP action profile."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from dynamic_firm.mcp_connector import (
    McpActionConfig,
    McpActionConfigSet,
    McpActionPolicy,
    mcp_action_config_from_settings,
    mcp_action_configs,
)


_HEADER = re.compile(r"(?m)^\[mcp_action\][ \t]*(?:\r?\n|$)")
_TABLE = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")


def _without_action_table(text: str) -> str:
    match = _HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE.search(text, match.end())
    remainder = text[:match.start()] + (
        text[following.start():] if following is not None else ""
    )
    return remainder.strip()


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


def _action_values(config: McpActionConfig) -> list[str]:
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    values = [
        f"python_command = {quote(str(config.python_command.expanduser().resolve()))}",
        f"transport = {quote(config.transport)}",
        f"tool_name = {quote(config.tool_name)}",
        f"profile = {quote(config.profile)}",
        f"environment = [{', '.join(quote(item) for item in config.environment_names)}]",
        f"timeout_seconds = {config.timeout_seconds:g}",
        f"max_result_bytes = {config.max_result_bytes}",
    ]
    if config.transport == "stdio":
        assert config.server_command is not None
        values[2:2] = [
            f"server_command = {quote(str(config.server_command.expanduser().resolve()))}",
            f"server_args = [{', '.join(quote(item) for item in config.server_args)}]",
        ]
    else:
        assert config.server_url is not None
        values[2:2] = [
            f"server_url = {quote(config.server_url)}",
            "headers = { " + ", ".join(
                f"{header} = {quote(environment)}"
                for header, environment in config.header_environment
            ) + " }",
            f"oauth_enabled = {str(config.oauth_enabled).lower()}",
        ]
        if config.oauth_client_id_environment is not None:
            values.append(f"oauth_client_id_environment = {quote(config.oauth_client_id_environment)}")
        if config.oauth_client_secret_environment is not None:
            values.append(f"oauth_client_secret_environment = {quote(config.oauth_client_secret_environment)}")
        if config.oauth_scope is not None:
            values.append(f"oauth_scope = {quote(config.oauth_scope)}")
    return values


def _inline_action(config: McpActionConfig) -> str:
    return "{ " + ", ".join(_action_values(config)) + " }"


def _action_table_text(config: McpActionPolicy) -> str:
    configs = mcp_action_configs(config)
    if len(configs) == 1:
        return "\n".join(("[mcp_action]", "enabled = true", *_action_values(configs[0]), ""))
    return "\n".join(("[mcp_action]", "enabled = true", "actions = [", *(f"  {_inline_action(item)}," for item in configs), "]", ""))


def write_mcp_action_settings(path: Path, config: McpActionPolicy) -> Path:
    """Replace only `[mcp_action]`, preserving unrelated local settings."""

    mcp_action_configs(config)
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_action_table(existing)
    text = (remainder + "\n\n" if remainder else "") + _action_table_text(config)
    return _atomic_write(target, text)


def remove_mcp_action_settings(path: Path) -> bool:
    """Remove only the action profile; never alter the read-sidecar table."""

    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if _HEADER.search(existing) is None:
        return False
    remainder = _without_action_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True


def configured_mcp_action_policy(path: Path) -> McpActionPolicy | None:
    """Load the optional action profile without connecting to its server."""

    target = path.expanduser().resolve()
    if not target.is_file():
        return None
    return mcp_action_config_from_settings(tomllib.loads(target.read_text(encoding="utf-8")))


def append_mcp_action_settings(path: Path, config: McpActionConfig) -> Path:
    """Append one explicit action profile; profile identity must remain unique."""
    config.validate()
    existing = configured_mcp_action_policy(path)
    if existing is None:
        return write_mcp_action_settings(path, config)
    combined = McpActionConfigSet(mcp_action_configs(existing) + (config,))
    combined.validate()
    return write_mcp_action_settings(path, combined)


def remove_mcp_action_profile_settings(path: Path, profile: str) -> bool:
    """Remove one profile while preserving the remaining explicit action policy."""
    existing = configured_mcp_action_policy(path)
    if existing is None:
        return False
    remaining = tuple(item for item in mcp_action_configs(existing) if item.profile != profile)
    if len(remaining) == len(mcp_action_configs(existing)):
        return False
    if not remaining:
        return remove_mcp_action_settings(path)
    policy: McpActionPolicy = remaining[0] if len(remaining) == 1 else McpActionConfigSet(remaining)
    write_mcp_action_settings(path, policy)
    return True
