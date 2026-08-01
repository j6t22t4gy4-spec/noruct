"""Knowledge, Intent, Decision, Question and Research command schemas."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from dynamic_firm.knowledge import (
    AssetStatus,
    AttributionStatus,
    ContentTrustClass,
    DecisionStatus,
    EpistemicStatus,
    IntentStatus,
    OutcomeVerdict,
    QuestionStatus,
    ResearchRequestStatus,
)


def add_knowledge_intent_commands(
    commands: argparse._SubParsersAction,
    *,
    add_local_knowledge_options: Callable[[argparse.ArgumentParser], None],
    add_execution_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register local knowledge-plane schemas without CLI ingress imports."""

    knowledge = commands.add_parser(
        "knowledge",
        help="Manage the user-owned Knowledge DB and Vault without contacting a model.",
    )
    knowledge_commands = knowledge.add_subparsers(dest="knowledge_command", required=True)
    knowledge_status = knowledge_commands.add_parser(
        "status", help="Show content-free Knowledge DB and Vault health."
    )
    add_local_knowledge_options(knowledge_status)
    knowledge_capabilities = knowledge_commands.add_parser(
        "capabilities",
        help="Show local document extraction routes without reading a document or making a network call.",
    )
    add_local_knowledge_options(knowledge_capabilities)
    knowledge_repair = knowledge_commands.add_parser(
        "repair",
        help="Recover one journaled local Asset deletion after an interrupted process.",
    )
    knowledge_repair.add_argument("--confirm", action="store_true")
    add_local_knowledge_options(knowledge_repair)
    knowledge_add = knowledge_commands.add_parser(
        "add", help="Preserve a local file and extract it when an audited processor is available."
    )
    knowledge_add.add_argument("source", type=Path)
    knowledge_add.add_argument("--title", default="")
    knowledge_add.add_argument("--origin", default="local-file")
    knowledge_add.add_argument("--access-scope", default="private")
    knowledge_add.add_argument("--label", action="append", default=[])
    knowledge_add.add_argument("--parent-asset-id", default=None)
    knowledge_add.add_argument("--processor", default="auto")
    knowledge_add.add_argument("--timeout", type=float, default=120.0)
    add_local_knowledge_options(knowledge_add)
    knowledge_remote_fetch = knowledge_commands.add_parser(
        "remote-fetch",
        help="Explicitly copy one public HTTPS document into the local Knowledge Vault; never follows redirects or syncs.",
    )
    knowledge_remote_fetch.add_argument("url")
    knowledge_remote_fetch.add_argument("--title", default="")
    knowledge_remote_fetch.add_argument("--access-scope", default="private")
    knowledge_remote_fetch.add_argument("--label", action="append", default=[])
    knowledge_remote_fetch.add_argument("--processor", default="auto")
    knowledge_remote_fetch.add_argument("--timeout", type=float, default=20.0)
    knowledge_remote_fetch.add_argument(
        "--expected-sha256",
        default=None,
        help="Optional exact SHA-256 for this one downloaded response; it is not retained as a sync policy.",
    )
    knowledge_remote_fetch.add_argument("--confirm", action="store_true")
    add_local_knowledge_options(knowledge_remote_fetch)
    knowledge_remote_refresh = knowledge_commands.add_parser(
        "remote-refresh",
        help="Explicitly recheck one prior public remote import; only changed content creates a new local Asset.",
    )
    knowledge_remote_refresh.add_argument("asset_id")
    knowledge_remote_refresh.add_argument("--processor", default="auto")
    knowledge_remote_refresh.add_argument("--timeout", type=float, default=20.0)
    knowledge_remote_refresh.add_argument(
        "--expected-sha256",
        default=None,
        help="Optional exact SHA-256 required only if this refresh downloads changed content.",
    )
    knowledge_remote_refresh.add_argument("--confirm", action="store_true")
    add_local_knowledge_options(knowledge_remote_refresh)
    knowledge_process = knowledge_commands.add_parser(
        "process", help="Retry extraction for one already-preserved Knowledge Asset."
    )
    knowledge_process.add_argument("asset_id")
    knowledge_process.add_argument("--processor", default="auto")
    knowledge_process.add_argument("--timeout", type=float, default=120.0)
    add_local_knowledge_options(knowledge_process)
    knowledge_assets = knowledge_commands.add_parser(
        "assets", help="List Knowledge Asset metadata; original content is not printed."
    )
    knowledge_assets.add_argument("--limit", type=int, default=50)
    knowledge_assets.add_argument("--status", choices=tuple(item.value for item in AssetStatus), default=None)
    add_local_knowledge_options(knowledge_assets)
    knowledge_folder_add = knowledge_commands.add_parser(
        "folder-add",
        help="Register a user-owned raw Knowledge Folder and scan it locally.",
    )
    knowledge_folder_add.add_argument("source", type=Path)
    knowledge_folder_add.add_argument("--name", default="")
    knowledge_folder_add.add_argument("--access-scope", default="private")
    knowledge_folder_add.add_argument("--ignore", action="append", default=[], metavar="GLOB", help="Persist one relative POSIX glob exclusion; repeatable.")
    knowledge_folder_add.add_argument("--no-scan", action="store_true")
    knowledge_folder_add.add_argument("--max-files", type=int, default=2000)
    knowledge_folder_add.add_argument("--max-depth", type=int, default=20)
    knowledge_folder_add.add_argument("--max-total-bytes", type=int, default=512 * 1024 * 1024)
    knowledge_folder_add.add_argument("--extract-documents", action="store_true")
    knowledge_folder_add.add_argument("--max-document-files", type=int, default=32)
    knowledge_folder_add.add_argument("--document-timeout", type=float, default=20.0)
    add_local_knowledge_options(knowledge_folder_add)
    knowledge_folder_scan = knowledge_commands.add_parser(
        "folder-scan",
        help="Reconcile one registered Knowledge Folder without following symlinks.",
    )
    knowledge_folder_scan.add_argument("folder_id")
    knowledge_folder_scan.add_argument("--max-files", type=int, default=2000)
    knowledge_folder_scan.add_argument("--max-depth", type=int, default=20)
    knowledge_folder_scan.add_argument("--max-total-bytes", type=int, default=512 * 1024 * 1024)
    knowledge_folder_scan.add_argument("--extract-documents", action="store_true")
    knowledge_folder_scan.add_argument("--max-document-files", type=int, default=32)
    knowledge_folder_scan.add_argument("--document-timeout", type=float, default=20.0)
    add_local_knowledge_options(knowledge_folder_scan)
    knowledge_folder_plan = knowledge_commands.add_parser(
        "folder-plan",
        help="Preview deterministic system/secret-like exclusions before registering or scanning a folder; reads no file content and saves nothing.",
    )
    knowledge_folder_plan.add_argument("source", type=Path)
    knowledge_folder_plan.add_argument("--max-files", type=int, default=2000)
    knowledge_folder_plan.add_argument("--max-depth", type=int, default=20)
    knowledge_folder_plan.add_argument("--sample-limit", type=int, default=100)
    knowledge_folder_plan.add_argument("--ignore", action="append", default=[], metavar="GLOB", help="Preview one relative POSIX glob exclusion without saving it; repeatable.")
    add_local_knowledge_options(knowledge_folder_plan)
    knowledge_folder_watch = knowledge_commands.add_parser(
        "folder-watch",
        help="Poll one active Knowledge Folder locally and reconcile only after a detected change.",
    )
    knowledge_folder_watch.add_argument("folder_id")
    knowledge_folder_watch.add_argument("--interval", type=float, default=5.0)
    knowledge_folder_watch.add_argument("--cycles", type=int, default=None)
    knowledge_folder_watch.add_argument("--max-files", type=int, default=2000)
    knowledge_folder_watch.add_argument("--max-depth", type=int, default=20)
    knowledge_folder_watch.add_argument("--max-total-bytes", type=int, default=512 * 1024 * 1024)
    knowledge_folder_watch.add_argument("--extract-documents", action="store_true")
    knowledge_folder_watch.add_argument("--max-document-files", type=int, default=32)
    knowledge_folder_watch.add_argument("--document-timeout", type=float, default=20.0)
    add_local_knowledge_options(knowledge_folder_watch)
    knowledge_folders = knowledge_commands.add_parser(
        "folders", help="List registered raw Knowledge Folders without reading their content."
    )
    knowledge_folders.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(knowledge_folders)
    knowledge_folder_files = knowledge_commands.add_parser(
        "folder-files", help="List indexed paths and revisions for one Knowledge Folder."
    )
    knowledge_folder_files.add_argument("folder_id")
    knowledge_folder_files.add_argument("--include-deleted", action="store_true")
    knowledge_folder_files.add_argument("--limit", type=int, default=500)
    add_local_knowledge_options(knowledge_folder_files)
    knowledge_folder_open = knowledge_commands.add_parser(
        "folder-open",
        help="Open one indexed text entry with a byte bound and freeze its evidence snapshot.",
    )
    knowledge_folder_open.add_argument("entry_id")
    knowledge_folder_open.add_argument("--max-bytes", type=int, default=16_000)
    add_local_knowledge_options(knowledge_folder_open)
    knowledge_folder_preview = knowledge_commands.add_parser(
        "folder-preview",
        help="Preview indexed local text without a snapshot; credential-like values are redacted.",
    )
    knowledge_folder_preview.add_argument("entry_id")
    knowledge_folder_preview.add_argument("--max-bytes", type=int, default=16_000)
    add_local_knowledge_options(knowledge_folder_preview)
    knowledge_folder_pause = knowledge_commands.add_parser(
        "folder-pause", help="Pause future scans without touching raw folder files."
    )
    knowledge_folder_pause.add_argument("folder_id")
    add_local_knowledge_options(knowledge_folder_pause)
    knowledge_folder_resume = knowledge_commands.add_parser(
        "folder-resume", help="Resume future explicit scans for one Knowledge Folder."
    )
    knowledge_folder_resume.add_argument("folder_id")
    add_local_knowledge_options(knowledge_folder_resume)
    knowledge_folder_ignore = knowledge_commands.add_parser(
        "folder-ignore-set",
        help="Replace user-owned Folder ignore rules; raw files are never modified.",
    )
    knowledge_folder_ignore.add_argument("folder_id")
    knowledge_folder_ignore.add_argument("--ignore", action="append", default=[], metavar="GLOB", help="Relative POSIX glob; omit all rules to clear them.")
    knowledge_folder_ignore.add_argument("--confirm", action="store_true")
    add_local_knowledge_options(knowledge_folder_ignore)
    knowledge_folder_relink = knowledge_commands.add_parser(
        "folder-relink", help="Relink one registration to a new existing local folder."
    )
    knowledge_folder_relink.add_argument("folder_id")
    knowledge_folder_relink.add_argument("source", type=Path)
    knowledge_folder_relink.add_argument("--name", default=None)
    add_local_knowledge_options(knowledge_folder_relink)
    knowledge_folder_remove = knowledge_commands.add_parser(
        "folder-remove", help="Forget one folder registration and derived local index only."
    )
    knowledge_folder_remove.add_argument("folder_id")
    knowledge_folder_remove.add_argument("--confirm", action="store_true")
    add_local_knowledge_options(knowledge_folder_remove)
    knowledge_show = knowledge_commands.add_parser(
        "show", help="Show one Asset, record, Evidence Pack, candidate, Intent, or Decision."
    )
    knowledge_show.add_argument("identifier")
    add_local_knowledge_options(knowledge_show)
    knowledge_remember = knowledge_commands.add_parser(
        "remember", help="Write one explicit user-owned knowledge record."
    )
    knowledge_remember.add_argument("statement")
    knowledge_remember.add_argument("--kind", default="NOTE")
    knowledge_remember.add_argument("--confidence", type=float, default=1.0)
    knowledge_remember.add_argument("--access-scope", default="private")
    knowledge_remember.add_argument(
        "--epistemic-status",
        choices=tuple(item.value for item in EpistemicStatus),
        default=EpistemicStatus.UNKNOWN.value,
    )
    knowledge_remember.add_argument(
        "--trust-class",
        choices=tuple(item.value for item in ContentTrustClass),
        default=ContentTrustClass.USER_ASSERTED.value,
    )
    knowledge_remember.add_argument("--fresh-until", default=None)
    knowledge_remember.add_argument("--conflict-ref", action="append", default=[])
    knowledge_remember.add_argument("--unknown-ref", action="append", default=[])
    add_local_knowledge_options(knowledge_remember)
    knowledge_recall = knowledge_commands.add_parser(
        "recall", help="Build a bounded, cited Evidence Pack using local lexical retrieval."
    )
    knowledge_recall.add_argument("query")
    knowledge_recall.add_argument("--limit", type=int, default=5)
    knowledge_recall.add_argument("--max-bytes", type=int, default=12_000)
    knowledge_recall.add_argument("--max-excerpt-bytes", type=int, default=2400)
    knowledge_recall.add_argument("--access-scope", default="private")
    knowledge_recall.add_argument("--no-persist", action="store_true")
    add_local_knowledge_options(knowledge_recall)
    knowledge_why = knowledge_commands.add_parser(
        "why", help="Show the immutable citations and hashes behind one Evidence Pack."
    )
    knowledge_why.add_argument("pack_id")
    add_local_knowledge_options(knowledge_why)
    knowledge_correct = knowledge_commands.add_parser(
        "correct", help="Create a replacement revision and supersede one knowledge record."
    )
    knowledge_correct.add_argument("record_id")
    knowledge_correct.add_argument("statement")
    knowledge_correct.add_argument("--kind", default=None)
    knowledge_correct.add_argument("--confidence", type=float, default=None)
    knowledge_correct.add_argument(
        "--epistemic-status",
        choices=tuple(item.value for item in EpistemicStatus),
        default=None,
    )
    knowledge_correct.add_argument(
        "--trust-class",
        choices=tuple(item.value for item in ContentTrustClass),
        default=None,
    )
    knowledge_correct.add_argument("--fresh-until", default=None)
    knowledge_correct.add_argument("--conflict-ref", action="append", default=None)
    knowledge_correct.add_argument("--unknown-ref", action="append", default=None)
    add_local_knowledge_options(knowledge_correct)
    knowledge_forget = knowledge_commands.add_parser(
        "forget", help="Delete one record or Asset after explicit confirmation."
    )
    knowledge_forget.add_argument("identifier")
    knowledge_forget.add_argument("--confirm", action="store_true")
    add_local_knowledge_options(knowledge_forget)
    knowledge_candidates = knowledge_commands.add_parser(
        "candidates", help="List pending or resolved Knowledge Write Candidates."
    )
    knowledge_candidates.add_argument("--status", choices=("PENDING", "ACCEPTED", "REJECTED"), default=None)
    knowledge_candidates.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(knowledge_candidates)
    knowledge_lineage = knowledge_commands.add_parser(
        "lineage",
        help="Show a bounded content-free source→claim→decision→Job→outcome lineage graph without modifying Knowledge.",
    )
    knowledge_lineage.add_argument("--job-id", default=None)
    knowledge_lineage.add_argument("--intent-id", default=None)
    knowledge_lineage.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(knowledge_lineage)
    knowledge_review = knowledge_commands.add_parser(
        "review",
        help=(
            "Show bounded repeat-synthesis and page drift/conflict review leads "
            "without changing Knowledge records or pages."
        ),
    )
    knowledge_review.add_argument("--limit", type=int, default=32)
    add_local_knowledge_options(knowledge_review)
    for candidate_action, candidate_help in (
        ("candidate-accept", "Accept one pending candidate into the Knowledge DB."),
        ("candidate-reject", "Reject one pending candidate without recording its statement."),
    ):
        candidate_command = knowledge_commands.add_parser(candidate_action, help=candidate_help)
        candidate_command.add_argument("candidate_id")
        add_local_knowledge_options(candidate_command)
    candidate_page_preview = knowledge_commands.add_parser(
        "candidate-page-preview",
        help=(
            "Render deterministic Markdown for one accepted candidate without "
            "writing the user-owned Folder."
        ),
    )
    candidate_page_preview.add_argument("candidate_id")
    candidate_page_preview.add_argument("--folder-id", required=True)
    candidate_page_preview.add_argument(
        "--path",
        dest="relative_path",
        required=True,
        help="New page path under pages/ with a .md suffix.",
    )
    candidate_page_preview.add_argument("--title", required=True)
    add_local_knowledge_options(candidate_page_preview)
    candidate_page_publish = knowledge_commands.add_parser(
        "candidate-page-publish",
        help=(
            "Exclusively create a previewed accepted-candidate Markdown page; "
            "existing files are never overwritten."
        ),
    )
    candidate_page_publish.add_argument("candidate_id")
    candidate_page_publish.add_argument("--folder-id", required=True)
    candidate_page_publish.add_argument(
        "--path",
        dest="relative_path",
        required=True,
        help="New page path under pages/ with a .md suffix.",
    )
    candidate_page_publish.add_argument("--title", required=True)
    candidate_page_publish.add_argument(
        "--expected-sha256",
        required=True,
        help="Exact digest printed by candidate-page-preview.",
    )
    candidate_page_publish.add_argument("--confirm", action="store_true")
    add_local_knowledge_options(candidate_page_publish)
    for action, help_text in (
        (
            "candidate-page-bundle-preview",
            "Preview one Markdown page assembled from explicitly selected accepted candidates.",
        ),
        (
            "candidate-page-bundle-publish",
            "Exclusively create a previewed multi-candidate page; accepted records are unchanged.",
        ),
    ):
        command = knowledge_commands.add_parser(action, help=help_text)
        command.add_argument("--candidate-id", action="append", default=[], required=True)
        command.add_argument("--folder-id", required=True)
        command.add_argument("--path", dest="relative_path", required=True)
        command.add_argument("--title", required=True)
        if action == "candidate-page-bundle-publish":
            command.add_argument("--expected-sha256", required=True)
            command.add_argument("--confirm", action="store_true")
        add_local_knowledge_options(command)
    for action, help_text in (
        (
            "page-index-preview",
            "Render a deterministic type/topic navigation index without writing the Folder.",
        ),
        (
            "page-index-publish",
            "Exclusively create pages/index.md after exact digest confirmation; existing files are never overwritten.",
        ),
    ):
        command = knowledge_commands.add_parser(action, help=help_text)
        command.add_argument("--folder-id", required=True)
        command.add_argument("--max-pages", type=int, default=1000)
        command.add_argument("--max-entries", type=int, default=10_000)
        command.add_argument("--max-page-bytes", type=int, default=256_000)
        command.add_argument("--max-total-bytes", type=int, default=32_000_000)
        if action == "page-index-publish":
            command.add_argument("--expected-sha256", required=True)
            command.add_argument("--confirm", action="store_true")
        add_local_knowledge_options(command)
    page_lint = knowledge_commands.add_parser(
        "page-lint",
        help=(
            "Read bounded Markdown page metadata and links without modifying "
            "the Folder, Knowledge DB, or publication receipts."
        ),
    )
    page_lint.add_argument("--folder-id", required=True)
    page_lint.add_argument(
        "--as-of",
        default=None,
        metavar="YYYY-MM-DD",
        help="Optional deterministic date for staleness evaluation.",
    )
    page_lint.add_argument("--stale-after-days", type=int, default=90)
    page_lint.add_argument("--max-pages", type=int, default=1000)
    page_lint.add_argument("--max-entries", type=int, default=10_000)
    page_lint.add_argument("--max-page-bytes", type=int, default=256_000)
    page_lint.add_argument("--max-total-bytes", type=int, default=32_000_000)
    add_local_knowledge_options(page_lint)
    knowledge_outcomes = knowledge_commands.add_parser(
        "outcomes",
        help="List delayed real-world outcome observations separately from Firm Job success.",
    )
    knowledge_outcomes.add_argument(
        "--verdict",
        choices=tuple(item.value for item in OutcomeVerdict),
        default=None,
    )
    knowledge_outcomes.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(knowledge_outcomes)
    knowledge_observe = knowledge_commands.add_parser(
        "outcome-observe",
        help="Record one explicit Oracle observation; this does not auto-accept Knowledge or a Company Patch.",
    )
    knowledge_observe.add_argument("outcome_id")
    knowledge_observe.add_argument(
        "--verdict",
        choices=tuple(
            item.value for item in OutcomeVerdict
            if item is not OutcomeVerdict.NOT_YET_OBSERVED
        ),
        required=True,
    )
    knowledge_observe.add_argument("--signal", required=True)
    knowledge_observe.add_argument("--source-ref", required=True)
    knowledge_observe.add_argument("--reviewer-ref", required=True)
    knowledge_observe.add_argument("--observed-at", default=None)
    knowledge_observe.add_argument("--confounder", action="append", default=[])
    knowledge_observe.add_argument(
        "--attribution-status",
        choices=tuple(item.value for item in AttributionStatus),
        default=AttributionStatus.UNASSESSED.value,
    )
    add_local_knowledge_options(knowledge_observe)
    knowledge_export = knowledge_commands.add_parser(
        "export", help="Export a self-verifying Knowledge DB and Vault archive."
    )
    knowledge_export.add_argument("destination", type=Path)
    knowledge_export.add_argument("--force", action="store_true")
    add_local_knowledge_options(knowledge_export)
    knowledge_restore = knowledge_commands.add_parser(
        "restore", help="Validate and atomically restore a Knowledge archive."
    )
    knowledge_restore.add_argument("archive", type=Path)
    knowledge_restore.add_argument("--force", action="store_true")
    add_local_knowledge_options(knowledge_restore)
    knowledge_delete = knowledge_commands.add_parser(
        "delete", help="Delete the separate Knowledge DB and Vault after explicit confirmation."
    )
    knowledge_delete.add_argument("--confirm", action="store_true")
    add_local_knowledge_options(knowledge_delete)

    intent = commands.add_parser(
        "intent", help="Manage explicit, versioned user Intent records."
    )
    intent_commands = intent.add_subparsers(dest="intent_command", required=True)
    intent_create = intent_commands.add_parser("create", help="Create a local Intent.")
    intent_create.add_argument("goal")
    intent_create.add_argument("--priority", type=int, default=50)
    intent_create.add_argument("--status", choices=tuple(item.value for item in IntentStatus), default=IntentStatus.ACTIVE.value)
    intent_create.add_argument("--constraint", action="append", default=[])
    intent_create.add_argument("--accept", dest="acceptance_criteria", action="append", default=[])
    intent_create.add_argument("--knowledge-query", default="")
    add_local_knowledge_options(intent_create)
    intent_list = intent_commands.add_parser("list", help="List local Intents.")
    intent_list.add_argument("--status", choices=tuple(item.value for item in IntentStatus), default=None)
    intent_list.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(intent_list)
    intent_show = intent_commands.add_parser("show", help="Show one Intent and its revision history.")
    intent_show.add_argument("intent_id")
    add_local_knowledge_options(intent_show)
    intent_set_status = intent_commands.add_parser("status", help="Set an Intent lifecycle status.")
    intent_set_status.add_argument("intent_id")
    intent_set_status.add_argument("status", choices=tuple(item.value for item in IntentStatus))
    add_local_knowledge_options(intent_set_status)
    intent_run = intent_commands.add_parser(
        "run", help="Run one ACTIVE Intent with a bounded Evidence Pack through the Firm."
    )
    intent_run.add_argument("intent_id")
    intent_run.add_argument("--access-scope", default="private")
    intent_run.add_argument("--evidence-limit", type=int, default=6)
    intent_run.add_argument("--evidence-max-bytes", type=int, default=16_000)
    add_execution_options(intent_run)
    intent_run.add_argument("--json", action="store_true")
    intent_bindings = intent_commands.add_parser(
        "bindings",
        help="List content-free Intent execution bindings, including interrupted runs.",
    )
    intent_bindings.add_argument("--status", choices=("PREPARED", "TERMINAL"), default=None)
    intent_bindings.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(intent_bindings)
    intent_interrupt = intent_commands.add_parser(
        "interrupt",
        help="Explicitly terminalize an orphaned PREPARED Intent execution.",
    )
    intent_interrupt.add_argument("binding_id")
    intent_interrupt.add_argument("--confirm", action="store_true")
    add_local_knowledge_options(intent_interrupt)

    decision = commands.add_parser(
        "decision", help="Manage versioned decisions and local review reminders."
    )
    decision_commands = decision.add_subparsers(dest="decision_command", required=True)
    decision_record = decision_commands.add_parser("record", help="Record one Decision.")
    decision_record.add_argument("statement")
    decision_record.add_argument("--rationale", required=True)
    decision_record.add_argument("--status", choices=tuple(item.value for item in DecisionStatus if item is not DecisionStatus.SUPERSEDED), default=DecisionStatus.PROPOSED.value)
    decision_record.add_argument("--intent-id", default=None)
    decision_record.add_argument("--evidence-pack-id", default=None)
    decision_record.add_argument("--supersedes", default=None)
    decision_record.add_argument("--review-at", default=None)
    decision_record.add_argument("--actor", default="user:cli")
    add_local_knowledge_options(decision_record)
    decision_list = decision_commands.add_parser("list", help="List local Decisions.")
    decision_list.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(decision_list)
    decision_show = decision_commands.add_parser("show", help="Show one Decision and its revision history.")
    decision_show.add_argument("decision_id")
    add_local_knowledge_options(decision_show)
    decision_set_status = decision_commands.add_parser("status", help="Set a Decision status.")
    decision_set_status.add_argument("decision_id")
    decision_set_status.add_argument("status", choices=tuple(item.value for item in DecisionStatus if item is not DecisionStatus.SUPERSEDED))
    add_local_knowledge_options(decision_set_status)
    decision_due = decision_commands.add_parser("due", help="List Decisions due for review without starting a scheduler.")
    decision_due.add_argument("--as-of", default=None)
    decision_due.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(decision_due)

    question = commands.add_parser("question", help="Manage open questions in the local Intent and Decision plane.")
    question_commands = question.add_subparsers(dest="question_command", required=True)
    question_create = question_commands.add_parser("create", help="Create a user-owned open question without starting research.")
    question_create.add_argument("prompt")
    question_create.add_argument("--owner", default="user:cli")
    question_create.add_argument("--status", choices=tuple(item.value for item in QuestionStatus), default=QuestionStatus.OPEN.value)
    question_create.add_argument("--intent-id", default=None)
    question_create.add_argument("--decision-id", default=None)
    question_create.add_argument("--evidence-pack-id", default=None)
    question_create.add_argument("--answer-criterion", action="append", default=[])
    question_create.add_argument("--knowledge-query", default="")
    question_create.add_argument("--review-at", default=None)
    add_local_knowledge_options(question_create)
    question_list = question_commands.add_parser("list", help="List local open questions.")
    question_list.add_argument("--status", choices=tuple(item.value for item in QuestionStatus), default=None)
    question_list.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(question_list)
    question_show = question_commands.add_parser("show", help="Show one Question and immutable history.")
    question_show.add_argument("question_id")
    add_local_knowledge_options(question_show)
    question_status = question_commands.add_parser("status", help="Set Question lifecycle status.")
    question_status.add_argument("question_id")
    question_status.add_argument("status", choices=tuple(item.value for item in QuestionStatus))
    add_local_knowledge_options(question_status)

    research = commands.add_parser("research", help="Propose, approve, and track bounded research without automatic execution.")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    research_create = research_commands.add_parser("create", help="Create a DRAFT Research Request; it does not run a Firm Job.")
    research_create.add_argument("title")
    research_create.add_argument("--objective", required=True)
    research_create.add_argument("--owner", default="user:cli")
    research_create.add_argument("--question-id", default=None)
    research_create.add_argument("--intent-id", default=None)
    research_create.add_argument("--decision-id", default=None)
    research_create.add_argument("--evidence-pack-id", default=None)
    research_create.add_argument("--knowledge-query", default="")
    research_create.add_argument("--required-evidence", action="append", default=[])
    research_create.add_argument("--freshness-at", default=None)
    research_create.add_argument("--counterargument-required", action="store_true")
    research_create.add_argument("--max-cost-units", type=int, default=0)
    research_create.add_argument("--max-duration-minutes", type=int, default=60)
    add_local_knowledge_options(research_create)
    research_list = research_commands.add_parser("list", help="List Research Requests.")
    research_list.add_argument("--status", choices=tuple(item.value for item in ResearchRequestStatus), default=None)
    research_list.add_argument("--limit", type=int, default=100)
    add_local_knowledge_options(research_list)
    research_show = research_commands.add_parser("show", help="Show a Research Request and immutable history.")
    research_show.add_argument("request_id")
    add_local_knowledge_options(research_show)
    research_propose = research_commands.add_parser("review-propose", help="Create one DRAFT Question and Research Request for a Decision revision.")
    research_propose.add_argument("decision_id")
    research_propose.add_argument("--owner", default="user:cli")
    add_local_knowledge_options(research_propose)
    research_accept = research_commands.add_parser("accept", help="Accept a DRAFT Research Request and compile an ACTIVE Intent; no Job starts.")
    research_accept.add_argument("request_id")
    research_accept.add_argument("--priority", type=int, default=50)
    add_local_knowledge_options(research_accept)
    research_status = research_commands.add_parser("status", help="Set a non-acceptance Research Request lifecycle status.")
    research_status.add_argument("request_id")
    research_status.add_argument("status", choices=tuple(item.value for item in ResearchRequestStatus if item is not ResearchRequestStatus.ACCEPTED))
    add_local_knowledge_options(research_status)
