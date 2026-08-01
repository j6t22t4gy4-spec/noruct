"""Operator connectivity command adapters.

This component owns direct, explicit configuration and operator-only tests for
local browser/computer, media, search, and Home Assistant capabilities. It
does not create Company Jobs or own Company state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output
from dynamic_firm.browser_connector import (
    BrowserReadOnlyConfig,
    BrowserReadOnlyConnector,
    browser_config_from_settings,
    configured_node_version,
)
from dynamic_firm.computer_use_connector import (
    ComputerUseConfig,
    ComputerUseConnector,
    computer_use_config_from_settings,
    configured_driver_version,
)
from dynamic_firm.home_assistant import (
    HomeAssistantConfig,
    config_from_settings as home_assistant_config_from_settings,
    remove_home_assistant_settings,
    status as home_assistant_status,
    write_home_assistant_settings,
)
from dynamic_firm.openai_media import OpenAIMediaConfig, media_config_from_settings
from dynamic_firm.product.browser_lifecycle import (
    browser_lifecycle_status,
    close_isolated_browser,
    launch_isolated_browser,
    lifecycle_state_path,
)
from dynamic_firm.product.browser_settings import (
    remove_browser_settings,
    write_browser_settings,
)
from dynamic_firm.product.computer_use_settings import (
    remove_computer_use_settings,
    write_computer_use_settings,
)
from dynamic_firm.product.openai_media_settings import (
    remove_media_settings,
    write_media_settings,
)
from dynamic_firm.product.web_search_settings import (
    remove_web_search_settings,
    write_web_search_settings,
)
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.web_search import (
    WEB_SEARCH_TOOL,
    SearxngSearchConfig,
    config_from_settings as web_search_config_from_settings,
)

EXIT_OK = 0
EXIT_INPUT = 2

def browser_status_record(config: BrowserReadOnlyConfig | None) -> dict[str, object]:
    if config is None:
        return {
            "enabled": False,
            "authority": "no_local_browser_capability",
            "next_action": "noruct browser configure",
        }
    version = configured_node_version(config)
    return {
        "enabled": True,
        "node_version": version,
        "node_ready": version is not None,
        "endpoint": "configured_loopback",
        "public_tools": ["list_browser_tabs", "read_browser_page"] + (["navigate_browser_tab", "click_browser_element", "type_browser_text"] if config.allow_control else []) + (["capture_browser_screenshot"] if config.capture_directory is not None else []),
        "browser_control": "approval_only_existing_tabs" if config.allow_control else "not_enabled",
        "screenshot_capture": "approval_only_local_artifact" if config.capture_directory is not None else "not_enabled",
        "cookie_access": "not_enabled",
        "authority": "explicit_user_managed_loopback_browser_with_approval_only_control" if config.allow_control else "explicit_user_managed_loopback_browser_read_only",
        "next_action": None if version is not None else "Configure a Node 22+ executable for the local browser sidecar.",
    }


def run_browser_command(
    args: argparse.Namespace,
    settings: dict,
    config_path: Path,
    output: TextIO,
) -> int:
    lifecycle_path = lifecycle_state_path(config_path)
    if args.browser_command == "configure":
        config = BrowserReadOnlyConfig(
            node_command=args.node_command.expanduser(),
            cdp_endpoint=str(args.cdp_endpoint).strip(),
            timeout_seconds=float(args.timeout_seconds),
            max_result_bytes=int(args.max_result_bytes),
            allow_control=bool(args.allow_control),
            capture_directory=args.capture_directory.expanduser() if args.capture_directory is not None else None,
        )
        target = write_browser_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **browser_status_record(config)}
    elif args.browser_command == "launch":
        if not args.confirm:
            raise ValueError("Browser launch requires --confirm because it starts an isolated local browser process")
        config = browser_config_from_settings(settings)
        if config is None:
            raise ValueError("Configure the Node browser sidecar before launching an isolated browser")
        launched = launch_isolated_browser(
            state_path=lifecycle_path,
            browser_command=args.browser_executable.expanduser(),
            timeout_seconds=float(args.timeout_seconds),
        )
        updated = replace(config, cdp_endpoint=launched.endpoint)
        target = write_browser_settings(config_path, updated)
        record = {
            "launched": True,
            "config_path": str(target),
            "lifecycle": browser_lifecycle_status(lifecycle_path),
            **browser_status_record(updated),
        }
    elif args.browser_command == "close":
        if not args.confirm:
            raise ValueError("Browser close requires --confirm because it terminates the Noruct-managed isolated browser process")
        record = {
            "closed": close_isolated_browser(lifecycle_path),
            "config_path": str(config_path.expanduser().resolve()),
            "lifecycle": browser_lifecycle_status(lifecycle_path),
            **browser_status_record(browser_config_from_settings(settings)),
        }
    elif args.browser_command == "disable":
        record = {
            "configuration_changed": remove_browser_settings(config_path),
            "config_path": str(config_path.expanduser().resolve()),
            **browser_status_record(None),
        }
    elif args.browser_command == "test":
        if not args.confirm:
            raise ValueError("Browser test requires --confirm because it reads the configured local browser")
        config = browser_config_from_settings(settings)
        if config is None:
            raise ValueError("No local browser read profile is configured")
        connector = BrowserReadOnlyConnector(config)
        definitions = connector.definitions()
        if args.tab_index is None:
            definition = definitions[0]
            result = asyncio.run(definition.handler({}, CancellationToken()))
            operation = "tabs"
        else:
            definition = definitions[1]
            arguments = definition.validator({"tab_index": int(args.tab_index)})
            result = asyncio.run(definition.handler(arguments, CancellationToken()))
            operation = "snapshot"
        rendered = redact_terminal_output(result, force=True)
        try:
            safe_result = json.loads(rendered)
        except json.JSONDecodeError:
            safe_result = {"redacted_text": rendered}
        record = {
            "completed": True,
            "operation": operation,
            "authority": "operator_confirmed_local_browser_read_no_company_job_or_learning",
            "result": safe_result,
        }
    else:
        record = browser_status_record(browser_config_from_settings(settings))
        record["config_path"] = str(config_path.expanduser().resolve())
    record.setdefault("lifecycle", browser_lifecycle_status(lifecycle_path))
    if args.browser_command == "test":
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(f"Local browser {record['operation']} test completed · no Company Job created", file=output)
            print(json.dumps(record["result"], ensure_ascii=False, sort_keys=True, indent=2), file=output)
        return EXIT_OK
    if getattr(args, "json", False):
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    elif not record["enabled"]:
        print("Local browser profile: disabled", file=output)
    else:
        print(
            f"Local browser profile: {'ready' if record['node_ready'] else 'needs Node'} · {len(record['public_tools'])} bounded tools",
            file=output,
        )
        lifecycle = record.get("lifecycle", {})
        lifecycle_state = "managed isolated browser running" if isinstance(lifecycle, dict) and lifecycle.get("running") else "existing configured loopback browser"
        print(f"Authority: {lifecycle_state}; raw CDP, cookies, downloads, uploads and form submission remain disabled", file=output)
        if record["next_action"]:
            print(f"Next: {record['next_action']}", file=output)
    return EXIT_OK if not record.get("enabled") or bool(record.get("node_ready", False)) else EXIT_INPUT


def computer_use_status_record(config: ComputerUseConfig | None) -> dict[str, object]:
    if config is None:
        return {
            "enabled": False,
            "authority": "no_local_computer_use_capability",
            "next_action": "noruct computer-use configure",
        }
    version = configured_driver_version(config)
    return {
        "enabled": True,
        "driver_ready": version is not None,
        "driver_version": version,
        "allowed_app_count": len(config.allowed_apps),
        "public_tools": ["computer_use"],
        "actions": ["capture", "list_apps", "wait"] + (["click", "double_click", "right_click", "middle_click", "drag", "scroll", "type", "key", "set_value"] if config.allow_control else []),
        "control": "approval_only_configured_apps" if config.allow_control else "capture_and_inventory_only",
        "screenshot_model_context": False,
        "credential_access": "not_enabled",
        "authority": "explicit_user_managed_driver_configured_app_allowlist_per_action_approval",
        "next_action": None if version is not None else "Install/configure a user-managed cua-driver executable; Noruct does not install it automatically.",
    }


def media_status_record(config: OpenAIMediaConfig | None) -> dict[str, object]:
    if config is None:
        return {
            "enabled": False,
            "authority": "no_direct_media_capability",
            "next_action": "noruct media configure --enable image",
        }
    return {
        "enabled": True,
        "ready": bool(os.environ.get(config.api_key_env)),
        "credential_environment": config.api_key_env,
        "credential_values_stored": False,
        "public_tools": {
            "image": "generate_image",
            "speech": "synthesize_speech",
            "transcription": "transcribe_audio",
            "video": "generate_video",
        },
        "enabled_capabilities": list(config.enabled_capabilities),
        "approval": "individual_high_risk_no_session_approval",
        "artifact_boundary": "new_workspace_relative_artifact_paths_only",
        "authority": "explicit_openai_media_endpoints_per_action_approval_no_background_polling_after_job",
        "next_action": None if os.environ.get(config.api_key_env) else f"Set {config.api_key_env} in the environment before starting a Company Job.",
    }


def run_media_command(
    args: argparse.Namespace,
    settings: dict,
    config_path: Path,
    output: TextIO,
) -> int:
    if args.media_command == "configure":
        enabled = set(args.enable)
        config = OpenAIMediaConfig(
            api_key_env=str(args.api_key_env),
            image_enabled="image" in enabled,
            speech_enabled="speech" in enabled,
            transcription_enabled="transcription" in enabled,
            video_enabled="video" in enabled,
            image_model=str(args.image_model),
            speech_model=str(args.speech_model),
            transcription_model=str(args.transcription_model),
            video_model=str(args.video_model),
            timeout_seconds=float(args.timeout_seconds),
            video_timeout_seconds=float(args.video_timeout_seconds),
        )
        target = write_media_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **media_status_record(config)}
    elif args.media_command == "disable":
        record = {"configuration_changed": remove_media_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **media_status_record(None)}
    else:
        record = media_status_record(media_config_from_settings(settings))
        record["config_path"] = str(config_path.expanduser().resolve())
    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    elif not record["enabled"]:
        print("Direct media capability: disabled", file=output)
    else:
        state = "ready" if record["ready"] else "needs credential environment"
        print(f"Direct media capability: {state} · {', '.join(record['enabled_capabilities'])}", file=output)
        print("Authority: one HIGH-approved operation per tool per Company Job; prompts/audio may be sent to the configured external service.", file=output)
        if record["next_action"]:
            print(f"Next: {record['next_action']}", file=output)
    return EXIT_OK if not record.get("enabled") or bool(record.get("ready")) else EXIT_INPUT


def web_search_status_record(config: SearxngSearchConfig | None) -> dict[str, object]:
    if config is None:
        return {"enabled": False, "authority": "no_user_managed_web_search", "next_action": "noruct web-search configure --base-url https://search.example"}
    return {
        "enabled": True,
        "ready": True,
        "base_url": config.normalized_base_url,
        "max_results": config.max_results,
        "public_tools": [WEB_SEARCH_TOOL],
        "credential_access": "none",
        "trust": "untrusted_search_metadata_only",
        "authority": "explicit_user_managed_searxng_endpoint_bounded_network_read",
        "next_action": None,
    }


def run_web_search_command(args: argparse.Namespace, settings: dict, config_path: Path, output: TextIO) -> int:
    if args.web_search_command == "configure":
        config = SearxngSearchConfig(
            base_url=str(args.base_url), timeout_seconds=float(args.timeout_seconds),
            max_results=int(args.max_results), max_result_bytes=int(args.max_result_bytes),
        )
        target = write_web_search_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **web_search_status_record(config)}
    elif args.web_search_command == "disable":
        record = {"configuration_changed": remove_web_search_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **web_search_status_record(None)}
    else:
        record = web_search_status_record(web_search_config_from_settings(settings))
        record["config_path"] = str(config_path.expanduser().resolve())
    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    elif not record["enabled"]:
        print("SearXNG web-search capability: disabled", file=output)
        print("Configure it with: noruct web-search configure --base-url http://127.0.0.1:8080", file=output)
    else:
        print(f"SearXNG web-search capability: ready · {record['base_url']} · {record['max_results']} results per call", file=output)
        print("Search result metadata is untrusted; Noruct never follows result links automatically.", file=output)
    return EXIT_OK


def run_home_assistant_command(args: argparse.Namespace, settings: dict, config_path: Path, output: TextIO) -> int:
    if args.home_assistant_command == "configure":
        config = HomeAssistantConfig(
            base_url=str(args.base_url).strip(), token_env=str(args.token_env).strip(),
            allowed_entities=tuple(dict.fromkeys(str(item).strip() for item in args.allow_entity)),
            allowed_services=tuple(dict.fromkeys(str(item).strip() for item in args.allow_service)),
            timeout_seconds=float(args.timeout_seconds), max_result_bytes=int(args.max_result_bytes),
        )
        target = write_home_assistant_settings(config_path, config)
        record: dict[str, object] = {"configuration_changed": True, "config_path": str(target), **home_assistant_status(config)}
    elif args.home_assistant_command == "disable":
        record = {"configuration_changed": remove_home_assistant_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **home_assistant_status(None)}
    else:
        record = dict(home_assistant_status(home_assistant_config_from_settings(settings)))
        record["config_path"] = str(config_path.expanduser().resolve())
    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    elif not record["enabled"]:
        print("Home Assistant tools: disabled", file=output)
        print("Configure with: noruct home-assistant configure --base-url HTTPS_OR_LOOPBACK_URL --allow-entity light.example", file=output)
    else:
        print(f"Home Assistant tools: {'ready' if record['ready'] else 'needs token environment'} · {len(record['allowed_entities'])} entity pattern(s)", file=output)
        print("Read tools are bounded; every service call requires an individual approval.", file=output)
        if record.get("next_action"): print(f"Next: {record['next_action']}", file=output)
    return EXIT_OK if args.home_assistant_command in {"configure", "disable"} or not record.get("enabled") or bool(record.get("ready", False)) else EXIT_INPUT

def run_computer_use_command(
    args: argparse.Namespace,
    settings: dict,
    config_path: Path,
    output: TextIO,
) -> int:
    if args.computer_use_command == "configure":
        config = ComputerUseConfig(
            driver_command=args.driver_command.expanduser(),
            allowed_apps=tuple(args.allow_app),
            timeout_seconds=float(args.timeout_seconds),
            max_result_bytes=int(args.max_result_bytes),
            allow_control=bool(args.allow_control),
        )
        target = write_computer_use_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **computer_use_status_record(config)}
    elif args.computer_use_command == "disable":
        record = {
            "configuration_changed": remove_computer_use_settings(config_path),
            "config_path": str(config_path.expanduser().resolve()),
            **computer_use_status_record(None),
        }
    elif args.computer_use_command in {"test", "preflight"}:
        if not args.confirm:
            raise ValueError(f"Computer-use {args.computer_use_command} requires --confirm because it contacts the configured local driver")
        config = computer_use_config_from_settings(settings)
        if config is None:
            raise ValueError("No local computer-use policy is configured")
        connector = ComputerUseConnector(config)
        definition = connector.definitions()[0]
        arguments = definition.validator(
            {"action": "capture", "app": str(args.app)}
            if args.computer_use_command == "test"
            else {"action": "list_apps"}
        )
        result = asyncio.run(definition.handler(arguments, CancellationToken()))
        try:
            safe_result = json.loads(redact_terminal_output(result, force=True))
        except json.JSONDecodeError:
            safe_result = {"redacted_text": redact_terminal_output(result, force=True)}
        record = {
            "completed": True,
            "operation": "capture" if args.computer_use_command == "test" else "list_allowed_apps",
            "authority": (
                "operator_confirmed_local_computer_capture_no_company_job_or_learning"
                if args.computer_use_command == "test"
                else "operator_confirmed_local_computer_allowed_app_preflight_no_screen_capture_company_job_or_learning"
            ),
            "result": safe_result,
        }
    else:
        record = computer_use_status_record(computer_use_config_from_settings(settings))
        record["config_path"] = str(config_path.expanduser().resolve())
    if args.computer_use_command in {"test", "preflight"}:
        if args.json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(
                "Local computer-use capture test completed · no Company Job created"
                if args.computer_use_command == "test"
                else "Local computer-use allowed-app preflight completed · no screen capture or Company Job created",
                file=output,
            )
            print(json.dumps(record["result"], ensure_ascii=False, sort_keys=True, indent=2), file=output)
        return EXIT_OK
    if getattr(args, "json", False):
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    elif not record["enabled"]:
        print("Local computer-use policy: disabled", file=output)
    else:
        print(
            f"Local computer-use policy: {'ready' if record['driver_ready'] else 'needs cua-driver'} · "
            f"{len(record['actions'])} bounded action(s)",
            file=output,
        )
        print("Authority: configured app allowlist and frozen capability trust policy; screenshots never enter model context", file=output)
        if record["next_action"]:
            print(f"Next: {record['next_action']}", file=output)
    return EXIT_OK if not record.get("enabled") or bool(record.get("driver_ready", False)) else EXIT_INPUT

