"""Provider-free terminal commands for the user-owned Knowledge Runtime.

This adapter is deliberately below both terminal surfaces and above the
Knowledge Store.  It never opens Company sessions, builds a model provider, or
routes a command through the Firm Kernel.  Consequently, evidence displayed by
``/knowledge`` cannot enter employee-session memory through this path.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from dynamic_firm.knowledge.folder_service import KnowledgeFolderService
from dynamic_firm.knowledge.models import IntentStatus, QuestionStatus, ResearchRequestStatus
from dynamic_firm.knowledge.service import UserKnowledgeService
from dynamic_firm.knowledge.store import (
    KnowledgeStore,
    knowledge_state_path,
    knowledge_vault_path,
)
from dynamic_firm.knowledge.review import build_knowledge_review_surface
from dynamic_firm.knowledge.vault import KnowledgeVault
from dynamic_firm.product.terminal import strip_ansi
from dynamic_firm.product.knowledge_workbench import build_knowledge_workbench


_MAX_COMMAND_BYTES = 64_000
_MAX_DISPLAY_CHARS = 240


def _folder_help_messages() -> tuple[str, ...]:
    return (
        "Knowledge Folder commands · local, provider-free",
        "/knowledge folder add <directory>",
        "/knowledge folder add <directory> --extract-documents",
        "/knowledge folder scan <folder-id> [--extract-documents]",
        "/knowledge folder pause <folder-id>",
        "/knowledge folder resume <folder-id>",
        "/knowledge folder relink <folder-id> <directory>",
        "/knowledge folder remove <folder-id> --confirm",
        "/knowledge folder list",
        "/knowledge folder files <folder-id>",
        "/knowledge folder open <entry-id>",
    )


def _one_line(value: str, maximum: int = _MAX_DISPLAY_CHARS) -> str:
    normalized = " ".join(strip_ansi(value).split())
    if len(normalized) <= maximum:
        return normalized
    return normalized[: max(0, maximum - 1)].rstrip() + "…"


def _bounded_argument(value: str, *, label: str, required: bool = False) -> str:
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{label} must be non-empty")
    if len(normalized.encode("utf-8")) > _MAX_COMMAND_BYTES:
        raise ValueError(f"{label} exceeds the 64000 byte local command limit")
    return normalized


def _command_tokens(value: str, *, label: str) -> list[str]:
    try:
        tokens = shlex.split(value)
    except ValueError as exc:
        raise ValueError(f"{label} could not be parsed: {exc}") from exc
    if not tokens:
        raise ValueError(f"{label} requires an action")
    return tokens


def _option(tokens: list[str], name: str) -> str | None:
    try:
        index = tokens.index(name)
    except ValueError:
        return None
    if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
        raise ValueError(f"{name} requires a value")
    return tokens[index + 1]


def _words_before_options(tokens: list[str], *, start: int) -> str:
    values: list[str] = []
    index = start
    while index < len(tokens) and not tokens[index].startswith("--"):
        values.append(tokens[index])
        index += 1
    return " ".join(values).strip()


def _knowledge_summary(store: KnowledgeStore) -> tuple[str, ...]:
    counts = store.counts()
    active_intents = len(store.list_intents(status=IntentStatus.ACTIVE, limit=100))
    due = len(store.due_decisions(limit=100))
    questions = len(store.list_questions(status=QuestionStatus.OPEN, limit=100))
    research = len(store.list_research_requests(status=ResearchRequestStatus.DRAFT, limit=100))
    return (
        "Knowledge Runtime · local, user-owned, provider-free",
        (
            f"{counts['knowledge_assets']} asset(s) · {counts['knowledge_records']} record(s) · "
            f"{active_intents} active intent(s) · {due} decision review(s) due · "
            f"{questions} open question(s) · {research} draft research request(s)"
        ),
        "Use /knowledge <query> to retrieve a bounded, non-persisted local view.",
    )


def _knowledge_query(
    store: KnowledgeStore,
    state_path: Path,
    query: str,
) -> tuple[str, ...]:
    service = UserKnowledgeService(
        store,
        KnowledgeVault(knowledge_vault_path(state_path)),
    )
    pack = service.build_evidence_pack(
        query,
        limit=5,
        max_bytes=12_000,
        max_excerpt_bytes=1_600,
        persist=False,
    )
    messages = [
        (
            f"Knowledge view · {len(pack.items)} match(es) from "
            f"{pack.candidate_count} candidate(s) · {pack.selected_bytes} bytes · no Job created"
        )
    ]
    if not pack.items:
        messages.append(
            "No matching local evidence. Nothing was sent to a model or employee session."
        )
        return tuple(messages)
    messages.extend(
        (
            f"[{index}] {_one_line(item.title, 80)} · {item.source_id} · "
            f"basis={','.join(item.retrieval_basis) or 'lexical'} · {_one_line(item.excerpt)}"
        )
        for index, item in enumerate(pack.items, start=1)
    )
    messages.append(
        "Local view only · evidence body was not appended to Company or employee history."
    )
    return tuple(messages)


def _folder_scan_messages(report) -> tuple[str, ...]:
    result = (
        f"Knowledge Folder scan · {report.folder.folder_id} · "
        f"{report.scanned_files} file(s) · {report.ready_files} searchable · "
        f"{report.metadata_only_files} metadata-only"
    )
    changes = (
        f"Changes · {report.created_entries} created · {report.updated_entries} updated · "
        f"{report.renamed_entries} renamed · {report.deleted_entries} deleted · "
        f"{report.unchanged_entries} unchanged"
    )
    safety = (
        "Bounded scan was incomplete; unseen files were preserved."
        if report.truncated
        else "Raw files remain in place; no source file was copied, moved, or deleted."
    )
    return result, changes, safety, *tuple(_one_line(item) for item in report.messages[:5])


def _knowledge_folder_command(
    store: KnowledgeStore,
    state_path: Path,
    argument: str,
) -> tuple[str, ...] | None:
    try:
        tokens = shlex.split(argument)
    except ValueError as exc:
        raise ValueError(f"Knowledge Folder command could not be parsed: {exc}") from exc
    if not tokens or tokens[0].lower() != "folder":
        return None
    if len(tokens) == 1 or tokens[1].lower() in {"help", "?"}:
        return _folder_help_messages()
    action = tokens[1].lower()
    service = KnowledgeFolderService(store, KnowledgeVault(knowledge_vault_path(state_path)))
    if action == "add":
        extract_documents = len(tokens) == 4 and tokens[3] == "--extract-documents"
        if len(tokens) != (4 if extract_documents else 3):
            raise ValueError(
                "Usage: /knowledge folder add <directory> [--extract-documents]"
            )
        folder, existing = service.register(tokens[2])
        report = service.scan(folder.folder_id, extract_documents=extract_documents)
        prefix = "Already registered" if existing else "Registered"
        return (
            f"{prefix} Knowledge Folder · {folder.folder_id} · {_one_line(folder.root_path)}",
            *_folder_scan_messages(report),
        )
    if action == "scan":
        extract_documents = len(tokens) == 4 and tokens[3] == "--extract-documents"
        if len(tokens) != (4 if extract_documents else 3):
            raise ValueError(
                "Usage: /knowledge folder scan <folder-id> [--extract-documents]"
            )
        return _folder_scan_messages(
            service.scan(tokens[2], extract_documents=extract_documents)
        )
    if action in {"pause", "resume"}:
        if len(tokens) != 3:
            raise ValueError(f"Usage: /knowledge folder {action} <folder-id>")
        folder = service.pause(tokens[2]) if action == "pause" else service.resume(tokens[2])
        return (
            f"Knowledge Folder {folder.status.value.lower()} · {folder.folder_id}",
            "Raw files remain in place; this only changes whether a future explicit scan is allowed.",
        )
    if action == "relink":
        if len(tokens) != 4:
            raise ValueError("Usage: /knowledge folder relink <folder-id> <directory>")
        folder = service.relink(tokens[2], tokens[3])
        return (
            f"Knowledge Folder relinked · {folder.folder_id} · {_one_line(folder.root_path, 140)}",
            "Run an explicit scan to reconcile this new raw folder; no source file was moved or copied.",
        )
    if action == "remove":
        if len(tokens) != 4 or tokens[3] != "--confirm":
            raise ValueError("Usage: /knowledge folder remove <folder-id> --confirm")
        if not service.remove(tokens[2]):
            raise ValueError(f"Knowledge Folder was not found: {tokens[2]}")
        return (
            f"Knowledge Folder registration removed · {tokens[2]}",
            "The derived local index was removed. Raw folder files and preserved snapshots were not deleted.",
        )
    if action == "list":
        if len(tokens) != 2:
            raise ValueError("Usage: /knowledge folder list")
        folders = store.list_knowledge_folders(limit=50)
        if not folders:
            return ("Knowledge Folders · none",)
        return (
            f"Knowledge Folders · {len(folders)} shown",
            *(
                f"{item.folder_id} · {_one_line(item.display_name, 80)} · "
                f"{item.last_scan_status.lower()} · {_one_line(item.root_path, 120)}"
                for item in folders
            ),
        )
    if action == "files":
        if len(tokens) != 3:
            raise ValueError("Usage: /knowledge folder files <folder-id>")
        folder = store.knowledge_folder(tokens[2])
        if folder is None:
            raise ValueError(f"Knowledge Folder was not found: {tokens[2]}")
        entries = store.list_knowledge_folder_entries(folder.folder_id, limit=50)
        if not entries:
            return (f"Knowledge Folder files · {folder.folder_id} · none",)
        return (
            f"Knowledge Folder files · {folder.folder_id} · {len(entries)} shown",
            *(
                f"{item.entry_id} · {item.index_status.value.lower()} · "
                f"{_one_line(item.relative_path, 140)}"
                for item in entries
            ),
        )
    if action == "open":
        if len(tokens) != 3:
            raise ValueError("Usage: /knowledge folder open <entry-id>")
        result = service.open_entry(tokens[2], max_bytes=12_000)
        return (
            f"Knowledge Folder file · {result.entry.entry_id} · "
            f"{_one_line(result.entry.relative_path, 120)} · {result.selected_bytes} bytes",
            _one_line(result.content, 4_000),
            f"Evidence snapshot fixed locally · {result.snapshot_asset_id}",
        )
    raise ValueError(f"Unknown Knowledge Folder command: {action}")


def _intent_messages(store: KnowledgeStore, argument: str) -> tuple[str, ...]:
    if argument.strip().lower().startswith("create "):
        tokens = _command_tokens(argument, label="Intent create")
        goal = _words_before_options(tokens, start=1)
        if not goal:
            raise ValueError("Usage: /intent create <goal> [--query <query>] [--priority 0..100] [--active]")
        query = _option(tokens, "--query") or ""
        priority_raw = _option(tokens, "--priority")
        priority = int(priority_raw) if priority_raw is not None else 50
        if priority < 0 or priority > 100:
            raise ValueError("Intent priority must be between 0 and 100")
        allowed = {"create", "--query", "--priority", "--active"}
        unknown = [token for token in tokens if token.startswith("--") and token not in allowed]
        if unknown:
            raise ValueError(f"Unknown Intent option: {unknown[0]}")
        intent = store.create_intent(
            goal=goal,
            priority=priority,
            status=IntentStatus.ACTIVE if "--active" in tokens else IntentStatus.DRAFT,
            knowledge_query=query,
        )
        return (
            f"Intent {intent.status.value.lower()} · {intent.intent_id} · p{intent.priority}",
            "No Job was created. Activate an Intent intentionally, then inspect `/workbench ready <intent-id>`.",
        )
    if argument.strip().lower().startswith(("activate ", "pause ")):
        tokens = _command_tokens(argument, label="Intent lifecycle")
        if len(tokens) != 2:
            raise ValueError("Usage: /intent activate <intent-id> or /intent pause <intent-id>")
        status = IntentStatus.ACTIVE if tokens[0].lower() == "activate" else IntentStatus.PAUSED
        intent = store.set_intent_status(tokens[1], status)
        return (
            f"Intent {intent.status.value.lower()} · {intent.intent_id} · revision {intent.revision}",
            "Lifecycle update only · no Firm Job was created.",
        )
    reference = argument.removeprefix("show ").strip()
    if reference and reference.lower() not in {"active", "list"}:
        verified = store.verified_intent(reference)
        if verified is None:
            raise ValueError(f"Intent was not found: {reference}")
        intent, _ = verified
        messages = [
            (
                f"{intent.intent_id} · {intent.status.value.lower()} · "
                f"priority {intent.priority} · revision {intent.revision}"
            ),
            _one_line(intent.goal),
        ]
        if intent.knowledge_query:
            messages.append(f"Knowledge query · {_one_line(intent.knowledge_query)}")
        if intent.constraints:
            messages.append(
                "Constraints · "
                + " · ".join(_one_line(item, 80) for item in intent.constraints[:4])
            )
        return tuple(messages)

    intents = store.list_intents(status=IntentStatus.ACTIVE, limit=8)
    if not intents:
        return ("Active intents · none", "Create one with `noruct intent create GOAL`.")
    return (
        f"Active intents · {len(intents)} shown · local state",
        *(
            f"{item.intent_id} · p{item.priority} · r{item.revision} · "
            f"{_one_line(item.goal, 160)}"
            for item in intents
        ),
    )


def _decision_messages(store: KnowledgeStore, argument: str) -> tuple[str, ...]:
    normalized = argument.strip()
    if normalized.lower().startswith("record "):
        tokens = _command_tokens(normalized, label="Decision record")
        statement = _words_before_options(tokens, start=1)
        rationale = _option(tokens, "--rationale")
        if not statement or not rationale:
            raise ValueError("Usage: /decision record <statement> --rationale <reason> [--intent <intent-id>]")
        intent_id = _option(tokens, "--intent")
        allowed = {"record", "--rationale", "--intent"}
        unknown = [token for token in tokens if token.startswith("--") and token not in allowed]
        if unknown:
            raise ValueError(f"Unknown Decision option: {unknown[0]}")
        decision = store.create_decision(
            statement=statement,
            rationale=rationale,
            intent_id=intent_id,
        )
        return (
            f"Decision proposed · {decision.decision_id} · revision {decision.revision}",
            "No external commitment or Firm Job was created.",
        )
    if normalized.lower().startswith("review "):
        tokens = _command_tokens(normalized, label="Decision review")
        if len(tokens) != 2:
            raise ValueError("Usage: /decision review <decision-id>")
        question, request = store.propose_review_research(tokens[1])
        return (
            f"Review proposal · question={question.question_id} · research={request.request_id}",
            "Research remains DRAFT. Accept it explicitly before it becomes an ACTIVE Intent; no Job was created.",
        )
    if normalized.lower() == "due":
        decisions = store.due_decisions(limit=8)
        heading = "Decisions due for review"
    else:
        reference = normalized.removeprefix("show ").strip()
        if reference:
            verified = store.verified_decision(reference)
            if verified is None:
                raise ValueError(f"Decision was not found: {reference}")
            decision, _ = verified
            messages = [
                (
                    f"{decision.decision_id} · {decision.status.value.lower()} · "
                    f"revision {decision.revision}"
                ),
                _one_line(decision.statement),
                f"Rationale · {_one_line(decision.rationale)}",
            ]
            if decision.review_at:
                messages.append(f"Review at · {decision.review_at}")
            return tuple(messages)
        decisions = tuple(
            item for item in store.list_decisions(limit=16) if item.status.value != "SUPERSEDED"
        )[:8]
        heading = "Current decisions"

    if not decisions:
        return (f"{heading} · none",)
    return (
        f"{heading} · {len(decisions)} shown · local state",
        *(
            f"{item.decision_id} · {item.status.value.lower()} · "
            f"{_one_line(item.statement, 160)}"
            + (f" · review {item.review_at}" if item.review_at else "")
            for item in decisions
        ),
    )


def _question_messages(store: KnowledgeStore, argument: str) -> tuple[str, ...]:
    reference = argument.removeprefix("show ").strip()
    if reference and reference.lower() not in {"open", "list"}:
        verified = store.verified_question(reference)
        if verified is None:
            raise ValueError(f"Question was not found: {reference}")
        question, _ = verified
        return (
            f"{question.question_id} · {question.status.value.lower()} · revision {question.revision}",
            _one_line(question.prompt),
            *( ("Answer criteria · " + " · ".join(_one_line(item, 80) for item in question.answer_criteria[:3]),) if question.answer_criteria else () ),
        )
    questions = store.list_questions(status=QuestionStatus.OPEN, limit=8)
    if not questions:
        return ("Open questions · none", "Create one with `noruct question create PROMPT`.")
    return ("Open questions · local state", *(f"{item.question_id} · {_one_line(item.prompt, 160)}" for item in questions))


def _research_messages(store: KnowledgeStore, argument: str) -> tuple[str, ...]:
    reference = argument.removeprefix("show ").strip()
    if reference and reference.lower() not in {"draft", "list"}:
        request = store.research_request(reference)
        if request is None:
            raise ValueError(f"Research Request was not found: {reference}")
        return (
            f"{request.request_id} · {request.status.value.lower()} · revision {request.revision}",
            _one_line(request.title),
            (f"Compiled Intent · {request.compiled_intent_id}" if request.compiled_intent_id else "No compiled Intent · no Job started"),
        )
    requests = store.list_research_requests(status=ResearchRequestStatus.DRAFT, limit=8)
    if not requests:
        return ("Draft research requests · none",)
    return ("Draft research requests · no Job starts until explicit acceptance", *(f"{item.request_id} · {_one_line(item.title, 160)}" for item in requests))


def _workbench_messages(store: KnowledgeStore, argument: str) -> tuple[str, ...]:
    """Render a content-free relation map for both terminal surfaces."""

    normalized = argument.strip()
    lowered = normalized.lower()
    if lowered in {"review", "review queue"}:
        review = build_knowledge_review_surface(store, limit=16)
        messages = [
            "Knowledge review queue · local, read-only · explicit candidate resolution required",
            f"synthesis leads={len(review.syntheses)} · lexical leads={len(review.lexical_near_duplicates)} · page drift/conflict items={len(review.page_issues)} · complete={'no' if review.truncated else 'yes'}",
        ]
        messages.extend(
            f"SYNTHESIS {item.fingerprint} · kind={item.kind} · jobs={len(item.job_ids)} · candidates={','.join(item.candidate_ids)}"
            for item in review.syntheses
        )
        messages.extend(
            f"LEXICAL {item.fingerprint} · kind={item.kind} · jobs={len(item.job_ids)} · candidates={','.join(item.candidate_ids)} · similarity={item.similarity_basis_points / 100:.2f}%"
            for item in review.lexical_near_duplicates
        )
        messages.extend(
            f"PAGE {item.folder_id} · {item.code} · {item.relative_path or 'folder root'}"
            + (f" · {item.reference}" if item.reference else "")
            for item in review.page_issues
        )
        if not review.syntheses and not review.lexical_near_duplicates and not review.page_issues:
            messages.append("No repeat synthesis lead or stale/conflicting/drifted published page is awaiting review.")
        messages.append("No candidate, page, Folder, Intent, Decision, Company state, provider call, or Job changed.")
        return tuple(messages)
    if lowered in {"candidates", "candidate list"}:
        candidates = store.list_write_candidates(status="PENDING", limit=8)
        if not candidates:
            return (
                "Pending Knowledge candidates · none",
                "Firm results never enter Knowledge automatically.",
            )
        return (
            f"Pending Knowledge candidates · {len(candidates)} shown · explicit review required",
            *(
                f"{item.candidate_id} · job={item.job_id} · kind={item.kind} · "
                f"evidence={item.evidence_pack_id or 'none'}"
                for item in candidates
            ),
            "Use `/workbench candidate <candidate-id>` to inspect, then `/workbench accept|reject <candidate-id>`.",
        )
    if lowered.startswith("candidate "):
        tokens = _command_tokens(normalized, label="Workbench candidate")
        if len(tokens) != 2:
            raise ValueError("Usage: /workbench candidate <candidate-id>")
        candidate = store.write_candidate(tokens[1])
        if candidate is None:
            raise ValueError(f"Knowledge write candidate was not found: {tokens[1]}")
        return (
            f"Knowledge candidate · {candidate.candidate_id} · {candidate.status.lower()} · job={candidate.job_id}",
            _one_line(candidate.statement, 4_000),
            f"Evidence Pack · {candidate.evidence_pack_id or 'none'} · accepted record={candidate.accepted_record_id or 'none'}",
            (
                "This is a proposed Knowledge record, not an accepted fact. "
                "Use `/workbench accept <candidate-id>` or `/workbench reject <candidate-id>`."
                if candidate.status == "PENDING"
                else "Resolved candidates are immutable; accepting one never changes an Intent or Decision."
            ),
        )
    if lowered.startswith(("accept ", "reject ")):
        tokens = _command_tokens(normalized, label="Workbench candidate resolution")
        if len(tokens) != 2:
            raise ValueError("Usage: /workbench accept <candidate-id> or /workbench reject <candidate-id>")
        accepted = tokens[0].lower() == "accept"
        candidate = store.resolve_write_candidate(tokens[1], accept=accepted)
        return (
            f"Knowledge candidate {candidate.status.lower()} · {candidate.candidate_id} · record={candidate.accepted_record_id or 'none'}",
            (
                "Accepted as a local Knowledge record with its candidate provenance. "
                "No Intent, Decision, Company Patch, provider call, or Job changed."
                if accepted
                else "Rejected without creating a Knowledge record. No Intent, Decision, Company Patch, provider call, or Job changed."
            ),
        )
    if lowered.startswith("ready "):
        tokens = _command_tokens(argument, label="Workbench readiness")
        if len(tokens) != 2:
            raise ValueError("Usage: /workbench ready <intent-id>")
        verified = store.verified_intent(tokens[1])
        if verified is None:
            raise ValueError(f"Intent was not found: {tokens[1]}")
        intent, _ = verified
        if intent.status is not IntentStatus.ACTIVE:
            raise ValueError("Only an ACTIVE Intent can be prepared for Firm execution")
        service = UserKnowledgeService(store, KnowledgeVault(knowledge_vault_path(store.path)))
        pack = service.build_evidence_pack(
            intent.knowledge_query.strip() or intent.goal,
            limit=6,
            max_bytes=16_000,
            persist=False,
        )
        return (
            f"Execution readiness · {intent.intent_id} · citations={len(pack.items)} · bytes={pack.selected_bytes}",
            f"Evidence query · {_one_line(pack.query, 160)}",
            "No binding, provider call, or Job was created. To execute, use the explicit `noruct intent run <intent-id>` path.",
        )
    reference = normalized.removeprefix("show ").strip() or None
    view = build_knowledge_workbench(store, intent_id=reference, limit=8)
    messages = [
        "Knowledge Workbench · local relation map · no provider or Job created",
        f"open questions={view.open_questions} · draft research={view.draft_research_requests} · decisions due={len(view.due_decisions)} · pending candidates={view.pending_candidates}",
    ]
    if not view.intents:
        return tuple((*messages, "No Intent yet. Create an Intent before asking the Company to act."))
    for row in view.intents:
        intent = row.intent
        messages.append(
            f"INTENT {intent.intent_id} · {intent.status.value.lower()} · p{intent.priority} · r{intent.revision} · {_one_line(intent.goal, 140)}"
        )
        if intent.knowledge_query:
            messages.append(f"  evidence query · {_one_line(intent.knowledge_query, 120)}")
        if row.bindings:
            for binding in row.bindings:
                messages.append(
                    f"  JOB {binding.job_id} · {binding.status.lower()}/{binding.job_status.lower() or 'pending'} · "
                    f"pack={binding.pack_id}@r{binding.pack_revision} · citations={binding.item_count} · candidate={binding.candidate_id or 'none'}"
                )
        else:
            messages.append("  JOB · none (an Intent never starts a Job without explicit run)")
        for candidate in row.candidates:
            messages.append(
                f"  CANDIDATE {candidate.candidate_id} · {candidate.status.lower()} · "
                f"job={candidate.job_id} · evidence={candidate.evidence_pack_id or 'none'}"
            )
        for decision in row.decisions:
            messages.append(
                f"  DECISION {decision.decision_id} · {decision.status.value.lower()} · "
                f"evidence={decision.evidence_pack_id or 'none'} · {_one_line(decision.statement, 120)}"
            )
    if view.due_decisions:
        messages.append("DUE · " + " · ".join(item.decision_id for item in view.due_decisions[:4]))
    if view.pending_candidates:
        messages.append("CANDIDATES · `/workbench candidates` lists pending results; only explicit accept writes Knowledge.")
    return tuple(messages)


def execute_local_knowledge_command(
    runtime_state_path: str | Path,
    command: str,
    argument: str = "",
) -> tuple[str, ...]:
    """Execute one local slash command without Company/model side effects."""

    if command not in {"/remember", "/knowledge", "/intent", "/decision", "/question", "/research", "/workbench"}:
        raise ValueError(f"Unsupported local Knowledge command: {command}")
    normalized = _bounded_argument(
        argument,
        label=("Memory text" if command == "/remember" else "Command argument"),
        required=command == "/remember",
    )
    state_path = knowledge_state_path(runtime_state_path)
    folder_tokens: list[str] = []
    if command == "/knowledge" and normalized:
        try:
            candidate_tokens = shlex.split(normalized)
        except ValueError as exc:
            raise ValueError(f"Knowledge command could not be parsed: {exc}") from exc
        if candidate_tokens and candidate_tokens[0].lower() == "folder":
            folder_tokens = candidate_tokens
    creates_local_state = (
        command == "/remember"
        or (command == "/intent" and normalized.lower().startswith("create "))
        or (command == "/decision" and normalized.lower().startswith("record "))
    )
    if not creates_local_state and not state_path.is_file():
        if command == "/knowledge":
            if folder_tokens:
                action = folder_tokens[1].lower() if len(folder_tokens) > 1 else "help"
                if action in {"help", "?"}:
                    return _folder_help_messages()
                if action == "list":
                    return ("Knowledge Folders · none",)
                if action != "add":
                    raise ValueError("No local Knowledge DB exists; register a folder first")
                # Registration is an explicit write and may create local Knowledge state.
                with KnowledgeStore(state_path) as store:
                    messages = _knowledge_folder_command(store, state_path, normalized)
                    assert messages is not None
                    return messages
            else:
                if normalized:
                    return (
                        "Knowledge view · 0 match(es) from 0 candidate(s) · 0 bytes · no Job created",
                        "No local Knowledge DB yet. Nothing was sent to a model or employee session.",
                    )
                return (
                    "Knowledge Runtime · local, user-owned, provider-free",
                    "0 asset(s) · 0 record(s) · 0 active intent(s) · 0 decision review(s) due · 0 open question(s) · 0 draft research request(s)",
                    "Use /remember <text> or `noruct knowledge add PATH` to create local state.",
                )
        reference = normalized.removeprefix("show ").strip()
        reference_keyword = reference.lower()
        if command == "/intent" and reference_keyword not in {"", "active", "list"}:
            raise ValueError(f"Intent was not found: {reference}")
        if command == "/decision" and reference_keyword not in {"", "due"}:
            raise ValueError(f"Decision was not found: {reference}")
        if command == "/question" and reference_keyword not in {"", "open", "list"}:
            raise ValueError(f"Question was not found: {reference}")
        if command == "/research" and reference_keyword not in {"", "draft", "list"}:
            raise ValueError(f"Research Request was not found: {reference}")
        if command == "/intent":
            return ("Active intents · none",)
        if command == "/question":
            return ("Open questions · none",)
        if command == "/research":
            return ("Draft research requests · none",)
        if command == "/workbench":
            return (
                "Knowledge Workbench · local relation map · no provider or Job created",
                "No Intent yet. Create an Intent before asking the Company to act.",
            )
        return (
            "Decisions due for review · none"
            if normalized.lower() == "due"
            else "Current decisions · none",
        )
    with KnowledgeStore(state_path) as store:
        if command == "/remember":
            record = store.create_record(
                kind="NOTE",
                statement=normalized,
                access_scope="private",
            )
            return (
                f"Remembered locally · {record.record_id} · private · no Company Job created",
            )
        if command == "/knowledge":
            folder_messages = _knowledge_folder_command(store, state_path, normalized)
            if folder_messages is not None:
                return folder_messages
            return (
                _knowledge_query(store, state_path, normalized)
                if normalized
                else _knowledge_summary(store)
            )
        if command == "/intent":
            return _intent_messages(store, normalized)
        if command == "/decision":
            return _decision_messages(store, normalized)
        if command == "/question":
            return _question_messages(store, normalized)
        if command == "/research":
            return _research_messages(store, normalized)
        return _workbench_messages(store, normalized)


__all__ = ["execute_local_knowledge_command"]
