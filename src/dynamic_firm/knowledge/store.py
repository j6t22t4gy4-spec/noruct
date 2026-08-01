from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .models import (
    AssetStatus,
    DecisionRecord,
    DecisionStatus,
    DerivedRepresentation,
    EvidenceItem,
    EvidencePack,
    IntentRecord,
    IntentStatus,
    KnowledgeAsset,
    KnowledgeExecutionBinding,
    KnowledgeRecord,
    KnowledgeWriteCandidate,
    QuestionRecord,
    QuestionStatus,
    ResearchRequest,
    ResearchRequestStatus,
)
from .epistemic import ContentTrustClass, EpistemicStatus
from .epistemic_store import EpistemicStoreMixin
from .locking import KnowledgeStateLock
from .delivery import runtime_delivery_from_evidence_pack
from .folder_models import KnowledgeFolderEntryStatus
from .folder_store import FolderKnowledgeStoreMixin
from .store_asset_deletion import KnowledgeAssetDeletionMixin
from .store_assets import KnowledgeAssetMutationMixin
from .store_read import KnowledgeAssetReadProjectionMixin
from .store_records import KnowledgeRecordProjectionMixin
from .store_write_candidates import KnowledgeWriteCandidateMixin
from .store_pages import KnowledgePagePublicationMixin
from .store_core_lifecycle import KnowledgeCoreLifecycleMixin
from .store_retrieval import KnowledgeRetrievalMixin
from .store_evidence_execution import KnowledgeEvidenceExecutionMixin
from .store_intent_decision import KnowledgeIntentDecisionMixin
from .store_primitives import (
    _bounded_mapping,
    _bounded_text,
    _json,
    _loads,
    _normalized_scope,
    _normalized_timestamp,
    _now,
    _truncated_text,
)
from dynamic_firm.korean_lexical import korean_retrieval_variants


SCHEMA_VERSION = 8


def knowledge_state_path(runtime_state_path: str | Path) -> Path:
    target = Path(runtime_state_path).expanduser().resolve()
    return target.with_name(f"{target.stem}.knowledge.db")


def knowledge_vault_path(state_path: str | Path) -> Path:
    target = Path(state_path).expanduser().resolve()
    return target.with_name(f"{target.stem}.vault")


def knowledge_runtime_paths(runtime_state_path: str | Path) -> tuple[Path, Path]:
    """Return the canonical sibling database and vault for one runtime state.

    ``knowledge_vault_path`` accepts a Knowledge database path.  Keeping this
    composition in one helper prevents employee tools and operator commands
    from accidentally deriving two different vault locations.
    """

    database = knowledge_state_path(runtime_state_path)
    return database, knowledge_vault_path(database)


