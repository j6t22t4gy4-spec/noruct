"""Read-only MCP policy collections and settings projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .mcp_connector import McpReadOnlyConfig, _runtime_tool_name

@dataclass(frozen=True, slots=True)
class McpReadOnlyConfigSet:
    """A bounded set of independently user-owned read profiles.

    This is not a general plugin registry: every member remains an explicit
    executable/endpoint and allowlist policy.  HTTPS profiles may opt into the
    separately operator-confirmed OAuth lifecycle; no profile has write or
    automatic-discovery authority.
    """

    configs: tuple[McpReadOnlyConfig, ...]

    def validate(self) -> None:
        if not 2 <= len(self.configs) <= 4:
            raise ValueError("MCP profile set must contain between two and four sidecars")
        profiles = [config.profile for config in self.configs]
        if len(profiles) != len(set(profiles)):
            raise ValueError("MCP profile set must not contain duplicate profiles")
        for config in self.configs:
            config.validate()
        if len(self.selected_tool_names()) > 8:
            raise ValueError("MCP profile set cannot expose more than eight read-only tools")
        runtime = self.selected_runtime_tool_names()
        if len(runtime) != len(set(runtime)):
            raise ValueError("MCP profile set produced duplicate runtime tool identities")

    def selected_tool_names(self) -> tuple[str, ...]:
        return tuple(name for config in self.configs for name in config.selected_tool_names())

    def selected_runtime_tool_names(self) -> tuple[str, ...]:
        values: list[str] = []
        for config in self.configs:
            for index, external_name in enumerate(config.selected_tool_names()):
                values.append(
                    _runtime_tool_name(
                        config.profile,
                        external_name,
                        multi_tool=True,
                        index=index,
                    )
                )
        return tuple(values)

    @property
    def environment_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for config in self.configs for name in config.environment_names))

    def profile_for_runtime_tool(self, runtime_tool_name: str) -> str:
        for config in self.configs:
            for index, external_name in enumerate(config.selected_tool_names()):
                if runtime_tool_name == _runtime_tool_name(
                    config.profile, external_name, multi_tool=True, index=index
                ):
                    return config.profile
        raise ValueError("Unknown configured MCP runtime tool")


McpReadOnlyPolicy = McpReadOnlyConfig | McpReadOnlyConfigSet


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    capability_id: str
    external_tool_name: str
    runtime_tool_name: str
    input_schema: Mapping[str, Any]
    schema_fingerprint: str
    external_open_world: bool


def config_from_settings(settings: Mapping[str, Any]) -> McpReadOnlyPolicy | None:
    from .mcp_connector import _config_from_mapping
    raw = settings.get("mcp", {})
    if not isinstance(raw, dict):
        raise ValueError("Configuration section [mcp] must be a TOML table.")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("mcp.enabled must be true or false")
    if not enabled:
        return None
    allowed = {
        "enabled",
        "python_command",
        "server_command",
        "server_args",
        "tool_name",
        "tool_names",
        "profile",
        "environment",
        "timeout_seconds",
        "max_result_bytes",
        "transport",
        "server_url",
        "headers",
        "oauth_enabled",
        "oauth_client_id_environment",
        "oauth_client_secret_environment",
        "oauth_scope",
    }
    if "servers" in raw:
        if set(raw) != {"enabled", "servers"}:
            raise ValueError("MCP profile-set configuration may contain only enabled and servers")
        servers = raw["servers"]
        if not isinstance(servers, list):
            raise ValueError("mcp.servers must be an array of profile objects")
        configs = tuple(_config_from_mapping(item, label="mcp.servers entry") for item in servers)
        result = McpReadOnlyConfigSet(configs)
        result.validate()
        return result
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Unknown MCP configuration field: {sorted(unknown)[0]}")
    return _config_from_mapping(raw, label="mcp")
