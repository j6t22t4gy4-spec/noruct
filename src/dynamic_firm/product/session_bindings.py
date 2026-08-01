"""Secret-free session identity projections shared by terminal surfaces."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from dynamic_firm.mcp_connector import mcp_action_configs, session_binding_digest


def session_provider_binding(config: Any) -> dict[str, str | None]:
    """Return only the transport identity needed to protect a saved session."""

    return {
        "provider_kind": config.provider_kind,
        "provider_base_url": config.base_url,
        "provider_api_key_env": config.api_key_env,
    }


def session_mcp_binding(config: Any) -> dict[str, str]:
    """Persist an opaque digest of configured MCP authority, never credentials."""

    action = config.mcp_action
    action_projection: tuple[Mapping[str, object], ...] | None = None
    if action is not None:
        action_projection = tuple(
            {
                "python_command": str(item.python_command),
                "server_command": str(item.server_command),
                "server_args": item.server_args,
                "tool_name": item.tool_name,
                "profile": item.profile,
                "environment_names": item.environment_names,
                "timeout_seconds": item.timeout_seconds,
                "max_result_bytes": item.max_result_bytes,
            }
            for item in mcp_action_configs(action)
        )
    material = {
        "read_policy": session_binding_digest(config.mcp_read_only),
        "action_profile": action_projection,
    }
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"mcp_binding_digest": digest}


def session_cost_mode_binding(config: Any) -> dict[str, str]:
    return {"cost_efficiency_mode": config.run_limits.cost_efficiency_mode.value}
