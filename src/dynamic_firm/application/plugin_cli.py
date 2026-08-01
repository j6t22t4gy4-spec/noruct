"""Executable-plugin operator command adapter.

This component owns explicit plugin catalog and local install lifecycle dispatch.
Plugin registry and dependency-environment authority remain in the product stores;
it does not create Company Jobs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, TextIO

from dynamic_firm.product.executable_plugins import (
    ExecutablePlugin,
    ExecutablePluginStore,
    PluginRuntimeConfig,
    plugin_config_from_settings,
)
from dynamic_firm.product.plugin_catalog import PluginCatalogSource, PluginCatalogStore
from dynamic_firm.product.plugin_settings import (
    configured_plugin_runtime,
    remove_plugin_settings,
    write_plugin_settings,
)

EXIT_OK = 0


def settings_table(settings: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = settings.get(key, {})
    return value if isinstance(value, Mapping) else {}


def plugin_status_record(config: PluginRuntimeConfig | None) -> dict[str, object]:
    if config is None:
        return {
            "enabled": False,
            "installed_count": 0,
            "active_count": 0,
            "authority": "no_executable_plugin_runtime",
            "next_action": "noruct plugin install <local-plugin-directory> --confirm",
        }
    store = ExecutablePluginStore(config.root)
    installed = store.list()
    catalogs = PluginCatalogStore(config.root).list()
    active = config.plugins
    enabled = [item for item in installed if item.enabled]
    dependency_environments = [
        {
            "plugin_id": item.plugin_id,
            "version": item.version,
            "ready": item.dependency_environment_ready,
        }
        for item in installed
        if item.dependency_lock is not None
    ]
    receipts = [
        {
            "schema": "noruct.capability-receipt.v1",
            "kind": "EXECUTABLE_PLUGIN",
            "plugin_id": item.plugin_id,
            "version": item.version,
            "state": "ACTIVE_FUTURE_JOB" if item.enabled else "INSTALLED_INACTIVE",
            "package_sha256": item.package_digest,
            "source": dict(store.source_receipt(item.plugin_id, version=item.version)),
            "dependency_lock_sha256": item.dependency_lock_digest,
            "dependency_environment_ready": item.dependency_environment_ready,
            "automatic_replacement": False,
            "running_job_mutation": False,
        }
        for item in installed
    ]
    return {
        "enabled": True,
        "root": str(config.root),
        "installed_count": len(installed),
        "enabled_count": len(enabled),
        "active_count": len(active),
        "active_plugins": [
            {"plugin_id": item.plugin_id, "version": item.version, "tools": [tool.name for tool in item.tools]}
            for item in active
        ],
        "dependency_environments": dependency_environments,
        "receipts": receipts,
        "catalog_count": len(catalogs),
        "catalogs": [
            {"catalog_id": item.catalog_id, "digest": item.digest, "entry_count": len(item.entries)}
            for item in catalogs
        ],
        "authority": "explicit_local_package_enable_out_of_process_high_approval_per_tool_call",
        "next_action": (
            None
            if active
            else (
                f"noruct plugin environment-build {enabled[0].plugin_id} --version {enabled[0].version} --confirm"
                if any(item.dependency_lock is not None and not item.dependency_environment_ready for item in enabled)
                else "noruct plugin enable <plugin_id> --confirm"
            )
        ),
    }


def plugin_root(config_path: Path, root: Path | None = None) -> Path:
    return (root if root is not None else config_path.expanduser().resolve().parent / "plugins").expanduser().resolve()


def _installed_projection(
    store: ExecutablePluginStore,
    installed: ExecutablePlugin,
) -> dict[str, object]:
    """Show exact intake identity without exposing local paths or secret values."""

    plugin_id = installed.plugin_id
    version = installed.version
    dependency_lock = installed.dependency_lock
    return {
        "plugin_id": plugin_id,
        "version": version,
        "state": "INSTALLED_INACTIVE",
        "enabled": False,
        "package_sha256": installed.package_digest,
        "source": dict(store.source_receipt(plugin_id, version=version)),
        "dependency_closure": {
            "lock_file": dependency_lock,
            "lock_sha256": installed.dependency_lock_digest,
            "environment_ready": installed.dependency_environment_ready,
            "install_mode": (
                "none"
                if dependency_lock is None
                else "explicit_hash_locked_environment_build"
            ),
        },
        "declared_capabilities": {
            "tools": [tool.name for tool in installed.tools],
            "effects": ["EXECUTE"],
            "requires_per_call_approval": True,
            "environment_names": list(installed.environment_names),
            "process_boundary": "out_of_process_not_an_os_network_sandbox",
        },
        "activation_requested": False,
        "next_action": (
            f"noruct plugin enable {plugin_id} --version {version} --confirm"
        ),
    }


def run_plugin_command(args: argparse.Namespace, settings: dict, config_path: Path, output: TextIO) -> int:
    configured = configured_plugin_runtime(config_path)
    if args.plugin_command == "history":
        root = plugin_root(config_path, args.root)
        receipts = ExecutablePluginStore(root).lifecycle_receipts(
            args.plugin_id, limit=args.limit
        )
        record: dict[str, object] = {
            "configuration_changed": False,
            "network_attempted": False,
            "plugin_id": args.plugin_id,
            "receipt_count": len(receipts),
            "receipts": list(receipts),
            "authority": "bounded_content_free_local_lifecycle_history_no_host_load_or_execution",
        }
    elif args.plugin_command == "catalog-source-add":
        if not args.confirm:
            raise ValueError("Catalog source registration requires --confirm")
        root = plugin_root(config_path, args.root)
        source = PluginCatalogStore(root).register_source(
            PluginCatalogSource(
                catalog_id=str(args.catalog_id), source_url=str(args.url), signature_url=str(args.signature_url),
                allowed_signers_path=args.allowed_signers, principal=str(args.principal), ssh_keygen=args.ssh_keygen,
            )
        )
        record: dict[str, object] = {
            "configuration_changed": True, "network_attempted": False,
            "source": {"catalog_id": source.catalog_id, "source_url": source.source_url, "signature_url": source.signature_url, "principal": source.principal},
            "authority": "local_registered_catalog_origin_explicit_refresh_only_no_install_enable_or_execution",
            "next_action": f"noruct plugin catalog-refresh {source.catalog_id} --confirm",
        }
    elif args.plugin_command == "catalog-source-list":
        root = plugin_root(config_path, args.root)
        sources = PluginCatalogStore(root).list_sources()
        record = {
            "configuration_changed": False, "network_attempted": False,
            "source_count": len(sources),
            "sources": [{"catalog_id": item.catalog_id, "source_url": item.source_url, "signature_url": item.signature_url, "principal": item.principal} for item in sources],
            "authority": "local_catalog_origin_metadata_only_no_network_or_plugin_execution",
        }
    elif args.plugin_command == "catalog-source-remove":
        if not args.confirm:
            raise ValueError("Catalog source removal requires --confirm")
        root = plugin_root(config_path, args.root)
        record = {
            "configuration_changed": PluginCatalogStore(root).remove_source(str(args.catalog_id)),
            "network_attempted": False, "catalog_id": str(args.catalog_id),
            "authority": "local_catalog_origin_removal_only_staged_catalogs_and_plugins_unchanged",
        }
    elif args.plugin_command == "catalog-refresh":
        if not args.confirm:
            raise ValueError("Catalog refresh requires --confirm because it contacts the registered HTTPS endpoints")
        root = plugin_root(config_path, args.root)
        catalog = PluginCatalogStore(root).refresh_source(str(args.catalog_id))
        record = {
            "configuration_changed": False, "network_attempted": True, "catalog_staged": True,
            "catalog_id": catalog.catalog_id, "catalog_digest": catalog.digest, "entry_count": len(catalog.entries),
            "authority": "registered_origin_explicit_signed_refresh_only_no_install_enable_or_execution",
            "next_action": f"noruct plugin catalog-candidates --catalog-id {catalog.catalog_id}",
        }
    elif args.plugin_command == "catalog-fetch":
        if not args.confirm:
            raise ValueError("Catalog fetch requires --confirm because it contacts the supplied HTTPS endpoints")
        root = plugin_root(config_path, args.root)
        catalog = PluginCatalogStore(root).fetch_and_stage(
            source_url=str(args.url), signature_url=str(args.signature_url),
            allowed_signers_path=args.allowed_signers, principal=str(args.principal),
            ssh_keygen=args.ssh_keygen,
        )
        record: dict[str, object] = {
            "configuration_changed": False,
            "network_attempted": True,
            "catalog_staged": True,
            "authority": "signed_catalog_discovery_only_no_plugin_install_enable_or_execution",
            "catalog_id": catalog.catalog_id,
            "catalog_digest": catalog.digest,
            "entry_count": len(catalog.entries),
            "entries": [{"plugin_id": item.plugin_id, "version": item.version, "description": item.description} for item in catalog.entries],
            "next_action": f"noruct plugin catalog-install {catalog.catalog_id} <plugin-id> --confirm",
        }
    elif args.plugin_command == "catalog-list":
        root = plugin_root(config_path, args.root)
        store = PluginCatalogStore(root)
        catalogs = store.list()
        latest = {item.catalog_id: item.digest for item in store.latest()}
        record = {
            "configuration_changed": False,
            "network_attempted": False,
            "catalog_count": len(catalogs),
            "catalogs": [{"catalog_id": item.catalog_id, "digest": item.digest, "entry_count": len(item.entries), "source_url": item.source_url, "verified_at": item.verified_at, "current": latest.get(item.catalog_id) == item.digest} for item in catalogs],
            "authority": "local_verified_catalog_metadata_only_no_plugin_execution",
        }
    elif args.plugin_command == "catalog-candidates":
        root = plugin_root(config_path, args.root)
        candidates = PluginCatalogStore(root).candidates(
            ExecutablePluginStore(root).list(),
            catalog_id=(str(args.catalog_id) if args.catalog_id is not None else None),
        )
        record = {
            "configuration_changed": False,
            "network_attempted": False,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "catalog_id": item.catalog_id,
                    "catalog_digest": item.catalog_digest,
                    "plugin_id": item.plugin_id,
                    "candidate_version": item.candidate_version,
                    "description": item.description,
                    "installed_versions": list(item.installed_versions),
                    "next_action": f"noruct plugin catalog-install {item.catalog_id} {item.plugin_id} --version {item.candidate_version} --catalog-digest {item.catalog_digest} --confirm",
                }
                for item in candidates
            ],
            "authority": "local_catalog_to_local_receipt_comparison_only_no_version_ordering_install_enable_or_execution",
        }
    elif args.plugin_command == "catalog-install":
        if not args.confirm:
            raise ValueError("Catalog installation requires --confirm")
        root = plugin_root(config_path, args.root)
        store = ExecutablePluginStore(root)
        installed = PluginCatalogStore(root).install(args.catalog_id, args.plugin_id, version=args.version, catalog_digest=args.catalog_digest, plugin_store=store)
        target = write_plugin_settings(config_path, root)
        record = {
            "configuration_changed": True,
            "catalog_install": True,
            "catalog_id": str(args.catalog_id),
            "config_path": str(target),
            "installed": _installed_projection(store, installed),
            "activation_requested": False,
            **plugin_status_record(configured_plugin_runtime(target)),
            "authority": "verified_catalog_exact_git_commit_installed_inactive_no_execution_authority",
        }
    elif args.plugin_command in {"install", "install-git"}:
        if not args.confirm:
            raise ValueError("Plugin installation requires --confirm")
        root = plugin_root(config_path, args.root)
        store = ExecutablePluginStore(root)
        installed = (
            store.install(args.source)
            if args.plugin_command == "install"
            else store.install_git(str(args.url), str(args.commit), subdirectory=str(args.subdirectory))
        )
        target = write_plugin_settings(config_path, root)
        record: dict[str, object] = {
            "configuration_changed": True,
            "config_path": str(target),
            "installed": _installed_projection(store, installed),
            "activation_requested": False,
            **plugin_status_record(configured_plugin_runtime(target)),
            "authority": "exact_package_installed_inactive_activation_requires_separate_confirmed_command",
        }
    elif args.plugin_command == "update-check":
        if not args.confirm:
            raise ValueError("Plugin update check requires --confirm because it contacts the configured Git repository")
        if configured is None:
            raise ValueError("Executable plugin runtime is not configured")
        review = ExecutablePluginStore(configured.root).review_git_update(
            args.plugin_id,
            ref=str(args.ref),
            version=args.version,
        )
        record = {
            "configuration_changed": False,
            "network_attempted": True,
            "authority": "operator_confirmed_git_ref_resolution_no_checkout_install_enable_or_execution",
            "plugin_id": review.plugin_id,
            "installed_version": review.installed_version,
            "installed_commit": review.installed_commit,
            "repository_url": review.repository_url,
            "subdirectory": review.subdirectory,
            "ref": review.ref,
            "candidate_commit": review.candidate_commit,
            "update_available": review.update_available,
            "next_action": (
                None
                if not review.update_available
                else f"noruct plugin install-git --url {review.repository_url} --commit {review.candidate_commit} --subdirectory {review.subdirectory} --confirm"
            ),
        }
    elif args.plugin_command == "environment-build":
        if not args.confirm:
            raise ValueError("Plugin dependency environment build requires --confirm because pip may retrieve hash-locked wheels")
        if configured is None:
            raise ValueError("Executable plugin runtime is not configured")
        built = ExecutablePluginStore(configured.root).build_dependency_environment(
            args.plugin_id,
            version=args.version,
            python_command=args.python_command,
        )
        refreshed = configured_plugin_runtime(config_path)
        record = {
            "configuration_changed": False,
            "environment_changed": True,
            "authority": "operator_confirmed_hash_locked_dependency_environment_no_plugin_execution",
            "plugin_id": built.plugin_id,
            "version": built.version,
            "dependency_lock": built.dependency_lock,
            **plugin_status_record(refreshed),
        }
    elif args.plugin_command in {"enable", "disable", "remove", "rollback"}:
        if not args.confirm:
            raise ValueError(f"Plugin {args.plugin_command} requires --confirm")
        if configured is None:
            raise ValueError("Executable plugin runtime is not configured")
        store = ExecutablePluginStore(configured.root)
        if args.plugin_command == "remove":
            changed = store.remove(args.plugin_id)
            record = {
                "configuration_changed": changed,
                "removed": args.plugin_id,
                "future_job_effect": "WITHDRAWN_FROM_REGISTRY",
                "running_job_effect": "EXACT_ASSEMBLED_PACKAGE_RETAINED",
                **plugin_status_record(configured_plugin_runtime(config_path)),
            }
        elif args.plugin_command == "rollback":
            updated = store.rollback(args.plugin_id)
            refreshed = configured_plugin_runtime(config_path)
            record = {"configuration_changed": True, "plugin_id": updated.plugin_id, "version": updated.version, "enabled": True, "rollback": "installed_previous_version", **plugin_status_record(refreshed)}
        else:
            updated = store.activate(args.plugin_id, version=args.version) if args.plugin_command == "enable" else store.set_enabled(args.plugin_id, False)
            refreshed = configured_plugin_runtime(config_path)
            record = {"configuration_changed": True, "plugin_id": updated.plugin_id, "version": updated.version, "enabled": updated.enabled, **plugin_status_record(refreshed)}
    elif args.plugin_command == "runtime-disable":
        record = {"configuration_changed": remove_plugin_settings(config_path), **plugin_status_record(None)}
    else:
        record = plugin_status_record(configured)
        if args.plugin_command == "list" and configured is not None:
            record["plugins"] = [
                {
                    "plugin_id": item.plugin_id,
                    "version": item.version,
                    "enabled": item.enabled,
                    "description": item.description,
                    "tools": [tool.name for tool in item.tools],
                    "dependency_lock": item.dependency_lock,
                    "dependency_environment_ready": item.dependency_environment_ready,
                    "source": dict(ExecutablePluginStore(configured.root).source_receipt(item.plugin_id, version=item.version)),
                }
                for item in ExecutablePluginStore(configured.root).list()
            ]
    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    elif "installed" in record:
        installed = record["installed"]
        assert isinstance(installed, Mapping)
        print(
            f"Plugin installed inactive: {installed['plugin_id']}@{installed['version']}",
            file=output,
        )
        print(f"Package SHA-256: {installed['package_sha256']}", file=output)
        print("No plugin tool was activated or executed.", file=output)
        print(f"Next: {installed['next_action']}", file=output)
    elif not record.get("enabled"):
        print("Executable plugins: disabled", file=output)
        print("Install a local package with: noruct plugin install <directory> --confirm", file=output)
    else:
        print(f"Executable plugins: {record['active_count']} active / {record['installed_count']} installed", file=output)
        for item in record.get("active_plugins", []):
            print(f"- {item['plugin_id']}@{item['version']} · {', '.join(item['tools'])}", file=output)
        trust_mode = str(settings_table(settings, "run").get("capability_trust_mode", "trusted"))
        print(
            "Each plugin tool runs out of process and is audited; "
            + (
                "strict trust asks before each call."
                if trust_mode == "strict"
                else f"{trust_mode} trust follows the configured capability policy."
            ),
            file=output,
        )
    return EXIT_OK
