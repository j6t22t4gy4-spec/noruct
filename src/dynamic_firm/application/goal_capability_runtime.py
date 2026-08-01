"""Capability registration stage for one frozen Company job."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GoalCapabilityPorts:
    """Explicit composition boundary for optional runtime connectors."""

    mcp_connector: Any
    mcp_action_config_set: type
    mcp_action_connector: Any
    mcp_action_connector_group: Any
    browser_read_only_connector: Any
    computer_use_connector: Any
    openai_media_connector: Any
    web_read_connector: Any
    web_search_connector: Any
    product_event: Any
    product_event_type: Any
    emit_product_event: Any
    mcp_action_configs: Any
    workspace_id: str


async def register_goal_capabilities(
    *,
    config,
    registry,
    capability_lane,
    runtime_mcp_read_only,
    mcp_package_decision,
    event_sink,
    job_id,
    ports: GoalCapabilityPorts,
):
    McpActionConfigSet = ports.mcp_action_config_set
    McpActionConnector = ports.mcp_action_connector
    McpActionConnectorGroup = ports.mcp_action_connector_group
    BrowserReadOnlyConnector = ports.browser_read_only_connector
    ComputerUseConnector = ports.computer_use_connector
    OpenAIMediaConnector = ports.openai_media_connector
    WebReadConnector = ports.web_read_connector
    SearxngSearchConnector = ports.web_search_connector
    ProductEvent = ports.product_event
    ProductEventType = ports.product_event_type
    _emit_product_event = ports.emit_product_event
    _mcp_connector = ports.mcp_connector
    mcp_action_configs = ports.mcp_action_configs
    WORKSPACE_ID = ports.workspace_id
    if capability_lane and runtime_mcp_read_only is not None and config.external_read_mode != "blocked":
        external_read = _mcp_connector(runtime_mcp_read_only)
        definitions = await external_read.definitions()
        for definition in definitions:
            registry.register(definition)
        _emit_product_event(
            event_sink,
            ProductEvent(
                ProductEventType.CAPABILITY_READY,
                (
                    "External read capabilities ready via pinned MCP policy package"
                    if mcp_package_decision in {
                        "MCP_POLICY_PACKAGE_BOUND",
                        "MCP_POLICY_PACKAGE_PROFILE_SUBSET_BOUND",
                    }
                    else "External read capabilities ready"
                ),
                job_id=job_id,
                data={
                    "tool_names": tuple(definition.name for definition in definitions),
                    "trust": "untrusted",
                    "policy_package": mcp_package_decision,
                },
            ),
        )
    if capability_lane and config.mcp_action is not None and config.permission_mode == "ask" and config.external_state_mode != "blocked":
        if isinstance(config.mcp_action, McpActionConfigSet):
            definitions = await McpActionConnectorGroup(config.mcp_action).definitions()
        else:
            definitions = (await McpActionConnector(config.mcp_action).definition(),)
        for definition in definitions:
            registry.register(definition)
        _emit_product_event(
            event_sink,
            ProductEvent(
                ProductEventType.CAPABILITY_READY,
                "Configured external action capability ready",
                job_id=job_id,
                data={
                    "tool_names": tuple(definition.name for definition in definitions),
                    "trust": "untrusted",
                    "approval": "individual_high_risk",
                    "profile_count": len(mcp_action_configs(config.mcp_action)),
                },
            ),
        )
    if capability_lane and config.browser_read_only is not None and config.external_read_mode != "blocked":
        browser_read = BrowserReadOnlyConnector(config.browser_read_only)
        definitions = browser_read.definitions()
        for definition in definitions:
            registry.register(definition)
        _emit_product_event(
            event_sink,
            ProductEvent(
                ProductEventType.CAPABILITY_READY,
                "Configured local browser read capabilities ready",
                job_id=job_id,
                data={
                    "tool_names": tuple(definition.name for definition in definitions),
                    "trust": "untrusted",
                    "endpoint": "local_loopback",
                },
            ),
        )
    if capability_lane and config.computer_use is not None and config.external_state_mode != "blocked":
        computer_use = ComputerUseConnector(config.computer_use)
        definitions = computer_use.definitions()
        for definition in definitions:
            registry.register(definition)
        _emit_product_event(
            event_sink,
            ProductEvent(
                ProductEventType.CAPABILITY_READY,
                "Configured local computer-use capability ready",
                job_id=job_id,
                data={
                    "tool_names": tuple(definition.name for definition in definitions),
                    "apps": len(config.computer_use.allowed_apps),
                    "screenshots_in_model_context": False,
                },
            ),
        )
    if capability_lane and config.openai_media is not None and config.external_state_mode != "blocked":
        media = OpenAIMediaConnector(
            config.openai_media,
            config.workspace,
            workspace_id=WORKSPACE_ID,
        )
        definitions = media.definitions()
        for definition in definitions:
            registry.register(definition)
        _emit_product_event(
            event_sink,
            ProductEvent(
                ProductEventType.CAPABILITY_READY,
                "Configured media generation and transcription capabilities ready",
                job_id=job_id,
                data={
                    "tool_names": tuple(definition.name for definition in definitions),
                    "approval": "individual_high_risk",
                    "workspace_artifacts_only": True,
                    "credential_values_stored": False,
                },
            ),
        )
    if capability_lane and config.web_read is not None and config.external_read_mode != "blocked":
        definition = WebReadConnector(config.web_read).definition()
        registry.register(definition)
        _emit_product_event(event_sink, ProductEvent(ProductEventType.CAPABILITY_READY, "Configured public web-read capability ready", job_id=job_id, data={"tool_names": (definition.name,), "trust": "untrusted", "domains": len(config.web_read.allowed_domains)}))
    if capability_lane and config.web_search is not None and config.external_read_mode != "blocked":
        definition = SearxngSearchConnector(config.web_search).definition()
        registry.register(definition)
        _emit_product_event(event_sink, ProductEvent(ProductEventType.CAPABILITY_READY, "Configured SearXNG web-search capability ready", job_id=job_id, data={"tool_names": (definition.name,), "trust": "untrusted", "endpoint": "user_managed"}))
    if capability_lane and config.executable_plugins is not None and config.executable_plugins.plugins and config.external_state_mode != "blocked":
        _emit_product_event(
            event_sink,
            ProductEvent(
                ProductEventType.CAPABILITY_READY,
                "Enabled executable plugin capabilities ready",
                job_id=job_id,
                data={
                    "plugins": tuple(f"{item.plugin_id}@{item.version}" for item in config.executable_plugins.plugins),
                    "tool_names": tuple(definition.name for item in config.executable_plugins.plugins for definition in item.definitions()),
                    "approval": "individual_high_risk",
                    "execution": "out_of_process_untrusted_plugin_host",
                },
            ),
        )
