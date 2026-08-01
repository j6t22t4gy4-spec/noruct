from __future__ import annotations

"""Bounded local integration commands for the Modern terminal controller.

This module performs only local configuration and service-control adaptation.
It accepts the controller as a host so session, Company state, approval, and
runtime authority remain with the controller and their existing owners.
"""

from typing import Any

from dataclasses import fields
import argparse
import io
import json
from pathlib import Path
from dynamic_firm.mcp_connector import McpReadOnlyConfig, McpActionConfig, McpActionConfigSet, McpActionPolicy, mcp_action_configs
from dynamic_firm.browser_connector import BrowserReadOnlyConfig
from dynamic_firm.computer_use_connector import ComputerUseConfig
from dynamic_firm.openai_media import OpenAIMediaConfig
from dynamic_firm.product.openai_media_settings import write_media_settings
from dynamic_firm.web_search import SearxngSearchConfig
from dynamic_firm.home_assistant import HomeAssistantConfig, write_home_assistant_settings
from dynamic_firm.product import append_mcp_settings, remove_mcp_profile_settings, configured_mcp_action_policy, write_mcp_action_settings, SlackChannelConfig, write_slack_channel_settings, SlackInboundConfig, write_slack_inbound_settings, DiscordChannelConfig, write_discord_channel_settings, DiscordInboundConfig, write_discord_inbound_settings, NtfyChannelConfig, write_ntfy_channel_settings, NtfyInboundConfig, write_ntfy_inbound_settings, EmailChannelConfig, write_email_channel_settings, EmailInboundConfig, write_email_inbound_settings, MattermostChannelConfig, write_mattermost_channel_settings, MattermostInboundConfig, write_mattermost_inbound_settings, MatrixChannelConfig, write_matrix_channel_settings, MatrixInboundConfig, write_matrix_inbound_settings, DingTalkChannelConfig, write_dingtalk_channel_settings, TeamsChannelConfig, write_teams_channel_settings, ChannelConfig, write_channel_settings, InboundChannelConfig, write_inbound_channel_settings, TelegramChannelConfig, write_telegram_channel_settings, write_browser_settings, write_computer_use_settings, RemoteWorkerSettings, write_remote_worker_settings, ContainerSettings, write_container_settings, write_web_search_settings, ExecutablePluginStore, PluginLifecycleError, write_plugin_settings
from dynamic_firm.product.modern_tui import ModernTerminalCommandResult
from dynamic_firm.product.external_skill_settings import write_external_skill_settings
from dynamic_firm.product.schedules import ScheduleStore

# Gateway and schedule command adapters preserve the CLI success convention
# without importing the CLI module.
EXIT_OK = 0


