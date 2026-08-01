"""Read-only MCP profile aggregation and platform process helper."""

from __future__ import annotations

import os
from pathlib import Path

from dynamic_firm.runtime.tools import ToolDefinition

from .mcp_connector import (
    ExternalCapabilityError,
    McpReadOnlyConfigSet,
    McpReadOnlyConnector,
    _runtime_tool_name,
)

class McpReadOnlyConnectorGroup:
    """Aggregate a bounded profile set while preserving each sidecar boundary."""

    def __init__(self, config: McpReadOnlyConfigSet, *, bridge_path: Path | None = None) -> None:
        config.validate()
        self.config = config
        self._connectors: tuple[McpReadOnlyConnector, ...] = tuple(
            McpReadOnlyConnector(
                item,
                bridge_path=bridge_path,
                runtime_tool_names=tuple(
                    _runtime_tool_name(item.profile, external_name, multi_tool=True, index=index)
                    for index, external_name in enumerate(item.selected_tool_names())
                ),
            )
            for item in config.configs
        )

    async def definitions(self) -> tuple[ToolDefinition, ...]:
        definitions: list[ToolDefinition] = []
        for connector in self._connectors:
            definitions.extend(await connector.definitions())
        names = [definition.name for definition in definitions]
        if len(names) != len(set(names)):
            raise ExternalCapabilityError("RUNTIME_NAME_COLLISION", "MCP profile set produced duplicate runtime tools")
        return tuple(definitions)


def sys_platform_posix() -> bool:
    return os.name == "posix"
