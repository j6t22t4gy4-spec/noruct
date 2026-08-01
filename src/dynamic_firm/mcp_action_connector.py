"""Explicit high-risk MCP action configuration and connector boundary."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.runtime.models import IdempotencyMode, ToolEffect, ToolRisk
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolDefinition, ToolValidationError

from .mcp_connector import (
    McpReadOnlyConfig,
    McpReadOnlyConnector,
    _BLOCKED_ENV,
    _ENV_RE,
    _HEADER_NAME_RE,
    _MCP_TRANSPORTS,
    _OAUTH_SCOPE_RE,
    _PROFILE_RE,
    _SENSITIVE_ARG_RE,
    _TOOL_NAME_RE,
    _validate_executable,
    _validate_mcp_url,
)
from .mcp_schema_contract import (
    ExternalCapabilityError,
    canonical_json as _canonical_json,
    sanitize_schema as _sanitize_schema,
    validate_value as _validate_value,
)

@dataclass(frozen=True, slots=True)
class McpActionConfig:
    """One user-selected MCP action, separate from `[mcp]` reads.

    The action path intentionally shares the audited private MCP bridge with
    reads, but it never inherits the read-only declaration.  A remote action
    is therefore still one named tool behind an explicit HTTPS endpoint,
    bounded credential environment names and a per-call HIGH approval.
    """

    python_command: Path
    tool_name: str
    server_command: Path | None = None
    profile: str = "external-action"
    server_args: tuple[str, ...] = ()
    environment_names: tuple[str, ...] = ()
    timeout_seconds: float = 15.0
    max_result_bytes: int = 48_000
    transport: str = "stdio"
    server_url: str | None = None
    header_environment: tuple[tuple[str, str], ...] = ()
    oauth_enabled: bool = False
    oauth_client_id_environment: str | None = None
    oauth_client_secret_environment: str | None = None
    oauth_scope: str | None = None

    def validate(self) -> None:
        _validate_executable(self.python_command, "MCP action Python command")
        transport = self.transport.strip().lower()
        if transport not in _MCP_TRANSPORTS:
            raise ValueError("MCP action transport must be stdio or streamable_http")
        if transport == "stdio":
            if self.server_command is None:
                raise ValueError("Stdio MCP action requires a server command")
            _validate_executable(self.server_command, "MCP action server command")
            if self.server_url is not None:
                raise ValueError("Stdio MCP action must not configure a server URL")
            if self.header_environment:
                raise ValueError("Stdio MCP action must not configure HTTP headers")
            if self.oauth_enabled or self.oauth_client_id_environment or self.oauth_client_secret_environment or self.oauth_scope:
                raise ValueError("OAuth MCP action is available only for Streamable HTTP")
        else:
            if self.server_command is not None or self.server_args:
                raise ValueError("Streamable HTTP MCP action must not configure a server command or arguments")
            _validate_mcp_url(self.server_url)
        if not _TOOL_NAME_RE.fullmatch(self.tool_name):
            raise ValueError("MCP action tool_name must be a bounded identifier")
        if not _PROFILE_RE.fullmatch(self.profile):
            raise ValueError("MCP action profile must use lowercase letters, digits, and hyphens")
        if len(self.server_args) > 32 or sum(len(item.encode("utf-8")) for item in self.server_args) > 8_192:
            raise ValueError("MCP action server_args exceed the bounded limit")
        if any("\x00" in item or "\n" in item or "\r" in item or _SENSITIVE_ARG_RE.search(item) for item in self.server_args):
            raise ValueError("MCP action server_args contain an unsupported or sensitive value")
        # The shared private bridge has the same bounded lifetime as the
        # read-sidecar; do not let an action profile claim a timeout that the
        # bridge contract cannot enforce.
        if not 0.1 <= self.timeout_seconds <= 30.0:
            raise ValueError("MCP action timeout_seconds must be between 0.1 and 30")
        if not 1_024 <= self.max_result_bytes <= 64_000:
            raise ValueError("MCP action max_result_bytes must be between 1024 and 64000")
        if len(self.environment_names) > 16:
            raise ValueError("MCP action environment allowlist cannot exceed 16 names")
        for name in self.environment_names:
            if not _ENV_RE.fullmatch(name) or name in _BLOCKED_ENV or name.startswith(("LD_", "DYLD_")):
                raise ValueError(f"MCP action environment name is not allowed: {name}")
        header_names: set[str] = set()
        for header, environment_name in self.header_environment:
            normalized_header = header.lower()
            if not _HEADER_NAME_RE.fullmatch(header) or normalized_header in header_names:
                raise ValueError("MCP action HTTP header names must be unique bounded identifiers")
            header_names.add(normalized_header)
            if not _ENV_RE.fullmatch(environment_name) or environment_name in _BLOCKED_ENV:
                raise ValueError("MCP action HTTP headers must reference allowed environment variable names")
        if len(self.header_environment) > 8:
            raise ValueError("MCP action HTTP header environment allowlist cannot exceed 8 names")
        if not isinstance(self.oauth_enabled, bool):
            raise ValueError("MCP action OAuth setting must be true or false")
        if any(value is not None for value in (self.oauth_client_id_environment, self.oauth_client_secret_environment, self.oauth_scope)) and not self.oauth_enabled:
            raise ValueError("MCP action OAuth options require oauth_enabled=true")
        if self.oauth_client_secret_environment is not None and self.oauth_client_id_environment is None:
            raise ValueError("MCP action OAuth client secret requires an OAuth client ID environment variable")
        for value in (self.oauth_client_id_environment, self.oauth_client_secret_environment):
            if value is not None and (not _ENV_RE.fullmatch(value) or value in _BLOCKED_ENV or value.startswith(("LD_", "DYLD_"))):
                raise ValueError("MCP action OAuth credentials must reference allowed environment variable names")
        if self.oauth_scope is not None and (len(self.oauth_scope.encode("utf-8")) > 512 or not _OAUTH_SCOPE_RE.fullmatch(self.oauth_scope)):
            raise ValueError("MCP action OAuth scope must be a bounded space-separated scope list")


def _action_runtime_tool_name(config: McpActionConfig, *, multi_action: bool, index: int) -> str:
    """Return a Noruct-owned identity without projecting the upstream tool name."""
    if not multi_action:
        return "run_external_action"
    safe_profile = re.sub(r"[^a-z0-9]+", "_", config.profile).strip("_")
    identity = hashlib.sha256(
        f"noruct.external-action.runtime-name.v1|{config.profile}|{config.tool_name}".encode("utf-8")
    ).hexdigest()[:12]
    return f"run_external_action_{safe_profile}_{index + 1}_{identity}"


@dataclass(frozen=True, slots=True)
class McpActionConfigSet:
    """A bounded set of explicitly approved write/action endpoints."""

    configs: tuple[McpActionConfig, ...]

    def validate(self) -> None:
        if not 2 <= len(self.configs) <= 4:
            raise ValueError("MCP action profile set must contain between two and four actions")
        profiles = tuple(item.profile for item in self.configs)
        if len(profiles) != len(set(profiles)):
            raise ValueError("MCP action profile set must not contain duplicate profiles")
        for config in self.configs:
            config.validate()
        names = self.runtime_tool_names()
        if len(names) != len(set(names)):
            raise ValueError("MCP action profile set produced duplicate runtime tool identities")

    def runtime_tool_names(self) -> tuple[str, ...]:
        return tuple(_action_runtime_tool_name(item, multi_action=True, index=index) for index, item in enumerate(self.configs))


McpActionPolicy = McpActionConfig | McpActionConfigSet


def mcp_action_configs(policy: McpActionPolicy) -> tuple[McpActionConfig, ...]:
    if isinstance(policy, McpActionConfigSet):
        policy.validate()
        return policy.configs
    policy.validate()
    return (policy,)


def mcp_action_runtime_tool_names(policy: McpActionPolicy) -> tuple[str, ...]:
    if isinstance(policy, McpActionConfigSet):
        return policy.runtime_tool_names()
    policy.validate()
    return ("run_external_action",)


def mcp_action_config_for_profile(policy: McpActionPolicy, profile: str | None) -> McpActionConfig:
    configs = mcp_action_configs(policy)
    if profile is None:
        if len(configs) != 1:
            raise ValueError("Select one MCP action profile explicitly")
        return configs[0]
    matched = tuple(item for item in configs if item.profile == profile)
    if len(matched) != 1:
        raise ValueError("Configured MCP action profile was not found")
    return matched[0]


def mcp_action_config_from_settings(settings: Mapping[str, Any]) -> McpActionPolicy | None:
    raw = settings.get("mcp_action")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    if "actions" in raw:
        if set(raw) != {"enabled", "actions"} or not isinstance(raw["actions"], list):
            raise ValueError("MCP action profile-set configuration is malformed")
        result = McpActionConfigSet(
            tuple(_mcp_action_config_from_mapping(item, label="mcp_action.actions entry") for item in raw["actions"])
        )
        result.validate()
        return result
    return _mcp_action_config_from_mapping(raw, label="mcp_action")


def _mcp_action_config_from_mapping(raw: Mapping[str, Any], *, label: str) -> McpActionConfig:
    allowed = {
        "enabled", "python_command", "server_command", "server_args", "tool_name", "profile", "environment",
        "timeout_seconds", "max_result_bytes", "transport", "server_url", "headers", "oauth_enabled",
        "oauth_client_id_environment", "oauth_client_secret_environment", "oauth_scope",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Unknown {label} configuration field: {sorted(unknown)[0]}")
    command, server, tool = raw.get("python_command"), raw.get("server_command"), raw.get("tool_name")
    args, environment, headers = raw.get("server_args", []), raw.get("environment", []), raw.get("headers", {})
    transport, server_url = raw.get("transport", "stdio"), raw.get("server_url")
    oauth_enabled = raw.get("oauth_enabled", False)
    oauth_client_id_environment = raw.get("oauth_client_id_environment")
    oauth_client_secret_environment = raw.get("oauth_client_secret_environment")
    oauth_scope = raw.get("oauth_scope")
    timeout, result_limit = raw.get("timeout_seconds", 15.0), raw.get("max_result_bytes", 48_000)
    if (
        not isinstance(command, str) or not isinstance(tool, str)
        or not isinstance(args, list) or not all(isinstance(item, str) for item in args)
        or not isinstance(environment, list) or not all(isinstance(item, str) for item in environment)
        or not isinstance(headers, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items())
        or not isinstance(transport, str) or not isinstance(oauth_enabled, bool)
        or not all(value is None or isinstance(value, str) for value in (server, server_url, oauth_client_id_environment, oauth_client_secret_environment, oauth_scope))
        or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
        or not isinstance(result_limit, int) or isinstance(result_limit, bool)
    ):
        raise ValueError(f"{label} configuration is malformed")
    config = McpActionConfig(
        python_command=Path(command).expanduser(),
        server_command=(Path(server).expanduser() if isinstance(server, str) and server.strip() else None),
        tool_name=tool.strip(),
        profile=str(raw.get("profile", "external-action")).strip(), server_args=tuple(args),
        environment_names=tuple(dict.fromkeys(environment)), timeout_seconds=float(timeout), max_result_bytes=result_limit,
        transport=transport.strip().lower(),
        server_url=(server_url.strip() if isinstance(server_url, str) and server_url.strip() else None),
        header_environment=tuple((key, value) for key, value in headers.items()),
        oauth_enabled=oauth_enabled,
        oauth_client_id_environment=(oauth_client_id_environment.strip() if isinstance(oauth_client_id_environment, str) and oauth_client_id_environment.strip() else None),
        oauth_client_secret_environment=(oauth_client_secret_environment.strip() if isinstance(oauth_client_secret_environment, str) and oauth_client_secret_environment.strip() else None),
        oauth_scope=(oauth_scope.strip() if isinstance(oauth_scope, str) and oauth_scope.strip() else None),
    )
    config.validate()
    return config


class McpActionConnector:
    """Normalize one explicitly configured external write/action tool."""

    def __init__(self, config: McpActionConfig, *, bridge_path: Path | None = None, runtime_tool_name: str = "run_external_action") -> None:
        config.validate()
        if not _TOOL_NAME_RE.fullmatch(runtime_tool_name) or not runtime_tool_name.startswith("run_external_action"):
            raise ValueError("MCP action runtime tool name is invalid")
        self.config = config
        self.runtime_tool_name = runtime_tool_name
        # Keep OAuth client/token state for an action separate from a read
        # profile that happens to use the same operator-facing profile name
        # and endpoint.  The opaque bridge profile never becomes a product
        # identity or an external request field.
        bridge_profile = "a-" + hashlib.sha256(
            f"noruct.mcp-action-oauth-state.v1|{config.profile}|{config.server_url}".encode("utf-8")
        ).hexdigest()[:30]
        self._bridge = McpReadOnlyConnector(
            McpReadOnlyConfig(
                python_command=config.python_command, server_command=config.server_command, server_args=config.server_args,
                tool_name=config.tool_name, profile=bridge_profile, environment_names=config.environment_names,
                timeout_seconds=config.timeout_seconds, max_result_bytes=config.max_result_bytes,
                transport=config.transport, server_url=config.server_url, header_environment=config.header_environment,
                oauth_enabled=config.oauth_enabled, oauth_client_id_environment=config.oauth_client_id_environment,
                oauth_client_secret_environment=config.oauth_client_secret_environment, oauth_scope=config.oauth_scope,
            ), bridge_path=bridge_path,
        )

    async def authorize(self) -> None:
        """Run an explicit OAuth login for this remote action profile only."""

        await self._bridge.authorize()

    def clear_oauth_state(self) -> bool:
        """Delete only this profile's private local OAuth state."""

        return self._bridge.clear_oauth_state()

    async def definition(self) -> ToolDefinition:
        response = await self._bridge._invoke("list")
        selected, schema, fingerprint = self._descriptor(response)

        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if len(_canonical_json(arguments)) > 8_192:
                raise ToolValidationError("External action arguments exceed the byte limit")
            normalized = _validate_value(dict(arguments), schema)
            assert isinstance(normalized, dict)
            return normalized

        async def handle(arguments: Mapping[str, Any], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            response = await self._bridge._invoke("call", tool_name=self.config.tool_name, arguments=arguments)
            _, _, current_fingerprint = self._descriptor(response)
            if current_fingerprint != fingerprint:
                raise ExternalCapabilityError("CAPABILITY_CHANGED", "External action schema changed after job startup")
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise ExternalCapabilityError("MALFORMED_RESULT", "External action returned no result")
            if result.get("isError") is True:
                raise ExternalCapabilityError("REMOTE_TOOL_ERROR", "External action reported a failure")
            content = result.get("content", [])
            if not isinstance(content, list):
                raise ExternalCapabilityError("MALFORMED_RESULT", "External action result is invalid")
            text: list[str] = []
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                    raise ExternalCapabilityError("UNSUPPORTED_RESULT", "External action returned unsupported content")
                text.append(block["text"])
            structured = result.get("structuredContent")
            if structured is not None and not isinstance(structured, Mapping):
                raise ExternalCapabilityError("MALFORMED_RESULT", "External action structured content is invalid")
            normalized = {"source": "configured_external_action", "completed": True, "receipt": text, "structured_content": structured}
            encoded = _canonical_json(normalized)
            if len(encoded) > self.config.max_result_bytes:
                raise ExternalCapabilityError("RESULT_TOO_LARGE", "External action result exceeds the byte limit")
            return encoded.decode("utf-8")

        return ToolDefinition(
            name=self.runtime_tool_name,
            description="Run one explicitly configured external action tool. This may create an external side effect and always requires individual approval.",
            input_schema=schema, effect=ToolEffect.EXECUTE, risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY, validator=validate,
            resource_key=lambda _arguments: f"external-action:{self.config.profile}", handler=handle,
            timeout_ms=int((self.config.timeout_seconds + 6.0) * 1_000), output_limit_bytes=self.config.max_result_bytes,
            requires_approval=True, approval_preview=lambda _arguments: "Run the configured external action tool",
        )

    def _descriptor(self, response: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
        tools = response.get("tools")
        if response.get("has_more_tools") is not False or not isinstance(tools, list):
            raise ExternalCapabilityError("TOOL_COUNT", "External action server tool list is invalid")
        selected = next((item for item in tools if isinstance(item, Mapping) and item.get("name") == self.config.tool_name), None)
        if selected is None:
            raise ExternalCapabilityError("UNKNOWN_TOOL", "Configured external action tool was not found")
        if selected.get("read_only") is True:
            raise ExternalCapabilityError("READ_ONLY_TOOL", "Configured action tool declares itself read-only")
        schema = _sanitize_schema(selected.get("input_schema"))
        return selected, schema, hashlib.sha256(_canonical_json(schema)).hexdigest()


class McpActionConnectorGroup:
    """Expose each configured high-risk action under a distinct private runtime name."""

    def __init__(self, config: McpActionConfigSet, *, bridge_path: Path | None = None) -> None:
        config.validate()
        self.config = config
        self._connectors = tuple(
            McpActionConnector(item, bridge_path=bridge_path, runtime_tool_name=runtime_name)
            for item, runtime_name in zip(config.configs, config.runtime_tool_names(), strict=True)
        )

    async def definitions(self) -> tuple[ToolDefinition, ...]:
        definitions: list[ToolDefinition] = []
        for connector in self._connectors:
            definitions.append(await connector.definition())
        if len({item.name for item in definitions}) != len(definitions):
            raise ExternalCapabilityError("RUNTIME_NAME_COLLISION", "MCP action profile set produced duplicate runtime tools")
        return tuple(definitions)