def execute_integration_command(owner: Any, command: str, argument: str) -> ModernTerminalCommandResult | None:
    """Apply bounded local integration and operator-service configuration commands."""

    if command in {"/capabilities", "/tools-status"}:
        # Keep the complete configuration lifecycle visible from the
        # persistent TUI too.  This invokes the same local, no-network
        # catalog as `noruct capabilities`; it never starts, installs, or
        # authorizes a capability.
        buffer = io.StringIO()
        owner.ports.run_capabilities_command(argparse.Namespace(json=False), owner.settings, buffer)
        lines = tuple(line for line in buffer.getvalue().splitlines() if line)
        return ModernTerminalCommandResult(
            messages=lines[:40] or ("No capability catalog is available.",)
        )
    if command == "/quick-web-search":
        if not argument:
            return ModernTerminalCommandResult(
                messages=("Use /quick-web-search <HTTPS-or-loopback-SearXNG-URL> from Settings Center.",)
            )
        try:
            target = write_web_search_settings(
                owner.config.config_path,
                SearxngSearchConfig(base_url=argument),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Web search connection was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("Web search connected. It is available to future Jobs under the external-read policy.",)
        )
    if command == "/quick-browser":
        try:
            payload = json.loads(argument)
            allowed_keys = {"node_command", "cdp_endpoint", "allow_control", "capture_directory"}
            if not isinstance(payload, dict) or not {"node_command", "cdp_endpoint"} <= set(payload) or set(payload) - allowed_keys:
                raise ValueError("Browser connection requires node_command and cdp_endpoint")
            node_command = payload["node_command"]
            endpoint = payload["cdp_endpoint"]
            allow_control = payload.get("allow_control", False)
            capture_directory = payload.get("capture_directory")
            if not isinstance(node_command, str) or not isinstance(endpoint, str) or not isinstance(allow_control, bool):
                raise ValueError("Browser connection values must be strings")
            if capture_directory is not None and not isinstance(capture_directory, str):
                raise ValueError("Browser capture_directory must be a string when supplied")
            target = write_browser_settings(
                owner.config.config_path,
                BrowserReadOnlyConfig(
                    node_command=Path(node_command),
                    cdp_endpoint=endpoint,
                    allow_control=allow_control,
                    capture_directory=Path(capture_directory) if capture_directory else None,
                ),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Browser connection was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=(("Browser connected. Existing tabs are available to future Jobs under the external-read policy. "
                       + ("Control actions follow the selected Capability trust profile." if allow_control else "Read-only access is configured.")),)
        )
    if command == "/quick-computer":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"driver_command", "allowed_apps", "allow_control"}:
                raise ValueError("Computer connection requires driver_command, allowed_apps, and allow_control")
            driver_command, allowed_apps, allow_control = (
                payload["driver_command"], payload["allowed_apps"], payload["allow_control"]
            )
            if not isinstance(driver_command, str) or not isinstance(allowed_apps, list) or not isinstance(allow_control, bool):
                raise ValueError("Computer connection values are malformed")
            if not all(isinstance(app, str) for app in allowed_apps):
                raise ValueError("Computer allowed_apps must be a list of application identifiers")
            target = write_computer_use_settings(
                owner.config.config_path,
                ComputerUseConfig(
                    driver_command=Path(driver_command),
                    allowed_apps=tuple(allowed_apps),
                    allow_control=allow_control,
                ),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Computer-use connection was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("Computer-use configured. Capture and control follow the selected Capability trust profile.",)
        )
    if command == "/quick-media":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"api_key_env", "capabilities"}:
                raise ValueError("Media connection requires api_key_env and capabilities")
            api_key_env = payload["api_key_env"]
            raw_capabilities = payload["capabilities"]
            if not isinstance(api_key_env, str) or not isinstance(raw_capabilities, str):
                raise ValueError("Media connection values must be strings")
            capabilities = {item.strip().lower() for item in raw_capabilities.split(",") if item.strip()}
            allowed = {"image", "speech", "transcription", "video"}
            if not capabilities or capabilities - allowed:
                raise ValueError("Media capabilities must be image, speech, transcription, and/or video")
            target = write_media_settings(
                owner.config.config_path,
                OpenAIMediaConfig(
                    api_key_env=api_key_env.strip(),
                    image_enabled="image" in capabilities,
                    speech_enabled="speech" in capabilities,
                    transcription_enabled="transcription" in capabilities,
                    video_enabled="video" in capabilities,
                ),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Media connection was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("Media capabilities connected. Each generation call remains approval-gated.",)
        )
    if command == "/quick-plugin":
        try:
            source = Path(argument).expanduser().resolve()
            store = ExecutablePluginStore(owner.ports.plugin_root(owner.config.config_path))
            installed = store.install(source)
            target = write_plugin_settings(owner.config.config_path, store.root)
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (OSError, ValueError, PluginLifecycleError) as exc:
            return ModernTerminalCommandResult(messages=(f"Plugin was not installed · {exc}",))
        return ModernTerminalCommandResult(
            messages=(
                f"Plugin {installed.plugin_id}@{installed.version} installed inactive. "
                f"Review its receipt, then activate separately with `noruct plugin enable "
                f"{installed.plugin_id} --version {installed.version} --confirm`; no plugin tool ran.",
            )
        )
    if command == "/quick-mcp":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"python_command", "server_command", "tool_name"}:
                raise ValueError("MCP connection requires python_command, server_command, and tool_name")
            values = tuple(payload[key] for key in ("python_command", "server_command", "tool_name"))
            if not all(isinstance(value, str) and value.strip() for value in values):
                raise ValueError("MCP connection values must be non-empty strings")
            # This named profile is replaceable from Settings Center while
            # preserving separately configured MCP profiles.
            remove_mcp_profile_settings(owner.config.config_path, "settings-mcp")
            target = append_mcp_settings(
                owner.config.config_path,
                McpReadOnlyConfig(
                    python_command=Path(str(payload["python_command"])),
                    server_command=Path(str(payload["server_command"])),
                    tool_name=str(payload["tool_name"]),
                    profile="settings-mcp",
                ),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"MCP connection was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("Read-only MCP connected. It is available to future Jobs under the external-read policy.",)
        )
    if command == "/quick-mcp-action":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"python_command", "server_command", "tool_name"}:
                raise ValueError("MCP action requires python_command, server_command, and tool_name")
            values = tuple(payload[key] for key in ("python_command", "server_command", "tool_name"))
            if not all(isinstance(value, str) and value.strip() for value in values):
                raise ValueError("MCP action values must be non-empty strings")
            replacement = McpActionConfig(
                python_command=Path(str(payload["python_command"])),
                server_command=Path(str(payload["server_command"])),
                tool_name=str(payload["tool_name"]),
                profile="settings-mcp-action",
            )
            current = configured_mcp_action_policy(owner.config.config_path)
            profiles = tuple(
                item for item in (mcp_action_configs(current) if current is not None else ())
                if item.profile != replacement.profile
            ) + (replacement,)
            policy: McpActionPolicy = profiles[0] if len(profiles) == 1 else McpActionConfigSet(profiles)
            target = write_mcp_action_settings(owner.config.config_path, policy)
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"MCP action was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("MCP action connected. Every external action remains individually approval-gated.",)
        )
    if command == "/quick-home-assistant":
        try:
            payload = json.loads(argument)
            required = {"base_url", "token_env", "allowed_entities", "allowed_services"}
            if not isinstance(payload, dict) or set(payload) != required:
                raise ValueError("Home Assistant requires URL, token environment, entities, and services")
            if not isinstance(payload["base_url"], str) or not isinstance(payload["token_env"], str):
                raise ValueError("Home Assistant URL and token environment must be strings")
            if not isinstance(payload["allowed_entities"], list) or not isinstance(payload["allowed_services"], list):
                raise ValueError("Home Assistant allowlists must be lists")
            if not all(isinstance(item, str) for item in payload["allowed_entities"] + payload["allowed_services"]):
                raise ValueError("Home Assistant allowlist values must be strings")
            target = write_home_assistant_settings(
                owner.config.config_path,
                HomeAssistantConfig(
                    base_url=payload["base_url"].strip(),
                    token_env=payload["token_env"].strip(),
                    allowed_entities=tuple(item.strip() for item in payload["allowed_entities"] if item.strip()),
                    allowed_services=tuple(item.strip() for item in payload["allowed_services"] if item.strip()),
                ),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Home Assistant was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("Home Assistant connected. Reads follow external-read policy and service calls retain individual approval.",)
        )
    if command == "/quick-skills":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"roots"}:
                raise ValueError("Skill connection requires roots")
            roots = payload["roots"]
            if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
                raise ValueError("Skill roots must be a list of local directories")
            target = write_external_skill_settings(owner.config.config_path, roots)
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Skill roots were not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("External skill roots connected. Future Jobs discover compatible SKILL.md files read-only; scripts and assets are not executed during discovery.",)
        )
    if command == "/quick-channel":
        """Persist one selected messaging profile from the Settings Dashboard.

        The dashboard sends only typed public identifiers and environment
        variable *names*.  This path intentionally does not contact a
        channel, inspect a secret, start a gateway, or enable delivery.
        """
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) not in (
                {"kind", "fields"}, {"direction", "kind", "fields"},
            ):
                raise ValueError("Messaging connection requires direction, kind, and fields")
            direction = str(payload.get("direction", "outbound")).strip().lower()
            kind, fields = payload["kind"], payload["fields"]
            if direction not in {"inbound", "outbound"} or not isinstance(kind, str) or not isinstance(fields, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in fields.items()
            ):
                raise ValueError("Messaging connection values are malformed")
            value = lambda key: str(fields.get(key, "")).strip()
            csv = lambda key: tuple(item.strip() for item in value(key).split(",") if item.strip())
            if direction == "inbound" and kind == "custom":
                target = write_inbound_channel_settings(
                    owner.config.config_path,
                    InboundChannelConfig(
                        source_id=value("one"), command=Path(value("two")),
                        workspace=Path(value("three")), allowed_senders=csv("four"),
                        environment_names=csv("five"),
                    ),
                )
            elif direction == "inbound" and kind == "telegram":
                target = write_telegram_channel_settings(
                    owner.config.config_path,
                    TelegramChannelConfig(
                        workspace=Path(value("one")),
                        allowed_senders=csv("two"),
                        token_env=value("three"),
                    ),
                )
            elif direction == "inbound" and kind == "slack":
                target = write_slack_inbound_settings(
                    owner.config.config_path,
                    SlackInboundConfig(
                        workspace=Path(value("one")), allowed_senders=csv("two"),
                        allowed_channels=csv("three"), signing_secret_env=value("four"),
                    ),
                )
            elif direction == "inbound" and kind == "discord":
                target = write_discord_inbound_settings(
                    owner.config.config_path,
                    DiscordInboundConfig(
                        workspace=Path(value("one")), allowed_senders=csv("two"),
                        allowed_channels=csv("three"), token_env=value("four"),
                    ),
                )
            elif direction == "inbound" and kind == "ntfy":
                target = write_ntfy_inbound_settings(
                    owner.config.config_path,
                    NtfyInboundConfig(
                        workspace=Path(value("one")), topic=value("two"),
                        server_url=value("three"), token_env=value("four") or None,
                    ),
                )
            elif direction == "inbound" and kind == "email":
                target = write_email_inbound_settings(
                    owner.config.config_path,
                    EmailInboundConfig(
                        workspace=Path(value("one")), mailbox=value("two"),
                        imap_host=value("three"), allowed_senders=csv("four"),
                        password_env=value("five"),
                    ),
                )
            elif direction == "inbound" and kind == "matrix":
                target = write_matrix_inbound_settings(
                    owner.config.config_path,
                    MatrixInboundConfig(
                        workspace=Path(value("one")), homeserver_url=value("two"),
                        room_id=value("three"), allowed_senders=csv("four"),
                        token_env=value("five"),
                    ),
                )
            elif direction == "inbound" and kind == "mattermost":
                target = write_mattermost_inbound_settings(
                    owner.config.config_path,
                    MattermostInboundConfig(
                        workspace=Path(value("one")), base_url=value("two"),
                        channel_id=value("three"), allowed_senders=csv("four"),
                        token_env=value("five"),
                    ),
                )
            elif direction == "inbound":
                raise ValueError("Unsupported inbound messaging app")
            elif kind == "custom":
                target = write_channel_settings(
                    owner.config.config_path,
                    ChannelConfig(
                        command=Path(value("one")), environment_names=csv("two"), args=csv("three")
                    ),
                )
            # Backward compatibility: old Settings payloads used Telegram
            # without an explicit direction, and Telegram's bounded config
            # is itself an allowlisted inbox profile.
            elif kind == "telegram":
                target = write_telegram_channel_settings(
                    owner.config.config_path,
                    TelegramChannelConfig(
                        workspace=Path(value("one")), allowed_senders=csv("two"), token_env=value("three")
                    ),
                )
            elif kind == "slack":
                target = write_slack_channel_settings(
                    owner.config.config_path,
                    SlackChannelConfig(channel_id=value("one"), token_env=value("two")),
                )
            elif kind == "discord":
                target = write_discord_channel_settings(
                    owner.config.config_path, DiscordChannelConfig(webhook_env=value("one"))
                )
            elif kind == "teams":
                target = write_teams_channel_settings(
                    owner.config.config_path, TeamsChannelConfig(webhook_env=value("one"))
                )
            elif kind == "dingtalk":
                target = write_dingtalk_channel_settings(
                    owner.config.config_path, DingTalkChannelConfig(webhook_env=value("one"))
                )
            elif kind == "ntfy":
                target = write_ntfy_channel_settings(
                    owner.config.config_path,
                    NtfyChannelConfig(
                        topic=value("one"), server_url=value("two"),
                        token_env=value("three") or None,
                    ),
                )
            elif kind == "email":
                target = write_email_channel_settings(
                    owner.config.config_path,
                    EmailChannelConfig(
                        sender=value("one"),
                        recipients=tuple(
                            item.strip() for item in value("two").split(",") if item.strip()
                        ),
                        smtp_host=value("three"), password_env=value("four"),
                    ),
                )
            elif kind == "matrix":
                target = write_matrix_channel_settings(
                    owner.config.config_path,
                    MatrixChannelConfig(
                        homeserver_url=value("one"), room_id=value("two"), token_env=value("three")
                    ),
                )
            elif kind == "mattermost":
                target = write_mattermost_channel_settings(
                    owner.config.config_path,
                    MattermostChannelConfig(
                        base_url=value("one"), channel_id=value("two"), token_env=value("three")
                    ),
                )
            else:
                raise ValueError("Unsupported messaging app")
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(
                messages=(f"Messaging connection was not saved · {exc}",)
            )
        return ModernTerminalCommandResult(
            messages=(
                f"{direction.title()} {kind.title()} configured. Credentials remain in the environment; sending, intake, and gateway start stay explicit actions.",
            )
        )
    if command == "/quick-telegram":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"workspace", "allowed_senders", "token_env"}:
                raise ValueError("Telegram connection requires workspace, allowed_senders, and token_env")
            workspace, token_env = payload["workspace"], payload["token_env"]
            allowed_senders = payload["allowed_senders"]
            if not isinstance(workspace, str) or not isinstance(token_env, str) or not isinstance(allowed_senders, list):
                raise ValueError("Telegram connection values are malformed")
            if not all(isinstance(sender, str) for sender in allowed_senders):
                raise ValueError("Telegram allowed_senders must be a list of sender IDs")
            target = write_telegram_channel_settings(
                owner.config.config_path,
                TelegramChannelConfig(
                    workspace=Path(workspace),
                    allowed_senders=tuple(allowed_senders),
                    token_env=token_env,
                ),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Telegram connection was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("Telegram configured. Start its foreground receiver explicitly when you are ready to accept allowlisted messages.",)
        )
    if command == "/quick-slack":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"channel_id", "token_env"}:
                raise ValueError("Slack connection requires channel_id and token_env")
            channel_id, token_env = payload["channel_id"], payload["token_env"]
            if not isinstance(channel_id, str) or not isinstance(token_env, str):
                raise ValueError("Slack connection values must be strings")
            target = write_slack_channel_settings(
                owner.config.config_path,
                SlackChannelConfig(channel_id=channel_id, token_env=token_env),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Slack connection was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("Slack configured. Every outbound send remains an explicit approved action.",)
        )
    if command == "/quick-schedule":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"goal", "every_minutes", "name", "workspace"}:
                raise ValueError("Schedule requires goal, every_minutes, name, and workspace")
            goal, every_minutes, name, workspace = (
                payload["goal"], payload["every_minutes"], payload["name"], payload["workspace"]
            )
            if not isinstance(goal, str) or not isinstance(name, str) or not isinstance(workspace, str):
                raise ValueError("Schedule goal, name, and workspace must be strings")
            if not isinstance(every_minutes, int) or isinstance(every_minutes, bool):
                raise ValueError("Schedule every_minutes must be an integer")
            with ScheduleStore(owner.config.state_path) as store:
                created = store.create(
                    name=name or goal[:72],
                    goal=goal,
                    workspace=Path(workspace),
                    interval_minutes=every_minutes,
                )
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Schedule was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=(f"Schedule {created.schedule_id} created. It remains local and will not run until an operator explicitly ticks or starts the schedule service.",)
        )
    if command == "/quick-container":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"image", "programs", "docker_command"}:
                raise ValueError("Container connection requires image, programs, and docker_command")
            image, raw_programs, docker_command = payload["image"], payload["programs"], payload["docker_command"]
            if not isinstance(image, str) or not isinstance(docker_command, str) or not isinstance(raw_programs, dict):
                raise ValueError("Container connection values are malformed")
            programs: dict[str, tuple[str, ...]] = {}
            for identifier, value in raw_programs.items():
                if not isinstance(identifier, str) or not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError("Container programs must map identifiers to command arrays")
                programs[identifier] = tuple(value)
            target = write_container_settings(
                owner.config.config_path,
                ContainerSettings(image=image, programs=programs, docker_command=docker_command),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Container workspace was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("Container workspace configured. Image verification and each program execution remain explicit approved actions.",)
        )
    if command == "/quick-remote-worker":
        try:
            payload = json.loads(argument)
            required = {"target_id", "receipt", "programs", "identity_file"}
            if not isinstance(payload, dict) or set(payload) != required:
                raise ValueError("Remote worker requires target_id, receipt, programs, and identity_file")
            target_id, receipt, raw_programs, identity_file = (
                payload["target_id"], payload["receipt"], payload["programs"], payload["identity_file"]
            )
            if not isinstance(target_id, str) or not isinstance(receipt, str) or not isinstance(raw_programs, dict):
                raise ValueError("Remote worker connection values are malformed")
            if identity_file is not None and not isinstance(identity_file, str):
                raise ValueError("Remote worker identity_file must be a path or null")
            if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_programs.items()):
                raise ValueError("Remote worker programs must map identifiers to absolute programs")
            target = write_remote_worker_settings(
                owner.config.config_path,
                RemoteWorkerSettings(
                    target_id=target_id,
                    receipt=Path(receipt),
                    programs=raw_programs,
                    identity_file=Path(identity_file) if identity_file else None,
                ),
            )
            owner.settings = owner.ports.load_config(target)
            owner._persist_global_runtime_defaults()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Remote worker was not saved · {exc}",))
        return ModernTerminalCommandResult(
            messages=("Remote worker configured from its verified receipt. Verification and every remote program run remain explicit approved actions.",)
        )
    if command == "/gateway-service":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"action", "receivers"}:
                raise ValueError("Gateway service requires action and receivers")
            action, receivers = payload["action"], payload["receivers"]
            if action not in {"status", "start", "restart", "stop"}:
                raise ValueError("Gateway action must be status, start, restart, or stop")
            if not isinstance(receivers, list) or not all(isinstance(item, str) for item in receivers):
                raise ValueError("Gateway receivers must be a list of configured receiver names")
            allowed_receivers = {"telegram", "slack", "discord", "email", "ntfy", "matrix", "mattermost"}
            if len(receivers) != len(set(receivers)) or any(item not in allowed_receivers for item in receivers):
                raise ValueError(
                    "Gateway receivers must be unique configured names: telegram, slack, discord, email, ntfy, matrix, or mattermost"
                )
            if action in {"start", "restart"} and not receivers:
                raise ValueError("Gateway start requires at least one receiver")
            gateway_args = argparse.Namespace(
                gateway_service_command=action,
                state=owner.config.state_path,
                config=owner.config.config_path,
                json=False,
                confirm=True,
                receiver=receivers,
                poll_seconds=15.0,
                receiver_seconds=10.0,
                log_file=None,
                lines=80,
            )
            output = io.StringIO()
            exit_code = owner.ports.run_gateway_service_command(gateway_args, owner.settings, output)
            if exit_code != EXIT_OK:
                raise ValueError("Gateway service command did not succeed")
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Gateway service was not changed · {exc}",))
        message = output.getvalue().strip().splitlines()
        return ModernTerminalCommandResult(
            messages=tuple(message[:4]) or (f"Gateway service {action} completed.",)
        )
    if command == "/schedule-service":
        try:
            payload = json.loads(argument)
            if not isinstance(payload, dict) or set(payload) != {"action"}:
                raise ValueError("Schedule service requires action")
            action = payload["action"]
            if action not in {"status", "start", "restart", "stop"}:
                raise ValueError("Schedule action must be status, start, restart, or stop")
            schedule_args = argparse.Namespace(
                schedule_service_command=action,
                state=owner.config.state_path,
                config=owner.config.config_path,
                json=False,
                confirm=True,
                poll_seconds=60.0,
                limit=4,
                log_file=None,
                lines=80,
            )
            output = io.StringIO()
            exit_code = owner.ports.run_schedule_service_command(schedule_args, owner.settings, output)
            if exit_code != EXIT_OK:
                raise ValueError("Schedule service command did not succeed")
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return ModernTerminalCommandResult(messages=(f"Schedule service was not changed · {exc}",))
        message = output.getvalue().strip().splitlines()
        return ModernTerminalCommandResult(
            messages=tuple(message[:4]) or (f"Schedule service {action} completed.",)
        )

    return None
