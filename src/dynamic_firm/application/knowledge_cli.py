"""Application adapter for parsed local Knowledge commands.

The CLI remains the parser, configuration ingress, and terminal error boundary.
This module receives an explicit Knowledge database/Vault pair and rendering
ports, then calls the existing Knowledge services.  It never becomes another
Knowledge database, Vault, Company state, or authority boundary.
"""

from __future__ import annotations

import argparse
import re
import signal
from datetime import date
from pathlib import Path
from typing import Callable, TextIO

from dynamic_firm.knowledge import (
    AttributionStatus,
    ContentTrustClass,
    EpistemicStatus,
    KnowledgeFolderScanControl,
    KnowledgeFolderService,
    KnowledgeFolderWatcher,
    KnowledgePageLinter,
    KnowledgePageIndexService,
    KnowledgePageBundleService,
    KnowledgePageService,
    KnowledgeStore,
    OutcomeVerdict,
    build_knowledge_review_surface,
    build_knowledge_lineage,
)
from dynamic_firm.knowledge.lifecycle import (
    authorize_knowledge_deletion,
    delete_knowledge_state,
    export_knowledge_archive,
    knowledge_diagnostics,
    restore_knowledge_archive,
)
from dynamic_firm.knowledge.service import UserKnowledgeService
from dynamic_firm.knowledge.vault import KnowledgeVault
from dynamic_firm.product.knowledge_cli_values import knowledge_limit, show_knowledge_value


KNOWLEDGE_COMMAND_OK = 0


def _candidate_page_expected_sha256(value: str) -> str:
    expected = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError(
            "Knowledge page expected SHA-256 must be exactly 64 hexadecimal characters"
        )
    return expected


def _page_lint_as_of(value: str | None) -> date | None:
    if value is None:
        return None
    normalized = value.strip()
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("Knowledge page lint --as-of must be YYYY-MM-DD") from error
    if parsed.isoformat() != normalized:
        raise ValueError("Knowledge page lint --as-of must be YYYY-MM-DD")
    return parsed


def _scan_knowledge_folder_cli(
    service: KnowledgeFolderService,
    folder_id: str,
    args: argparse.Namespace,
):
    """Make Ctrl-C a safe partial reconciliation instead of a process abort.

    This is deliberately a CLI adapter around the surface-neutral scan control.
    A second UI can own the same control from another thread without importing
    terminal signal behavior into the Knowledge runtime.
    """

    control = KnowledgeFolderScanControl()
    previous_handler = None
    installed_handler = False

    def request_cancel(_signal_number: int, _frame: object) -> None:
        control.cancel()

    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, request_cancel)
        installed_handler = True
    except (ValueError, OSError):
        # Embedders may invoke the command off the main thread.  They still
        # receive the safe runtime control contract, just not terminal SIGINT.
        pass
    try:
        return service.scan(
            folder_id,
            max_files=args.max_files,
            max_depth=args.max_depth,
            max_total_bytes=args.max_total_bytes,
            extract_documents=args.extract_documents,
            max_document_files=args.max_document_files,
            document_timeout_seconds=args.document_timeout,
            control=control,
        )
    finally:
        if installed_handler:
            signal.signal(signal.SIGINT, previous_handler)


def _watch_knowledge_folder_cli(
    service: KnowledgeFolderService, folder_id: str, args: argparse.Namespace
):
    """Foreground watcher with Ctrl-C translated to safe scan cancellation."""
    control = KnowledgeFolderScanControl()
    previous_handler = None
    installed_handler = False
    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda _number, _frame: control.cancel())
        installed_handler = True
    except (ValueError, OSError):
        pass
    try:
        return KnowledgeFolderWatcher(service, interval_seconds=args.interval).watch(
            folder_id,
            control=control,
            max_cycles=args.cycles,
            max_files=args.max_files,
            max_depth=args.max_depth,
            max_total_bytes=args.max_total_bytes,
            extract_documents=args.extract_documents,
            max_document_files=args.max_document_files,
            document_timeout_seconds=args.document_timeout,
        )
    finally:
        if installed_handler:
            signal.signal(signal.SIGINT, previous_handler)


