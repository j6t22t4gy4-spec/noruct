"""Application adapter for parsed Noruct Network commands.

CLI parsing and evolution-state path derivation remain at the product ingress.
This module receives the parsed command and explicit local catalog path, then
uses the existing signed-artifact lifecycle without creating another registry,
installer, or Company authority.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, TextIO

from dynamic_firm.evolution.store import EvolutionStore
from dynamic_firm.network import NoructNetworkService
from dynamic_firm.runtime.models import to_primitive


NETWORK_COMMAND_OK = 0


def network_human_summary(command: str, payload: object) -> str:
    if not isinstance(payload, Mapping):
        return "Noruct Network action complete"
    if command == "source":
        if "sources" in payload:
            return f"Noruct Network sources · {len(payload['sources'])} trusted source(s)"
        return f"Noruct Network source registered · {payload['source']['source_id']}"
    if command == "discover":
        return f"Noruct Network discovery · {len(payload['registries'])} immutable registry pointer(s)"
    if command == "search":
        return (
            "Noruct Network local catalog search · "
            f"{len(payload['available'])} installed/cataloged · {len(payload['staged'])} staged"
        )
    if command == "stage":
        return f"Noruct Network registry staged · {payload['snapshot']['snapshot_id']} · not installed"
    if command == "review":
        return f"Noruct Network registry review · {payload['status']}"
    if command == "install":
        return (
            "Noruct Network template installed · "
            f"{payload['artifact']['artifact_id']}@{payload['artifact']['version']} · "
            f"origin={payload['artifact']['origin_kind']} · inactive"
        )
    if command == "activate":
        activation = payload["activation"]
        return (
            "Noruct Network template active for future Jobs · "
            f"{activation['artifact_id']}@{activation['version']} · "
            f"origin={activation['artifact']['origin_kind']}"
        )
    if command == "rollback":
        activation = payload["activation"]
        return (
            f"Noruct Network rollback active · {activation['artifact_id']}@{activation['version']}"
            f" · origin={activation['artifact']['origin_kind']}"
        )
    if command == "update-mode":
        return f"Noruct Network update mode · {payload['preference']['mode']}"
    if command == "updates":
        return f"Noruct Network update preferences · {len(payload['preferences'])} artifact(s)"
    if command == "sync":
        return "Noruct Network automatic sync is disabled · explicit stage/review/install/activate required"
    return "Noruct Network action complete"


def run_network_command(args: argparse.Namespace, *, state_path: Path, output: TextIO) -> int:
    """Run the product-level Network surface over the existing local catalog."""

    command = args.network_command
    with EvolutionStore(state_path) as store:
        service = NoructNetworkService(store)
        if command == "source":
            if args.network_source_command == "list":
                payload: object = {"sources": service.list_sources(), "runtime_effect": "NONE"}
            elif args.network_source_command == "first-party":
                if not args.confirm:
                    raise ValueError("First-party Network source registration requires --confirm")
                payload = service.bootstrap_first_party_source(
                    allowed_signers=args.allowed_signers,
                    operator_id=args.operator_id,
                    ssh_keygen=args.ssh_keygen,
                    auto_update_enabled=False,
                )
            else:
                if not args.confirm:
                    raise ValueError("Network source registration requires --confirm")
                payload = service.register_source(
                    source_id=args.source_id,
                    publisher_class=args.publisher_class,
                    origin=args.origin,
                    allowed_signers=args.allowed_signers,
                    signer_principal=args.principal,
                    ssh_keygen=args.ssh_keygen,
                    operator_id=args.operator_id,
                    credential_env=args.credential_env,
                    private_registry_id=args.registry_id,
                    auto_update_enabled=False,
                    allow_insecure_loopback=args.allow_insecure_loopback,
                )
        elif command == "discover":
            payload = service.discover(args.source_id)
        elif command == "search":
            payload = service.search(args.query, source_id=args.source_id)
        elif command == "details":
            payload = service.details(
                artifact_id=args.artifact_id, source_id=args.source_id
            )
        elif command == "compare":
            payload = service.compare_versions(
                artifact_id=args.artifact_id,
                left_version=args.left_version,
                right_version=args.right_version,
            )
        elif command == "stage":
            if not args.confirm:
                raise ValueError("Network registry stage requires --confirm")
            payload = service.stage_discovered_registry(
                source_id=args.source_id, registry_id=args.registry_id
            )
        elif command == "review":
            if not args.confirm:
                raise ValueError("Network registry review requires --confirm")
            payload = service.review_snapshot(
                snapshot_id=args.snapshot_id,
                operator_id=args.operator_id,
                decision=args.decision,
                reason=args.reason,
            )
        elif command == "install":
            if not args.confirm:
                raise ValueError("Network template installation requires --confirm")
            payload = service.install(
                snapshot_id=args.snapshot_id,
                artifact_id=args.artifact_id,
                version=args.version,
            )
        elif command == "activate":
            if not args.confirm:
                raise ValueError("Network template activation requires --confirm")
            payload = service.activate(
                scope_key=args.scope_key,
                artifact_id=args.artifact_id,
                version=args.version,
                allowed_capabilities=tuple(args.allowed_capability),
            )
        elif command == "rollback":
            if not args.confirm:
                raise ValueError("Network template rollback requires --confirm")
            payload = service.rollback(
                scope_key=args.scope_key,
                artifact_id=args.artifact_id,
                kind=args.kind,
            )
        elif command == "update-mode":
            if not args.confirm:
                raise ValueError("Network update mode change requires --confirm")
            payload = service.set_update_mode(
                scope_key=args.scope_key,
                artifact_id=args.artifact_id,
                source_id=args.source_id,
                mode=args.mode,
            )
        elif command == "updates":
            payload = service.list_updates(args.scope_key)
        elif command == "sync":
            if not args.confirm:
                raise ValueError("First-party Network sync requires --confirm")
            payload = service.sync_first_party_updates(
                source_id=args.source_id,
                scope_key=args.scope_key,
                allowed_capabilities=tuple(args.allowed_capability),
            )
        elif command == "evaluate":
            if not args.confirm:
                raise ValueError("Registered Network evaluation requires --confirm")
            def read_evaluation_input(path: Path, label: str) -> Mapping[str, Any]:
                checked = path.expanduser().resolve()
                if not checked.is_file() or checked.stat().st_size > 64 * 1024:
                    raise ValueError(f"Network {label} must be an existing JSON file up to 64 KiB")
                try:
                    value = json.loads(checked.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"Network {label} must be valid UTF-8 JSON") from exc
                if not isinstance(value, Mapping):
                    raise ValueError(f"Network {label} must be a JSON object")
                return value

            payload = service.evaluate_registered_benchmark(
                scope_key=args.scope_key,
                benchmark_artifact_id=args.benchmark_artifact_id,
                evaluator_artifact_id=args.evaluator_artifact_id,
                blueprint=read_evaluation_input(args.blueprint, "Blueprint"),
                delta=read_evaluation_input(args.delta, "Blueprint Delta"),
            )
        else:
            raise ValueError(f"unknown network command: {command}")
    if args.json:
        print(json.dumps(to_primitive(payload), ensure_ascii=False, sort_keys=True, indent=2), file=output)
    else:
        print(network_human_summary(command, payload), file=output)
    return NETWORK_COMMAND_OK
