"""Argument schemas for active Jobs, sessions, graphs, and runtime data."""
from __future__ import annotations

import argparse
from pathlib import Path

from dynamic_firm.product.graph_cli_values import GraphMutationPolicy


def add_runtime_control_commands(
    commands: argparse._SubParsersAction,
    *,
    add_execution_options,
) -> None:
    run = commands.add_parser(
        "run",
        help="Run a goal through the smallest sufficient company configuration.",
    )
    run.add_argument("goal", help="The outcome the company should produce.")
    add_execution_options(run)
    run.add_argument(
        "--json",
        action="store_true",
        help="Print the stable structured result instead of the human summary.",
    )
    continue_read_only = commands.add_parser(
        "continue-read-only",
        help="Explicitly resume only a receipt-proven, unmodified local read-only partial Job.",
    )
    continue_read_only.add_argument("job_id")
    add_execution_options(continue_read_only)
    continue_read_only.add_argument("--confirm", action="store_true")
    continue_read_only.add_argument("--json", action="store_true")
    handoff_read_only = commands.add_parser(
        "handoff-read-only",
        help="Transfer one unclaimed read-only continuation authority to an enrolled device.",
    )
    handoff_read_only.add_argument("job_id")
    handoff_read_only.add_argument("target_device_id")
    add_execution_options(handoff_read_only)
    handoff_read_only.add_argument("--confirm", action="store_true")
    handoff_read_only.add_argument("--json", action="store_true")
    continue_graph_proposal = commands.add_parser(
        "continue-graph-proposal",
        help="Explicitly approve or reject one paused Graph proposal and resume its exact same Job.",
    )
    continue_graph_proposal.add_argument("job_id")
    continue_graph_proposal.add_argument("proposal_id")
    continue_graph_proposal.add_argument("decision", choices=("approve", "reject"))
    add_execution_options(continue_graph_proposal)
    continue_graph_proposal.add_argument("--confirm", action="store_true")
    continue_graph_proposal.add_argument("--json", action="store_true")
    ask = commands.add_parser(
        "ask",
        help="Answer a question directly or form a company when the request benefits from one.",
    )
    ask.add_argument("goal", help="A question, request, or company goal.")
    add_execution_options(ask)
    ask.add_argument(
        "--json",
        action="store_true",
        help="Print the stable structured result instead of the human summary.",
    )
    chat = commands.add_parser(
        "chat",
        help="Open the persistent company interface in the current terminal.",
    )
    add_execution_options(chat)
    resume = commands.add_parser(
        "resume",
        help="Resume the latest company session, or a session by id/title.",
    )
    resume.add_argument("session", nargs="?", default=None)
    add_execution_options(resume, workspace_default=None)
    sessions = commands.add_parser(
        "sessions",
        help="List persistent company sessions without contacting a model.",
    )
    sessions.add_argument("--state", type=Path, default=None)
    sessions.add_argument("--limit", type=int, default=20)
    sessions.add_argument("--json", action="store_true")
    portfolio = commands.add_parser(
        "portfolio",
        help="Inspect and deliberately govern local Work Order portfolio admission; it never starts a daemon.",
    )
    portfolio_commands = portfolio.add_subparsers(dest="portfolio_command", required=True)
    portfolio_submit = portfolio_commands.add_parser(
        "submit",
        help="Freeze one canonical Work Order into the local queue without contacting a provider or starting a Job.",
    )
    portfolio_submit.add_argument("goal")
    add_execution_options(portfolio_submit)
    portfolio_submit.add_argument("--priority", type=int, default=50)
    portfolio_submit.add_argument("--reserved-cost-usd", type=float, default=None)
    portfolio_submit.add_argument(
        "--depends-on",
        action="append",
        default=[],
        metavar="WORK_ORDER_ID",
        help="Require a retained Work Order to finish successfully before this one is admitted.",
    )
    portfolio_submit.add_argument(
        "--deadline",
        default=None,
        help="Timezone-aware ISO-8601 scheduling deadline; a missed deadline blocks instead of auto-running.",
    )
    portfolio_submit.add_argument(
        "--requires-capability",
        action="append",
        default=[],
        metavar="CAPABILITY",
        help="Reserve one configured scarce-capability slot while this Work Order is active.",
    )
    portfolio_submit.add_argument("--confirm", action="store_true")
    portfolio_submit.add_argument("--json", action="store_true")
    reestimate = portfolio_commands.add_parser(
        "reestimate",
        help="Record or explicitly decide a changed local estimate; it never stops or edits a running Job.",
    )
    reestimate_commands = reestimate.add_subparsers(dest="portfolio_reestimate_command", required=True)
    reestimate_report = reestimate_commands.add_parser(
        "report", help="Record an estimate change and leave the existing execution untouched."
    )
    reestimate_report.add_argument("work_order_id")
    reestimate_report.add_argument("--proposed-reserved-cost-usd", type=float, required=True)
    reestimate_report.add_argument("--reason", required=True, help="Uppercase content-free reason code.")
    reestimate_report.add_argument("--confirm", action="store_true")
    reestimate_report.add_argument("--state", type=Path, default=None)
    reestimate_report.add_argument("--json", action="store_true")
    reestimate_decide = reestimate_commands.add_parser(
        "decide", help="Append an explicit continue, reduce, or cancel choice without a hidden runtime action."
    )
    reestimate_decide.add_argument("reestimate_id")
    reestimate_decide.add_argument("--choice", choices=("CONTINUE", "REDUCE", "CANCEL"), required=True)
    reestimate_decide.add_argument("--reason", required=True, help="Uppercase content-free reason code.")
    reestimate_decide.add_argument("--confirm", action="store_true")
    reestimate_decide.add_argument("--state", type=Path, default=None)
    reestimate_decide.add_argument("--json", action="store_true")
    reestimate_list = reestimate_commands.add_parser("list", help="List content-free estimate notices and choices.")
    reestimate_list.add_argument("--state", type=Path, default=None)
    reestimate_list.add_argument("--json", action="store_true")
    for name, help_text in (
        ("status", "Show content-free queue, deferred work, local leases, and terminal settlement mirrors."),
        ("preview", "Reconcile local admission and preview the next explicit dispatch boundary without executing."),
    ):
        item = portfolio_commands.add_parser(name, help=help_text)
        item.add_argument("--state", type=Path, default=None)
        item.add_argument("--json", action="store_true")
    preview = portfolio_commands.choices["preview"]
    preview.add_argument("--context-fingerprint", default="", help="Exact workflow context digest for reuse evidence; empty disables reuse.")
    preview.add_argument("--manager-employee-id", default="")
    preview.add_argument("--automatic-blueprint-requested", action="store_true")
    preview.add_argument("--manager-campaign-directory", type=Path, default=None, help="Sealed manager-campaign directory to inspect read-only.")
    drain = portfolio_commands.add_parser(
        "drain",
        help="Run the currently admitted local portfolio in one bounded, explicitly confirmed Front Door drain.",
    )
    add_execution_options(drain)
    drain.add_argument("--confirm", action="store_true")
    drain.add_argument("--json", action="store_true")
    policy = portfolio_commands.add_parser("policy", help="View or edit only future local admission bounds.")
    policy_commands = policy.add_subparsers(dest="portfolio_policy_command", required=True)
    policy_show = policy_commands.add_parser("show", help="Show the saved local planning policy.")
    policy_show.add_argument("--state", type=Path, default=None)
    policy_show.add_argument("--json", action="store_true")
    policy_set = policy_commands.add_parser("set", help="Persist future local planning bounds; it never reconciles or dispatches.")
    policy_set.add_argument("--state", type=Path, default=None)
    policy_set.add_argument("--max-active-jobs", type=int, required=True)
    policy_set.add_argument("--max-reserved-cost-usd", type=float, required=True)
    policy_set.add_argument("--max-incremental-model-calls", type=int, default=0)
    policy_set.add_argument("--max-incremental-tool-calls", type=int, default=0)
    policy_set.add_argument("--max-incremental-cost-usd", type=float, default=0.0)
    policy_set.add_argument(
        "--capability-slot",
        action="append",
        default=[],
        metavar="CAPABILITY=COUNT",
        help="Declare a future local scarce-capability capacity.",
    )
    policy_set.add_argument("--confirm", action="store_true")
    policy_set.add_argument("--json", action="store_true")
    campaign_gate = portfolio_commands.add_parser(
        "campaign-gate",
        help="Explain whether a sealed Manager 16-slot campaign permits automatic Blueprint reuse.",
    )
    campaign_gate.add_argument("directory", type=Path)
    campaign_gate.add_argument("--json", action="store_true")
    from dynamic_firm.application.session_cli_parser import add_session_commands

    add_session_commands(commands)
    from dynamic_firm.application.skills_cli_parser import add_skills_commands

    add_skills_commands(commands)
    from dynamic_firm.application.schedule_cli_parser import add_schedule_commands

    add_schedule_commands(commands)
    from dynamic_firm.application.gateway_cli_parser import add_gateway_commands

    add_gateway_commands(commands)
    job = commands.add_parser(
        "job",
        help="Inspect the durable ACTIVE JOB audit ledger without resuming execution.",
    )
    job_commands = job.add_subparsers(dest="job_command", required=True)
    job_list = job_commands.add_parser("list", help="List recent ACTIVE JOB audits.")
    job_list.add_argument("--state", type=Path, default=None)
    job_list.add_argument("--limit", type=int, default=20)
    job_list.add_argument("--json", action="store_true")
    job_inspect = job_commands.add_parser(
        "inspect",
        help="Validate hashes and replay one ACTIVE JOB audit trajectory.",
    )
    job_inspect.add_argument("job_id")
    job_inspect.add_argument("--state", type=Path, default=None)
    job_inspect.add_argument("--json", action="store_true")
    job_summary = job_commands.add_parser(
        "summary",
        help="Show one bounded honest terminal summary; never resumes or changes a Job.",
    )
    job_summary.add_argument("job_id")
    job_summary.add_argument("--state", type=Path, default=None)
    job_summary.add_argument("--json", action="store_true")
    job_graph = job_commands.add_parser(
        "graph",
        help="Project a replay-verified ACTIVE JOB into immutable Graph revision lineage.",
    )
    job_graph.add_argument("job_id")
    job_graph.add_argument("--state", type=Path, default=None)
    job_graph.add_argument("--json", action="store_true")
    job_checkpoints = job_commands.add_parser(
        "checkpoints",
        help="Show read-only parent-linked ACTIVE JOB state checkpoints; never resumes execution.",
    )
    job_checkpoints.add_argument("job_id")
    job_checkpoints.add_argument("--state", type=Path, default=None)
    job_checkpoints.add_argument("--json", action="store_true")
    job_timeline = job_commands.add_parser(
        "timeline",
        help="Show a bounded, redacted Employee Runtime event timeline for one ACTIVE JOB.",
    )
    job_timeline.add_argument("job_id")
    job_timeline.add_argument("--state", type=Path, default=None)
    job_timeline.add_argument("--from", dest="timeline_from", default=None)
    job_timeline.add_argument("--to", dest="timeline_to", default=None)
    job_timeline.add_argument("--limit", type=int, default=200)
    job_timeline.add_argument("--json", action="store_true")
    job_recovery = job_commands.add_parser(
        "recovery",
        help="Show read-only recovery guidance without resuming an ACTIVE JOB.",
    )
    job_recovery.add_argument("job_id")
    job_recovery.add_argument("--state", type=Path, default=None)
    job_recovery.add_argument("--json", action="store_true")
    job_frozen_seal = job_commands.add_parser(
        "frozen-run-seal",
        help="Explicitly seal one inspected abandoned frozen dispatcher; never replays it.",
    )
    job_frozen_seal.add_argument("job_id")
    job_frozen_seal.add_argument("run_id")
    job_frozen_seal.add_argument("binding_digest")
    job_frozen_seal.add_argument("recovery_id")
    job_frozen_seal.add_argument("--state", type=Path, default=None)
    job_frozen_seal.add_argument("--confirm", action="store_true")
    job_frozen_seal.add_argument("--json", action="store_true")
    job_control = job_commands.add_parser(
        "control",
        help="Apply one explicit durable hold transition; never resumes a Job process.",
    )
    job_control.add_argument("job_id")
    job_control.add_argument("action", choices=("defer", "pause", "resume", "cancel"))
    job_control.add_argument("--reason", required=True)
    job_control.add_argument("--revision", type=int, default=None)
    job_control.add_argument("--state", type=Path, default=None)
    job_control.add_argument("--confirm", action="store_true")
    job_control.add_argument("--json", action="store_true")
    job_settle_unknown = job_commands.add_parser(
        "settle-unknown",
        help="Forfeit interrupted graph-mutation capacity after a pause/cancel; it is never reusable.",
    )
    job_settle_unknown.add_argument("job_id")
    job_settle_unknown.add_argument("--reason", required=True)
    job_settle_unknown.add_argument("--state", type=Path, default=None)
    job_settle_unknown.add_argument("--confirm", action="store_true")
    job_settle_unknown.add_argument("--json", action="store_true")
    job_effect_resolve = job_commands.add_parser(
        "effect-resolve",
        help=(
            "Append an operator resolution for one indeterminate effect; "
            "it never replays the action or reconstructs output."
        ),
    )
    job_effect_resolve.add_argument("job_id")
    job_effect_resolve.add_argument("action_id")
    job_effect_resolve.add_argument(
        "outcome",
        choices=("confirmed-succeeded", "confirmed-no-effect", "compensated", "seal-unknown"),
    )
    job_effect_resolve.add_argument(
        "--evidence-digest",
        default=None,
        help="Lowercase SHA-256 of operator-held evidence; required to release the resource.",
    )
    job_effect_resolve.add_argument("--operator-id", required=True)
    job_effect_resolve.add_argument("--reason", required=True)
    job_effect_resolve.add_argument("--state", type=Path, default=None)
    job_effect_resolve.add_argument("--confirm", action="store_true")
    job_effect_resolve.add_argument("--json", action="store_true")
    job_correct = job_commands.add_parser(
        "correct",
        help="Queue one bounded user correction for a task; it is delivered only at that task's result boundary.",
    )
    job_correct.add_argument("job_id")
    job_correct.add_argument("task_id")
    job_correct.add_argument("--reference", required=True)
    job_correct.add_argument("--state", type=Path, default=None)
    job_correct.add_argument("--confirm", action="store_true")
    job_correct.add_argument("--json", action="store_true")
    job_continue_read_only = job_commands.add_parser(
        "authorize-read-only-continuation",
        help=(
            "Authorize one receipt-bound partial read-only continuation from the local Work Order authority; "
            "it never reconstructs a request from ACTIVE JOB."
        ),
    )
    job_continue_read_only.add_argument("job_id")
    job_continue_read_only.add_argument("--state", type=Path, default=None)
    job_continue_read_only.add_argument("--confirm", action="store_true")
    job_continue_read_only.add_argument("--json", action="store_true")
    graph = commands.add_parser(
        "graph",
        help="Inspect and govern inert local Graph Blueprint preferences; it never starts a Job.",
    )
    graph_commands = graph.add_subparsers(dest="graph_command", required=True)
    graph_dashboard = graph_commands.add_parser(
        "dashboard",
        help="Serve a loopback-only visual Graph workbench; it is read-only and owns no Company state.",
    )
    add_execution_options(graph_dashboard)
    graph_dashboard.add_argument("--port", type=int, default=0)
    graph_dashboard.add_argument("--max-requests", type=int, default=None)
    graph_dashboard.add_argument("--confirm", action="store_true")
    graph_dashboard.add_argument("--json", action="store_true")
    graph_list = graph_commands.add_parser("list", help="List local Blueprint revisions and the selected preference.")
    graph_list.add_argument("--slot", default="default")
    graph_list.add_argument("--state", type=Path, default=None)
    graph_list.add_argument("--json", action="store_true")
    graph_show = graph_commands.add_parser("show", help="Show one immutable local Blueprint revision.")
    graph_show.add_argument("blueprint_id")
    graph_show.add_argument("version", type=int)
    graph_show.add_argument("--state", type=Path, default=None)
    graph_show.add_argument("--json", action="store_true")
    graph_import = graph_commands.add_parser("import", help="Import one data-only local Blueprint JSON as a DRAFT or user-owned revision.")
    graph_import.add_argument("payload_file", type=Path)
    graph_import.add_argument("--state", type=Path, default=None)
    graph_import.add_argument("--confirm", action="store_true")
    graph_import.add_argument("--json", action="store_true")
    graph_fork = graph_commands.add_parser("fork", help="Fork one immutable local Blueprint revision into a user-owned Blueprint.")
    graph_fork.add_argument("source_blueprint_id")
    graph_fork.add_argument("source_version", type=int)
    graph_fork.add_argument("blueprint_id")
    graph_fork.add_argument("--version", type=int, default=1)
    graph_fork.add_argument("--state", type=Path, default=None)
    graph_fork.add_argument("--confirm", action="store_true")
    graph_fork.add_argument("--json", action="store_true")
    graph_revise = graph_commands.add_parser(
        "revise",
        help="Save one validated immutable USER_REVISION from a data-only Blueprint JSON file.",
    )
    graph_revise.add_argument("source_blueprint_id")
    graph_revise.add_argument("source_version", type=int)
    graph_revise.add_argument("payload_file", type=Path)
    graph_revise.add_argument(
        "--reason",
        required=True,
        help="Short human rationale retained in the local revision receipt.",
    )
    graph_revise.add_argument("--state", type=Path, default=None)
    graph_revise.add_argument("--confirm", action="store_true")
    graph_revise.add_argument("--json", action="store_true")
    graph_natural_edit = graph_commands.add_parser(
        "natural-edit",
        help=(
            "Ask the configured provider for one validated, unsaved Blueprint revision candidate. "
            "It never selects, executes, or persists a Blueprint revision."
        ),
    )
    graph_natural_edit.add_argument("blueprint_id")
    graph_natural_edit.add_argument("version", type=int)
    graph_natural_edit.add_argument(
        "instruction",
        help="Plain-language change request for this one immutable Blueprint revision.",
    )
    graph_natural_edit.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Optional new JSON path for the candidate. Feed it to `noruct graph revise` "
            "after review; an existing path is never overwritten."
        ),
    )
    add_execution_options(graph_natural_edit)
    graph_natural_edit.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm the one provider call that generates the unsaved candidate.",
    )
    graph_natural_edit.add_argument("--json", action="store_true")
    graph_history = graph_commands.add_parser(
        "history",
        help="Show accepted or rejected local revision receipts for one Blueprint id.",
    )
    graph_history.add_argument("blueprint_id")
    graph_history.add_argument("--state", type=Path, default=None)
    graph_history.add_argument("--json", action="store_true")
    graph_replica_evaluate = graph_commands.add_parser(
        "replica-evaluate",
        help=(
            "Compare observed SINGLE and same-Employee REPLICA trials under exact shared budgets; "
            "repeat --pair three times for a durable-reuse recommendation."
        ),
    )
    graph_replica_evaluate.add_argument(
        "--pair",
        nargs=2,
        action="append",
        required=True,
        metavar=("SINGLE_JSON", "REPLICA_JSON"),
    )
    graph_replica_evaluate.add_argument("--json", action="store_true")
    graph_preview = graph_commands.add_parser(
        "preview",
        help="Preview one future Work Order against the current Company, ROSTER, constraints, and budget; it never starts a Job.",
    )
    graph_preview.add_argument("goal", help="Future Company goal to bind to the Blueprint without executing it.")
    graph_preview.add_argument("--blueprint-id", default=None, help="Blueprint id; defaults to the selected local revision.")
    graph_preview.add_argument("--version", type=int, default=None, help="Blueprint version; requires --blueprint-id.")
    graph_preview.add_argument("--slot", default="default")
    graph_preview.add_argument("--pin-employee", action="append", default=None)
    graph_preview.add_argument("--exclude-employee", action="append", default=None)
    graph_preview.add_argument("--require-independent-review", action="store_true", default=None)
    graph_preview.add_argument("--max-concurrency", type=int, default=None)
    graph_preview.add_argument(
        "--mutation-policy",
        choices=tuple(item.value for item in GraphMutationPolicy),
        default=None,
    )
    # Preview uses the same configuration-derived authority and hard limits as
    # a future Company Job, but never constructs a provider or employee run.
    add_execution_options(graph_preview)
    graph_preview.add_argument("--json", action="store_true")
    graph_select = graph_commands.add_parser("select", help="Pin one exact Blueprint revision and optional future-Job constraints in a local slot.")
    graph_select.add_argument("blueprint_id")
    graph_select.add_argument("version", type=int)
    graph_select.add_argument("--slot", default="default")
    graph_select.add_argument("--pin-employee", action="append", default=None)
    graph_select.add_argument("--exclude-employee", action="append", default=None)
    graph_select.add_argument("--require-independent-review", action="store_true", default=None)
    graph_select.add_argument("--max-concurrency", type=int, default=None)
    graph_select.add_argument("--max-cost-usd", type=float, default=None)
    graph_select.add_argument("--max-wall-time-ms", type=int, default=None)
    graph_select.add_argument(
        "--mutation-policy",
        choices=tuple(item.value for item in GraphMutationPolicy),
        default=None,
    )
    graph_select.add_argument("--state", type=Path, default=None)
    graph_select.add_argument("--confirm", action="store_true")
    graph_select.add_argument("--json", action="store_true")
    graph_clear = graph_commands.add_parser("clear", help="Remove a local Blueprint selection; no Blueprint revision is deleted.")
    graph_clear.add_argument("--slot", default="default")
    graph_clear.add_argument("--clear-constraints", action="store_true")
    graph_clear.add_argument("--state", type=Path, default=None)
    graph_clear.add_argument("--confirm", action="store_true")
    graph_clear.add_argument("--json", action="store_true")
    from dynamic_firm.application.graph_community_cli_parser import add_community_graph_commands

    add_community_graph_commands(graph_commands)
    data_scope_help = (
        "Manage runtime/company state only, or create a redacted support view; "
        "Knowledge DB/Vault lifecycle is under `noruct knowledge`."
    )
    data = commands.add_parser("data", help=data_scope_help, description=data_scope_help)
    data_commands = data.add_subparsers(dest="data_command", required=True)
    data_export_help = (
        "Back up runtime/company SQLite state only; use `noruct knowledge export` "
        "for the separate Knowledge DB/Vault."
    )
    data_export = data_commands.add_parser(
        "export",
        help=data_export_help,
        description=data_export_help,
    )
    data_export.add_argument("destination", type=Path)
    data_export.add_argument("--state", type=Path, default=None)
    data_export.add_argument("--force", action="store_true")
    data_export.add_argument("--json", action="store_true")
    data_delete_help = (
        "Delete runtime/company SQLite state only; use `noruct knowledge delete` "
        "for the separate Knowledge DB/Vault."
    )
    data_delete = data_commands.add_parser(
        "delete",
        help=data_delete_help,
        description=data_delete_help,
    )
    data_delete.add_argument("--state", type=Path, default=None)
    data_delete.add_argument("--confirm", action="store_true")
    data_delete.add_argument("--json", action="store_true")
    support_bundle = data_commands.add_parser(
        "support-bundle",
        help="Write redacted diagnostics without prompts, messages, tool output, or raw state.",
    )
    support_bundle.add_argument("destination", type=Path)
    support_bundle.add_argument("--state", type=Path, default=None)
    support_bundle.add_argument("--force", action="store_true")
    support_bundle.add_argument("--json", action="store_true")