def run_knowledge_command(
    args: argparse.Namespace,
    *,
    database: Path,
    vault_path: Path,
    render_json: Callable[[object, TextIO], None],
    render_human: Callable[[str, object, TextIO], None],
    output: TextIO,
) -> int:
    command = args.knowledge_command
    if command == "status":
        payload: object = knowledge_diagnostics(database, vault_path)
    elif command == "capabilities":
        from dynamic_firm.knowledge.intake import LocalDocumentExtractor

        payload = LocalDocumentExtractor.capabilities()
    elif command == "folder-plan":
        payload = KnowledgeFolderService.preview_root(
            args.source,
            max_files=args.max_files,
            max_depth=args.max_depth,
            sample_limit=args.sample_limit,
            ignore_globs=tuple(args.ignore),
        )
    elif command == "export":
        payload = export_knowledge_archive(
            database,
            vault_path,
            args.destination,
            overwrite=args.force,
        )
    elif command == "restore":
        payload = restore_knowledge_archive(
            args.archive,
            database,
            vault_path,
            overwrite=args.force,
        )
    elif command == "delete":
        authorization = authorize_knowledge_deletion(
            database,
            vault_path,
            confirmed=args.confirm,
        )
        payload = delete_knowledge_state(
            database,
            vault_path,
            authorization=authorization,
        )
    elif command in {"remote-fetch", "remote-refresh"} and not args.confirm:
        raise ValueError(
            "Knowledge remote fetch or refresh requires --confirm; it reads one external URL into local Knowledge state"
        )
    elif command in {"candidate-page-publish", "candidate-page-bundle-publish", "page-index-publish"} and not args.confirm:
        raise ValueError("Knowledge page publication requires --confirm")
    else:
        page_expected_sha256: str | None = None
        page_lint_as_of: date | None = None
        if command in {"remote-fetch", "remote-refresh"}:
            from dynamic_firm.knowledge.remote_fetch import normalize_expected_sha256

            # Reject malformed one-shot integrity input before opening the
            # local Knowledge store or issuing the explicit network read.
            normalize_expected_sha256(args.expected_sha256)
        elif command in {"candidate-page-publish", "candidate-page-bundle-publish", "page-index-publish"}:
            # Reject malformed confirmation input before opening the local DB.
            page_expected_sha256 = _candidate_page_expected_sha256(
                args.expected_sha256
            )
        elif command == "page-lint":
            page_lint_as_of = _page_lint_as_of(args.as_of)
        mutating = command in {
            "add", "remote-fetch", "remote-refresh", "remember", "folder-add", "folder-pause", "folder-resume",
            "folder-relink", "folder-remove",
        }
        if not database.is_file() and not mutating:
            if command in {"assets", "candidates", "outcomes", "folders", "lineage", "review"}:
                payload = (
                    {
                        "schema": "noruct.knowledge-lineage.v1",
                        "job_id": args.job_id,
                        "intent_id": args.intent_id,
                        "nodes": (),
                        "edges": (),
                        "truncated": False,
                        "network_request_performed": False,
                    }
                    if command == "lineage"
                    else ()
                )
                if command == "review":
                    payload = {
                        "syntheses": (),
                        "lexical_near_duplicates": (),
                        "page_issues": (),
                        "truncated": False,
                    }
                if args.json:
                    render_json(payload, output)
                else:
                    render_human(command, payload, output)
                return KNOWLEDGE_COMMAND_OK
            raise ValueError("Knowledge DB has not been created; add or remember something first")
        store = KnowledgeStore(database)
        try:
            if command == "repair" and not args.confirm:
                raise ValueError("Knowledge repair requires --confirm")
            vault = KnowledgeVault(vault_path)
            service = UserKnowledgeService(store, vault)
            if command == "repair":
                payload = {"recovery": service.last_recovery or "NO_PENDING_MUTATION"}
            elif command == "add":
                payload = service.ingest(
                    args.source,
                    title=args.title,
                    origin=args.origin,
                    access_scope=args.access_scope,
                    labels=tuple(args.label),
                    parent_asset_id=args.parent_asset_id,
                    processor=args.processor,
                    timeout_seconds=args.timeout,
                )
            elif command == "remote-fetch":
                result, download = service.ingest_public_https(
                    args.url,
                    title=args.title,
                    access_scope=args.access_scope,
                    labels=tuple(args.label),
                    processor=args.processor,
                    timeout_seconds=args.timeout,
                    expected_sha256=args.expected_sha256,
                )
                payload = {
                    "result": result,
                    "remote": {
                        "source_url": download.source_url,
                        "content_type": download.content_type,
                        "downloaded_bytes": download.downloaded_bytes,
                        "mode": "EXPLICIT_ONE_SHOT_LOCAL_IMPORT",
                    },
                }
            elif command == "remote-refresh":
                payload = service.refresh_public_https(
                    args.asset_id,
                    processor=args.processor,
                    timeout_seconds=args.timeout,
                    expected_sha256=args.expected_sha256,
                )
            elif command == "process":
                payload = service.process(
                    args.asset_id,
                    processor=args.processor,
                    timeout_seconds=args.timeout,
                )
            elif command == "assets":
                payload = store.list_assets(
                    limit=knowledge_limit(args.limit, label="Asset list limit"),
                    status=args.status,
                )
            elif command == "folder-add":
                folder_service = KnowledgeFolderService(store, vault)
                folder, duplicate = folder_service.register(
                    args.source,
                    display_name=args.name,
                    access_scope=args.access_scope,
                    ignore_globs=tuple(args.ignore),
                )
                scan = None
                if not args.no_scan:
                    scan = _scan_knowledge_folder_cli(
                        folder_service,
                        folder.folder_id,
                        args,
                    )
                    folder = scan.folder
                payload = {"folder": folder, "duplicate": duplicate, "scan": scan}
            elif command == "folder-scan":
                payload = _scan_knowledge_folder_cli(
                    KnowledgeFolderService(store, vault),
                    args.folder_id,
                    args,
                )
            elif command == "folder-watch":
                payload = _watch_knowledge_folder_cli(
                    KnowledgeFolderService(store, vault), args.folder_id, args
                )
            elif command == "folders":
                payload = store.list_knowledge_folders(
                    limit=knowledge_limit(args.limit, label="folder list limit")
                )
            elif command == "folder-files":
                payload = store.list_knowledge_folder_entries(
                    args.folder_id,
                    include_deleted=args.include_deleted,
                    limit=knowledge_limit(args.limit, label="folder entry limit"),
                )
            elif command == "folder-open":
                payload = KnowledgeFolderService(store, vault).open_entry(
                    args.entry_id,
                    max_bytes=args.max_bytes,
                )
            elif command == "folder-preview":
                payload = KnowledgeFolderService(store, vault).preview_entry(
                    args.entry_id, max_bytes=args.max_bytes
                )
            elif command == "folder-pause":
                payload = KnowledgeFolderService(store, vault).pause(args.folder_id)
            elif command == "folder-resume":
                payload = KnowledgeFolderService(store, vault).resume(args.folder_id)
            elif command == "folder-ignore-set":
                if not args.confirm:
                    raise ValueError("Knowledge Folder ignore rule changes require --confirm")
                payload = KnowledgeFolderService(store, vault).set_ignore_globs(
                    args.folder_id,
                    ignore_globs=tuple(args.ignore),
                )
            elif command == "folder-relink":
                payload = KnowledgeFolderService(store, vault).relink(
                    args.folder_id, args.source, display_name=args.name
                )
            elif command == "folder-remove":
                if not args.confirm:
                    raise ValueError("Knowledge Folder removal requires --confirm")
                if not KnowledgeFolderService(store, vault).remove(args.folder_id):
                    raise ValueError(f"Knowledge Folder was not found: {args.folder_id}")
                payload = {"folder_id": args.folder_id, "removed": True}
            elif command == "show":
                payload = show_knowledge_value(store, args.identifier)
            elif command == "remember":
                payload = store.create_record(
                    kind=args.kind,
                    statement=args.statement,
                    confidence=args.confidence,
                    access_scope=args.access_scope,
                    epistemic_status=EpistemicStatus(args.epistemic_status),
                    trust_class=ContentTrustClass(args.trust_class),
                    freshness_expires_at=args.fresh_until,
                    conflict_refs=tuple(args.conflict_ref),
                    unknown_refs=tuple(args.unknown_ref),
                )
            elif command == "recall":
                payload = service.build_evidence_pack(
                    args.query,
                    limit=args.limit,
                    max_bytes=args.max_bytes,
                    max_excerpt_bytes=args.max_excerpt_bytes,
                    access_scope=args.access_scope,
                    persist=not args.no_persist,
                )
            elif command == "why":
                payload = store.evidence_pack(args.pack_id)
                if payload is None:
                    raise ValueError(f"Evidence Pack was not found: {args.pack_id}")
            elif command == "correct":
                previous = store.record(args.record_id)
                if previous is None:
                    raise ValueError(f"Knowledge record was not found: {args.record_id}")
                previous_epistemic = store.epistemic_annotation(
                    "RECORD", previous.record_id
                )
                payload = store.create_record(
                    kind=args.kind or previous.kind,
                    statement=args.statement,
                    confidence=(previous.confidence if args.confidence is None else args.confidence),
                    source_asset_id=previous.source_asset_id,
                    source_representation_id=previous.source_representation_id,
                    source_span=previous.source_span,
                    supersedes_record_id=previous.record_id,
                    source_candidate_id=previous.source_candidate_id,
                    source_job_id=previous.source_job_id,
                    evidence_pack_id=previous.evidence_pack_id,
                    access_scope=previous.access_scope,
                    epistemic_status=EpistemicStatus(
                        args.epistemic_status
                        or (
                            previous_epistemic.epistemic_status.value
                            if previous_epistemic is not None
                            else EpistemicStatus.UNKNOWN.value
                        )
                    ),
                    trust_class=ContentTrustClass(
                        args.trust_class
                        or (
                            previous_epistemic.trust_class.value
                            if previous_epistemic is not None
                            else ContentTrustClass.UNSPECIFIED.value
                        )
                    ),
                    freshness_expires_at=(
                        args.fresh_until
                        if args.fresh_until is not None
                        else (
                            previous_epistemic.freshness_expires_at
                            if previous_epistemic is not None
                            else None
                        )
                    ),
                    conflict_refs=tuple(
                        args.conflict_ref
                        if args.conflict_ref is not None
                        else (
                            previous_epistemic.conflict_refs
                            if previous_epistemic is not None
                            else ()
                        )
                    ),
                    unknown_refs=tuple(
                        args.unknown_ref
                        if args.unknown_ref is not None
                        else (
                            previous_epistemic.unknown_refs
                            if previous_epistemic is not None
                            else ()
                        )
                    ),
                )
            elif command == "forget":
                if not args.confirm:
                    raise ValueError("Knowledge forget requires --confirm")
                if args.identifier.startswith("asset-"):
                    service.delete_asset(args.identifier)
                elif args.identifier.startswith("record-"):
                    if not store.forget_record(args.identifier):
                        raise ValueError(f"Knowledge record was not found: {args.identifier}")
                else:
                    raise ValueError("Knowledge forget accepts an asset- or record- identifier")
                payload = {"forgotten": True, "identifier": args.identifier}
            elif command == "candidates":
                payload = store.list_write_candidates(
                    status=args.status,
                    limit=knowledge_limit(args.limit, label="candidate list limit"),
                )
            elif command == "lineage":
                payload = build_knowledge_lineage(
                    store,
                    job_id=args.job_id,
                    intent_id=args.intent_id,
                    limit=knowledge_limit(args.limit, label="lineage limit"),
                )
            elif command == "review":
                payload = build_knowledge_review_surface(
                    store,
                    limit=knowledge_limit(args.limit, label="review limit"),
                )
            elif command in {"candidate-accept", "candidate-reject"}:
                payload = store.resolve_write_candidate(
                    args.candidate_id,
                    accept=command == "candidate-accept",
                )
            elif command == "candidate-page-preview":
                payload = KnowledgePageService(store).preview_candidate_page(
                    candidate_id=args.candidate_id,
                    folder_id=args.folder_id,
                    relative_path=args.relative_path,
                    title=args.title,
                )
            elif command == "candidate-page-publish":
                assert page_expected_sha256 is not None
                payload = KnowledgePageService(store).publish_candidate_page(
                    candidate_id=args.candidate_id,
                    folder_id=args.folder_id,
                    relative_path=args.relative_path,
                    title=args.title,
                    expected_content_sha256=page_expected_sha256,
                    confirm=True,
                )
            elif command == "candidate-page-bundle-preview":
                payload = KnowledgePageBundleService(store).preview(
                    candidate_ids=args.candidate_id,
                    folder_id=args.folder_id,
                    relative_path=args.relative_path,
                    title=args.title,
                )
            elif command == "candidate-page-bundle-publish":
                assert page_expected_sha256 is not None
                payload = KnowledgePageBundleService(store).publish(
                    candidate_ids=args.candidate_id,
                    folder_id=args.folder_id,
                    relative_path=args.relative_path,
                    title=args.title,
                    expected_content_sha256=page_expected_sha256,
                    confirm=True,
                )
            elif command == "page-index-preview":
                payload = KnowledgePageIndexService(store).preview(
                    folder_id=args.folder_id,
                    max_pages=args.max_pages,
                    max_entries=args.max_entries,
                    max_page_bytes=args.max_page_bytes,
                    max_total_bytes=args.max_total_bytes,
                )
            elif command == "page-index-publish":
                assert page_expected_sha256 is not None
                payload = KnowledgePageIndexService(store).publish(
                    folder_id=args.folder_id,
                    expected_content_sha256=page_expected_sha256,
                    confirm=True,
                    max_pages=args.max_pages,
                    max_entries=args.max_entries,
                    max_page_bytes=args.max_page_bytes,
                    max_total_bytes=args.max_total_bytes,
                )
            elif command == "page-lint":
                report = KnowledgePageLinter(store).lint(
                    folder_id=args.folder_id,
                    as_of=page_lint_as_of,
                    stale_after_days=args.stale_after_days,
                    max_pages=args.max_pages,
                    max_entries=args.max_entries,
                    max_page_bytes=args.max_page_bytes,
                    max_total_bytes=args.max_total_bytes,
                )
                payload = {
                    "folder_id": report.folder_id,
                    "passed": report.passed,
                    "scanned_pages": report.scanned_pages,
                    "scanned_bytes": report.scanned_bytes,
                    "truncated": report.truncated,
                    "issues": report.issues,
                }
            elif command == "outcomes":
                payload = store.list_outcomes(
                    verdict=args.verdict,
                    limit=knowledge_limit(args.limit, label="outcome list limit"),
                )
            elif command == "outcome-observe":
                payload = store.observe_outcome(
                    args.outcome_id,
                    verdict=OutcomeVerdict(args.verdict),
                    observed_signal=args.signal,
                    source_ref=args.source_ref,
                    reviewer_ref=args.reviewer_ref,
                    observed_at=args.observed_at,
                    confounders=tuple(args.confounder),
                    attribution_status=AttributionStatus(args.attribution_status),
                )
            else:
                raise ValueError(f"Unknown knowledge command: {command}")
        finally:
            store.close()
    if args.json:
        render_json(payload, output)
    else:
        render_human(command, payload, output)
    return KNOWLEDGE_COMMAND_OK