class KnowledgeStore(
    KnowledgeCoreLifecycleMixin,
    KnowledgeRetrievalMixin,
    KnowledgeEvidenceExecutionMixin,
    KnowledgeIntentDecisionMixin,
    KnowledgeAssetDeletionMixin,
    KnowledgeAssetMutationMixin,
    KnowledgeAssetReadProjectionMixin,
    KnowledgeRecordProjectionMixin,
    KnowledgeWriteCandidateMixin,
    KnowledgePagePublicationMixin,
    FolderKnowledgeStoreMixin,
    EpistemicStoreMixin,
):
    """Canonical user-owned Knowledge DB; never a Company or employee store."""

    @staticmethod
    def _question(row: sqlite3.Row) -> QuestionRecord:
        return QuestionRecord(
            question_id=str(row["question_id"]),
            prompt=str(row["prompt"]),
            owner=str(row["owner"]),
            status=QuestionStatus(str(row["status"])),
            intent_id=(str(row["intent_id"]) if row["intent_id"] else None),
            decision_id=(str(row["decision_id"]) if row["decision_id"] else None),
            evidence_pack_id=(str(row["evidence_pack_id"]) if row["evidence_pack_id"] else None),
            answer_criteria=tuple(str(item) for item in _loads(row["answer_criteria_json"], [])),
            knowledge_query=str(row["knowledge_query"]),
            review_at=(str(row["review_at"]) if row["review_at"] else None),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _question_revision_payload(row: sqlite3.Row) -> dict[str, object]:
        return {
            "question_id": str(row["question_id"]), "prompt": str(row["prompt"]),
            "owner": str(row["owner"]), "status": str(row["status"]),
            "intent_id": row["intent_id"], "decision_id": row["decision_id"],
            "evidence_pack_id": row["evidence_pack_id"],
            "answer_criteria": _loads(row["answer_criteria_json"], []),
            "knowledge_query": str(row["knowledge_query"]), "review_at": row["review_at"],
            "revision": int(row["revision"]), "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @classmethod
    def _append_question_revision(cls, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        payload = cls._question_revision_payload(row)
        encoded = _json(payload)
        conn.execute(
            "INSERT INTO knowledge_question_revisions(question_id, revision, payload_json, content_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (row["question_id"], row["revision"], encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), _now()),
        )

    @classmethod
    def _verified_question_row(cls, conn: sqlite3.Connection, question_id: str) -> tuple[sqlite3.Row, str]:
        row = conn.execute("SELECT * FROM knowledge_questions WHERE question_id = ?", (question_id,)).fetchone()
        if row is None:
            raise ValueError(f"Question was not found: {question_id}")
        history = conn.execute(
            "SELECT payload_json, content_hash FROM knowledge_question_revisions WHERE question_id = ? AND revision = ?",
            (question_id, row["revision"]),
        ).fetchone()
        if history is None:
            raise ValueError("Current Question has no matching immutable revision history")
        payload = _loads(history["payload_json"], {})
        encoded = _json(payload) if isinstance(payload, dict) else ""
        observed = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if not isinstance(payload, dict) or observed != str(history["content_hash"]) or payload != cls._question_revision_payload(row):
            raise ValueError("Current Question does not match its immutable revision history")
        return row, observed

    def create_question(
        self, *, prompt: str, owner: str = "user:local", status: QuestionStatus = QuestionStatus.OPEN,
        intent_id: str | None = None, decision_id: str | None = None, evidence_pack_id: str | None = None,
        answer_criteria: Sequence[str] = (), knowledge_query: str = "", review_at: str | None = None,
    ) -> QuestionRecord:
        normalized_prompt = _bounded_text(prompt, "Question prompt", 32_000)
        normalized_owner = _bounded_text(owner or "user:local", "Question owner", 1_024)
        normalized_criteria = tuple(_bounded_text(item, "Question answer criterion", 8_000) for item in answer_criteria)
        if len(normalized_criteria) > 100:
            raise ValueError("Question answer criteria are limited to 100 items")
        normalized_query = _bounded_text(knowledge_query, "Question knowledge query", 32_000, required=False)
        normalized_review_at = _normalized_timestamp(review_at)
        question_id, now = f"question-{uuid.uuid4()}", _now()
        with self._transaction() as conn:
            if intent_id:
                self._verified_intent_row(conn, intent_id)
            if decision_id:
                self._verified_decision_row(conn, decision_id)
            if evidence_pack_id and conn.execute("SELECT 1 FROM evidence_packs WHERE pack_id = ?", (evidence_pack_id,)).fetchone() is None:
                raise ValueError("Question Evidence Pack was not found")
            conn.execute(
                """INSERT INTO knowledge_questions(question_id, prompt, owner, status, intent_id, decision_id, evidence_pack_id, answer_criteria_json, knowledge_query, review_at, revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (question_id, normalized_prompt, normalized_owner, status.value, intent_id, decision_id, evidence_pack_id, _json(list(normalized_criteria)), normalized_query, normalized_review_at, now, now),
            )
            row = conn.execute("SELECT * FROM knowledge_questions WHERE question_id = ?", (question_id,)).fetchone()
            assert row is not None
            self._append_question_revision(conn, row)
            self._event(conn, "QUESTION_CREATED", "question", question_id, {"status": status.value})
        value = self.question(question_id)
        assert value is not None
        return value

    def question(self, question_id: str) -> QuestionRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM knowledge_questions WHERE question_id = ?", (question_id,)).fetchone()
        return None if row is None else self._question(row)

    def verified_question(self, question_id: str) -> tuple[QuestionRecord, str] | None:
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM knowledge_questions WHERE question_id = ?", (question_id,)).fetchone()
            if exists is None:
                return None
            row, digest = self._verified_question_row(self._conn, question_id)
        return self._question(row), digest

    def list_questions(self, *, status: QuestionStatus | None = None, limit: int = 100) -> tuple[QuestionRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Question list limit must be between 1 and 1000")
        query, parameters = "SELECT question_id FROM knowledge_questions", []
        if status is not None:
            query += " WHERE status = ?"; parameters.append(status.value)
        query += " ORDER BY updated_at DESC, question_id LIMIT ?"; parameters.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(parameters)).fetchall()
            return tuple(self._question(self._verified_question_row(self._conn, str(row[0]))[0]) for row in rows)

    def question_history(self, question_id: str) -> tuple[dict[str, object], ...]:
        with self._lock:
            rows = self._conn.execute("SELECT payload_json, content_hash FROM knowledge_question_revisions WHERE question_id = ? ORDER BY revision", (question_id,)).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            payload = _loads(row["payload_json"], {})
            if not isinstance(payload, dict): raise ValueError("Question revision payload is malformed")
            digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
            if digest != str(row["content_hash"]): raise ValueError("Question revision payload hash is invalid")
            result.append({**payload, "content_hash": digest})
        return tuple(result)

    def set_question_status(self, question_id: str, status: QuestionStatus) -> QuestionRecord:
        now = _now()
        with self._transaction() as conn:
            self._verified_question_row(conn, question_id)
            conn.execute("UPDATE knowledge_questions SET status = ?, revision = revision + 1, updated_at = ? WHERE question_id = ?", (status.value, now, question_id))
            row = conn.execute("SELECT * FROM knowledge_questions WHERE question_id = ?", (question_id,)).fetchone(); assert row is not None
            self._append_question_revision(conn, row)
            self._event(conn, "QUESTION_STATUS_CHANGED", "question", question_id, {"status": status.value})
        value = self.question(question_id); assert value is not None; return value

    @staticmethod
    def _research_request(row: sqlite3.Row) -> ResearchRequest:
        return ResearchRequest(
            request_id=str(row["request_id"]), title=str(row["title"]), objective=str(row["objective"]), owner=str(row["owner"]), status=ResearchRequestStatus(str(row["status"])),
            question_id=(str(row["question_id"]) if row["question_id"] else None), intent_id=(str(row["intent_id"]) if row["intent_id"] else None), decision_id=(str(row["decision_id"]) if row["decision_id"] else None), decision_revision=(int(row["decision_revision"]) if row["decision_revision"] is not None else None), evidence_pack_id=(str(row["evidence_pack_id"]) if row["evidence_pack_id"] else None), knowledge_query=str(row["knowledge_query"]), required_evidence=tuple(str(item) for item in _loads(row["required_evidence_json"], [])), freshness_at=(str(row["freshness_at"]) if row["freshness_at"] else None), counterargument_required=bool(row["counterargument_required"]), max_cost_units=int(row["max_cost_units"]), max_duration_minutes=int(row["max_duration_minutes"]), compiled_intent_id=(str(row["compiled_intent_id"]) if row["compiled_intent_id"] else None), revision=int(row["revision"]), created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _research_revision_payload(row: sqlite3.Row) -> dict[str, object]:
        return {"request_id": str(row["request_id"]), "title": str(row["title"]), "objective": str(row["objective"]), "owner": str(row["owner"]), "status": str(row["status"]), "question_id": row["question_id"], "intent_id": row["intent_id"], "decision_id": row["decision_id"], "decision_revision": row["decision_revision"], "evidence_pack_id": row["evidence_pack_id"], "knowledge_query": str(row["knowledge_query"]), "required_evidence": _loads(row["required_evidence_json"], []), "freshness_at": row["freshness_at"], "counterargument_required": bool(row["counterargument_required"]), "max_cost_units": int(row["max_cost_units"]), "max_duration_minutes": int(row["max_duration_minutes"]), "compiled_intent_id": row["compiled_intent_id"], "revision": int(row["revision"]), "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"])}

    @classmethod
    def _append_research_revision(cls, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        payload = cls._research_revision_payload(row); encoded = _json(payload)
        conn.execute("INSERT INTO knowledge_research_request_revisions(request_id, revision, payload_json, content_hash, created_at) VALUES (?, ?, ?, ?, ?)", (row["request_id"], row["revision"], encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), _now()))

    @classmethod
    def _verified_research_row(cls, conn: sqlite3.Connection, request_id: str) -> tuple[sqlite3.Row, str]:
        row = conn.execute("SELECT * FROM knowledge_research_requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None: raise ValueError(f"Research Request was not found: {request_id}")
        history = conn.execute("SELECT payload_json, content_hash FROM knowledge_research_request_revisions WHERE request_id = ? AND revision = ?", (request_id, row["revision"])).fetchone()
        if history is None: raise ValueError("Current Research Request has no matching immutable revision history")
        payload = _loads(history["payload_json"], {}); encoded = _json(payload) if isinstance(payload, dict) else ""; digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if not isinstance(payload, dict) or digest != str(history["content_hash"]) or payload != cls._research_revision_payload(row): raise ValueError("Current Research Request does not match its immutable revision history")
        return row, digest

    def create_research_request(
        self, *, title: str, objective: str, owner: str = "user:local", question_id: str | None = None,
        intent_id: str | None = None, decision_id: str | None = None, evidence_pack_id: str | None = None,
        knowledge_query: str = "", required_evidence: Sequence[str] = (), freshness_at: str | None = None,
        counterargument_required: bool = False, max_cost_units: int = 0, max_duration_minutes: int = 60,
        decision_revision: int | None = None,
    ) -> ResearchRequest:
        normalized_title = _bounded_text(title, "Research Request title", 8_000); normalized_objective = _bounded_text(objective, "Research Request objective", 32_000); normalized_owner = _bounded_text(owner or "user:local", "Research Request owner", 1_024); normalized_query = _bounded_text(knowledge_query, "Research Request knowledge query", 32_000, required=False); normalized_evidence = tuple(_bounded_text(item, "Research required evidence", 8_000) for item in required_evidence)
        if len(normalized_evidence) > 100: raise ValueError("Research required evidence is limited to 100 items")
        if max_cost_units < 0 or max_cost_units > 1_000_000: raise ValueError("Research cost limit is invalid")
        if max_duration_minutes < 1 or max_duration_minutes > 10_080: raise ValueError("Research duration limit is invalid")
        normalized_freshness = _normalized_timestamp(freshness_at); request_id, now = f"research-{uuid.uuid4()}", _now()
        with self._transaction() as conn:
            if question_id: self._verified_question_row(conn, question_id)
            if intent_id: self._verified_intent_row(conn, intent_id)
            if decision_id:
                decision_row, _ = self._verified_decision_row(conn, decision_id)
                decision_revision = int(decision_row["revision"]) if decision_revision is None else decision_revision
            if evidence_pack_id and conn.execute("SELECT 1 FROM evidence_packs WHERE pack_id = ?", (evidence_pack_id,)).fetchone() is None: raise ValueError("Research Request Evidence Pack was not found")
            conn.execute("""INSERT INTO knowledge_research_requests(request_id, title, objective, owner, status, question_id, intent_id, decision_id, decision_revision, evidence_pack_id, knowledge_query, required_evidence_json, freshness_at, counterargument_required, max_cost_units, max_duration_minutes, compiled_intent_id, revision, created_at, updated_at)
                         VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)""", (request_id, normalized_title, normalized_objective, normalized_owner, question_id, intent_id, decision_id, decision_revision, evidence_pack_id, normalized_query, _json(list(normalized_evidence)), normalized_freshness, int(counterargument_required), max_cost_units, max_duration_minutes, now, now))
            row = conn.execute("SELECT * FROM knowledge_research_requests WHERE request_id = ?", (request_id,)).fetchone(); assert row is not None
            self._append_research_revision(conn, row); self._event(conn, "RESEARCH_REQUEST_CREATED", "research_request", request_id, {"status": "DRAFT"})
        value = self.research_request(request_id); assert value is not None; return value

    def research_request(self, request_id: str) -> ResearchRequest | None:
        with self._lock: row = self._conn.execute("SELECT * FROM knowledge_research_requests WHERE request_id = ?", (request_id,)).fetchone()
        return None if row is None else self._research_request(row)

    def list_research_requests(self, *, status: ResearchRequestStatus | None = None, limit: int = 100) -> tuple[ResearchRequest, ...]:
        if limit < 1 or limit > 1000: raise ValueError("Research Request list limit must be between 1 and 1000")
        query, parameters = "SELECT request_id FROM knowledge_research_requests", []
        if status is not None: query += " WHERE status = ?"; parameters.append(status.value)
        query += " ORDER BY updated_at DESC, request_id LIMIT ?"; parameters.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(parameters)).fetchall()
            return tuple(self._research_request(self._verified_research_row(self._conn, str(row[0]))[0]) for row in rows)

    def research_history(self, request_id: str) -> tuple[dict[str, object], ...]:
        with self._lock: rows = self._conn.execute("SELECT payload_json, content_hash FROM knowledge_research_request_revisions WHERE request_id = ? ORDER BY revision", (request_id,)).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            payload = _loads(row["payload_json"], {})
            if not isinstance(payload, dict): raise ValueError("Research Request revision payload is malformed")
            digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
            if digest != str(row["content_hash"]): raise ValueError("Research Request revision payload hash is invalid")
            result.append({**payload, "content_hash": digest})
        return tuple(result)

    def set_research_status(self, request_id: str, status: ResearchRequestStatus) -> ResearchRequest:
        if status == ResearchRequestStatus.ACCEPTED: raise ValueError("Accept a Research Request with accept_research_request")
        now = _now()
        with self._transaction() as conn:
            row, _ = self._verified_research_row(conn, request_id)
            if str(row["status"]) == ResearchRequestStatus.ACCEPTED.value and status in {ResearchRequestStatus.REJECTED, ResearchRequestStatus.CANCELLED}: raise ValueError("An accepted Research Request cannot be rejected or cancelled")
            conn.execute("UPDATE knowledge_research_requests SET status = ?, revision = revision + 1, updated_at = ? WHERE request_id = ?", (status.value, now, request_id))
            updated = conn.execute("SELECT * FROM knowledge_research_requests WHERE request_id = ?", (request_id,)).fetchone(); assert updated is not None
            self._append_research_revision(conn, updated); self._event(conn, "RESEARCH_REQUEST_STATUS_CHANGED", "research_request", request_id, {"status": status.value})
        value = self.research_request(request_id); assert value is not None; return value

    def accept_research_request(self, request_id: str, *, priority: int = 50) -> tuple[ResearchRequest, IntentRecord]:
        if priority < 0 or priority > 100: raise ValueError("Intent priority must be between 0 and 100")
        now = _now()
        with self._transaction() as conn:
            row, _ = self._verified_research_row(conn, request_id)
            if str(row["status"]) == ResearchRequestStatus.ACCEPTED.value:
                intent_id = row["compiled_intent_id"]
                if not intent_id: raise ValueError("Accepted Research Request has no compiled Intent")
                intent_row, _ = self._verified_intent_row(conn, str(intent_id))
                return self._research_request(row), self._intent(intent_row)
            if str(row["status"]) != ResearchRequestStatus.DRAFT.value: raise ValueError("Only a DRAFT Research Request can be accepted")
            constraints = [f"Research cost ceiling: {int(row['max_cost_units'])} local units.", f"Research duration ceiling: {int(row['max_duration_minutes'])} minutes.", *[str(value) for value in _loads(row['required_evidence_json'], [])]]
            if bool(row["counterargument_required"]): constraints.append("Include at least one evidence-backed counterargument or explicitly report none found.")
            intent_id = f"intent-{uuid.uuid4()}"; query = str(row["knowledge_query"]).strip() or str(row["objective"])
            conn.execute("INSERT INTO knowledge_intents(intent_id, goal, priority, status, constraints_json, acceptance_criteria_json, knowledge_query, revision, created_at, updated_at) VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, 1, ?, ?)", (intent_id, str(row["objective"]), priority, _json(constraints), _json(["Return cited findings and unresolved uncertainty."]), query, now, now))
            intent_row = conn.execute("SELECT * FROM knowledge_intents WHERE intent_id = ?", (intent_id,)).fetchone(); assert intent_row is not None
            self._append_intent_revision(conn, intent_row)
            conn.execute("UPDATE knowledge_research_requests SET status = 'ACCEPTED', compiled_intent_id = ?, revision = revision + 1, updated_at = ? WHERE request_id = ?", (intent_id, now, request_id))
            updated = conn.execute("SELECT * FROM knowledge_research_requests WHERE request_id = ?", (request_id,)).fetchone(); assert updated is not None
            self._append_research_revision(conn, updated); self._event(conn, "RESEARCH_REQUEST_ACCEPTED", "research_request", request_id, {"intent_id": intent_id})
        request = self.research_request(request_id); assert request is not None
        intent = self.intent(request.compiled_intent_id or ""); assert intent is not None
        return request, intent

    def propose_review_research(self, decision_id: str, *, owner: str = "user:local") -> tuple[QuestionRecord, ResearchRequest]:
        with self._transaction() as conn:
            decision, _ = self._verified_decision_row(conn, decision_id)
            existing = conn.execute("SELECT * FROM knowledge_research_requests WHERE decision_id = ? AND decision_revision = ?", (decision_id, decision["revision"])).fetchone()
            if existing is not None:
                request = self._research_request(existing)
                question_row = conn.execute("SELECT * FROM knowledge_questions WHERE question_id = ?", (existing["question_id"],)).fetchone()
                if question_row is None: raise ValueError("Review Research Request has no Question")
                return self._question(question_row), request
        question = self.create_question(prompt=f"What evidence could change or reaffirm this decision? {decision['statement']}", owner=owner, intent_id=(str(decision['intent_id']) if decision['intent_id'] else None), decision_id=decision_id, evidence_pack_id=(str(decision['evidence_pack_id']) if decision['evidence_pack_id'] else None), answer_criteria=("State whether the decision remains supported, unsupported, or uncertain.",), knowledge_query=str(decision["statement"]), review_at=(str(decision['review_at']) if decision['review_at'] else None))
        request = self.create_research_request(title=f"Review decision: {str(decision['statement'])}", objective=f"Research only the evidence needed to review this decision: {str(decision['statement'])}", owner=owner, question_id=question.question_id, intent_id=(str(decision['intent_id']) if decision['intent_id'] else None), decision_id=decision_id, evidence_pack_id=(str(decision['evidence_pack_id']) if decision['evidence_pack_id'] else None), knowledge_query=str(decision['statement']), required_evidence=("Cite current supporting evidence.",), counterargument_required=True, max_cost_units=0, max_duration_minutes=60, decision_revision=int(decision['revision']))
        return question, request

    @staticmethod
    def _provenance_closure(
        conn: sqlite3.Connection,
        *,
        pack_ids: set[str] | None = None,
        record_ids: set[str] | None = None,
    ) -> dict[str, set[str]]:
        """Find all content-bearing descendants of an Evidence/Record source."""

        packs = set(pack_ids or ())
        records = set(record_ids or ())
        candidates: set[str] = set()
        decisions: set[str] = set()
        questions: set[str] = set()
        research_requests: set[str] = set()

        def values(query: str, identities: set[str]) -> set[str]:
            if not identities:
                return set()
            placeholders = ",".join("?" for _ in identities)
            rows = conn.execute(query.format(placeholders=placeholders), tuple(sorted(identities)))
            return {str(row[0]) for row in rows.fetchall()}

        while True:
            before = (len(packs), len(records), len(candidates), len(decisions), len(questions), len(research_requests))
            packs.update(
                values(
                    """
                    SELECT DISTINCT pack_id FROM evidence_pack_sources
                    WHERE source_type = 'knowledge_record'
                      AND source_id IN ({placeholders})
                    """,
                    records,
                )
            )
            candidates.update(
                values(
                    """
                    SELECT candidate_id FROM knowledge_write_candidates
                    WHERE evidence_pack_id IN ({placeholders})
                    """,
                    packs,
                )
            )
            candidates.update(
                values(
                    """
                    SELECT candidate_id FROM knowledge_write_candidates
                    WHERE accepted_record_id IN ({placeholders})
                    """,
                    records,
                )
            )
            records.update(
                values(
                    """
                    SELECT record_id FROM knowledge_records
                    WHERE evidence_pack_id IN ({placeholders})
                    """,
                    packs,
                )
            )
            records.update(
                values(
                    """
                    SELECT record_id FROM knowledge_records
                    WHERE source_candidate_id IN ({placeholders})
                    """,
                    candidates,
                )
            )
            records.update(
                values(
                    """
                    SELECT record_id FROM knowledge_records
                    WHERE supersedes_record_id IN ({placeholders})
                    """,
                    records,
                )
            )
            decisions.update(
                values(
                    """
                    SELECT decision_id FROM knowledge_decisions
                    WHERE evidence_pack_id IN ({placeholders})
                    """,
                    packs,
                )
            )
            decisions.update(
                values(
                    """
                    SELECT decision_id FROM knowledge_decisions
                    WHERE supersedes_decision_id IN ({placeholders})
                    """,
                    decisions,
                )
            )
            questions.update(values("SELECT question_id FROM knowledge_questions WHERE evidence_pack_id IN ({placeholders})", packs))
            questions.update(values("SELECT question_id FROM knowledge_questions WHERE decision_id IN ({placeholders})", decisions))
            research_requests.update(values("SELECT request_id FROM knowledge_research_requests WHERE evidence_pack_id IN ({placeholders})", packs))
            research_requests.update(values("SELECT request_id FROM knowledge_research_requests WHERE decision_id IN ({placeholders})", decisions))
            research_requests.update(values("SELECT request_id FROM knowledge_research_requests WHERE question_id IN ({placeholders})", questions))
            if before == (len(packs), len(records), len(candidates), len(decisions), len(questions), len(research_requests)):
                break
        return {
            "packs": packs,
            "records": records,
            "candidates": candidates,
            "decisions": decisions,
            "questions": questions,
            "research_requests": research_requests,
        }

    @staticmethod
    def _delete_provenance_closure(
        conn: sqlite3.Connection,
        closure: Mapping[str, set[str]],
    ) -> None:
        """Delete a precomputed content closure without leaving detached copies."""

        def delete(table: str, column: str, identities: set[str]) -> None:
            if not identities:
                return
            placeholders = ",".join("?" for _ in identities)
            conn.execute(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                tuple(sorted(identities)),
            )

        decisions = closure["decisions"]
        questions = closure["questions"]
        research_requests = closure["research_requests"]
        records = closure["records"]
        candidates = closure["candidates"]
        packs = closure["packs"]
        if packs:
            placeholders = ",".join("?" for _ in packs)
            active = conn.execute(
                f"""
                SELECT job_id FROM knowledge_execution_bindings
                WHERE status = 'PREPARED' AND pack_id IN ({placeholders})
                ORDER BY created_at LIMIT 1
                """,
                tuple(sorted(packs)),
            ).fetchone()
            if active is not None:
                raise ValueError(
                    "Knowledge is leased by an active Intent Job; interrupt or finish "
                    f"{active['job_id']} before forgetting it"
                )
            binding_ids = {
                str(row[0])
                for row in conn.execute(
                    f"""
                    SELECT binding_id FROM knowledge_execution_bindings
                    WHERE pack_id IN ({placeholders})
                    """,
                    tuple(sorted(packs)),
                ).fetchall()
            }
        else:
            binding_ids = set()

        # Decision Contexts and Oracle/Outcome records are provenance-bearing
        # control metadata.  They contain no Knowledge bodies, but their
        # immutable source digest is no longer reproducible after the source
        # closure is forgotten, so remove the dependent closure first.  The
        # terminal execution binding remains as the minimal job audit record.
        delete("knowledge_outcome_observations", "binding_id", binding_ids)
        delete("knowledge_oracle_contracts", "binding_id", binding_ids)
        delete("knowledge_decision_contexts", "binding_id", binding_ids)

        if records:
            placeholders = ",".join("?" for _ in records)
            conn.execute(
                f"""
                DELETE FROM knowledge_epistemic_annotations
                WHERE subject_type = 'RECORD' AND subject_id IN ({placeholders})
                """,
                tuple(sorted(records)),
            )
        if candidates:
            placeholders = ",".join("?" for _ in candidates)
            conn.execute(
                f"""
                DELETE FROM knowledge_epistemic_annotations
                WHERE subject_type = 'WRITE_CANDIDATE'
                  AND subject_id IN ({placeholders})
                """,
                tuple(sorted(candidates)),
            )
        delete("knowledge_decision_revisions", "decision_id", decisions)
        delete("knowledge_research_request_revisions", "request_id", research_requests)
        delete("knowledge_research_requests", "request_id", research_requests)
        delete("knowledge_question_revisions", "question_id", questions)
        delete("knowledge_questions", "question_id", questions)
        delete("knowledge_decisions", "decision_id", decisions)
        delete("knowledge_records", "record_id", records)
        delete("knowledge_write_candidates", "candidate_id", candidates)
        delete("evidence_packs", "pack_id", packs)

    def counts(self) -> dict[str, int]:
        tables = (
            "knowledge_folders",
            "knowledge_folder_entries",
            "knowledge_assets",
            "knowledge_remote_asset_sources",
            "knowledge_representations",
            "knowledge_chunks",
            "knowledge_records",
            "evidence_packs",
            "knowledge_write_candidates",
            "knowledge_intents",
            "knowledge_decisions",
            "knowledge_questions",
            "knowledge_research_requests",
            "knowledge_execution_bindings",
            "knowledge_epistemic_annotations",
            "knowledge_decision_contexts",
            "knowledge_oracle_contracts",
            "knowledge_outcome_observations",
        )
        with self._lock:
            return {
                table: int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }

    def integrity_check(self) -> str:
        with self._lock:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "missing"

    @staticmethod
    def _intent_revision_payload(row: sqlite3.Row) -> dict[str, object]:
        return {
            "intent_id": str(row["intent_id"]),
            "goal": str(row["goal"]),
            "priority": int(row["priority"]),
            "status": str(row["status"]),
            "constraints": _loads(row["constraints_json"], []),
            "acceptance_criteria": _loads(row["acceptance_criteria_json"], []),
            "knowledge_query": str(row["knowledge_query"]),
            "revision": int(row["revision"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @classmethod
    def _append_intent_revision(cls, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        payload = cls._intent_revision_payload(row)
        encoded = _json(payload)
        conn.execute(
            """
            INSERT INTO knowledge_intent_revisions(
                intent_id, revision, payload_json, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["intent_id"],
                row["revision"],
                encoded,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                _now(),
            ),
        )

    @staticmethod
    def _decision_revision_payload(row: sqlite3.Row) -> dict[str, object]:
        return {
            "decision_id": str(row["decision_id"]),
            "statement": str(row["statement"]),
            "rationale": str(row["rationale"]),
            "status": str(row["status"]),
            "intent_id": row["intent_id"],
            "evidence_pack_id": row["evidence_pack_id"],
            "supersedes_decision_id": row["supersedes_decision_id"],
            "review_at": row["review_at"],
            "actor": str(row["actor"]),
            "revision": int(row["revision"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @classmethod
    def _append_decision_revision(cls, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        payload = cls._decision_revision_payload(row)
        encoded = _json(payload)
        conn.execute(
            """
            INSERT INTO knowledge_decision_revisions(
                decision_id, revision, payload_json, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["decision_id"],
                row["revision"],
                encoded,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                _now(),
            ),
        )

    def intent_history(self, intent_id: str) -> tuple[dict[str, object], ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT payload_json, content_hash FROM knowledge_intent_revisions
                WHERE intent_id = ? ORDER BY revision
                """,
                (intent_id,),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            payload = _loads(row["payload_json"], {})
            if not isinstance(payload, dict):
                raise ValueError("Intent revision payload is malformed")
            observed = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
            if observed != str(row["content_hash"]):
                raise ValueError("Intent revision payload hash is invalid")
            result.append({**payload, "content_hash": observed})
        return tuple(result)

    @classmethod
    def _verified_intent_row(
        cls, conn: sqlite3.Connection, intent_id: str
    ) -> tuple[sqlite3.Row, str]:
        row = conn.execute(
            "SELECT * FROM knowledge_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Intent was not found: {intent_id}")
        history = conn.execute(
            """
            SELECT payload_json, content_hash FROM knowledge_intent_revisions
            WHERE intent_id = ? AND revision = ?
            """,
            (intent_id, row["revision"]),
        ).fetchone()
        if history is None:
            raise ValueError("Current Intent has no matching immutable revision history")
        history_payload = _loads(history["payload_json"], {})
        if not isinstance(history_payload, dict):
            raise ValueError("Current Intent revision payload is malformed")
        encoded = _json(history_payload)
        observed = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if observed != str(history["content_hash"]) or history_payload != cls._intent_revision_payload(row):
            raise ValueError("Current Intent does not match its immutable revision history")
        return row, observed

    def verified_intent(self, intent_id: str) -> tuple[IntentRecord, str] | None:
        """Read the current Intent only when it exactly matches its immutable revision."""

        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM knowledge_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if exists is None:
                return None
            row, observed = self._verified_intent_row(self._conn, intent_id)
        return self._intent(row), observed

    def decision_history(self, decision_id: str) -> tuple[dict[str, object], ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT payload_json, content_hash FROM knowledge_decision_revisions
                WHERE decision_id = ? ORDER BY revision
                """,
                (decision_id,),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            payload = _loads(row["payload_json"], {})
            if not isinstance(payload, dict):
                raise ValueError("Decision revision payload is malformed")
            observed = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
            if observed != str(row["content_hash"]):
                raise ValueError("Decision revision payload hash is invalid")
            result.append({**payload, "content_hash": observed})
        return tuple(result)

    @classmethod
    def _verified_decision_row(
        cls, conn: sqlite3.Connection, decision_id: str
    ) -> tuple[sqlite3.Row, str]:
        row = conn.execute(
            "SELECT * FROM knowledge_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Decision was not found: {decision_id}")
        history = conn.execute(
            """
            SELECT payload_json, content_hash FROM knowledge_decision_revisions
            WHERE decision_id = ? AND revision = ?
            """,
            (decision_id, row["revision"]),
        ).fetchone()
        if history is None:
            raise ValueError("Current Decision has no matching immutable revision history")
        history_payload = _loads(history["payload_json"], {})
        if not isinstance(history_payload, dict):
            raise ValueError("Current Decision revision payload is malformed")
        encoded = _json(history_payload)
        observed = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if observed != str(history["content_hash"]) or history_payload != cls._decision_revision_payload(row):
            raise ValueError("Current Decision does not match its immutable revision history")
        return row, observed

    def verified_decision(self, decision_id: str) -> tuple[DecisionRecord, str] | None:
        """Read the current Decision only when immutable history authenticates it."""

        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM knowledge_decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            if exists is None:
                return None
            row, observed = self._verified_decision_row(self._conn, decision_id)
        return self._decision(row), observed

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        event_type: str,
        subject_type: str,
        subject_id: str,
        metadata: Mapping[str, object],
    ) -> None:
        conn.execute(
            """
            INSERT INTO knowledge_events(
                event_id, event_type, subject_type, subject_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"event-{uuid.uuid4()}",
                event_type,
                subject_type,
                subject_id,
                _json(dict(metadata)),
                _now(),
            ),
        )
