#!/usr/bin/env python3
"""Run a bounded, named provider-free Noruct test shard.

The repository's test suite intentionally covers several execution planes.  A
single ``unittest discover`` process makes a failure hard to localise and is
needlessly expensive for a contributor who only changed one plane.  This
runner keeps the canonical unittest suites intact and gives local development
and CI the same small, inspectable shard contract.

``acceptance`` is deliberately a cross-plane lane.  It uses only local fake
providers and temporary workspaces, while exercising the operator promises a
new Company must preserve: setup/settings persistence, provider ingress,
approval before a write, Graph proposal continuation, and explicit Knowledge
candidate acceptance.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


# Every provider-free Python test belongs to exactly one broad implementation
# shard.  ``acceptance`` is an intentionally overlapping, fast release gate.
SHARDS: dict[str, tuple[str, ...]] = {
    "ingress": (
        "tests.test_packaging",
        "tests.test_installer",
        "tests.test_runtime_selection",
        "tests.test_openai_media",
        "tests.test_capability_status",
        "tests.test_cli",
        "tests.test_component_budget_verifier",
        "tests.test_component_import_boundaries",
        "tests.test_pre_release_qualification",
        "tests.test_provider_free_test_shard",
        "tests.application",
        "tests.providers",
    ),
    "company": (
        "tests.company",
        "tests.compiler",
        "tests.kernel",
    ),
    "runtime": ("tests.runtime",),
    "product": ("tests.product",),
    "knowledge": ("tests.knowledge",),
    "evolution": (
        "tests.evolution",
        "tests.evaluation",
        "tests.foundation",
    ),
    "acceptance": (
        "tests.test_cli.CliTests.test_setup_writes_config_without_accepting_a_secret_value",
        "tests.test_cli.CliTests.test_run_reaches_local_openai_compatible_provider_end_to_end",
        "tests.test_cli.CliTests.test_interactive_write_requires_visible_approval_before_mutation",
        "tests.runtime.test_shadow_coding.ShadowCodingRuntimeTests.test_small_codex_command_request_exposes_host_command_tool",
        "tests.product.test_modern_tui.ModernTerminalTests.test_settings_modal_keeps_selection_open_until_done",
        "tests.product.test_modern_tui.ModernTerminalTests.test_connection_settings_exposes_account_login_and_model_picker",
        "tests.product.test_modern_tui.ModernTerminalTests.test_pending_graph_proposal_is_explicitly_resolved_from_job_audit",
        "tests.product.test_windows_operator_qualification.WindowsOperatorQualificationTests.test_manager_knowledge_delegation_tool_approval_and_session_resume_are_local",
        "tests.kernel.test_service.FirmKernelTests.test_approved_graph_proposal_resumes_the_same_job_once",
        "tests.knowledge.test_service.UserKnowledgeServiceTests.test_write_candidate_requires_explicit_accept_and_preserves_provenance_scope",
        "tests.product.test_knowledge_commands.LocalKnowledgeCommandTests.test_workbench_candidate_review_requires_explicit_accept_or_reject",
    ),
}

# ``unittest discover -s tests`` does not recursively collect the repository's
# namespace-package test directories.  A release-wide run must therefore use
# the same explicit directory entries as CI's implementation shards, without
# the intentionally overlapping acceptance smoke lane.
FULL_SHARDS = ("ingress", "company", "runtime", "product", "knowledge", "evolution")


def _suite_args(target: str) -> list[str]:
    """Return a unittest invocation for a package/module/test target."""

    if target in {
        "tests.application",
        "tests.company",
        "tests.compiler",
        "tests.evaluation",
        "tests.evolution",
        "tests.foundation",
        "tests.kernel",
        "tests.knowledge",
        "tests.product",
        "tests.providers",
        "tests.runtime",
    }:
        relative = Path(*target.split("."))
        return ["discover", "-s", str(relative), "-q"]
    return [target, "-q"]


def run(shard: str) -> int:
    for target in SHARDS[shard]:
        command = [sys.executable, "-m", "unittest", *_suite_args(target)]
        print("+", " ".join(command), flush=True)
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


def run_all() -> int:
    """Run every non-overlapping provider-free implementation shard."""

    for shard in FULL_SHARDS:
        result = run(shard)
        if result:
            return result
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", choices=tuple(SHARDS), help="test shard to run")
    parser.add_argument(
        "--all",
        action="store_true",
        help="run every non-overlapping provider-free implementation shard",
    )
    parser.add_argument("--list", action="store_true", help="list available test shards")
    parser.add_argument(
        "--require-modern-terminal",
        action="store_true",
        help="fail unless the audited optional Modern TUI profile is installed",
    )
    args = parser.parse_args(argv)
    if args.list:
        for name, targets in SHARDS.items():
            print(f"{name}: {len(targets)} suite(s)")
        return 0
    if args.all and args.shard:
        parser.error("--all and --shard are mutually exclusive")
    if not args.all and not args.shard:
        parser.error("--shard or --all is required unless --list is used")
    if args.require_modern_terminal:
        from dynamic_firm.product.modern_tui import modern_terminal_available

        if not modern_terminal_available():
            parser.error("the audited Modern TUI profile is required for this lane")
    return run_all() if args.all else run(args.shard)


if __name__ == "__main__":
    raise SystemExit(main())
