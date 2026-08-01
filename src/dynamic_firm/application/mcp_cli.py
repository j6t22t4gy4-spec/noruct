"""MCP operator command adapter.

The component owns explicit MCP policy configuration, operator OAuth/test actions,
and local policy-package projection. Company/evolution state remains in its
canonical stores; CLI-only configuration and runtime paths enter through ports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, TextIO

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output
from dynamic_firm.evolution import EvolutionNetworkService, EvolutionStore
from dynamic_firm.evolution.mcp_package import (
    build_mcp_policy_artifact,
    mcp_policy_binding_digest_from_artifact,
    mcp_policy_profile,
)
from dynamic_firm.mcp_connector import (
    AUDITED_MCP_VERSION,
    McpActionConfig,
    McpActionConnector,
    McpActionPolicy,
    McpReadOnlyConfig,
    McpReadOnlyConfigSet,
    McpReadOnlyConnector,
    McpReadOnlyConnectorGroup,
    McpReadOnlyPolicy,
    configured_sdk_version,
    configured_sdk_versions,
    config_from_settings as mcp_config_from_settings,
    mcp_action_config_for_profile,
    mcp_action_config_from_settings,
    mcp_action_configs,
    mcp_action_runtime_tool_names,
    session_binding_digest as mcp_session_binding_digest,
)
from dynamic_firm.product.mcp_action_settings import (
    append_mcp_action_settings,
    remove_mcp_action_profile_settings,
    remove_mcp_action_settings,
    write_mcp_action_settings,
)
from dynamic_firm.product.mcp_settings import (
    append_mcp_settings,
    remove_mcp_profile_settings,
    remove_mcp_settings,
    write_mcp_settings,
)
from dynamic_firm.runtime.models import to_primitive
from dynamic_firm.runtime.ports import CancellationToken

EXIT_OK = 0
EXIT_INPUT = 2


@dataclass(frozen=True)
class McpCliPorts:
    load_settings: Callable[[Path], dict]
    state_path: Callable[[argparse.Namespace, dict], Path]


def mcp_connector(config: McpReadOnlyPolicy) -> McpReadOnlyConnector | McpReadOnlyConnectorGroup:
    return McpReadOnlyConnectorGroup(config) if isinstance(config, McpReadOnlyConfigSet) else McpReadOnlyConnector(config)


def configured_mcp_policy(path: Path, ports: McpCliPorts) -> McpReadOnlyPolicy | None:
    return mcp_config_from_settings(ports.load_settings(path))


def mcp_status_record(config: McpReadOnlyPolicy | None) -> dict[str, object]:
    """Project MCP readiness without exposing upstream tool names or secrets."""

    if config is None:
        return {
            "enabled": False,
            "authority": "no_external_capability",
            "next_action": "noruct mcp configure",
        }
    profiles = config.configs if isinstance(config, McpReadOnlyConfigSet) else (config,)
    installed = configured_sdk_versions(config)
    ready = all(version == AUDITED_MCP_VERSION for version in installed.values())
    record: dict[str, object] = {
        "enabled": True,
        "runtime_tools": list(config.selected_runtime_tool_names()),
        "tool_count": len(config.selected_runtime_tool_names()),
        "profile_count": len(profiles),
        "sdk_required": AUDITED_MCP_VERSION,
        "sidecar_ready": ready,
        "authority": "explicit_read_only_allowlist_one_call_per_selected_tool_per_job",
        "credential_values_stored": False,
        "transports": sorted({item.transport for item in profiles}),
        "remote_transport": (
            "enabled" if any(item.transport == "streamable_http" for item in profiles) else "not_enabled"
        ),
        "write_tools": "not_enabled",
        "next_action": None if ready else "Install the audited MCP SDK in the configured user-managed Python.",
        "profiles": [
            {
                "profile": item.profile,
                "runtime_tools": [
                    name for name in config.selected_runtime_tool_names()
                    if config.profile_for_runtime_tool(name) == item.profile
                ],
                "tool_count": len(item.selected_tool_names()),
                "sdk_installed": installed[item.profile],
                "sidecar_ready": installed[item.profile] == AUDITED_MCP_VERSION,
                "environment_names": list(item.environment_names),
                "transport": item.transport,
                "endpoint": "configured" if item.server_url else "local_process",
                "header_environment_names": [name for _, name in item.header_environment],
                "oauth": {
                    "enabled": item.oauth_enabled,
                    "client_id_environment": item.oauth_client_id_environment,
                    "client_secret_environment": item.oauth_client_secret_environment,
                    "scope_configured": item.oauth_scope is not None,
                    "login_required_before_job": item.oauth_enabled,
                },
            }
            for item in profiles
        ],
    }
    if len(profiles) == 1:
        record["profile"] = profiles[0].profile
        record["sdk_installed"] = installed[profiles[0].profile]
    return record


def mcp_action_status_record(config: McpActionPolicy | None) -> dict[str, object]:
    """Project explicit external actions without exposing upstream tool identities."""

    if config is None:
        return {
            "enabled": False,
            "authority": "no_external_action_capability",
            "next_action": "noruct mcp action-configure --python-command ... --server-command ... --tool ...",
        }
    actions = mcp_action_configs(config)
    versions = tuple(
        configured_sdk_version(
            McpReadOnlyConfig(
                python_command=item.python_command,
                server_command=item.server_command,
                server_args=item.server_args,
                tool_name=item.tool_name,
                profile=item.profile,
                environment_names=item.environment_names,
                timeout_seconds=item.timeout_seconds,
                max_result_bytes=item.max_result_bytes,
                transport=item.transport,
                server_url=item.server_url,
                header_environment=item.header_environment,
                oauth_enabled=item.oauth_enabled,
                oauth_client_id_environment=item.oauth_client_id_environment,
                oauth_client_secret_environment=item.oauth_client_secret_environment,
                oauth_scope=item.oauth_scope,
            )
        )
        for item in actions
    )
    ready = all(value == AUDITED_MCP_VERSION for value in versions)
    return {
        "enabled": True,
        "public_tools": list(mcp_action_runtime_tool_names(config)),
        "profile_count": len(actions),
        "sdk_required": AUDITED_MCP_VERSION,
        "sdk_installed": list(versions),
        "sidecar_ready": ready,
        "environment_names": list(
            dict.fromkeys(
                name
                for item in actions
                for name in (
                    *item.environment_names,
                    *(environment for _, environment in item.header_environment),
                    *(
                        value
                        for value in (
                            item.oauth_client_id_environment,
                            item.oauth_client_secret_environment,
                        )
                        if value is not None
                    ),
                )
            )
        ),
        "authority": "one_to_four_explicit_stdio_or_https_actions_high_risk_individual_approval_one_call_each_per_company_job",
        "credential_values_stored": False,
        "learning": "not_automatically_promoted",
        "next_action": None if ready else "Install the audited MCP SDK in the configured user-managed Python.",
    }


def mcp_profile_connector(config: McpReadOnlyPolicy, profile: str) -> McpReadOnlyConnector:
    """Select one explicit profile for an operator-only OAuth lifecycle action."""

    profiles = config.configs if isinstance(config, McpReadOnlyConfigSet) else (config,)
    matched = tuple(item for item in profiles if item.profile == profile)
    if not matched:
        raise ValueError("Configured MCP profile was not found")
    return McpReadOnlyConnector(matched[0])


def run_mcp_command(
    args: argparse.Namespace,
    settings: dict,
    config_path: Path,
    output: TextIO,
    ports: McpCliPorts,
) -> int:
    if args.mcp_command in {"action-configure", "action-add"}:
        transport = str(args.transport).replace("-", "_")
        if transport == "stdio" and args.server_command is None:
            raise ValueError("MCP stdio action configuration requires --server-command")
        if transport == "streamable_http" and not args.server_url:
            raise ValueError("MCP Streamable HTTP action configuration requires --server-url")
        headers: list[tuple[str, str]] = []
        for raw_header in args.header_env:
            header, separator, environment_name = str(raw_header).partition("=")
            if not separator or not header or not environment_name:
                raise ValueError("MCP action header environment entries must use HEADER=ENV_NAME")
            headers.append((header.strip(), environment_name.strip()))
        config = McpActionConfig(
            python_command=args.python_command.expanduser(),
            server_command=(args.server_command.expanduser() if args.server_command is not None else None),
            tool_name=str(args.tool).strip(),
            profile=str(args.profile).strip(),
            server_args=tuple(args.server_arg),
            environment_names=tuple(dict.fromkeys(args.environment)),
            timeout_seconds=float(args.timeout_seconds),
            max_result_bytes=int(args.max_result_bytes),
            transport=transport,
            server_url=(str(args.server_url).strip() if args.server_url else None),
            header_environment=tuple(headers),
            oauth_enabled=bool(args.oauth),
            oauth_client_id_environment=(str(args.oauth_client_id_env).strip() if args.oauth_client_id_env else None),
            oauth_client_secret_environment=(str(args.oauth_client_secret_env).strip() if args.oauth_client_secret_env else None),
            oauth_scope=(str(args.oauth_scope).strip() if args.oauth_scope else None),
        )
        target = (
            write_mcp_action_settings(config_path, config)
            if args.mcp_command == "action-configure"
            else append_mcp_action_settings(config_path, config)
        )
        effective_policy = mcp_action_config_from_settings(
            tomllib.loads(target.read_text(encoding="utf-8"))
        )
        record = {
            "configuration_changed": True,
            "config_path": str(target),
            **mcp_action_status_record(effective_policy),
        }
    elif args.mcp_command == "action-remove":
        removed = remove_mcp_action_profile_settings(config_path, str(args.profile).strip())
        record = {
            "configuration_changed": removed,
            "config_path": str(config_path.expanduser().resolve()),
            **mcp_action_status_record(mcp_action_config_from_settings(tomllib.loads(config_path.read_text(encoding="utf-8"))) if config_path.is_file() else None),
        }
    elif args.mcp_command == "action-disable":
        removed = remove_mcp_action_settings(config_path)
        record = {
            "configuration_changed": removed,
            "config_path": str(config_path.expanduser().resolve()),
            **mcp_action_status_record(None),
        }
    elif args.mcp_command == "action-test":
        if not args.confirm:
            raise ValueError("MCP action test requires --confirm because it can create an external side effect")
        policy = mcp_action_config_from_settings(settings)
        if policy is None:
            raise ValueError("No external action profile is configured")
        try:
            arguments = json.loads(args.arguments_json)
        except json.JSONDecodeError as exc:
            raise ValueError("MCP action test arguments must be a JSON object") from exc
        if not isinstance(arguments, dict):
            raise ValueError("MCP action test arguments must be a JSON object")
        config = mcp_action_config_for_profile(policy, args.profile)
        index = mcp_action_configs(policy).index(config)
        runtime_tool_name = mcp_action_runtime_tool_names(policy)[index]
        definition = asyncio.run(McpActionConnector(config, runtime_tool_name=runtime_tool_name).definition())
        validated = definition.validator(arguments)
        result = asyncio.run(definition.handler(validated, CancellationToken()))
        rendered_result = redact_terminal_output(result, force=True)
        try:
            safe_result: object = json.loads(rendered_result)
        except json.JSONDecodeError:
            safe_result = {"redacted_text": rendered_result}
        record = {
            "completed": True,
            "profile": config.profile,
            "runtime_tool": definition.name,
            "authority": "operator_confirmed_one_shot_external_action_test_no_company_job_or_learning",
            "result": safe_result,
        }
    elif args.mcp_command == "action-status":
        record = mcp_action_status_record(mcp_action_config_from_settings(settings))
        record["config_path"] = str(config_path.expanduser().resolve())
    elif args.mcp_command == "action-login":
        if not args.confirm:
            raise ValueError("MCP action OAuth login requires --confirm because it opens an authorization browser flow")
        policy = mcp_action_config_from_settings(settings)
        if policy is None:
            raise ValueError("No external action profile is configured")
        config = mcp_action_config_for_profile(policy, str(args.profile))
        asyncio.run(McpActionConnector(config).authorize())
        record = {
            "completed": True,
            "profile": config.profile,
            "authority": "operator_confirmed_local_oauth_browser_login_no_company_job_or_learning",
            "credential_values_stored": False,
        }
    elif args.mcp_command == "action-logout":
        if not args.confirm:
            raise ValueError("MCP action OAuth logout requires --confirm because it deletes local credentials")
        policy = mcp_action_config_from_settings(settings)
        if policy is None:
            raise ValueError("No external action profile is configured")
        config = mcp_action_config_for_profile(policy, str(args.profile))
        record = {
            "completed": True,
            "profile": config.profile,
            "oauth_state_deleted": McpActionConnector(config).clear_oauth_state(),
            "authority": "operator_confirmed_local_oauth_credential_delete_no_company_job_or_learning",
        }
    elif args.mcp_command in {"configure", "add"}:
        transport = str(args.transport).replace("-", "_")
        if transport == "stdio" and args.server_command is None:
            raise ValueError("MCP stdio configuration requires --server-command")
        if transport == "streamable_http" and not args.server_url:
            raise ValueError("MCP Streamable HTTP configuration requires --server-url")
        headers: list[tuple[str, str]] = []
        for raw_header in args.header_env:
            header, separator, environment_name = str(raw_header).partition("=")
            if not separator or not header or not environment_name:
                raise ValueError("Each --header-env value must be HEADER=ENV_NAME")
            headers.append((header, environment_name))
        config = McpReadOnlyConfig(
            python_command=args.python_command.expanduser(),
            server_command=(args.server_command.expanduser() if args.server_command is not None else None),
            server_args=tuple(args.server_arg),
            tool_names=tuple(args.tool),
            profile=str(args.profile),
            environment_names=tuple(dict.fromkeys(args.environment)),
            timeout_seconds=float(args.timeout_seconds),
            max_result_bytes=int(args.max_result_bytes),
            transport=transport,
            server_url=str(args.server_url).strip() if args.server_url else None,
            header_environment=tuple(headers),
            oauth_enabled=bool(args.oauth),
            oauth_client_id_environment=(str(args.oauth_client_id_env).strip() if args.oauth_client_id_env else None),
            oauth_client_secret_environment=(str(args.oauth_client_secret_env).strip() if args.oauth_client_secret_env else None),
            oauth_scope=(str(args.oauth_scope).strip() if args.oauth_scope else None),
        )
        target = (
            write_mcp_settings(config_path, config)
            if args.mcp_command == "configure"
            else append_mcp_settings(config_path, config)
        )
        record = {"configuration_changed": True, "config_path": str(target), **mcp_status_record(config)}
        if args.mcp_command == "add":
            record = {
                "configuration_changed": True,
                "config_path": str(target),
                **mcp_status_record(configured_mcp_policy(target, ports)),
            }
    elif args.mcp_command == "remove":
        removed = remove_mcp_profile_settings(config_path, str(args.profile))
        record = {
            "configuration_changed": removed,
            "config_path": str(config_path.expanduser().resolve()),
            **mcp_status_record(configured_mcp_policy(config_path, ports)),
        }
    elif args.mcp_command == "disable":
        removed = remove_mcp_settings(config_path)
        record = {
            "configuration_changed": removed,
            "config_path": str(config_path.expanduser().resolve()),
            **mcp_status_record(None),
        }
    elif args.mcp_command == "test":
        if not args.confirm:
            raise ValueError("MCP test requires --confirm because it starts the configured external server")
        config = mcp_config_from_settings(settings)
        if config is None:
            raise ValueError("No external read sidecar is configured")
        selected = config.selected_tool_names()
        index = int(args.tool_index) - 1
        if not 0 <= index < len(selected):
            raise ValueError(f"MCP tool index must be between 1 and {len(selected)}")
        try:
            arguments = json.loads(args.arguments_json)
        except json.JSONDecodeError as exc:
            raise ValueError("MCP test arguments must be a JSON object") from exc
        if not isinstance(arguments, dict):
            raise ValueError("MCP test arguments must be a JSON object")
        connector = mcp_connector(config)
        definitions = asyncio.run(connector.definitions())
        definition = definitions[index]
        validated = definition.validator(arguments)
        result = asyncio.run(definition.handler(validated, CancellationToken()))
        # An MCP response is untrusted external evidence.  Keep the normal
        # normalized envelope, but apply the same terminal secret redaction
        # used by the operator surfaces before rendering it.
        rendered_result = redact_terminal_output(result, force=True)
        try:
            safe_result: object = json.loads(rendered_result)
        except json.JSONDecodeError:
            safe_result = {"redacted_text": rendered_result}
        record = {
            "completed": True,
            "profile": config.profile_for_runtime_tool(definition.name),
            "tool_index": index + 1,
            "runtime_tool": definition.name,
            "authority": "operator_confirmed_one_shot_read_only_test_no_company_job_or_learning",
            "result": safe_result,
        }
    elif args.mcp_command == "login":
        if not args.confirm:
            raise ValueError("MCP OAuth login requires --confirm because it opens an authorization browser flow")
        config = mcp_config_from_settings(settings)
        if config is None:
            raise ValueError("No external read sidecar is configured")
        connector = mcp_profile_connector(config, str(args.profile))
        asyncio.run(connector.authorize())
        record = {
            "completed": True,
            "profile": str(args.profile),
            "authority": "operator_confirmed_local_oauth_browser_login_no_company_job_or_learning",
            "credential_values_stored": False,
        }
    elif args.mcp_command == "logout":
        if not args.confirm:
            raise ValueError("MCP OAuth logout requires --confirm because it deletes local credentials")
        config = mcp_config_from_settings(settings)
        if config is None:
            raise ValueError("No external read sidecar is configured")
        connector = mcp_profile_connector(config, str(args.profile))
        record = {
            "completed": True,
            "profile": str(args.profile),
            "oauth_state_deleted": connector.clear_oauth_state(),
            "authority": "operator_confirmed_local_oauth_credential_delete_no_company_job_or_learning",
        }
    elif args.mcp_command == "package":
        state_path = ports.state_path(args, settings).with_name(
            f"{ports.state_path(args, settings).stem}.evolution.db"
        )
        with EvolutionStore(state_path) as store:
            service = EvolutionNetworkService(store)
            if args.mcp_package_command in {"list", "status"}:
                packages: list[Mapping[str, object]] = []
                source = (
                    (item["artifact"] for item in service.list_active_artifacts(str(args.scope)))
                    if args.mcp_package_command == "status"
                    else service.list_artifacts(kind="TOOL_PACKAGE")
                )
                for item in source:
                    manifest = item.get("manifest")
                    if not isinstance(manifest, Mapping):
                        try:
                            manifest = json.loads(str(item["manifest_json"]))
                        except (KeyError, TypeError, json.JSONDecodeError):
                            continue
                    if isinstance(manifest, Mapping) and mcp_policy_binding_digest_from_artifact(manifest) is not None:
                        packages.append(manifest)
                configured_policy = mcp_config_from_settings(settings)
                policy_digests = () if configured_policy is None else tuple(
                    mcp_session_binding_digest(profile)
                    for profile in (
                        configured_policy.configs
                        if isinstance(configured_policy, McpReadOnlyConfigSet)
                        else (configured_policy,)
                    )
                )
                status_packages = [
                    {
                        "artifact_id": item["artifact_id"],
                        "version": item["version"],
                        "release_channel": item["release_channel"],
                        "binding_status": (
                            "MATCHES_CONFIGURED_POLICY"
                            if mcp_policy_binding_digest_from_artifact(item) in policy_digests
                            else "NO_CONFIGURED_POLICY" if not policy_digests else "DRIFTED_FROM_CONFIGURED_POLICY"
                        ),
                    }
                    for item in packages
                ]
                if args.mcp_package_command == "status":
                    record = {
                        "scope": str(args.scope),
                        "configured_policy": configured_policy is not None,
                        "active_package_count": len(status_packages),
                        "packages": status_packages,
                        "authority": "local_policy_digest_drift_projection_only_no_server_start_config_write_or_automatic_repair",
                    }
                else:
                    record = {
                        "package_count": len(packages),
                        "packages": [
                            {
                                "artifact_id": item["artifact_id"],
                                "version": item["version"],
                                "release_channel": item["release_channel"],
                                "required_capabilities": item["compatibility"]["required_capabilities"],
                                "lifecycle": "registered_only_stage_install_activate_or_rollback_are_explicit",
                            }
                            for item in packages
                        ],
                        "authority": "local_mcp_policy_package_catalog_only_no_server_start_remote_catalog_or_automatic_activation",
                    }
            else:
                config = mcp_config_from_settings(settings)
                if config is None:
                    raise ValueError("Configure an external read sidecar before creating its policy package")
                manifest = build_mcp_policy_artifact(
                    config=config,
                    artifact_id=str(args.artifact_id),
                    version=str(args.version),
                    profile=args.profile,
                )
                selected_profile = mcp_policy_profile(config, profile=args.profile)
                if args.mcp_package_command == "preview":
                    record = {
                        **service.preview_artifact_manifest(manifest),
                        "authority": "local_mcp_policy_digest_only_no_server_start_or_config_write",
                        "profile": selected_profile.profile,
                    }
                else:
                    if not args.confirm:
                        raise ValueError("MCP policy package registration requires --confirm")
                    record = {
                        "artifact": service.register_artifact_manifest(
                            manifest, ingress="MCP_POLICY_REGISTRATION"
                        ),
                        "authority": "catalog_only_stage_install_activate_remain_explicit",
                        "profile": selected_profile.profile,
                        "next_actions": (
                            "noruct evolution artifact stage <artifact-id> <version> --confirm",
                            "noruct evolution artifact install <artifact-id> <version> --confirm",
                            "noruct evolution artifact activate company_default <artifact-id> <version> --allowed-capability external_read --confirm",
                        ),
                    }
    else:
        record = mcp_status_record(mcp_config_from_settings(settings))
        record["config_path"] = str(config_path.expanduser().resolve())
    if args.mcp_command in {"test", "login", "logout", "action-test", "action-login", "action-logout"}:
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
        else:
            if args.mcp_command in {"test", "action-test"}:
                if args.mcp_command == "action-test":
                    print("External action test completed · no Company Job created", file=output)
                else:
                    print(f"External read test completed · tool #{record['tool_index']} · no Company Job created", file=output)
                print("Result is normalized untrusted evidence and terminal-redacted.", file=output)
                print(json.dumps(record["result"], ensure_ascii=False, sort_keys=True, indent=2), file=output)
            else:
                print(f"MCP OAuth {args.mcp_command} · {record['profile']}", file=output)
        return EXIT_OK
    if args.mcp_command == "package":
        if args.json:
            print(json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True), file=output)
        else:
            if args.mcp_package_command == "list":
                print(f"MCP policy package catalog · {record['package_count']} local package(s) · no server started", file=output)
            elif args.mcp_package_command == "status":
                print(f"MCP policy package status · {record['active_package_count']} active package(s) · no server started", file=output)
            else:
                action = "registered" if args.mcp_package_command == "register" else "previewed"
                print(f"MCP policy package {action} · no server started", file=output)
            if args.mcp_package_command == "register":
                print("Next: stage, install, then explicitly activate for the desired scope.", file=output)
        if args.mcp_package_command == "status":
            packages = record["packages"]
            assert isinstance(packages, list)
            return (
                EXIT_OK
                if all(item.get("binding_status") == "MATCHES_CONFIGURED_POLICY" for item in packages if isinstance(item, Mapping))
                else EXIT_INPUT
            )
        return EXIT_OK
    if getattr(args, "json", False):
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    elif args.mcp_command.startswith("action-") and not record["enabled"]:
        print("External action profile: disabled", file=output)
        print("Configure it with: noruct mcp action-configure --python-command ... --server-command ... --tool ...", file=output)
    elif args.mcp_command.startswith("action-"):
        print(
            f"External action profile: {'ready' if record['sidecar_ready'] else 'needs SDK'} · one approval-gated action",
            file=output,
        )
        print("Authority: individual HIGH approval; one call per Company Job; no automatic learning promotion", file=output)
        if record["next_action"]:
            print(f"Next: {record['next_action']}", file=output)
    elif not record["enabled"]:
        print("External read sidecar: disabled", file=output)
        print("Configure it with: noruct mcp configure --python-command ... --server-command ... --tool ...", file=output)
    else:
        print(
            f"External read sidecar: {'ready' if record['sidecar_ready'] else 'needs SDK'} · "
            f"{record['tool_count']} bounded read tool(s)",
            file=output,
        )
        print("Authority: explicit read-only allowlist; external writes, installation, and automatic discovery are disabled", file=output)
        if record["next_action"]:
            print(f"Next: {record['next_action']}", file=output)
    return EXIT_OK if not record.get("enabled") or bool(record.get("sidecar_ready", False)) else EXIT_INPUT
