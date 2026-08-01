"""Intent and Decision lifecycle for the canonical KnowledgeStore."""
from __future__ import annotations

import sqlite3
import uuid
from typing import Sequence

from .models import DecisionRecord, DecisionStatus, IntentRecord, IntentStatus
from .store_primitives import _bounded_text, _json, _loads, _normalized_timestamp, _now


class KnowledgeIntentDecisionMixin:
    def create_intent(
        self,
        *,
        goal: str,
        priority: int = 50,
        status: IntentStatus = IntentStatus.ACTIVE,
        constraints: Sequence[str] = (),
        acceptance_criteria: Sequence[str] = (),
        knowledge_query: str = "",
    ) -> IntentRecord:
        normalized = _bounded_text(goal, "Intent goal", 32_000)
        if priority < 0 or priority > 100:
            raise ValueError("Intent priority must be between 0 and 100")
        normalized_constraints = tuple(
            _bounded_text(value, "Intent constraint", 8_000)
            for value in constraints
        )
        normalized_acceptance = tuple(
            _bounded_text(value, "Intent acceptance criterion", 8_000)
            for value in acceptance_criteria
        )
        if len(normalized_constraints) > 100 or len(normalized_acceptance) > 100:
            raise ValueError("Intent constraint and acceptance lists are limited to 100 items")
        normalized_query = _bounded_text(
            knowledge_query,
            "Intent knowledge query",
            32_000,
            required=False,
        )
        intent_id = f"intent-{uuid.uuid4()}"
        now = _now()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_intents(
                    intent_id, goal, priority, status, constraints_json,
                    acceptance_criteria_json, knowledge_query, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    intent_id,
                    normalized,
                    priority,
                    status.value,
                    _json(list(normalized_constraints)),
                    _json(list(normalized_acceptance)),
                    normalized_query,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM knowledge_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            assert row is not None
            self._append_intent_revision(conn, row)
            self._event(conn, "INTENT_CREATED", "intent", intent_id, {"priority": priority})
        value = self.intent(intent_id)
        assert value is not None
        return value

    @staticmethod
    def _intent(row: sqlite3.Row) -> IntentRecord:
        return IntentRecord(
            intent_id=str(row["intent_id"]),
            goal=str(row["goal"]),
            priority=int(row["priority"]),
            status=IntentStatus(str(row["status"])),
            constraints=tuple(str(item) for item in _loads(row["constraints_json"], [])),
            acceptance_criteria=tuple(str(item) for item in _loads(row["acceptance_criteria_json"], [])),
            knowledge_query=str(row["knowledge_query"]),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def intent(self, intent_id: str) -> IntentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return None if row is None else self._intent(row)

    def list_intents(self, *, status: IntentStatus | None = None, limit: int = 100) -> tuple[IntentRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Intent list limit must be between 1 and 1000")
        query = "SELECT * FROM knowledge_intents"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += " ORDER BY priority DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
            return tuple(
                self._intent(self._verified_intent_row(self._conn, str(row["intent_id"]))[0])
                for row in rows
            )

    def set_intent_status(self, intent_id: str, status: IntentStatus) -> IntentRecord:
        now = _now()
        with self._transaction() as conn:
            self._verified_intent_row(conn, intent_id)
            changed = conn.execute(
                """
                UPDATE knowledge_intents
                SET status = ?, revision = revision + 1, updated_at = ?
                WHERE intent_id = ?
                """,
                (status.value, now, intent_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"Intent was not found: {intent_id}")
            row = conn.execute(
                "SELECT * FROM knowledge_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            assert row is not None
            self._append_intent_revision(conn, row)
            self._event(conn, "INTENT_STATUS_CHANGED", "intent", intent_id, {"status": status.value})
        value = self.intent(intent_id)
        assert value is not None
        return value

    def create_decision(
        self,
        *,
        statement: str,
        rationale: str,
        status: DecisionStatus = DecisionStatus.PROPOSED,
        intent_id: str | None = None,
        evidence_pack_id: str | None = None,
        supersedes_decision_id: str | None = None,
        review_at: str | None = None,
        actor: str = "user:local",
    ) -> DecisionRecord:
        normalized_statement = _bounded_text(statement, "Decision statement", 32_000)
        normalized_rationale = _bounded_text(rationale, "Decision rationale", 64_000)
        normalized_actor = _bounded_text(actor or "user:local", "Decision actor", 1_024)
        normalized_review_at = _normalized_timestamp(review_at)
        decision_id = f"decision-{uuid.uuid4()}"
        now = _now()
        with self._transaction() as conn:
            revision = 1
            if supersedes_decision_id:
                prior, _ = self._verified_decision_row(conn, supersedes_decision_id)
                revision = int(prior["revision"]) + 1
                conn.execute(
                    """
                    UPDATE knowledge_decisions
                    SET status = ?, revision = revision + 1, updated_at = ?
                    WHERE decision_id = ?
                    """,
                    (DecisionStatus.SUPERSEDED.value, now, supersedes_decision_id),
                )
                superseded = conn.execute(
                    "SELECT * FROM knowledge_decisions WHERE decision_id = ?",
                    (supersedes_decision_id,),
                ).fetchone()
                assert superseded is not None
                self._append_decision_revision(conn, superseded)
            conn.execute(
                """
                INSERT INTO knowledge_decisions(
                    decision_id, statement, rationale, status, intent_id,
                    evidence_pack_id, supersedes_decision_id, review_at, actor, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    normalized_statement,
                    normalized_rationale,
                    status.value,
                    intent_id,
                    evidence_pack_id,
                    supersedes_decision_id,
                    normalized_review_at,
                    normalized_actor,
                    revision,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM knowledge_decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            self._append_decision_revision(conn, row)
            self._event(conn, "DECISION_CREATED", "decision", decision_id, {"status": status.value})
        value = self.decision(decision_id)
        assert value is not None
        return value

    @staticmethod
    def _decision(row: sqlite3.Row) -> DecisionRecord:
        return DecisionRecord(
            decision_id=str(row["decision_id"]),
            statement=str(row["statement"]),
            rationale=str(row["rationale"]),
            status=DecisionStatus(str(row["status"])),
            intent_id=(str(row["intent_id"]) if row["intent_id"] else None),
            evidence_pack_id=(str(row["evidence_pack_id"]) if row["evidence_pack_id"] else None),
            supersedes_decision_id=(
                str(row["supersedes_decision_id"]) if row["supersedes_decision_id"] else None
            ),
            review_at=(str(row["review_at"]) if row["review_at"] else None),
            actor=str(row["actor"]),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def decision(self, decision_id: str) -> DecisionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        return None if row is None else self._decision(row)

    def list_decisions(self, *, limit: int = 100) -> tuple[DecisionRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Decision list limit must be between 1 and 1000")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_decisions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return tuple(
                self._decision(
                    self._verified_decision_row(self._conn, str(row["decision_id"]))[0]
                )
                for row in rows
            )

    def due_decisions(self, *, as_of: str | None = None, limit: int = 100) -> tuple[DecisionRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Decision due limit must be between 1 and 1000")
        boundary = _normalized_timestamp(as_of) if as_of else _now()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM knowledge_decisions
                WHERE review_at IS NOT NULL AND review_at <= ?
                  AND status IN (?, ?)
                ORDER BY review_at, decision_id LIMIT ?
                """,
                (
                    boundary,
                    DecisionStatus.PROPOSED.value,
                    DecisionStatus.ACCEPTED.value,
                    limit,
                ),
            ).fetchall()
            return tuple(
                self._decision(
                    self._verified_decision_row(self._conn, str(row["decision_id"]))[0]
                )
                for row in rows
            )

    def set_decision_status(self, decision_id: str, status: DecisionStatus) -> DecisionRecord:
        if status == DecisionStatus.SUPERSEDED:
            raise ValueError("Create a replacement decision to supersede an existing decision")
        now = _now()
        with self._transaction() as conn:
            self._verified_decision_row(conn, decision_id)
            changed = conn.execute(
                """
                UPDATE knowledge_decisions
                SET status = ?, revision = revision + 1, updated_at = ?
                WHERE decision_id = ?
                """,
                (status.value, now, decision_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"Decision was not found: {decision_id}")
            row = conn.execute(
                "SELECT * FROM knowledge_decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            self._append_decision_revision(conn, row)
            self._event(conn, "DECISION_STATUS_CHANGED", "decision", decision_id, {"status": status.value})
        value = self.decision(decision_id)
        assert value is not None
        return value

