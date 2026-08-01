"""Human rendering for Knowledge CLI results."""

from __future__ import annotations

from typing import TextIO

from dynamic_firm.application.cli_component_contract import cli


def _render_knowledge_human(command: str, payload: object, output: TextIO) -> None:
    primitive = cli.to_primitive(payload)
    if command == "status":
        assert isinstance(primitive, cli.Mapping)
        counts = primitive.get("table_counts", {})
        total = sum(int(value) for value in counts.values()) if isinstance(counts, cli.Mapping) else 0
        print(
            "Knowledge Runtime · "
            f"database={'ready' if primitive.get('database_present') else 'not created'} · "
            f"vault={'ready' if primitive.get('vault_present') else 'not created'} · "
            f"integrity={primitive.get('database_integrity')} · objects={total}",
            file=output,
        )
        if primitive.get("pending_asset_mutation"):
            print(
                "Interrupted Asset mutation detected · run `noruct knowledge repair --confirm`.",
                file=output,
            )
        return
    if command == "capabilities":
        assert isinstance(primitive, cli.Mapping)
        print(
            "Local document extraction · "
            f"DOCX={primitive['docx']} · PDF={primitive['pdf']} · "
            f"image OCR={primitive['image_ocr']} · network=none",
            file=output,
        )
        print(str(primitive["limitations"]), file=output)
        return
    if command == "repair":
        assert isinstance(primitive, cli.Mapping)
        print(f"Knowledge Asset recovery · {primitive['recovery']}", file=output)
        return
    if command in {"add", "process"}:
        assert isinstance(primitive, cli.Mapping)
        asset = primitive["asset"]
        print(
            f"{asset['asset_id']} · {asset['status']} · {cli._knowledge_display(asset['title'])} · "
            f"{primitive['processing_status']}",
            file=output,
        )
        for message in primitive.get("messages", []):
            print(f"  {cli._knowledge_display(message)}", file=output)
        return
    if command == "remote-fetch":
        assert isinstance(primitive, cli.Mapping)
        asset = primitive["result"]["asset"]
        remote = primitive["remote"]
        print(
            f"Remote Knowledge Asset {asset['asset_id']} · {asset['status']} · "
            f"bytes={remote['downloaded_bytes']} · {asset['processing_status']}",
            file=output,
        )
        return
    if command == "remote-refresh":
        assert isinstance(primitive, cli.Mapping)
        result = primitive.get("result")
        asset = primitive.get("asset") if primitive["status"] == "NOT_MODIFIED" else result["asset"]
        print(
            f"Remote Knowledge refresh · {primitive['status']} · asset={asset['asset_id']} · "
            f"previous={primitive['previous_asset_id']}",
            file=output,
        )
        return
    if command == "assets":
        assert isinstance(primitive, list)
        if not primitive:
            print("No Knowledge Assets yet.", file=output)
        for asset in primitive:
            print(
                f"{asset['asset_id']} · {asset['status']} · {asset['media_type']} · "
                f"{cli._knowledge_display(asset['title'])}",
                file=output,
            )
        return
    if command == "folder-add":
        assert isinstance(primitive, cli.Mapping)
        folder = primitive["folder"]
        print(
            f"Knowledge Folder {folder['folder_id']} · {cli._knowledge_display(folder['display_name'])} · "
            f"{folder['last_scan_status']}",
            file=output,
        )
        scan = primitive.get("scan")
        if isinstance(scan, cli.Mapping):
            print(
                f"  scanned={scan['scanned_files']} · ready={scan['ready_files']} · "
                f"metadata-only={scan['metadata_only_files']} · documents={scan['document_files']} · "
                f"deleted={scan['deleted_entries']}",
                file=output,
            )
        return
    if command == "folder-scan":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge Folder scan · {primitive['folder']['folder_id']} · "
            f"files={primitive['scanned_files']} · ready={primitive['ready_files']} · "
            f"documents={primitive['document_files']} · "
            f"created={primitive['created_entries']} · updated={primitive['updated_entries']} · "
            f"renamed={primitive['renamed_entries']} · deleted={primitive['deleted_entries']} · "
            f"user-ignored={primitive.get('skipped_user_ignored', 0)} · "
            f"complete={'no' if primitive['truncated'] else 'yes'} · "
            f"cancelled={'yes' if primitive.get('cancelled') else 'no'}",
            file=output,
        )
        return
    if command == "folder-plan":
        assert isinstance(primitive, cli.Mapping)
        print(
            "Knowledge Folder preflight · "
            f"candidates={primitive['candidate_files']} · "
            f"system-ignored={primitive['ignored_system']} · "
            f"secret-like-ignored={primitive['ignored_secret_like']} · "
            f"user-pattern-ignored={primitive['ignored_user_patterns']} · "
            f"symlinks={primitive['skipped_symlinks']} · "
            f"depth-limited={primitive['depth_limited']} · "
            f"file-limit={'yes' if primitive['file_limited'] else 'no'}",
            file=output,
        )
        for entry in primitive.get("samples", []):
            print(
                f"  {entry['classification']} · {cli._knowledge_display(entry['relative_path'])}",
                file=output,
            )
        return
    if command == "folder-watch":
        assert isinstance(primitive, list)
        changes = sum(1 for event in primitive if event.get("changed"))
        print(f"Knowledge Folder watcher · cycles={len(primitive)} · reconciliations={changes}", file=output)
        return
    if command == "folders":
        assert isinstance(primitive, list)
        if not primitive:
            print("No Knowledge Folders yet.", file=output)
        for folder in primitive:
            print(
                f"{folder['folder_id']} · {folder['status']} · {folder['last_scan_status']} · "
                f"{cli._knowledge_display(folder['display_name'])}",
                file=output,
            )
        return
    if command == "folder-files":
        assert isinstance(primitive, list)
        if not primitive:
            print("No indexed Knowledge Folder files.", file=output)
        for entry in primitive:
            print(
                f"{entry['entry_id']} · {entry['index_status']} · r{entry['revision']} · "
                f"{cli._knowledge_display(entry['relative_path'])}",
                file=output,
            )
        return
    if command == "folder-open":
        assert isinstance(primitive, cli.Mapping)
        entry = primitive["entry"]
        print(
            f"{entry['entry_id']} · {cli._knowledge_display(entry['relative_path'])} · "
            f"snapshot={primitive['snapshot_asset_id']} · bytes={primitive['selected_bytes']}",
            file=output,
        )
        print(cli._knowledge_display(primitive["content"]), file=output)
        return
    if command == "folder-preview":
        assert isinstance(primitive, cli.Mapping)
        entry = primitive["entry"]
        print(
            f"Knowledge preview · {cli._knowledge_display(entry['relative_path'])} · bytes={primitive['selected_bytes']} · "
            f"redacted={'yes' if primitive['redacted'] else 'no'}",
            file=output,
        )
        structure = primitive["structure"]
        print(f"  headings={len(structure['headings'])} · tables={structure['table_count']} · images={len(structure['image_references'])}", file=output)
        print(cli._knowledge_display(primitive["content"]), file=output)
        return
    if command in {"folder-pause", "folder-resume", "folder-relink", "folder-ignore-set"}:
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge Folder {primitive['status'].lower()} · {primitive['folder_id']} · "
            f"{cli._knowledge_display(primitive['display_name'])}",
            file=output,
        )
        if command == "folder-ignore-set":
            print(f"User ignore rules: {len(primitive.get('ignore_globs', ()))}", file=output)
        return
    if command == "folder-remove":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge Folder registration removed · {primitive['folder_id']} · raw files unchanged",
            file=output,
        )
        return
    if command in {"recall", "why"}:
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Evidence Pack {primitive['pack_id']} · {len(primitive['items'])} source(s) · "
            f"{primitive['selected_bytes']} byte(s) · sha256={primitive['digest']}",
            file=output,
        )
        if not primitive["items"]:
            print("No matching local evidence was selected.", file=output)
        for index, item in enumerate(primitive["items"], start=1):
            print(
                f"[{index}] {cli._knowledge_display(item['title'])} · "
                f"{cli._knowledge_display(item['source_id'])}",
                file=output,
            )
            if item.get("retrieval_basis"):
                print(
                    "  basis=" + cli._knowledge_display(",".join(item["retrieval_basis"])),
                    file=output,
                )
            print(cli._knowledge_display(item["excerpt"]), file=output)
        return
    if command in {"remember", "correct"}:
        assert isinstance(primitive, cli.Mapping)
        suffix = (
            f" · supersedes {primitive['supersedes_record_id']}"
            if primitive.get("supersedes_record_id")
            else ""
        )
        print(f"{primitive['record_id']} · {primitive['kind']} r{primitive['revision']}{suffix}", file=output)
        return
    if command == "forget":
        assert isinstance(primitive, cli.Mapping)
        print(f"Forgot {primitive['identifier']} · local Knowledge state only", file=output)
        print(
            "Previously delivered provider inputs, Job audits, exports, and filesystem snapshots are outside this deletion.",
            file=output,
        )
        return
    if command == "candidates":
        assert isinstance(primitive, list)
        if not primitive:
            print("No Knowledge Write Candidates.", file=output)
        for candidate in primitive:
            print(
                f"{candidate['candidate_id']} · {candidate['status']} · job={candidate['job_id']}",
                file=output,
            )
        return
    if command == "lineage":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge lineage · nodes={len(primitive['nodes'])} · edges={len(primitive['edges'])} · "
            f"truncated={'yes' if primitive['truncated'] else 'no'} · no network request",
            file=output,
        )
        return
    if command == "outcomes":
        assert isinstance(primitive, list)
        if not primitive:
            print("No delayed Outcome observations.", file=output)
        for outcome in primitive:
            print(
                f"{outcome['outcome_id']} · {outcome['verdict']} · "
                f"job={outcome['job_id']} · attribution={outcome['attribution_status']}",
                file=output,
            )
        return
    if command == "outcome-observe":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"{primitive['outcome_id']} · {primitive['verdict']} · "
            f"attribution={primitive['attribution_status']}",
            file=output,
        )
        return
    if command in {"candidate-accept", "candidate-reject"}:
        assert isinstance(primitive, cli.Mapping)
        print(
            f"{primitive['candidate_id']} · {primitive['status']} · "
            f"record={primitive.get('accepted_record_id') or 'none'}",
            file=output,
        )
        return
    if command == "candidate-page-preview":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge page preview · {cli._knowledge_display(primitive['relative_path'])} · "
            f"{primitive['target_state']} · "
            f"publishable={'yes' if primitive['publishable'] else 'no'} · "
            f"bytes={primitive['byte_size']} · sha256={primitive['content_sha256']}",
            file=output,
        )
        print(cli._knowledge_display(primitive["markdown"]), file=output)
        return
    if command == "candidate-page-publish":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge page published · {primitive['publication_id']} · "
            f"{cli._knowledge_display(primitive['relative_path'])} · "
            f"bytes={primitive['byte_size']} · sha256={primitive['content_sha256']}",
            file=output,
        )
        print(
            "Publication receipt recorded without overwrite; future file edits remain user-controlled.",
            file=output,
        )
        return
    if command == "candidate-page-bundle-preview":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge page bundle preview · {primitive['target_state']} · "
            f"candidates={len(primitive['candidate_ids'])} · bytes={primitive['byte_size']} · "
            f"sha256={primitive['content_sha256']}",
            file=output,
        )
        print(cli._knowledge_display(primitive["markdown"]), file=output)
        return
    if command == "candidate-page-bundle-publish":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge page bundle published · {primitive['relative_path']} · "
            f"candidates={len(primitive['candidate_ids'])} · sha256={primitive['content_sha256']}",
            file=output,
        )
        print("Accepted records were not merged or changed; future page edits remain user-controlled.", file=output)
        return
    if command == "page-index-preview":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge page index preview · {primitive['target_state']} · "
            f"pages={primitive['indexed_page_count']} · topics={primitive['topic_count']} · "
            f"bytes={primitive['byte_size']} · sha256={primitive['content_sha256']}",
            file=output,
        )
        print(cli._knowledge_display(primitive["markdown"]), file=output)
        return
    if command == "page-index-publish":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge page index published · {primitive['relative_path']} · "
            f"pages={primitive['indexed_page_count']} · topics={primitive['topic_count']} · "
            f"sha256={primitive['content_sha256']}",
            file=output,
        )
        print("No existing index was overwritten; future edits remain user-controlled.", file=output)
        return
    if command == "review":
        assert isinstance(primitive, cli.Mapping)
        syntheses = primitive["syntheses"]
        lexical_near_duplicates = primitive["lexical_near_duplicates"]
        page_issues = primitive["page_issues"]
        assert isinstance(syntheses, list)
        assert isinstance(lexical_near_duplicates, list)
        assert isinstance(page_issues, list)
        print(
            "Knowledge review · "
            f"synthesis leads={len(syntheses)} · lexical leads={len(lexical_near_duplicates)} · page issues={len(page_issues)} · "
            f"complete={'no' if primitive['truncated'] else 'yes'}",
            file=output,
        )
        for lead in syntheses:
            assert isinstance(lead, cli.Mapping)
            print(
                f"  SYNTHESIS {lead['fingerprint']} · candidates={len(lead['candidate_ids'])} · "
                f"jobs={len(lead['job_ids'])}",
                file=output,
            )
        for lead in lexical_near_duplicates:
            assert isinstance(lead, cli.Mapping)
            print(
                f"  LEXICAL {lead['fingerprint']} · candidates={len(lead['candidate_ids'])} · "
                f"similarity={lead['similarity_basis_points'] / 100:.2f}%",
                file=output,
            )
        for issue in page_issues:
            assert isinstance(issue, cli.Mapping)
            location = cli._knowledge_display(issue["relative_path"] or "<folder>")
            print(f"  PAGE {issue['code']} · {location}", file=output)
        if not syntheses and not lexical_near_duplicates and not page_issues:
            print("No repeat synthesis lead or page review issue is awaiting review.", file=output)
        print(
            "Read-only review · inspect and accept/reject each candidate explicitly; no page was published.",
            file=output,
        )
        return
    if command == "page-lint":
        assert isinstance(primitive, cli.Mapping)
        issues = primitive["issues"]
        assert isinstance(issues, list)
        print(
            f"Knowledge page lint · {'PASS' if primitive['passed'] else 'FAIL'} · "
            f"folder={primitive['folder_id']} · pages={primitive['scanned_pages']} · "
            f"bytes={primitive['scanned_bytes']} · "
            f"truncated={'yes' if primitive['truncated'] else 'no'}",
            file=output,
        )
        if not issues:
            print("No Knowledge page health issues.", file=output)
        for issue in issues:
            assert isinstance(issue, cli.Mapping)
            location = cli._knowledge_display(issue["relative_path"] or "<folder>")
            reference = issue.get("reference")
            suffix = (
                f" · reference={cli._knowledge_display(reference)}"
                if reference
                else ""
            )
            print(
                f"  {issue['severity']} {issue['code']} · {location}{suffix}",
                file=output,
            )
        return
    if command == "export":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge archive exported · sha256={primitive['archive_sha256']} · "
            f"{primitive['vault_object_count']} Vault object(s)",
            file=output,
        )
        print("The archive contains user content; protect it like the live Knowledge Vault.", file=output)
        return
    if command == "restore":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"Knowledge archive restored · integrity={primitive['database_integrity']} · "
            f"{primitive['vault_object_count']} Vault object(s)",
            file=output,
        )
        return
    if command == "delete":
        assert isinstance(primitive, cli.Mapping)
        print(
            "Knowledge state deletion · "
            f"{'deleted' if primitive['deleted'] else 'nothing to delete'}",
            file=output,
        )
        print(
            "Registered raw Knowledge Folder files were not modified or deleted.",
            file=output,
        )
        print(str(primitive["residual_backup_warning"]), file=output)
        return
    cli._knowledge_json(payload, output)
