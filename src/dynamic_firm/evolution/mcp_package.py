"""Local, versioned package manifests for an already-configured MCP policy.

An MCP policy package never contains a command, path, remote tool name,
credential or protocol payload.  It only binds an immutable local Artifact to
the opaque digest of the user's existing read-only MCP policy.  The package
therefore cannot install, discover or broaden an external capability.
"""

from __future__ import annotations

from typing import Any, Mapping

from dynamic_firm.mcp_connector import (
    McpReadOnlyConfig,
    McpReadOnlyConfigSet,
    McpReadOnlyPolicy,
    session_binding_digest,
)


MCP_POLICY_ADAPTER_REFERENCE = "mcp_readonly_policy_v1"
MCP_POLICY_ARTIFACT_KIND = "TOOL_PACKAGE"


def build_mcp_policy_artifact(
    *,
    config: McpReadOnlyPolicy,
    artifact_id: str,
    version: str,
    profile: str | None = None,
) -> Mapping[str, Any]:
    """Create an EXPERIMENTAL local policy package from explicit MCP settings.

    It uses the actual configuration digest as its benchmark identity and has
    no claimed quality evidence.  A stable/shared release needs an independent
    benchmarked Artifact and cannot be manufactured from local configuration.
    """

    selected = mcp_policy_profile(config, profile=profile)
    binding_digest = session_binding_digest(selected)
    return {
        "schema": "noruct.evolution-artifact.v1",
        "artifact_id": artifact_id,
        "version": version,
        "kind": MCP_POLICY_ARTIFACT_KIND,
        "release_channel": "EXPERIMENTAL",
        "compatibility": {
            "runtime_contract": "noruct_v1",
            "required_capabilities": ["external_read"],
        },
        "content": {
            "tool_class": "external_read",
            "adapter_reference": MCP_POLICY_ADAPTER_REFERENCE,
            "binding_digest": binding_digest,
            "input_fields": ["query"],
            "output_fields": ["external_context"],
            "required_capabilities": ["external_read"],
        },
        "passport": {
            "schema": "noruct.workforce-passport.v1",
            "benchmark": {
                "suite_id": "local_mcp_policy",
                "version": "1.0.0",
                "digest": binding_digest,
            },
            "metrics": {
                "quality_score": 0.0,
                "safety_score": 0.0,
                "cost_bucket": "LOW",
                "latency_bucket": "LOW",
            },
            "limitations": ["local_policy_only", "user_managed_external_service"],
        },
    }


def mcp_policy_profile(
    policy: McpReadOnlyPolicy,
    *,
    profile: str | None = None,
) -> McpReadOnlyConfig:
    """Return one validated local policy profile without exposing its content.

    A package for a multi-profile policy must bind one profile at a time.  This
    keeps the normal Artifact activation scope meaningful: disabling or
    changing an issue-tracker read sidecar cannot implicitly revoke or grant a
    separate repository-context sidecar.
    """

    policy.validate()
    configs = policy.configs if isinstance(policy, McpReadOnlyConfigSet) else (policy,)
    if profile is None:
        if len(configs) != 1:
            raise ValueError(
                "MCP policy package needs --profile when multiple local profiles are configured"
            )
        return configs[0]
    selected = next((item for item in configs if item.profile == profile), None)
    if selected is None:
        raise ValueError("Configured MCP profile was not found for this policy package")
    return selected


def mcp_policy_binding_digest_from_artifact(manifest: Mapping[str, Any]) -> str | None:
    """Return the opaque binding only for the narrow MCP policy adapter."""

    if manifest.get("kind") != MCP_POLICY_ARTIFACT_KIND:
        return None
    content = manifest.get("content")
    if not isinstance(content, Mapping):
        return None
    if content.get("adapter_reference") != MCP_POLICY_ADAPTER_REFERENCE:
        return None
    value = content.get("binding_digest")
    return value if isinstance(value, str) else None
