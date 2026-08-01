"""Argument schema composition for the Evaluation command family."""
from __future__ import annotations

import argparse
from pathlib import Path

from dynamic_firm.application.evaluation_workflow_cli_parser import (
    add_workflow_evaluation_commands,
)


def add_evaluation_commands(commands: argparse._SubParsersAction) -> None:
    evaluation = commands.add_parser(
        "eval",
        help="Run first-party offline evaluations or an explicitly confirmed live evaluation.",
    )
    evaluation_commands = evaluation.add_subparsers(dest="evaluation", required=True)
    manager_value_contract = evaluation_commands.add_parser(
        "manager-value-contract",
        help="Show the immutable four-way Manager-value campaign requirements without creating a Job or consuming quota.",
    )
    manager_value_contract.add_argument("--json", action="store_true")
    manager_campaign = evaluation_commands.add_parser(
        "manager-campaign",
        help="Prepare, inspect, or seal one explicit-evidence slot of the immutable 4x4 Manager qualification campaign.",
    )
    manager_campaign_commands = manager_campaign.add_subparsers(dest="manager_campaign_command", required=True)
    manager_campaign_prepare = manager_campaign_commands.add_parser("prepare", help="Freeze source, wheel, model and 16 arm slots without contacting a provider.")
    manager_campaign_prepare.add_argument("directory", type=Path)
    manager_campaign_prepare.add_argument("--wheel", type=Path, required=True)
    manager_campaign_prepare.add_argument("--source-root", type=Path, default=Path.cwd())
    manager_campaign_prepare.add_argument("--model", required=True)
    manager_campaign_prepare.add_argument("--company-revision", type=int, default=0)
    manager_campaign_prepare.add_argument("--roster-revision", type=int, default=0)
    manager_campaign_prepare.add_argument("--playbook-revision", type=int, default=0)
    manager_campaign_prepare.add_argument("--max-live-model-calls", type=int, default=6)
    manager_campaign_prepare.add_argument("--max-live-wall-time", type=float, default=180.0, metavar="SECONDS")
    manager_campaign_prepare.add_argument("--codex-command", default="codex", help="User-managed Codex executable recorded in the frozen campaign manifest.")
    manager_campaign_prepare.add_argument("--request-timeout", type=float, default=120.0, metavar="SECONDS")
    manager_campaign_prepare.add_argument("--json", action="store_true")
    manager_campaign_status = manager_campaign_commands.add_parser("status", help="Verify the sealed 16-slot ledger without consuming quota.")
    manager_campaign_status.add_argument("directory", type=Path); manager_campaign_status.add_argument("--json", action="store_true")
    manager_campaign_preflight = manager_campaign_commands.add_parser("preflight", help="Check the frozen source, wheel, command, ledger, and next-slot readiness without consuming quota.")
    manager_campaign_preflight.add_argument("directory", type=Path); manager_campaign_preflight.add_argument("--json", action="store_true")
    manager_campaign_rehearse = manager_campaign_commands.add_parser("rehearse", help="Run the complete provider-free 16-slot counterfactual rehearsal; it never seals or claims live evidence.")
    manager_campaign_rehearse.add_argument("--json", action="store_true")
    manager_campaign_seal = manager_campaign_commands.add_parser("seal-next", help="Seal exactly one independently produced live arm record after two explicit confirmations.")
    manager_campaign_seal.add_argument("directory", type=Path); manager_campaign_seal.add_argument("--record", type=Path, required=True)
    manager_campaign_seal.add_argument("--confirm-live-quota", action="store_true"); manager_campaign_seal.add_argument("--confirm-evaluator-risk", action="store_true"); manager_campaign_seal.add_argument("--json", action="store_true")
    manager_campaign_run = manager_campaign_commands.add_parser("run-next", help="Run and seal exactly one in-process Firm Kernel arm after two explicit confirmations.")
    manager_campaign_run.add_argument("directory", type=Path)
    manager_campaign_run.add_argument("--confirm-live-quota", action="store_true")
    manager_campaign_run.add_argument("--confirm-evaluator-risk", action="store_true")
    manager_campaign_run.add_argument("--json", action="store_true")
    manager_campaign_report = manager_campaign_commands.add_parser("report", help="Summarize a completed 16-slot campaign without creating a Patch or outcome claim.")
    manager_campaign_report.add_argument("directory", type=Path)
    manager_campaign_report.add_argument("--output", type=Path, default=None)
    manager_campaign_report.add_argument("--json", action="store_true")
    coding = evaluation_commands.add_parser(
        "coding",
        help="Exercise Compiler, Firm Kernel, shadow coding, approval, and apply end to end.",
    )
    coding.add_argument(
        "fixture",
        nargs="?",
        choices=("all", "solo-edit", "parallel-evidence", "test-guided-recovery"),
        default="all",
    )
    coding.add_argument(
        "--strategy",
        choices=("all", "dynamic", "solo", "fixed"),
        default="all",
    )
    coding.add_argument("--json", action="store_true", help="Print stable ledger-derived records.")
    coding.add_argument(
        "--live",
        action="store_true",
        help="Use the user-managed Codex runtime; consumes account or subscription quota.",
    )
    coding.add_argument(
        "--preflight-live",
        action="store_true",
        help="Rehearse the one parallel live case and check readiness without invoking a model.",
    )
    coding.add_argument(
        "--confirm-live-quota",
        action="store_true",
        help="Explicitly confirm that this one evaluation may consume Codex quota.",
    )
    coding.add_argument("--codex-command", default=None)
    coding.add_argument("--model", default=None)
    coding.add_argument("--request-timeout", type=float, default=None, metavar="SECONDS")
    coding.add_argument("--max-live-model-calls", type=int, default=4)
    coding.add_argument("--max-live-wall-time", type=float, default=180.0, metavar="SECONDS")
    coding.add_argument("--source-revision", default=None)
    coding.add_argument(
        "--wheel",
        type=Path,
        default=None,
        help="Record the verified Noruct wheel SHA-256 for Firm Value benchmarking.",
    )
    coding.add_argument("--company-revision", type=int, default=0)
    coding.add_argument("--roster-revision", type=int, default=0)
    coding.add_argument("--playbook-revision", type=int, default=0)
    coding.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the record atomically; required for live and preflight-live runs.",
    )
    tui = evaluation_commands.add_parser(
        "tui",
        help="Render offline conversation, SOLO, and approval acceptance previews.",
    )
    tui.add_argument(
        "scenario",
        nargs="?",
        choices=("all", "conversation", "solo", "approval"),
        default="all",
    )
    tui.add_argument("--width", type=int, default=80)
    tui.add_argument("--plain", action="store_true")
    tui.add_argument("--json", action="store_true", help="Print stable acceptance records.")
    company_learning = evaluation_commands.add_parser(
        "company",
        help="Evaluate NO_PATCH, Workflow Patch preview, replay, and synthetic-evidence refusal.",
    )
    company_learning.add_argument(
        "--json", action="store_true", help="Print the stable company-learning record."
    )
    patch_observation = evaluation_commands.add_parser(
        "observation",
        help="Evaluate patch exposure, alignment, bounded assessment, and no-auto-rollback.",
    )
    patch_observation.add_argument(
        "--json", action="store_true", help="Print the stable patch-observation record."
    )
    roster_patch = evaluation_commands.add_parser(
        "roster",
        help="Evaluate explicit Roster Patch revision, snapshot, and stale-apply guards.",
    )
    roster_patch.add_argument(
        "--json", action="store_true", help="Print the stable roster-governance record."
    )
    hiring = evaluation_commands.add_parser(
        "hiring",
        help="Evaluate repeated staffing demand, hiring recommendation, and no-auto-apply.",
    )
    hiring.add_argument(
        "--json", action="store_true", help="Print the stable hiring evaluation record."
    )
    hire_observation = evaluation_commands.add_parser(
        "hire-observation",
        help="Evaluate post-hire assignment attribution and no-auto-dormancy.",
    )
    hire_observation.add_argument(
        "--json", action="store_true", help="Print the stable hire-observation record."
    )
    retention_review = evaluation_commands.add_parser(
        "retention-review",
        help="Evaluate approval, auto-review, always-approve, and hard dormancy guards.",
    )
    retention_review.add_argument(
        "--json", action="store_true", help="Print the stable retention-review record."
    )
    employee_skill = evaluation_commands.add_parser(
        "employee-skill",
        help="Evaluate bounded Skill Patch proposal, versioning, attribution, and rollback offline.",
    )
    employee_skill.add_argument(
        "--json", action="store_true", help="Print the stable employee-skill record."
    )
    task_mutation = evaluation_commands.add_parser(
        "task-mutation",
        help="Evaluate bounded typed-failure RETRY/REROUTE trajectories offline.",
    )
    task_mutation.add_argument(
        "--json", action="store_true", help="Print the stable task-mutation record."
    )
    active_job_ledger = evaluation_commands.add_parser(
        "active-job-ledger",
        help="Evaluate durable ACTIVE JOB hashes, relations, interruption, and replay offline.",
    )
    active_job_ledger.add_argument(
        "--json", action="store_true", help="Print the stable ACTIVE JOB ledger record."
    )
    organization_admission = evaluation_commands.add_parser(
        "organization-admission",
        help="Evaluate SOLO-first, same-worker recovery, and typed organization escalation offline.",
    )
    organization_admission.add_argument(
        "--json",
        action="store_true",
        help="Print the stable organization-admission record.",
    )
    causal_workflow = evaluation_commands.add_parser(
        "causal-workflow",
        help=(
            "Evaluate a four-job Workflow Patch cohort, attribution, isolation, "
            "and rollback without provider quota."
        ),
    )
    causal_workflow.add_argument(
        "--json",
        action="store_true",
        help="Print the stable causal Workflow Patch record.",
    )
    alpha_readiness = evaluation_commands.add_parser(
        "alpha-readiness",
        help="Evaluate the 0.1.0a1 code, privacy, supply-chain, and operator release gates.",
    )
    alpha_readiness.add_argument(
        "--source-root",
        type=Path,
        default=Path.cwd(),
    )
    alpha_readiness.add_argument("--json", action="store_true")
    information_boundary = evaluation_commands.add_parser(
        "information-boundary",
        help=(
            "Run the provider-free information-boundary benchmark v3 or seal "
            "its live-control preflight."
        ),
    )
    information_boundary.add_argument("--json", action="store_true")
    information_boundary.add_argument("--create-preflight", type=Path, default=None)
    information_boundary.add_argument("--wheel", type=Path, default=None)
    information_boundary.add_argument("--source-root", type=Path, default=Path.cwd())
    information_boundary.add_argument("--model", default=None)
    information_boundary.add_argument("--company-revision", type=int, default=1)
    information_boundary.add_argument("--roster-revision", type=int, default=1)
    information_boundary.add_argument("--playbook-revision", type=int, default=1)
    information_boundary_v4 = evaluation_commands.add_parser(
        "information-boundary-v4",
        help=(
            "Run the provider-free two-fixture information-boundary identifiability suite."
        ),
    )
    information_boundary_v4.add_argument("--json", action="store_true")
    information_boundary_v4.add_argument("--company-revision", type=int, default=1)
    information_boundary_v4.add_argument("--roster-revision", type=int, default=1)
    information_boundary_v4.add_argument("--playbook-revision", type=int, default=1)
    information_boundary_pair = evaluation_commands.add_parser(
        "information-boundary-pair",
        help=(
            "Prepare and advance the exact SOLO-then-admission live information-boundary pair."
        ),
    )
    pair_commands = information_boundary_pair.add_subparsers(
        dest="pair_command",
        required=True,
    )
    pair_prepare = pair_commands.add_parser(
        "prepare",
        help="Seal the Phase 44 preflight and verify live readiness without model calls.",
    )
    pair_prepare.add_argument("directory", type=Path)
    pair_prepare.add_argument("--preflight", type=Path, required=True)
    pair_prepare.add_argument("--wheel", type=Path, required=True)
    pair_prepare.add_argument("--source-root", type=Path, default=Path.cwd())
    pair_prepare.add_argument("--codex-command", default=None)
    pair_prepare.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
    )
    pair_prepare.add_argument("--max-live-model-calls", type=int, default=6)
    pair_prepare.add_argument(
        "--max-pair-model-calls",
        type=int,
        default=12,
    )
    pair_prepare.add_argument(
        "--max-live-wall-time",
        type=float,
        default=180.0,
        metavar="SECONDS",
    )
    pair_prepare.add_argument("--expires-in-hours", type=int, default=168)
    pair_prepare.add_argument("--json", action="store_true")
    pair_status = pair_commands.add_parser(
        "status",
        help="Verify the sealed pair and show the next one-slot quota boundary.",
    )
    pair_status.add_argument("directory", type=Path)
    pair_status.add_argument("--json", action="store_true")
    pair_run = pair_commands.add_parser(
        "run-next",
        help="Run exactly one pair slot after explicit quota confirmation.",
    )
    pair_run.add_argument("directory", type=Path)
    pair_run.add_argument("--confirm-live-quota", action="store_true")
    pair_run.add_argument("--json", action="store_true")
    pair_compare = pair_commands.add_parser(
        "compare",
        help="Compare the exact two sealed records without an aggregator model call.",
    )
    pair_compare.add_argument("directory", type=Path)
    pair_compare.add_argument("--output", type=Path, default=None)
    pair_compare.add_argument("--json", action="store_true")
    release_authorization_pair = evaluation_commands.add_parser(
        "release-authorization-pair",
        help=(
            "Prepare and advance the immutable release-authorization "
            "SOLO-then-typed-admission live pair."
        ),
    )
    release_pair_commands = release_authorization_pair.add_subparsers(
        dest="release_pair_command",
        required=True,
    )
    release_pair_prepare = release_pair_commands.add_parser(
        "prepare",
        help=(
            "Run Suite v4 and seal release live readiness without model calls."
        ),
    )
    release_pair_prepare.add_argument("directory", type=Path)
    release_pair_prepare.add_argument("--wheel", type=Path, required=True)
    release_pair_prepare.add_argument(
        "--source-root",
        type=Path,
        default=Path.cwd(),
    )
    release_pair_prepare.add_argument("--model", required=True)
    release_pair_prepare.add_argument("--codex-command", default=None)
    release_pair_prepare.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
    )
    release_pair_prepare.add_argument("--company-revision", type=int, default=1)
    release_pair_prepare.add_argument("--roster-revision", type=int, default=1)
    release_pair_prepare.add_argument("--playbook-revision", type=int, default=1)
    release_pair_prepare.add_argument("--max-live-model-calls", type=int, default=6)
    release_pair_prepare.add_argument("--max-pair-model-calls", type=int, default=12)
    release_pair_prepare.add_argument(
        "--max-live-wall-time",
        type=float,
        default=180.0,
        metavar="SECONDS",
    )
    release_pair_prepare.add_argument("--expires-in-hours", type=int, default=168)
    release_pair_prepare.add_argument("--json", action="store_true")
    release_pair_status = release_pair_commands.add_parser(
        "status",
        help="Verify the sealed release pair and show the next quota boundary.",
    )
    release_pair_status.add_argument("directory", type=Path)
    release_pair_status.add_argument("--json", action="store_true")
    release_pair_run = release_pair_commands.add_parser(
        "run-next",
        help="Run exactly one release pair slot after quota confirmation.",
    )
    release_pair_run.add_argument("directory", type=Path)
    release_pair_run.add_argument("--confirm-live-quota", action="store_true")
    release_pair_run.add_argument("--json", action="store_true")
    release_pair_compare = release_pair_commands.add_parser(
        "compare",
        help="Compare two sealed release records without a model call.",
    )
    release_pair_compare.add_argument("directory", type=Path)
    release_pair_compare.add_argument("--output", type=Path, default=None)
    release_pair_compare.add_argument("--json", action="store_true")
    workflow_patch_cohort = evaluation_commands.add_parser(
        "workflow-patch-cohort",
        help=(
            "Prepare and advance the immutable four-record live cohort for an "
            "applied Workflow Patch."
        ),
    )
    workflow_patch_commands = workflow_patch_cohort.add_subparsers(
        dest="workflow_patch_command",
        required=True,
    )
    workflow_patch_prepare = workflow_patch_commands.add_parser(
        "prepare",
        help=(
            "Run the provider-free causal control and seal the live cohort "
            "without model calls."
        ),
    )
    workflow_patch_prepare.add_argument("directory", type=Path)
    workflow_patch_prepare.add_argument("--wheel", type=Path, required=True)
    workflow_patch_prepare.add_argument(
        "--source-root",
        type=Path,
        default=Path.cwd(),
    )
    workflow_patch_prepare.add_argument("--model", required=True)
    workflow_patch_prepare.add_argument("--codex-command", default=None)
    workflow_patch_prepare.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
    )
    workflow_patch_prepare.add_argument(
        "--max-live-model-calls",
        type=int,
        default=8,
    )
    workflow_patch_prepare.add_argument(
        "--max-cohort-model-calls",
        type=int,
        default=32,
    )
    workflow_patch_prepare.add_argument(
        "--max-live-wall-time",
        type=float,
        default=180.0,
        metavar="SECONDS",
    )
    workflow_patch_prepare.add_argument(
        "--expires-in-hours",
        type=int,
        default=168,
    )
    workflow_patch_prepare.add_argument("--json", action="store_true")
    workflow_patch_status = workflow_patch_commands.add_parser(
        "status",
        help="Verify the cohort, Company patch state, and next operator boundary.",
    )
    workflow_patch_status.add_argument("directory", type=Path)
    workflow_patch_status.add_argument("--json", action="store_true")
    workflow_patch_run = workflow_patch_commands.add_parser(
        "run-next",
        help="Run exactly one live cohort slot after explicit quota confirmation.",
    )
    workflow_patch_run.add_argument("directory", type=Path)
    workflow_patch_run.add_argument(
        "--confirm-live-quota",
        action="store_true",
    )
    workflow_patch_run.add_argument("--json", action="store_true")
    workflow_patch_preview = workflow_patch_commands.add_parser(
        "patch-preview",
        help="Show the immutable candidate without changing Company state.",
    )
    workflow_patch_preview.add_argument("directory", type=Path)
    workflow_patch_preview.add_argument("--json", action="store_true")
    for command_name, help_text in (
        ("patch-approve", "Explicitly approve the replayed Workflow Patch candidate."),
        ("patch-apply", "Explicitly apply the already-approved Workflow Patch."),
        ("rollback", "Explicitly roll back the applied Workflow Patch."),
    ):
        command = workflow_patch_commands.add_parser(
            command_name,
            help=help_text,
        )
        command.add_argument("directory", type=Path)
        command.add_argument("--confirm", action="store_true")
        command.add_argument("--actor", default="user:cli")
        command.add_argument("--json", action="store_true")
    workflow_patch_compare = workflow_patch_commands.add_parser(
        "compare",
        help="Compare four sealed records without an aggregator model call.",
    )
    workflow_patch_compare.add_argument("directory", type=Path)
    workflow_patch_compare.add_argument("--output", type=Path, default=None)
    workflow_patch_compare.add_argument("--json", action="store_true")

    add_workflow_evaluation_commands(evaluation_commands)
