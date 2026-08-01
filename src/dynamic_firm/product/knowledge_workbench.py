"""Read-only cross-plane projection for terminal and future GUI workbenches.

The workbench joins only immutable identifiers and status metadata. It never
loads Evidence excerpts, Job transcripts, employee memory, or secret values.
"""

from __future__ import annotations

from dataclasses import dataclass

from dynamic_firm.knowledge.models import (
    DecisionRecord,
    IntentRecord,
    KnowledgeExecutionBinding,
    QuestionStatus,
    ResearchRequestStatus,
)
from dynamic_firm.knowledge.store import KnowledgeStore


@dataclass(frozen=True, slots=True)
class WorkbenchIntent:
    intent: IntentRecord
    decisions: tuple[DecisionRecord, ...]
    bindings: tuple[KnowledgeExecutionBinding, ...]
    candidates: tuple["WorkbenchCandidate", ...]


@dataclass(frozen=True, slots=True)
class WorkbenchCandidate:
    """Content-free candidate metadata safe for the cross-plane map.

    A candidate statement is user-owned result content.  It stays out of the
    always-visible relation projection and is read only by the explicit
    ``/workbench candidate <id>`` user action.
    """

    candidate_id: str
    job_id: str
    kind: str
    evidence_pack_id: str | None
    status: str
    accepted_record_id: str | None


@dataclass(frozen=True, slots=True)
class KnowledgeWorkbench:
    """Small, content-free relation map for user-owned Knowledge control."""

    intents: tuple[WorkbenchIntent, ...]
    due_decisions: tuple[DecisionRecord, ...]
    open_questions: int
    draft_research_requests: int
    pending_candidates: int


def build_knowledge_workbench(
    store: KnowledgeStore, *, intent_id: str | None = None, limit: int = 8
) -> KnowledgeWorkbench:
    if not 1 <= limit <= 32:
        raise ValueError("Knowledge Workbench limit must be between 1 and 32")
    if intent_id:
        verified = store.verified_intent(intent_id)
        if verified is None:
            raise ValueError(f"Intent was not found: {intent_id}")
        intents = (verified[0],)
    else:
        intents = store.list_intents(limit=limit)
    decisions = store.list_decisions(limit=100)
    bindings = store.list_execution_bindings(limit=100)
    candidates = store.list_write_candidates(limit=100)
    rows = tuple(
        WorkbenchIntent(
            intent=intent,
            decisions=tuple(item for item in decisions if item.intent_id == intent.intent_id)[:limit],
            bindings=tuple(item for item in bindings if item.intent_id == intent.intent_id)[:limit],
            candidates=tuple(
                WorkbenchCandidate(
                    candidate_id=item.candidate_id,
                    job_id=item.job_id,
                    kind=item.kind,
                    evidence_pack_id=item.evidence_pack_id,
                    status=item.status,
                    accepted_record_id=item.accepted_record_id,
                )
                for item in candidates
                if item.job_id
                in {
                    binding.job_id
                    for binding in bindings
                    if binding.intent_id == intent.intent_id
                }
            )[:limit],
        )
        for intent in intents
    )
    return KnowledgeWorkbench(
        intents=rows,
        due_decisions=store.due_decisions(limit=limit),
        open_questions=len(store.list_questions(status=QuestionStatus.OPEN, limit=100)),
        draft_research_requests=len(
            store.list_research_requests(status=ResearchRequestStatus.DRAFT, limit=100)
        ),
        pending_candidates=sum(item.status == "PENDING" for item in candidates),
    )


__all__ = [
    "KnowledgeWorkbench",
    "WorkbenchCandidate",
    "WorkbenchIntent",
    "build_knowledge_workbench",
]
