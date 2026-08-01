"""Atomic lifecycle editing for the bounded, user-managed MCP sidecar.

This owns only the ``[mcp]`` TOML table.  It deliberately does not discover,
install, authenticate to, or execute an MCP server: those actions stay in the
existing private sidecar and its explicit user-owned environment.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from dynamic_firm.mcp_connector import (
    McpReadOnlyConfig,
    McpReadOnlyConfigSet,
    McpReadOnlyPolicy,
    config_from_settings,
)


_MCP_HEADER = re.compile(r"(?m)^\[mcp\][ \t]*(?:\r?\n|$)")
_TABLE_HEADER = re.compile(r"(?m)^\[\[?[^\]\r\n]+\]\]?[ \t]*(?:\r?\n|$)")


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _config_values(config: McpReadOnlyConfig) -> list[str]:
    """Render one profile as TOML key/value metadata, never secrets."""

    config.validate()
    tools = config.selected_tool_names()
    values = [
        f"python_command = {_quote(str(config.python_command.expanduser().resolve()))}",
        f"transport = {_quote(config.transport)}",
        "tool_names = [" + ", ".join(_quote(item) for item in tools) + "]",
        f"profile = {_quote(config.profile)}",
        "environment = [" + ", ".join(_quote(item) for item in config.environment_names) + "]",
        f"timeout_seconds = {config.timeout_seconds:g}",
        f"max_result_bytes = {config.max_result_bytes}",
    ]
    if config.transport == "stdio":
        assert config.server_command is not None
        values.insert(2, f"server_command = {_quote(str(config.server_command.expanduser().resolve()))}")
        values.insert(3, "server_args = [" + ", ".join(_quote(item) for item in config.server_args) + "]")
    else:
        assert config.server_url is not None
        values.insert(2, f"server_url = {_quote(config.server_url)}")
        values.insert(
            3,
            "headers = { " + ", ".join(
                f"{key} = {_quote(value)}" for key, value in config.header_environment
            ) + " }",
        )
        values.append(f"oauth_enabled = {str(config.oauth_enabled).lower()}")
        if config.oauth_client_id_environment is not None:
            values.append(f"oauth_client_id_environment = {_quote(config.oauth_client_id_environment)}")
        if config.oauth_client_secret_environment is not None:
            values.append(f"oauth_client_secret_environment = {_quote(config.oauth_client_secret_environment)}")
        if config.oauth_scope is not None:
            values.append(f"oauth_scope = {_quote(config.oauth_scope)}")
    return values


def _inline_config(config: McpReadOnlyConfig) -> str:
    return "{ " + ", ".join(_config_values(config)) + " }"


def mcp_table_text(config: McpReadOnlyPolicy) -> str:
    """Render a single profile or bounded multi-profile MCP policy."""

    config.validate()
    if isinstance(config, McpReadOnlyConfigSet):
        values = ["[mcp]", "enabled = true", "servers = ["]
        values.extend(f"  {_inline_config(item)}," for item in config.configs)
        values.append("]")
        return "\n".join(values) + "\n"
    values = ["[mcp]", "enabled = true", *_config_values(config)]
    return "\n".join(values) + "\n"


def extract_mcp_table(text: str) -> str | None:
    """Return the complete top-level MCP table, if present, without parsing TOML."""

    match = _MCP_HEADER.search(text)
    if match is None:
        return None
    following = _TABLE_HEADER.search(text, match.end())
    end = following.start() if following else len(text)
    return text[match.start() : end].strip() + "\n"


def _without_mcp_table(text: str) -> str:
    match = _MCP_HEADER.search(text)
    if match is None:
        return text.strip()
    following = _TABLE_HEADER.search(text, match.end())
    end = following.start() if following else len(text)
    return (text[: match.start()] + text[end:]).strip()


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


def write_mcp_settings(path: Path, config: McpReadOnlyPolicy) -> Path:
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_mcp_table(existing)
    value = (remainder + "\n\n" if remainder else "") + mcp_table_text(config)
    return _atomic_write(target, value)


def _configured_policy(path: Path) -> McpReadOnlyPolicy | None:
    if not path.is_file():
        return None
    try:
        settings = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("Cannot edit MCP profiles because the configuration is not valid TOML") from exc
    return config_from_settings(settings)


def append_mcp_settings(path: Path, config: McpReadOnlyConfig) -> Path:
    """Add one explicit profile without granting discovery or transport authority."""

    target = path.expanduser().resolve()
    existing = _configured_policy(target)
    if existing is None:
        policy: McpReadOnlyPolicy = config
    elif isinstance(existing, McpReadOnlyConfigSet):
        policy = McpReadOnlyConfigSet((*existing.configs, config))
    else:
        policy = McpReadOnlyConfigSet((existing, config))
    policy.validate()
    return write_mcp_settings(target, policy)


def remove_mcp_profile_settings(path: Path, profile: str) -> bool:
    """Remove exactly one named profile, leaving other local policy intact."""

    target = path.expanduser().resolve()
    existing = _configured_policy(target)
    if existing is None:
        return False
    configs = existing.configs if isinstance(existing, McpReadOnlyConfigSet) else (existing,)
    retained = tuple(config for config in configs if config.profile != profile)
    if len(retained) == len(configs):
        return False
    if not retained:
        return remove_mcp_settings(target)
    policy = retained[0] if len(retained) == 1 else McpReadOnlyConfigSet(retained)
    write_mcp_settings(target, policy)
    return True


def remove_mcp_settings(path: Path) -> bool:
    target = path.expanduser().resolve()
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    if extract_mcp_table(existing) is None:
        return False
    remainder = _without_mcp_table(existing)
    _atomic_write(target, remainder + ("\n" if remainder else ""))
    return True
