"""Company governance command schemas outside the global CLI ingress."""

from __future__ import annotations

import argparse
from pathlib import Path

from dynamic_firm.company import (
    EvolutionAutonomyMode,
    RetentionReviewMode,
    RosterPatchOperation,
)


def add_company_commands(commands: argparse._SubParsersAction) -> None:
    """Register Company state and governance command schemas."""

    company = commands.add_parser(
        "company",
        help="Inspect versioned company state and govern Workflow, Roster, and Employee Skill Patches.",
    )
    company_commands = company.add_subparsers(dest="company_command", required=True)
    coordination_enrollment_preview = company_commands.add_parser(
        "coordination-enrollment-preview",
        help=(
            "Show the exact device-bound Worker allowlist entry for the enabled "
            "multi-device profile without exposing its token or making a request."
        ),
    )
    coordination_enrollment_preview.add_argument("--json", action="store_true")
    coordination_preflight = company_commands.add_parser(
        "coordination-preflight",
        help=(
            "Verify the enabled device-bound coordination enrollment without "
            "creating a lease, Job, continuation, or remote execution."
        ),
    )
    coordination_preflight.add_argument("--json", action="store_true")
    for name, help_text in (
        ("status", "Show active COMPANY, ROSTER, PLAYBOOK, episode, and patch revisions."),
        ("episodes", "List ledger-derived organization episodes."),
        ("patches", "List Workflow Patch candidates without changing company state."),
        ("roster-patches", "List explicit Roster Patch candidates."),
        ("staffing-demands", "List bounded temporary staffing demand evidence."),
        ("hire-contracts", "List immutable post-hire observation contracts."),
        ("retention-reviews", "List immutable employee retention review records."),
        ("manager-status", "Show whether this Company has a persistent Manager and the explicit migration path."),
        ("manager-outcomes", "Assess Manager-led organization outcomes without changing Company state."),
        ("manager-report", "Show the current Manager's bounded decisions, outcome evidence, and unresolved qualification gaps."),
        ("organization-metrics", "Aggregate content-free organization outcome evidence without changing Company state."),
        ("organization-outcomes", "Show context-bound Solo, Team, or replica eligibility without changing Company state."),
        ("employee-skills", "List current versioned employee procedures."),
        ("skill-patches", "List approval-only Employee Skill Patch candidates."),
        ("review-policy", "Show the legacy retention-only review policy and history."),
        ("autonomy", "Show the Company evolution mode, scope, and non-bypassable limits."),
        ("budget-status", "Show the local company-cost budget, reservations, and any hard-stop incident."),
        ("attention", "Show bounded, read-only Company incidents that need operator attention."),
        ("curate", "Run the deterministic curator; the default result is NO_PATCH."),
        ("roster-recommend", "Recommend evidence-gated hires; never approve or apply."),
        ("evidence-pairs", "List explicitly imported verified live evidence pairs."),
    ):
        item = company_commands.add_parser(name, help=help_text)
        item.add_argument("--state", type=Path, default=None)
        item.add_argument("--json", action="store_true")
        if name == "episodes":
            item.add_argument("--limit", type=int, default=20)
        if name == "attention":
            item.add_argument("--limit", type=int, default=100)
        if name == "employee-skills":
            item.add_argument("--employee-id")
            item.add_argument("--context-key")
        if name == "manager-outcomes":
            item.add_argument("--manager-id")
            item.add_argument("--context-fingerprint")
        if name == "organization-outcomes":
            item.add_argument(
                "--context-fingerprint",
                help="Assess one exact workflow context; omit to list all observed contexts.",
            )
    manager_migrate = company_commands.add_parser(
        "manager-migrate",
        help=(
            "Propose, but never automatically approve or apply, the versioned "
            "persistent Manager Employee required by the M2 Company path."
        ),
    )
    manager_migrate.add_argument("--employee-id", default="employee-executive-manager")
    manager_migrate.add_argument("--role", default="Executive Manager")
    manager_migrate.add_argument("--model-profile", default="company-default")
    manager_migrate.add_argument("--state", type=Path, default=None)
    manager_migrate.add_argument("--json", action="store_true")
    manager_revise = company_commands.add_parser(
        "manager-revise",
        help=(
            "Propose one next-Job Manager runtime revision through the ordinary "
            "ROSTER approval lifecycle; active Jobs remain pinned."
        ),
    )
    manager_revise.add_argument("--role", default=None)
    manager_revise.add_argument("--model-profile", default=None)
    manager_revise.add_argument("--rationale", required=True)
    manager_revise.add_argument("--state", type=Path, default=None)
    manager_revise.add_argument("--json", action="store_true")
    manager_rollback = company_commands.add_parser(
        "manager-rollback",
        help=(
            "Propose restoration of one prior immutable Manager ROSTER revision; "
            "never changes the active Manager until ordinary approval and apply."
        ),
    )
    manager_rollback.add_argument("roster_revision", type=int)
    manager_rollback.add_argument("--rationale", required=True)
    manager_rollback.add_argument("--state", type=Path, default=None)
    manager_rollback.add_argument("--json", action="store_true")
    company_curate_daemon = company_commands.add_parser(
        "curate-daemon",
        help="Run an operator-confirmed foreground deterministic curator loop; it may propose evidence-backed patches but never approves, applies, or rolls them back.",
    )
    company_curate_daemon.add_argument("--poll-seconds", type=float, default=300.0, help="Bounded foreground polling interval (30 through 3600 seconds).")
    company_curate_daemon.add_argument("--max-cycles", type=int, default=None, help="Optional bounded cycle count (1 through 10000).")
    company_curate_daemon.add_argument("--state", type=Path, default=None)
    company_curate_daemon.add_argument("--confirm", action="store_true")
    company_curate_daemon.add_argument("--json", action="store_true")
    skill_propose = company_commands.add_parser(
        "skill-propose",
        help="Propose a bounded employee procedure from one confirmed user correction.",
    )
    skill_propose.add_argument("--employee-id", required=True)
    skill_propose.add_argument("--skill-key", required=True)
    skill_propose.add_argument("--context-key", required=True)
    skill_propose.add_argument("--purpose", required=True)
    skill_propose.add_argument("--step", action="append", required=True)
    skill_propose.add_argument("--verify", action="append", required=True)
    skill_propose.add_argument("--prohibition", action="append", default=[])
    skill_propose.add_argument("--correction-id", required=True)
    skill_propose.add_argument("--rationale", required=True)
    skill_propose.add_argument("--state", type=Path, default=None)
    skill_propose.add_argument("--confirm", action="store_true")
    skill_propose.add_argument("--json", action="store_true")
    skill_preview = company_commands.add_parser(
        "skill-preview",
        help="Preview one Employee Skill Patch, exact scope, evidence, and lifecycle.",
    )
    skill_preview.add_argument("patch_id")
    skill_preview.add_argument("--state", type=Path, default=None)
    skill_preview.add_argument("--json", action="store_true")
    skill_assess = company_commands.add_parser(
        "skill-assess",
        help="Assess observations as KEEP or ROLLBACK_CANDIDATE; never roll back.",
    )
    skill_assess.add_argument("patch_id")
    skill_assess.add_argument("--state", type=Path, default=None)
    skill_assess.add_argument("--json", action="store_true")
    for name, help_text in (
        ("skill-approve", "Explicitly approve one Employee Skill Patch."),
        ("skill-apply", "Apply one approved patch as an append-only skill version."),
        ("skill-rollback", "Explicitly append a version restoring the prior procedure."),
    ):
        item = company_commands.add_parser(name, help=help_text)
        item.add_argument("patch_id")
        item.add_argument("--state", type=Path, default=None)
        item.add_argument("--confirm", action="store_true")
        item.add_argument("--json", action="store_true")
    skill_reject = company_commands.add_parser(
        "skill-reject",
        help="Reject an open Employee Skill Patch with an operator reason.",
    )
    skill_reject.add_argument("patch_id")
    skill_reject.add_argument("--reason", required=True)
    skill_reject.add_argument("--state", type=Path, default=None)
    skill_reject.add_argument("--confirm", action="store_true")
    skill_reject.add_argument("--json", action="store_true")
    evidence_preview = company_commands.add_parser(
        "evidence-preview",
        help="Verify one SOLO/DYNAMIC live pair without changing company state.",
    )
    evidence_preview.add_argument("baseline", type=Path)
    evidence_preview.add_argument("dynamic", type=Path)
    evidence_preview.add_argument("--state", type=Path, default=None)
    evidence_preview.add_argument("--json", action="store_true")
    evidence_import = company_commands.add_parser(
        "evidence-import",
        help="Append one verified live pair and its organization episode atomically.",
    )
    evidence_import.add_argument("baseline", type=Path)
    evidence_import.add_argument("dynamic", type=Path)
    evidence_import.add_argument("--state", type=Path, default=None)
    evidence_import.add_argument("--confirm", action="store_true")
    evidence_import.add_argument("--json", action="store_true")
    workflow_promote_preview = company_commands.add_parser(
        "workflow-promote-preview",
        help="Verify one exact-context lineage and preview a production Workflow Patch.",
    )
    workflow_promote_preview.add_argument("pair_directory", type=Path)
    workflow_promote_preview.add_argument("--state", type=Path, default=None)
    workflow_promote_preview.add_argument("--json", action="store_true")
    workflow_promote = company_commands.add_parser(
        "workflow-promote",
        help="Append one exact-context Workflow Patch as PROPOSED only.",
    )
    workflow_promote.add_argument("pair_directory", type=Path)
    workflow_promote.add_argument("--state", type=Path, default=None)
    workflow_promote.add_argument("--confirm", action="store_true")
    workflow_promote.add_argument("--json", action="store_true")
    preview = company_commands.add_parser(
        "preview", help="Preview one patch, its evidence, and append-only lifecycle events."
    )
    preview.add_argument("patch_id")
    preview.add_argument("--state", type=Path, default=None)
    preview.add_argument("--json", action="store_true")
    observe = company_commands.add_parser(
        "observe",
        help="Show one applied patch's immutable contract, attribution, and assessments.",
    )
    observe.add_argument("patch_id")
    observe.add_argument("--state", type=Path, default=None)
    observe.add_argument("--json", action="store_true")
    assess = company_commands.add_parser(
        "assess",
        help="Append a deterministic KEEP/ROLLBACK_CANDIDATE recommendation; never roll back.",
    )
    assess.add_argument("patch_id")
    assess.add_argument("--state", type=Path, default=None)
    assess.add_argument("--json", action="store_true")
    replay = company_commands.add_parser(
        "replay", help="Deterministically replay one candidate from immutable episode evidence."
    )
    replay.add_argument("patch_id")
    replay.add_argument("--state", type=Path, default=None)
    replay.add_argument("--json", action="store_true")
    for name, help_text in (
        ("approve", "Explicitly approve one production-eligible Workflow Patch."),
        ("apply", "Apply one already-approved patch as a new PLAYBOOK revision."),
        ("rollback", "Append a PLAYBOOK revision that restores pre-patch content."),
    ):
        item = company_commands.add_parser(name, help=help_text)
        item.add_argument("patch_id")
        item.add_argument("--state", type=Path, default=None)
        item.add_argument("--confirm", action="store_true")
        item.add_argument("--json", action="store_true")
    reject = company_commands.add_parser(
        "reject", help="Reject an open patch with a recorded operator reason."
    )
    reject.add_argument("patch_id")
    reject.add_argument("--reason", required=True)
    reject.add_argument("--state", type=Path, default=None)
    reject.add_argument("--confirm", action="store_true")
    reject.add_argument("--json", action="store_true")
    roster_propose = company_commands.add_parser(
        "roster-propose",
        help="Propose one typed ROSTER change without changing the active ROSTER.",
    )
    roster_propose.add_argument(
        "operation",
        choices=tuple(operation.value for operation in RosterPatchOperation),
    )
    roster_propose.add_argument("--employee-id", required=True)
    roster_propose.add_argument("--role")
    roster_propose.add_argument("--capability", action="append", default=[])
    roster_propose.add_argument("--active", choices=("true", "false"))
    roster_propose.add_argument("--model-profile", default="company-default")
    roster_propose.add_argument("--rationale", required=True)
    roster_propose.add_argument("--state", type=Path, default=None)
    roster_propose.add_argument("--json", action="store_true")
    roster_preview = company_commands.add_parser(
        "roster-preview",
        help="Preview exact before/after employee state and Roster Patch events.",
    )
    roster_preview.add_argument("patch_id")
    roster_preview.add_argument("--state", type=Path, default=None)
    roster_preview.add_argument("--json", action="store_true")
    hire_preview = company_commands.add_parser(
        "hire-preview",
        help="Show one evidence-backed hire contract, observations, and assessment.",
    )
    hire_preview.add_argument("patch_id")
    hire_preview.add_argument("--state", type=Path, default=None)
    hire_preview.add_argument("--json", action="store_true")
    hire_assess = company_commands.add_parser(
        "hire-assess",
        help="Append a deterministic KEEP/DORMANCY_CANDIDATE recommendation only.",
    )
    hire_assess.add_argument("patch_id")
    hire_assess.add_argument("--state", type=Path, default=None)
    hire_assess.add_argument("--json", action="store_true")
    review_policy_set = company_commands.add_parser(
        "review-policy-set",
        help="Set approval, auto-review, or always-approve for reversible retention only.",
    )
    review_policy_set.add_argument(
        "mode",
        choices=tuple(mode.value for mode in RetentionReviewMode),
    )
    review_policy_set.add_argument("--state", type=Path, default=None)
    review_policy_set.add_argument("--confirm", action="store_true")
    review_policy_set.add_argument("--json", action="store_true")
    autonomy_set = company_commands.add_parser(
        "autonomy-set",
        help="Choose never, propose, or always-approve for qualifying Company evolution.",
    )
    autonomy_set.add_argument(
        "mode", choices=tuple(mode.value for mode in EvolutionAutonomyMode)
    )
    autonomy_set.add_argument("--state", type=Path, default=None)
    autonomy_set.add_argument("--confirm", action="store_true")
    autonomy_set.add_argument("--json", action="store_true")
    budget_policy_set = company_commands.add_parser(
        "budget-policy-set",
        help="Version a company cost budget; this never resumes a paused company.",
    )
    budget_policy_set.add_argument("--max-total-cost-usd", type=float, required=True)
    budget_policy_set.add_argument(
        "--window-kind",
        choices=("lifetime", "calendar_month_utc"),
        default="lifetime",
    )
    budget_policy_set.add_argument("--state", type=Path, default=None)
    budget_policy_set.add_argument("--confirm", action="store_true")
    budget_policy_set.add_argument("--json", action="store_true")
    budget_resolve = company_commands.add_parser(
        "budget-resolve",
        help="Explicitly raise/confirm a company budget and resume one paused incident.",
    )
    budget_resolve.add_argument("incident_id")
    budget_resolve.add_argument("--max-total-cost-usd", type=float, required=True)
    budget_resolve.add_argument(
        "--window-kind",
        choices=("lifetime", "calendar_month_utc"),
        default="lifetime",
    )
    budget_resolve.add_argument("--state", type=Path, default=None)
    budget_resolve.add_argument("--confirm", action="store_true")
    budget_resolve.add_argument("--json", action="store_true")
    retention_recommend = company_commands.add_parser(
        "roster-retention-recommend",
        help=(
            "Create a reversible dormancy proposal and execute the configured review policy."
        ),
    )
    retention_recommend.add_argument("hire_patch_id")
    retention_recommend.add_argument("--state", type=Path, default=None)
    retention_recommend.add_argument("--json", action="store_true")
    for name, help_text in (
        ("roster-approve", "Explicitly approve one proposed Roster Patch."),
        ("roster-apply", "Apply one approved Roster Patch as a new ROSTER revision."),
    ):
        item = company_commands.add_parser(name, help=help_text)
        item.add_argument("patch_id")
        item.add_argument("--state", type=Path, default=None)
        item.add_argument("--confirm", action="store_true")
        item.add_argument("--json", action="store_true")
    roster_reject = company_commands.add_parser(
        "roster-reject",
        help="Reject an open Roster Patch with an operator reason.",
    )
    roster_reject.add_argument("patch_id")
    roster_reject.add_argument("--reason", required=True)
    roster_reject.add_argument("--state", type=Path, default=None)
    roster_reject.add_argument("--confirm", action="store_true")
    roster_reject.add_argument("--json", action="store_true")

