from __future__ import annotations

"""Typed Network operator commands shared by the TUI and a future GUI.

This module is intentionally a thin product adapter over
``NoructNetworkService``.  It owns neither Registry state nor a second
installation workflow.  Mutating operations require an explicit JSON
``confirm: true`` field so Settings can stage a visible command and a future
GUI can use the exact same request shape.
"""

import json
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.evolution.store import EvolutionStore
from dynamic_firm.network import NoructNetworkService
from dynamic_firm.product.modern_tui import ModernTerminalCommandResult


def _object(argument: str, *, fields: set[str]) -> Mapping[str, Any]:
    try:
        payload = json.loads(argument)
    except json.JSONDecodeError as exc:
        raise ValueError("Network command requires one JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("Network command has unsupported or missing fields")
    return payload


def _confirmed(payload: Mapping[str, Any]) -> None:
    if payload.get("confirm") is not True:
        raise ValueError("Network mutation requires confirm=true")


def _state_path(owner: Any) -> Path:
    return owner.state_path.with_name(f"{owner.state_path.stem}.evolution.db")


def _source_lines(service: NoructNetworkService) -> ModernTerminalCommandResult:
    sources = service.list_sources()
    if not sources:
        return ModernTerminalCommandResult(
            messages=(
                "Noruct Network catalog is local but has no trusted sources.",
                "Use /network source-add JSON to register one signed origin.",
            )
        )
    return ModernTerminalCommandResult(
        messages=tuple(
            f"{item['source_id']} · {item['publisher_class']} · "
            "manual exact-version updates only"
            + (
                f" · private registry {item['private_registry_id']}"
                if item.get("publisher_class") == "PRIVATE_TEAM"
                else ""
            )
            for item in sources
        )
    )


def execute_network_command(owner: Any, argument: str) -> ModernTerminalCommandResult:
    """Run a bounded Network command against the one local Artifact catalog."""

    action, _, raw = argument.partition(" ")
    action = action.strip().lower()
    query = raw.strip()
    state_path = _state_path(owner)
    if action == "install" and not query:
        return ModernTerminalCommandResult(messages=(
            "Network install is explicit: `noruct network install SNAPSHOT_ID ARTIFACT_ID VERSION --confirm`.",
            "In the TUI use /network install JSON with confirm=true; activation remains a separate future-Job action.",
        ))
    if action == "rollback" and not query:
        return ModernTerminalCommandResult(messages=(
            "Network rollback is explicit and future-Job only: `noruct network rollback SCOPE [KIND] --artifact-id ARTIFACT --confirm`.",
            "In the TUI use /network rollback JSON with confirm=true.",
        ))
    read_only = {"", "sources", "search", "updates", "permissions", "trust"}
    if not state_path.is_file() and action in read_only:
        if action == "permissions":
            return ModernTerminalCommandResult(messages=(
                "Template activation must name already permitted local capabilities. Templates cannot add credentials, arbitrary binaries, or a new external-state grant.",
                "Only registered local adapters may be bound; running Jobs retain their frozen artifact snapshot.",
            ))
        if action == "trust":
            return ModernTerminalCommandResult(messages=(
                "Network boundary · discover → signature verify → stage → review → install → explicit local activation → Job pin → rollback.",
                "Templates cannot add credentials, unbounded authority, or arbitrary downloaded code. Running Jobs keep their pinned versions.",
            ))
        return ModernTerminalCommandResult(messages=(
            "Noruct Network has no local catalog yet.",
            "Register a signed source with /network source-add JSON; source registration, download, review, and activation remain separate steps.",
        ))
    try:
        with EvolutionStore(state_path, timeout_seconds=0.5) as store:
            service = NoructNetworkService(store)
            if action in {"", "sources"}:
                return _source_lines(service)
            if action == "search":
                result = service.search(query)
                total = tuple(result["available"]) + tuple(result["staged"])
                return ModernTerminalCommandResult(messages=(
                    ("No locally trusted Network template matches that search.",)
                    if not total else tuple(
                        f"{item.get('artifact_id')}@{item.get('version')} · {item.get('kind')} · {item.get('release_channel', item.get('snapshot_status', 'cataloged'))}"
                        for item in total[:12]
                    )
                ))
            if action == "updates":
                result = service.list_updates("company_default")
                preferences = tuple(result["preferences"])
                return ModernTerminalCommandResult(messages=(
                    "No Network update preferences for company_default; all templates are pinned."
                    if not preferences else "Network update preferences · " + ", ".join(
                        f"{item['artifact_id']}={item['mode']}" for item in preferences
                    ),
                ))
            if action == "permissions":
                return ModernTerminalCommandResult(messages=(
                    "Template activation must name already permitted local capabilities. Templates cannot add credentials, arbitrary binaries, or a new external-state grant.",
                    "Only registered local adapters may be bound; running Jobs retain their frozen artifact snapshot.",
                ))
            if action == "trust":
                return ModernTerminalCommandResult(messages=(
                    "Network boundary · discover → signature verify → stage → review → install → explicit local activation → Job pin → rollback.",
                    "Templates cannot add credentials, unbounded authority, or arbitrary downloaded code. Running Jobs keep their pinned versions.",
                ))
            if action == "source-add":
                payload = _object(query, fields={"confirm", "source_id", "publisher_class", "origin", "allowed_signers_path", "signer_principal", "ssh_keygen_path", "operator_id", "credential_env", "private_registry_id", "allow_insecure_loopback"})
                _confirmed(payload)
                result = service.register_source(
                    source_id=str(payload["source_id"]), publisher_class=str(payload["publisher_class"]),
                    origin=str(payload["origin"]), allowed_signers=Path(str(payload["allowed_signers_path"])),
                    signer_principal=str(payload["signer_principal"]), ssh_keygen=Path(str(payload["ssh_keygen_path"])),
                    operator_id=str(payload["operator_id"]), credential_env=(None if payload["credential_env"] is None else str(payload["credential_env"])),
                    private_registry_id=(None if payload["private_registry_id"] is None else str(payload["private_registry_id"])),
                    auto_update_enabled=False,
                    allow_insecure_loopback=payload["allow_insecure_loopback"] is True,
                )
                source = result["source"]
                return ModernTerminalCommandResult(messages=(f"Trusted Network source saved · {source['source_id']} · {source['publisher_class']}. No download or installation occurred.",))
            if action == "stage":
                payload = _object(query, fields={"confirm", "source_id", "registry_id"})
                _confirmed(payload)
                result = service.stage_discovered_registry(source_id=str(payload["source_id"]), registry_id=str(payload["registry_id"]))
                return ModernTerminalCommandResult(messages=(f"Network Registry staged for review · {result['snapshot']['snapshot_id']}. It is not installed or active.",))
            if action == "review":
                payload = _object(query, fields={"confirm", "snapshot_id", "operator_id", "decision", "reason"})
                _confirmed(payload)
                result = service.review_snapshot(snapshot_id=str(payload["snapshot_id"]), operator_id=str(payload["operator_id"]), decision=str(payload["decision"]), reason=str(payload["reason"]))
                return ModernTerminalCommandResult(messages=(f"Network snapshot review recorded · {result['snapshot_id']} · {result['decision']}.",))
            if action == "install":
                payload = _object(query, fields={"confirm", "snapshot_id", "artifact_id", "version"})
                _confirmed(payload)
                result = service.install(snapshot_id=str(payload["snapshot_id"]), artifact_id=str(payload["artifact_id"]), version=str(payload["version"]))
                return ModernTerminalCommandResult(messages=(f"Network artifact installed but inactive · {result['artifact']['artifact_id']}@{result['artifact']['version']} · origin={result['artifact']['origin_kind']}.",))
            if action == "activate":
                payload = _object(query, fields={"confirm", "scope_key", "artifact_id", "version", "allowed_capabilities"})
                _confirmed(payload)
                capabilities = payload["allowed_capabilities"]
                if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
                    raise ValueError("Network activation allowed_capabilities must be a string list")
                result = service.activate(scope_key=str(payload["scope_key"]), artifact_id=str(payload["artifact_id"]), version=str(payload["version"]), allowed_capabilities=tuple(capabilities))
                return ModernTerminalCommandResult(messages=(f"Network artifact activated for future Jobs · {result['activation']['artifact']['artifact_id']}@{result['activation']['artifact']['version']} · origin={result['activation']['artifact']['origin_kind']}.",))
            if action == "rollback":
                payload = _object(query, fields={"confirm", "scope_key", "artifact_id", "kind"})
                _confirmed(payload)
                result = service.rollback(scope_key=str(payload["scope_key"]), artifact_id=(None if payload["artifact_id"] is None else str(payload["artifact_id"])), kind=(None if payload["kind"] is None else str(payload["kind"])))
                return ModernTerminalCommandResult(messages=(f"Network rollback selected · {result['activation']['artifact']['artifact_id']}@{result['activation']['artifact']['version']} · origin={result['activation']['artifact']['origin_kind']} for future Jobs.",))
            if action == "update-mode":
                payload = _object(query, fields={"confirm", "scope_key", "artifact_id", "source_id", "mode"})
                _confirmed(payload)
                result = service.set_update_mode(scope_key=str(payload["scope_key"]), artifact_id=str(payload["artifact_id"]), source_id=str(payload["source_id"]), mode=str(payload["mode"]))
                return ModernTerminalCommandResult(messages=(f"Network update policy saved · {result['artifact_id']}={result['mode']}.",))
    except (OSError, ValueError, KeyError) as exc:
        return ModernTerminalCommandResult(messages=(f"Noruct Network operation was not applied · {exc}",))
    return ModernTerminalCommandResult(messages=(
        "Network command: /network [sources|search QUERY|updates|permissions|trust] or /network [source-add|stage|review|install|activate|rollback|update-mode] JSON.",
        "Every mutation requires confirm=true and affects only the local future-Job catalog.",
    ))
