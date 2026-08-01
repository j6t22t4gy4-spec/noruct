"""Argument schema for Network and local operator control-plane commands."""
from __future__ import annotations

import argparse
from pathlib import Path

from dynamic_firm.application.company_cli_parser import add_company_commands
from dynamic_firm.application.evolution_cli_parser import add_evolution_paths
from dynamic_firm.application.foundation_cli import (
    add_foundation_core_commands,
    add_foundation_evidence_commands,
)
from dynamic_firm.network import NETWORK_PUBLISHER_CLASSES, NETWORK_UPDATE_MODES


def add_operator_control_commands(
    commands: argparse._SubParsersAction,
    *,
    default_state_path: Path,
    provider_cli_choices,
    add_execution_options,
) -> None:
    network = commands.add_parser(
        "network",
        help="Discover, trust, install, update, and roll back signed Noruct Network templates.",
    )
    network_commands = network.add_subparsers(dest="network_command", required=True)
    network_source = network_commands.add_parser(
        "source", help="Register or inspect local trusted Network sources."
    )
    network_source_commands = network_source.add_subparsers(
        dest="network_source_command", required=True
    )
    network_source_add = network_source_commands.add_parser(
        "add", help="Register one signed FIRST_PARTY, COMMUNITY, or PRIVATE_TEAM source."
    )
    network_source_add.add_argument("source_id")
    network_source_add.add_argument("--publisher-class", choices=tuple(sorted(NETWORK_PUBLISHER_CLASSES)), required=True)
    network_source_add.add_argument("--origin", required=True)
    network_source_add.add_argument("--allowed-signers", type=Path, required=True)
    network_source_add.add_argument("--principal", required=True)
    network_source_add.add_argument("--ssh-keygen", type=Path, required=True)
    network_source_add.add_argument("--operator-id", required=True)
    network_source_add.add_argument(
        "--credential-env",
        help="Optional bearer credential environment-variable name; required for PRIVATE_TEAM sources and never stored as a value.",
    )
    network_source_add.add_argument(
        "--registry-id",
        help="Required PRIVATE_TEAM registry identifier; private registries are never remotely enumerated.",
    )
    network_source_add.add_argument("--allow-insecure-loopback", action="store_true")
    network_source_add.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_source_add)
    network_source_first_party = network_source_commands.add_parser(
        "first-party",
        help="Register the official First-party origin from a locally reviewed allowed-signers policy; it does not install templates.",
    )
    network_source_first_party.add_argument(
        "--allowed-signers",
        type=Path,
        required=True,
        help="Locally reviewed OpenSSH allowed-signers policy. No key material is downloaded or stored by this command.",
    )
    network_source_first_party.add_argument("--operator-id", required=True)
    network_source_first_party.add_argument(
        "--ssh-keygen",
        type=Path,
        help="Absolute OpenSSH ssh-keygen verifier path. Defaults to ssh-keygen resolved from PATH.",
    )
    network_source_first_party.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_source_first_party)
    network_source_list = network_source_commands.add_parser("list", help="List trusted local Network source metadata without network I/O.")
    add_evolution_paths(network_source_list)

    network_discover = network_commands.add_parser(
        "discover", help="List immutable public registry pointers from one trusted source; no bundle is installed."
    )
    network_discover.add_argument("source_id")
    add_evolution_paths(network_discover)
    network_search = network_commands.add_parser(
        "search", help="Search locally verified or installed Agent, Tool, Skill, Workflow, and Benchmark templates."
    )
    network_search.add_argument("query", nargs="?", default="")
    network_search.add_argument("--source-id")
    add_evolution_paths(network_search)
    network_details = network_commands.add_parser(
        "details", help="Show locally verified immutable versions, manifests, provenance, and current adapter effect."
    )
    network_details.add_argument("artifact_id")
    network_details.add_argument("--source-id")
    add_evolution_paths(network_details)
    network_compare = network_commands.add_parser(
        "compare", help="Compare two locally verified versions of one Network Artifact without activation."
    )
    network_compare.add_argument("artifact_id")
    network_compare.add_argument("left_version")
    network_compare.add_argument("right_version")
    add_evolution_paths(network_compare)
    network_stage = network_commands.add_parser(
        "stage", help="Fetch one index-pinned registry, verify its local trust root, and stage it without installation."
    )
    network_stage.add_argument("source_id")
    network_stage.add_argument("registry_id")
    network_stage.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_stage)
    network_review = network_commands.add_parser(
        "review", help="Approve or reject one trusted staged Network registry snapshot."
    )
    network_review.add_argument("snapshot_id")
    network_review.add_argument("--decision", choices=("APPROVE", "REJECT"), required=True)
    network_review.add_argument("--operator-id", required=True)
    network_review.add_argument("--reason", required=True)
    network_review.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_review)
    network_install = network_commands.add_parser(
        "install", help="Import, provenance-bind, stage, and install one reviewed Network Artifact without activating it."
    )
    network_install.add_argument("snapshot_id")
    network_install.add_argument("artifact_id")
    network_install.add_argument("version")
    network_install.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_install)
    network_activate = network_commands.add_parser(
        "activate", help="Explicitly activate one installed Network Artifact for future Jobs only."
    )
    network_activate.add_argument("scope_key")
    network_activate.add_argument("artifact_id")
    network_activate.add_argument("version")
    network_activate.add_argument("--allowed-capability", action="append", default=[])
    network_activate.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_activate)
    network_rollback = network_commands.add_parser(
        "rollback", help="Restore the immediately previous installed Network Artifact version."
    )
    network_rollback.add_argument("scope_key")
    network_rollback.add_argument("kind", nargs="?")
    network_rollback.add_argument("--artifact-id")
    network_rollback.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_rollback)
    network_update_mode = network_commands.add_parser(
        "update-mode", help="Set PINNED or PROPOSE; imported Artifacts never auto-activate."
    )
    network_update_mode.add_argument("scope_key")
    network_update_mode.add_argument("artifact_id")
    network_update_mode.add_argument("source_id")
    network_update_mode.add_argument("--mode", choices=tuple(sorted(NETWORK_UPDATE_MODES)), required=True)
    network_update_mode.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_update_mode)
    network_updates = network_commands.add_parser(
        "updates", help="List source-bound Network update preferences and local tracker state."
    )
    network_updates.add_argument("scope_key")
    add_evolution_paths(network_updates)
    network_sync = network_commands.add_parser(
        "sync", help="Show that automatic Network updates are disabled; use explicit stage/review/install/activate."
    )
    network_sync.add_argument("source_id")
    network_sync.add_argument("scope_key")
    network_sync.add_argument("--allowed-capability", action="append", default=[])
    network_sync.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_sync)
    network_evaluate = network_commands.add_parser(
        "evaluate",
        help="Run an explicitly selected registered local Benchmark/Evaluator pair; no publisher code is executed or promoted.",
    )
    network_evaluate.add_argument("scope_key")
    network_evaluate.add_argument("benchmark_artifact_id")
    network_evaluate.add_argument("evaluator_artifact_id")
    network_evaluate.add_argument("blueprint", type=Path)
    network_evaluate.add_argument("delta", type=Path)
    network_evaluate.add_argument("--confirm", action="store_true")
    add_evolution_paths(network_evaluate)
    foundation = commands.add_parser(
        "foundation",
        help="Verify the private Noruct employee-agent foundation.",
    )
    foundation_commands = foundation.add_subparsers(
        dest="foundation_command", required=True
    )
    from dynamic_firm.application.foundation_cli import (
        add_foundation_core_commands,
        add_foundation_evidence_commands,
    )

    add_foundation_core_commands(
        foundation_commands,
        default_state_path=default_state_path,
    )
    add_foundation_evidence_commands(
        foundation_commands,
        default_state_path=default_state_path,
    )
    from dynamic_firm.application.company_cli_parser import add_company_commands

    add_company_commands(commands)
    setup = commands.add_parser(
        "setup",
        help="Write non-secret provider and runtime configuration.",
    )
    setup.add_argument(
        "--provider",
        dest="provider_kind",
        choices=provider_cli_choices(),
        default=None,
    )
    setup.add_argument("--base-url", default=None)
    setup.add_argument("--model", default=None)
    setup.add_argument("--codex-command", default=None)
    setup.add_argument("--external-command", default=None)
    setup.add_argument("--request-timeout", type=float, default=None, metavar="SECONDS")
    setup.add_argument("--stale-timeout", type=float, default=None, metavar="SECONDS")
    setup.add_argument("--api-key-env", default=None)
    setup.add_argument("--no-auth", action="store_true", default=None)
    setup.add_argument("--state", type=Path, default=None)
    setup.add_argument("--force", action="store_true")
    provider = commands.add_parser(
        "provider",
        help="Inspect configured provider authentication or explicitly probe documented model metadata without invoking a model.",
    )
    provider_commands = provider.add_subparsers(dest="provider_command", required=True)
    provider_status = provider_commands.add_parser("status", help="Show non-secret configuration and authentication readiness without network access.")
    provider_status.add_argument("--json", action="store_true")
    provider_login = provider_commands.add_parser(
        "login",
        help="Open the user-managed external Codex CLI login flow; Noruct never reads or stores its credentials.",
    )
    provider_login.add_argument("--confirm", action="store_true")
    provider_preflight = provider_commands.add_parser("preflight", help="Call one documented model-list metadata endpoint; no model is invoked.")
    provider_preflight.add_argument("--timeout-seconds", type=float, default=10.0)
    provider_preflight.add_argument("--confirm", action="store_true")
    provider_preflight.add_argument("--json", action="store_true")
    update = commands.add_parser(
        "update",
        help="Inspect or explicitly activate an already-installed immutable Noruct release; it never downloads a release.",
    )
    update_commands = update.add_subparsers(dest="update_command", required=True)
    update_status = update_commands.add_parser("status", help="List managed local versions and the active command target without network access.")
    update_status.add_argument("--install-root", type=Path, default=None)
    update_status.add_argument("--bin-dir", type=Path, default=None)
    update_status.add_argument("--json", action="store_true")
    update_activate = update_commands.add_parser("activate", help="Atomically select an already-installed version for the Noruct command.")
    update_activate.add_argument("version")
    update_activate.add_argument("--install-root", type=Path, default=None)
    update_activate.add_argument("--bin-dir", type=Path, default=None)
    update_activate.add_argument("--confirm", action="store_true")
    update_activate.add_argument("--json", action="store_true")
    acp = commands.add_parser(
        "acp",
        help="Run Noruct as an Agent Client Protocol stdio server for a local editor.",
    )
    add_execution_options(acp)
    acp.set_defaults(permission_mode="ask")
    acp.add_argument(
        "--check",
        action="store_true",
        help="Validate the selected provider and workspace configuration without starting the ACP server.",
    )
    mcp = commands.add_parser(
        "mcp",
        help="Configure or inspect the bounded user-managed external-read sidecar.",
    )
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_status = mcp_commands.add_parser(
        "status", help="Show sidecar readiness without connecting to a server."
    )
    mcp_status.add_argument("--json", action="store_true")
    mcp_action_status = mcp_commands.add_parser(
        "action-status",
        help="Show separate approval-gated external action profiles without starting their servers.",
    )
    mcp_action_status.add_argument("--json", action="store_true")
    mcp_action_configure = mcp_commands.add_parser(
        "action-configure",
        help="Replace the action policy with one explicit stdio or HTTPS external action. It remains individually approval-gated in Company Jobs.",
    )
    mcp_action_configure.add_argument("--python-command", type=Path, required=True)
    mcp_action_configure.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    mcp_action_configure.add_argument("--server-command", type=Path, default=None)
    mcp_action_configure.add_argument("--server-url", default=None)
    mcp_action_configure.add_argument("--server-arg", action="append", default=[])
    mcp_action_configure.add_argument("--header-env", action="append", default=[], metavar="HEADER=ENV_NAME")
    mcp_action_configure.add_argument("--oauth", action="store_true")
    mcp_action_configure.add_argument("--oauth-client-id-env", default=None, metavar="ENV_NAME")
    mcp_action_configure.add_argument("--oauth-client-secret-env", default=None, metavar="ENV_NAME")
    mcp_action_configure.add_argument("--oauth-scope", default=None, metavar="SCOPE")
    mcp_action_configure.add_argument("--tool", required=True)
    mcp_action_configure.add_argument("--profile", default="external-action")
    mcp_action_configure.add_argument("--environment", action="append", default=[])
    mcp_action_configure.add_argument("--timeout-seconds", type=float, default=15.0)
    mcp_action_configure.add_argument("--max-result-bytes", type=int, default=48_000)
    mcp_action_configure.add_argument("--json", action="store_true")
    mcp_action_add = mcp_commands.add_parser(
        "action-add",
        help="Add one explicit stdio or HTTPS action profile, up to four; every runtime action remains individually approval-gated.",
    )
    mcp_action_add.add_argument("--python-command", type=Path, required=True)
    mcp_action_add.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    mcp_action_add.add_argument("--server-command", type=Path, default=None)
    mcp_action_add.add_argument("--server-url", default=None)
    mcp_action_add.add_argument("--server-arg", action="append", default=[])
    mcp_action_add.add_argument("--header-env", action="append", default=[], metavar="HEADER=ENV_NAME")
    mcp_action_add.add_argument("--oauth", action="store_true")
    mcp_action_add.add_argument("--oauth-client-id-env", default=None, metavar="ENV_NAME")
    mcp_action_add.add_argument("--oauth-client-secret-env", default=None, metavar="ENV_NAME")
    mcp_action_add.add_argument("--oauth-scope", default=None, metavar="SCOPE")
    mcp_action_add.add_argument("--tool", required=True)
    mcp_action_add.add_argument("--profile", required=True)
    mcp_action_add.add_argument("--environment", action="append", default=[])
    mcp_action_add.add_argument("--timeout-seconds", type=float, default=15.0)
    mcp_action_add.add_argument("--max-result-bytes", type=int, default=48_000)
    mcp_action_add.add_argument("--json", action="store_true")
    mcp_action_remove = mcp_commands.add_parser(
        "action-remove",
        help="Remove one named external action profile without changing MCP read-sidecars.",
    )
    mcp_action_remove.add_argument("--profile", required=True)
    mcp_action_remove.add_argument("--json", action="store_true")
    mcp_action_disable = mcp_commands.add_parser(
        "action-disable",
        help="Remove all [mcp_action] profiles; the read-sidecar stays unchanged.",
    )
    mcp_action_disable.add_argument("--json", action="store_true")
    mcp_action_test = mcp_commands.add_parser(
        "action-test",
        help="Run the configured external action once as an operator-only confirmation check; no Company Job or learning is created.",
    )
    mcp_action_test.add_argument("--arguments-json", required=True, help="JSON object for the configured action input schema.")
    mcp_action_test.add_argument("--profile", default=None, help="Required when more than one action profile is configured.")
    mcp_action_test.add_argument("--confirm", action="store_true")
    mcp_action_test.add_argument("--json", action="store_true")
    mcp_action_login = mcp_commands.add_parser(
        "action-login",
        help="Open a browser for explicit OAuth authorization of one configured HTTPS action profile.",
    )
    mcp_action_login.add_argument("profile")
    mcp_action_login.add_argument("--confirm", action="store_true")
    mcp_action_login.add_argument("--json", action="store_true")
    mcp_action_logout = mcp_commands.add_parser(
        "action-logout",
        help="Remove only the local OAuth tokens/client registration for one configured action profile.",
    )
    mcp_action_logout.add_argument("profile")
    mcp_action_logout.add_argument("--confirm", action="store_true")
    mcp_action_logout.add_argument("--json", action="store_true")
    def add_mcp_profile_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--python-command", type=Path, required=True)
        command.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
        command.add_argument("--server-command", type=Path, default=None)
        command.add_argument("--server-url", default=None)
        command.add_argument("--server-arg", action="append", default=[])
        command.add_argument(
            "--header-env",
            action="append",
            default=[],
            metavar="HEADER=ENV_NAME",
            help="For Streamable HTTP only: map one non-secret header name to an environment variable.",
        )
        command.add_argument(
            "--oauth",
            action="store_true",
            help="For Streamable HTTP only: require operator-confirmed OAuth login and keep tokens in the local credential store.",
        )
        command.add_argument("--oauth-client-id-env", default=None, metavar="ENV_NAME")
        command.add_argument("--oauth-client-secret-env", default=None, metavar="ENV_NAME")
        command.add_argument("--oauth-scope", default=None, metavar="SCOPE")
        command.add_argument("--tool", action="append", required=True)
        command.add_argument("--profile", default="external-context")
        command.add_argument("--environment", action="append", default=[])
        command.add_argument("--timeout-seconds", type=float, default=10.0)
        command.add_argument("--max-result-bytes", type=int, default=48_000)
        command.add_argument("--json", action="store_true")

    mcp_configure = mcp_commands.add_parser(
        "configure", help="Replace all explicit read-only sidecar profile configuration."
    )
    add_mcp_profile_arguments(mcp_configure)
    mcp_add = mcp_commands.add_parser(
        "add", help="Add one explicit local read-only sidecar profile (maximum four)."
    )
    add_mcp_profile_arguments(mcp_add)
    mcp_remove = mcp_commands.add_parser(
        "remove", help="Remove one named local read-only sidecar profile."
    )
    mcp_remove.add_argument("profile")
    mcp_remove.add_argument("--json", action="store_true")
    mcp_disable = mcp_commands.add_parser(
        "disable", help="Remove only the [mcp] table; provider and run settings remain unchanged."
    )
    mcp_disable.add_argument("--json", action="store_true")
    mcp_test = mcp_commands.add_parser(
        "test",
        help="Discover and invoke one explicitly selected configured read tool; no Company Job or learning is created.",
    )
    mcp_test.add_argument(
        "--tool-index",
        type=int,
        required=True,
        help="One-based position in the configured allowlist; upstream tool names are not emitted by this command.",
    )
    mcp_test.add_argument("--arguments-json", required=True, help="JSON object for the selected tool input schema.")
    mcp_test.add_argument("--confirm", action="store_true")
    mcp_test.add_argument("--json", action="store_true")
    mcp_login = mcp_commands.add_parser(
        "login",
        help="Open a browser for explicit OAuth authorization of one configured Streamable HTTP profile.",
    )
    mcp_login.add_argument("profile")
    mcp_login.add_argument("--confirm", action="store_true")
    mcp_login.add_argument("--json", action="store_true")
    mcp_logout = mcp_commands.add_parser(
        "logout",
        help="Remove only the local OAuth tokens/client registration for one configured profile.",
    )
    mcp_logout.add_argument("profile")
    mcp_logout.add_argument("--confirm", action="store_true")
    mcp_logout.add_argument("--json", action="store_true")
    mcp_package = mcp_commands.add_parser(
        "package",
        help="Preview or register an immutable local MCP policy package; it never starts a server.",
    )
    mcp_package_commands = mcp_package.add_subparsers(dest="mcp_package_command", required=True)
    for name, help_text in (
        ("preview", "Preview the EXPERIMENTAL package derived from the configured MCP policy."),
        ("register", "Register the package in the local Evolution catalog; stage/install/activate remain explicit."),
    ):
        command = mcp_package_commands.add_parser(name, help=help_text)
        command.add_argument("--artifact-id", default="mcp_policy")
        command.add_argument("--version", required=True)
        command.add_argument(
            "--profile",
            default=None,
            help="One configured MCP profile to bind. Required when more than one profile exists.",
        )
        command.add_argument("--state", type=Path, default=None)
        command.add_argument("--json", action="store_true")
        if name == "register":
            command.add_argument("--confirm", action="store_true")
    mcp_package_list = mcp_package_commands.add_parser(
        "list",
        help="List registered local MCP policy packages without starting an MCP server or contacting a marketplace.",
    )
    mcp_package_list.add_argument("--state", type=Path, default=None)
    mcp_package_list.add_argument("--json", action="store_true")
    mcp_package_status = mcp_package_commands.add_parser(
        "status",
        help="Check whether active local MCP policy packages still match configured local policy digests; it never starts a server.",
    )
    mcp_package_status.add_argument("--scope", default="company_default")
    mcp_package_status.add_argument("--state", type=Path, default=None)
    mcp_package_status.add_argument("--json", action="store_true")
