"""Workflow and value evaluation argument schemas."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


def add_workflow_evaluation_commands(
    evaluation_commands: argparse._SubParsersAction,
) -> None:
    workflow_patch_extension = evaluation_commands.add_parser(
        "workflow-patch-extension",
        help=(
            "Append two isolated post-apply observations without changing the "
            "parent four-record Workflow Patch cohort."
        ),
    )
    workflow_patch_extension_commands = workflow_patch_extension.add_subparsers(
        dest="workflow_patch_extension_command",
        required=True,
    )
    workflow_patch_extension_prepare = (
        workflow_patch_extension_commands.add_parser(
            "prepare",
            help=(
                "Clone the applied Company seed, verify the immutable parent, "
                "and seal a two-slot extension without model calls."
            ),
        )
    )
    workflow_patch_extension_prepare.add_argument(
        "parent_directory",
        type=Path,
    )
    workflow_patch_extension_prepare.add_argument("directory", type=Path)
    workflow_patch_extension_prepare.add_argument(
        "--wheel",
        type=Path,
        required=True,
    )
    workflow_patch_extension_prepare.add_argument(
        "--source-root",
        type=Path,
        default=Path.cwd(),
    )
    workflow_patch_extension_prepare.add_argument("--model", required=True)
    workflow_patch_extension_prepare.add_argument(
        "--codex-command",
        default=None,
    )
    workflow_patch_extension_prepare.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
    )
    workflow_patch_extension_prepare.add_argument(
        "--max-live-model-calls",
        type=int,
        default=8,
    )
    workflow_patch_extension_prepare.add_argument(
        "--max-extension-model-calls",
        type=int,
        default=16,
    )
    workflow_patch_extension_prepare.add_argument(
        "--max-live-wall-time",
        type=float,
        default=180.0,
        metavar="SECONDS",
    )
    workflow_patch_extension_prepare.add_argument(
        "--expires-in-hours",
        type=int,
        default=168,
    )
    workflow_patch_extension_prepare.add_argument("--json", action="store_true")
    workflow_patch_extension_status = (
        workflow_patch_extension_commands.add_parser(
            "status",
            help="Verify the parent anchor, cloned Company, and next quota boundary.",
        )
    )
    workflow_patch_extension_status.add_argument("directory", type=Path)
    workflow_patch_extension_status.add_argument("--json", action="store_true")
    workflow_patch_extension_run = workflow_patch_extension_commands.add_parser(
        "run-next",
        help="Run exactly one post-apply observation after quota confirmation.",
    )
    workflow_patch_extension_run.add_argument("directory", type=Path)
    workflow_patch_extension_run.add_argument(
        "--confirm-live-quota",
        action="store_true",
    )
    workflow_patch_extension_run.add_argument("--json", action="store_true")
    workflow_patch_extension_assess = (
        workflow_patch_extension_commands.add_parser(
            "assess",
            help=(
                "Run the existing provider-free three-observation assessment "
                "without automatic rollback."
            ),
        )
    )
    workflow_patch_extension_assess.add_argument("directory", type=Path)
    workflow_patch_extension_assess.add_argument("--json", action="store_true")
    workflow_patch_extension_rollback = (
        workflow_patch_extension_commands.add_parser(
            "rollback",
            help="Explicitly roll back only after a rollback-candidate assessment.",
        )
    )
    workflow_patch_extension_rollback.add_argument("directory", type=Path)
    workflow_patch_extension_rollback.add_argument(
        "--confirm",
        action="store_true",
    )
    workflow_patch_extension_rollback.add_argument(
        "--actor",
        default="user:cli",
    )
    workflow_patch_extension_rollback.add_argument("--json", action="store_true")
    workflow_patch_extension_compare = (
        workflow_patch_extension_commands.add_parser(
            "compare",
            help="Verify the three-observation result without a model call.",
        )
    )
    workflow_patch_extension_compare.add_argument("directory", type=Path)
    workflow_patch_extension_compare.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    workflow_patch_extension_compare.add_argument("--json", action="store_true")
    workflow_patch_efficiency = evaluation_commands.add_parser(
        "workflow-patch-efficiency",
        help=(
            "Compare the current global completion contract with one task-local "
            "machine-readable projection under a source-frozen pair."
        ),
    )
    workflow_patch_efficiency_commands = (
        workflow_patch_efficiency.add_subparsers(
            dest="workflow_patch_efficiency_command",
            required=True,
        )
    )
    workflow_patch_efficiency_prepare = (
        workflow_patch_efficiency_commands.add_parser(
            "prepare",
            help=(
                "Verify the immutable KEEP extension and seal a two-slot "
                "completion-efficiency pair without model calls."
            ),
        )
    )
    workflow_patch_efficiency_prepare.add_argument(
        "parent_directory",
        type=Path,
    )
    workflow_patch_efficiency_prepare.add_argument("directory", type=Path)
    workflow_patch_efficiency_prepare.add_argument(
        "--wheel",
        type=Path,
        required=True,
    )
    workflow_patch_efficiency_prepare.add_argument(
        "--source-root",
        type=Path,
        default=Path.cwd(),
    )
    workflow_patch_efficiency_prepare.add_argument("--model", required=True)
    workflow_patch_efficiency_prepare.add_argument(
        "--codex-command",
        default=None,
    )
    workflow_patch_efficiency_prepare.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
    )
    workflow_patch_efficiency_prepare.add_argument(
        "--max-live-model-calls",
        type=int,
        default=8,
    )
    workflow_patch_efficiency_prepare.add_argument(
        "--max-pair-model-calls",
        type=int,
        default=16,
    )
    workflow_patch_efficiency_prepare.add_argument(
        "--max-live-wall-time",
        type=float,
        default=180.0,
        metavar="SECONDS",
    )
    workflow_patch_efficiency_prepare.add_argument(
        "--completion-contract",
        choices=(
            "workflow-patch-task-local-completion-contract-v1",
            "workflow-patch-task-local-system-completion-contract-v2",
            "workflow-patch-task-local-objective-completion-contract-v3",
        ),
        default="workflow-patch-task-local-completion-contract-v1",
    )
    workflow_patch_efficiency_prepare.add_argument(
        "--expires-in-hours",
        type=int,
        default=168,
    )
    workflow_patch_efficiency_prepare.add_argument(
        "--json",
        action="store_true",
    )
    workflow_patch_context_bind = (
        workflow_patch_efficiency_commands.add_parser(
            "bind-context",
            help=(
                "Create a deterministic privacy-bounded binding from one "
                "natural preflight without model calls."
            ),
        )
    )
    workflow_patch_context_bind.add_argument("preflight", type=Path)
    workflow_patch_context_bind.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    workflow_patch_context_bind.add_argument("--json", action="store_true")
    workflow_patch_bound_prepare = (
        workflow_patch_efficiency_commands.add_parser(
            "prepare-bound",
            help=(
                "Verify a binding against source, goal, profile, and immutable "
                "parent lineage without model calls."
            ),
        )
    )
    workflow_patch_bound_prepare.add_argument("parent_directory", type=Path)
    workflow_patch_bound_prepare.add_argument("binding", type=Path)
    workflow_patch_bound_prepare.add_argument(
        "--source-root",
        type=Path,
        default=Path.cwd(),
    )
    workflow_patch_bound_prepare.add_argument("--goal", default=None)
    workflow_patch_bound_prepare.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    workflow_patch_bound_prepare.add_argument("--json", action="store_true")
    workflow_patch_efficiency_status = (
        workflow_patch_efficiency_commands.add_parser(
            "status",
            help="Verify the pair, parent anchor, and next quota boundary.",
        )
    )
    workflow_patch_efficiency_status.add_argument("directory", type=Path)
    workflow_patch_efficiency_status.add_argument(
        "--json",
        action="store_true",
    )
    workflow_patch_efficiency_run = (
        workflow_patch_efficiency_commands.add_parser(
            "run-next",
            help="Run exactly one source-frozen efficiency slot.",
        )
    )
    workflow_patch_efficiency_run.add_argument("directory", type=Path)
    workflow_patch_efficiency_run.add_argument(
        "--confirm-live-quota",
        action="store_true",
    )
    workflow_patch_efficiency_run.add_argument("--json", action="store_true")
    workflow_patch_efficiency_compare = (
        workflow_patch_efficiency_commands.add_parser(
            "compare",
            help="Compare quality, repairs, calls, tokens, and safety provider-free.",
        )
    )
    workflow_patch_efficiency_compare.add_argument("directory", type=Path)
    workflow_patch_efficiency_compare.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    workflow_patch_efficiency_compare.add_argument(
        "--json",
        action="store_true",
    )
    workflow_patch_natural = (
        workflow_patch_efficiency_commands.add_parser(
            "natural-preflight",
            help=(
                "Exercise the exact product workspace identity and applied-prior "
                "selection path without a model call."
            ),
        )
    )
    workflow_patch_natural.add_argument("parent_directory", type=Path)
    workflow_patch_natural.add_argument("workspace", type=Path)
    workflow_patch_natural.add_argument(
        "--source-root",
        type=Path,
        default=Path.cwd(),
    )
    workflow_patch_natural.add_argument("--goal", default=None)
    workflow_patch_natural.add_argument("--output", type=Path, default=None)
    workflow_patch_natural.add_argument("--json", action="store_true")
    exact_context_live_pair = evaluation_commands.add_parser(
        "exact-context-live-pair",
        help=(
            "Run the source-frozen exact production-context control/candidate "
            "pair one explicitly confirmed slot at a time."
        ),
    )
    exact_context_live_commands = exact_context_live_pair.add_subparsers(
        dest="exact_context_live_command",
        required=True,
    )
    exact_context_live_prepare = exact_context_live_commands.add_parser(
        "prepare",
        help=(
            "Seal natural evidence, exact context lineage, source, wheel, and "
            "two live slots without invoking a model."
        ),
    )
    exact_context_live_prepare.add_argument("parent_directory", type=Path)
    exact_context_live_prepare.add_argument("directory", type=Path)
    exact_context_live_prepare.add_argument(
        "--binding",
        type=Path,
        required=True,
    )
    exact_context_live_prepare.add_argument(
        "--preparation",
        type=Path,
        required=True,
    )
    exact_context_live_prepare.add_argument("--wheel", type=Path, required=True)
    exact_context_live_prepare.add_argument(
        "--source-root",
        type=Path,
        default=Path.cwd(),
    )
    exact_context_live_prepare.add_argument("--model", required=True)
    exact_context_live_prepare.add_argument("--codex-command", default=None)
    exact_context_live_prepare.add_argument(
        "--python-command",
        default="python3.11",
    )
    exact_context_live_prepare.add_argument(
        "--employee-runtime",
        choices=("native", "noruct"),
        default="native",
        help="Execution port for both sealed live slots (default: native).",
    )
    exact_context_live_prepare.add_argument(
        "--runtime-python",
        default=sys.executable,
        help="Absolute worker Python when --employee-runtime noruct is selected.",
    )
    exact_context_live_prepare.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
    )
    exact_context_live_prepare.add_argument(
        "--max-live-model-calls",
        type=int,
        default=5,
    )
    exact_context_live_prepare.add_argument(
        "--max-pair-model-calls",
        type=int,
        default=10,
    )
    exact_context_live_prepare.add_argument(
        "--max-input-tokens",
        type=int,
        default=200_000,
    )
    exact_context_live_prepare.add_argument(
        "--max-output-tokens",
        type=int,
        default=8_000,
    )
    exact_context_live_prepare.add_argument(
        "--max-cost-usd",
        type=float,
        default=2.0,
    )
    exact_context_live_prepare.add_argument(
        "--max-live-wall-time",
        type=float,
        default=180.0,
        metavar="SECONDS",
    )
    exact_context_live_prepare.add_argument(
        "--expires-in-hours",
        type=int,
        default=168,
    )
    exact_context_live_prepare.add_argument("--json", action="store_true")
    exact_context_live_status = exact_context_live_commands.add_parser(
        "status",
        help="Verify the sealed pair and show the next bounded quota boundary.",
    )
    exact_context_live_status.add_argument("directory", type=Path)
    exact_context_live_status.add_argument("--json", action="store_true")
    exact_context_live_run = exact_context_live_commands.add_parser(
        "run-next",
        help="Run exactly one source-frozen slot after explicit quota confirmation.",
    )
    exact_context_live_run.add_argument("directory", type=Path)
    exact_context_live_run.add_argument(
        "--confirm-live-quota",
        action="store_true",
    )
    exact_context_live_run.add_argument("--json", action="store_true")
    exact_context_live_compare = exact_context_live_commands.add_parser(
        "compare",
        help=(
            "Compare quality, calls, repairs, tokens, safety, and attribution "
            "without invoking a model."
        ),
    )
    exact_context_live_compare.add_argument("directory", type=Path)
    exact_context_live_compare.add_argument("--output", type=Path, default=None)
    exact_context_live_compare.add_argument("--json", action="store_true")
    firm_value = evaluation_commands.add_parser(
        "firm-value",
        help="Create or evaluate the provider-free 3x2 SOLO/DYNAMIC value gate.",
    )
    firm_value.add_argument("--create-manifest", type=Path, default=None)
    firm_value.add_argument("--manifest", type=Path, default=None)
    firm_value.add_argument("--record", type=Path, action="append", default=[])
    firm_value.add_argument("--wheel", type=Path, default=None)
    firm_value.add_argument("--source-revision", default=None)
    firm_value.add_argument("--model", default=None)
    firm_value.add_argument("--company-revision", type=int, default=0)
    firm_value.add_argument("--roster-revision", type=int, default=0)
    firm_value.add_argument("--playbook-revision", type=int, default=0)
    firm_value.add_argument("--max-live-model-calls", type=int, default=4)
    firm_value.add_argument("--max-live-wall-time", type=float, default=180.0, metavar="SECONDS")
    firm_value.add_argument("--expires-in-hours", type=int, default=168)
    firm_value.add_argument("--json", action="store_true")
    firm_value_v2 = evaluation_commands.add_parser(
        "firm-value-v2",
        help="Run the provider-free 4x2 organization-identifiable benchmark v2 contract.",
    )
    firm_value_v2.add_argument("--json", action="store_true")
    firm_campaign = evaluation_commands.add_parser(
        "firm-campaign",
        help="Prepare and advance one immutable, per-run-approved 3x2 value campaign.",
    )
    campaign_commands = firm_campaign.add_subparsers(
        dest="campaign_command",
        required=True,
    )
    campaign_prepare = campaign_commands.add_parser(
        "prepare",
        help="Freeze source and wheel, then run a provider-free six-slot preflight.",
    )
    campaign_prepare.add_argument("directory", type=Path)
    campaign_prepare.add_argument("--wheel", type=Path, required=True)
    campaign_prepare.add_argument("--source-root", type=Path, default=Path.cwd())
    campaign_prepare.add_argument("--model", default=None)
    campaign_prepare.add_argument("--codex-command", default=None)
    campaign_prepare.add_argument("--request-timeout", type=float, default=None, metavar="SECONDS")
    campaign_prepare.add_argument("--company-revision", type=int, default=0)
    campaign_prepare.add_argument("--roster-revision", type=int, default=0)
    campaign_prepare.add_argument("--playbook-revision", type=int, default=0)
    campaign_prepare.add_argument("--max-live-model-calls", type=int, default=4)
    campaign_prepare.add_argument(
        "--max-live-wall-time", type=float, default=180.0, metavar="SECONDS"
    )
    campaign_prepare.add_argument("--expires-in-hours", type=int, default=168)
    campaign_prepare.add_argument("--json", action="store_true")
    campaign_status_parser = campaign_commands.add_parser(
        "status",
        help="Verify the hash chain and show the next single run without consuming quota.",
    )
    campaign_status_parser.add_argument("directory", type=Path)
    campaign_status_parser.add_argument("--json", action="store_true")
    campaign_run = campaign_commands.add_parser(
        "run-next",
        help="Run exactly one reserved slot after explicit quota confirmation.",
    )
    campaign_run.add_argument("directory", type=Path)
    campaign_run.add_argument("--confirm-live-quota", action="store_true")
    campaign_run.add_argument("--json", action="store_true")
    campaign_compare = campaign_commands.add_parser(
        "compare",
        help="Aggregate only after all six sealed records match the immutable contract.",
    )
    campaign_compare.add_argument("directory", type=Path)
    campaign_compare.add_argument("--output", type=Path, default=None)
    campaign_compare.add_argument("--json", action="store_true")
    firm_campaign_v2 = evaluation_commands.add_parser(
        "firm-campaign-v2",
        help=(
            "Prepare and advance the immutable 4x2 v2 campaign with separate quota "
            "and evaluator-risk confirmation."
        ),
    )
    campaign_v2_commands = firm_campaign_v2.add_subparsers(
        dest="campaign_command",
        required=True,
    )
    campaign_v2_prepare = campaign_v2_commands.add_parser(
        "prepare",
        help="Freeze source and wheel, then run an eight-slot provider-free preflight.",
    )
    campaign_v2_prepare.add_argument("directory", type=Path)
    campaign_v2_prepare.add_argument("--wheel", type=Path, required=True)
    campaign_v2_prepare.add_argument("--source-root", type=Path, default=Path.cwd())
    campaign_v2_prepare.add_argument("--model", default=None)
    campaign_v2_prepare.add_argument("--codex-command", default=None)
    campaign_v2_prepare.add_argument(
        "--request-timeout", type=float, default=None, metavar="SECONDS"
    )
    campaign_v2_prepare.add_argument("--company-revision", type=int, default=0)
    campaign_v2_prepare.add_argument("--roster-revision", type=int, default=0)
    campaign_v2_prepare.add_argument("--playbook-revision", type=int, default=0)
    campaign_v2_prepare.add_argument("--max-live-model-calls", type=int, default=4)
    campaign_v2_prepare.add_argument(
        "--max-live-wall-time", type=float, default=180.0, metavar="SECONDS"
    )
    campaign_v2_prepare.add_argument("--expires-in-hours", type=int, default=168)
    campaign_v2_prepare.add_argument("--json", action="store_true")
    campaign_v2_status_parser = campaign_v2_commands.add_parser(
        "status",
        help="Verify the v2 hash chain and show the next single run without quota use.",
    )
    campaign_v2_status_parser.add_argument("directory", type=Path)
    campaign_v2_status_parser.add_argument("--json", action="store_true")
    campaign_v2_run = campaign_v2_commands.add_parser(
        "run-next",
        help=(
            "Run one slot only after quota and no-OS-sandbox evaluator risk are both "
            "confirmed."
        ),
    )
    campaign_v2_run.add_argument("directory", type=Path)
    campaign_v2_run.add_argument("--confirm-live-quota", action="store_true")
    campaign_v2_run.add_argument("--confirm-evaluator-risk", action="store_true")
    campaign_v2_run.add_argument("--json", action="store_true")
    campaign_v2_compare = campaign_v2_commands.add_parser(
        "compare",
        help="Aggregate only the exact eight sealed v2 live records without provider calls.",
    )
    campaign_v2_compare.add_argument("directory", type=Path)
    campaign_v2_compare.add_argument("--output", type=Path, default=None)
    campaign_v2_compare.add_argument("--json", action="store_true")
