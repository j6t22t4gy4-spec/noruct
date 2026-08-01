"""SQLite operations for epistemic metadata and delayed outcome evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Mapping, Sequence

from .epistemic import (
    AttributionStatus,
    ContentTrustClass,
    DecisionContextSnapshot,
    EpistemicAnnotation,
    EpistemicStatus,
    OracleContract,
    OracleValidatorType,
    OutcomeObservation,
    OutcomeVerdict,
    ValidatorIndependence,
    canonical_digest,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None) -> object:
    return json.loads(value) if value else []


def _text(value: str, label: str, maximum: int = 8192, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{label} must be non-empty")
    if len(normalized.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds its UTF-8 byte bound")
    return normalized


def _items(values: Sequence[str], label: str, *, limit: int = 100) -> tuple[str, ...]:
    if len(values) > limit:
        raise ValueError(f"{label} exceeds its item bound")
    normalized: list[str] = []
    for value in values:
        item = _text(value, label)
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _timestamp(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Epistemic timestamps require an explicit timezone")
    return parsed.astimezone(UTC).isoformat()


class EpistemicStoreMixin:
    """Requires KnowledgeStore's ``_conn``, ``_lock``, ``_transaction`` and ``_event``."""

    _conn: sqlite3.Connection

    def epistemic_annotation(
        self, subject_type: str, subject_id: str
    ) -> EpistemicAnnotation | None:
        normalized_type = _text(subject_type, "Epistemic subject type", 32).upper()
        normalized_id = _text(subject_id, "Epistemic subject id", 256)
        with self._lock:  # type: ignore[attr-defined]
            row = self._conn.execute(
                """
                SELECT * FROM knowledge_epistemic_annotations
                WHERE subject_type = ? AND subject_id = ?
                """,
                (normalized_type, normalized_id),
            ).fetchone()
        return None if row is None else self._epistemic_annotation(row)

    @staticmethod
    def _epistemic_annotation(row: sqlite3.Row) -> EpistemicAnnotation:
        return EpistemicAnnotation(
            subject_type=str(row["subject_type"]),
            subject_id=str(row["subject_id"]),
            epistemic_status=EpistemicStatus(str(row["epistemic_status"])),
            trust_class=ContentTrustClass(str(row["trust_class"])),
            freshness_expires_at=(
                str(row["freshness_expires_at"]) if row["freshness_expires_at"] else None
            ),
            conflict_refs=tuple(str(item) for item in _loads(row["conflict_refs_json"])),
            unknown_refs=tuple(str(item) for item in _loads(row["unknown_refs_json"])),
            source_revision=str(row["source_revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def set_epistemic_annotation(
        self,
        *,
        subject_type: str,
        subject_id: str,
        epistemic_status: EpistemicStatus,
        trust_class: ContentTrustClass,
        freshness_expires_at: str | None = None,
        conflict_refs: Sequence[str] = (),
        unknown_refs: Sequence[str] = (),
        source_revision: str = "1",
    ) -> EpistemicAnnotation:
        normalized_type = _text(subject_type, "Epistemic subject type", 32).upper()
        if normalized_type not in {"RECORD", "WRITE_CANDIDATE"}:
            raise ValueError("Epistemic subject type is unsupported")
        normalized_id = _text(subject_id, "Epistemic subject id", 256)
        status = EpistemicStatus(epistemic_status)
        trust = ContentTrustClass(trust_class)
        conflicts = _items(conflict_refs, "Epistemic conflict reference")
        unknowns = _items(unknown_refs, "Epistemic unknown reference")
        revision = _text(source_revision, "Epistemic source revision", 256)
        freshness = _timestamp(freshness_expires_at)
        now = _now()
        table = "knowledge_records" if normalized_type == "RECORD" else "knowledge_write_candidates"
        id_column = "record_id" if normalized_type == "RECORD" else "candidate_id"
        with self._transaction() as conn:  # type: ignore[attr-defined]
            subject = conn.execute(
                f"SELECT 1 FROM {table} WHERE {id_column} = ?", (normalized_id,)
            ).fetchone()
            if subject is None:
                raise ValueError("Epistemic annotation subject was not found")
            existing = conn.execute(
                """
                SELECT created_at FROM knowledge_epistemic_annotations
                WHERE subject_type = ? AND subject_id = ?
                """,
                (normalized_type, normalized_id),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing is not None else now
            conn.execute(
                """
                INSERT INTO knowledge_epistemic_annotations(
                    subject_type, subject_id, epistemic_status, trust_class,
                    freshness_expires_at, conflict_refs_json, unknown_refs_json,
                    source_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_type, subject_id) DO UPDATE SET
                    epistemic_status = excluded.epistemic_status,
                    trust_class = excluded.trust_class,
                    freshness_expires_at = excluded.freshness_expires_at,
                    conflict_refs_json = excluded.conflict_refs_json,
                    unknown_refs_json = excluded.unknown_refs_json,
                    source_revision = excluded.source_revision,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_type,
                    normalized_id,
                    status.value,
                    trust.value,
                    freshness,
                    _json(list(conflicts)),
                    _json(list(unknowns)),
                    revision,
                    created_at,
                    now,
                ),
            )
            self._event(  # type: ignore[attr-defined]
                conn,
                "EPISTEMIC_ANNOTATION_SET",
                normalized_type.lower(),
                normalized_id,
                {"epistemic_status": status.value, "trust_class": trust.value},
            )
        value = self.epistemic_annotation(normalized_type, normalized_id)
        assert value is not None
        return value

    def ensure_epistemic_admission(
        self,
        *,
        binding_id: str,
        known_refs: Sequence[str],
        unknown_refs: Sequence[str],
        assumptions: Sequence[str],
        constraints: Sequence[str],
        excluded_alternatives: Sequence[str],
        owner_ref: str,
        authority_ref: str,
        acceptance_criteria: Sequence[str],
        failure_criteria: Sequence[str] = (),
        observable_signals: Sequence[str] = (),
        observation_channel: str = "",
        validator_type: OracleValidatorType = OracleValidatorType.UNVERIFIABLE,
        independence_class: ValidatorIndependence = ValidatorIndependence.NONE,
        feedback_due_at: str | None = None,
        reversibility_class: str = "UNKNOWN",
        risk_class: str = "UNKNOWN",
        proxy_metric: str | None = None,
        proxy_failure_modes: Sequence[str] = (),
        inconclusive_policy: str = "REQUIRE_EXPLICIT_OBSERVATION",
        max_attempts: int = 1,
        max_evidence_items: int = 20,
    ) -> tuple[DecisionContextSnapshot, OracleContract]:
        """Create the immutable context and oracle for one prepared binding.

        Repeating the exact admission is idempotent.  Any changed input under
        the same binding is rejected instead of silently rewriting history.
        """

        binding_id = _text(binding_id, "Knowledge execution binding id", 256)
        known = _items(known_refs, "Known reference")
        unknown = _items(unknown_refs, "Unknown reference")
        assumptions_value = _items(assumptions, "Execution assumption")
        constraints_value = _items(constraints, "Execution constraint")
        excluded = _items(excluded_alternatives, "Excluded alternative")
        owner = _text(owner_ref, "Decision context owner", 256)
        authority = _text(authority_ref, "Decision context authority", 256)
        acceptance = _items(acceptance_criteria, "Oracle acceptance criterion")
        failures = _items(failure_criteria, "Oracle failure criterion")
        signals = _items(observable_signals, "Oracle observable signal")
        validator = OracleValidatorType(validator_type)
        independence = ValidatorIndependence(independence_class)
        if validator is OracleValidatorType.UNVERIFIABLE and signals:
            raise ValueError("An UNVERIFIABLE Oracle cannot claim observable signals")
        channel = _text(
            observation_channel,
            "Oracle observation channel",
            1024,
            required=validator is not OracleValidatorType.UNVERIFIABLE,
        )
        reversibility = _text(reversibility_class, "Oracle reversibility class", 64)
        risk = _text(risk_class, "Oracle risk class", 64)
        proxy = (
            _text(proxy_metric, "Oracle proxy metric", 4096)
            if proxy_metric is not None
            else None
        )
        proxy_failures = _items(proxy_failure_modes, "Oracle proxy failure mode")
        inconclusive = _text(inconclusive_policy, "Oracle inconclusive policy", 256)
        due_at = _timestamp(feedback_due_at)
        if max_attempts < 1 or max_attempts > 100:
            raise ValueError("Oracle attempt bound is invalid")
        if max_evidence_items < 0 or max_evidence_items > 1000:
            raise ValueError("Oracle evidence bound is invalid")
        now = _now()
        with self._transaction() as conn:  # type: ignore[attr-defined]
            binding = conn.execute(
                "SELECT * FROM knowledge_execution_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if binding is None or str(binding["status"]) != "PREPARED":
                raise ValueError("Epistemic admission requires a PREPARED execution binding")
            intent = conn.execute(
                "SELECT constraints_json FROM knowledge_intents WHERE intent_id = ? AND revision = ?",
                (binding["intent_id"], binding["intent_revision"]),
            ).fetchone()
            if intent is None:
                raise ValueError("Epistemic admission Intent revision is unavailable")

            snapshot_id = f"decision-context-{uuid.uuid5(uuid.NAMESPACE_URL, binding_id)}"
            snapshot_payload = {
                "schema": "noruct.decision-context.v1",
                "snapshot_id": snapshot_id,
                "binding_id": binding_id,
                "request_id": str(binding["request_id"]),
                "job_id": str(binding["job_id"]),
                "intent_id": str(binding["intent_id"]),
                "intent_revision": int(binding["intent_revision"]),
                "intent_hash": str(binding["intent_hash"]),
                "decision_id": None,
                "decision_revision": None,
                "evidence_pack_id": str(binding["pack_id"]),
                "evidence_pack_revision": int(binding["pack_revision"]),
                "evidence_pack_digest": str(binding["pack_digest"]),
                "known_refs": list(known),
                "unknown_refs": list(unknown),
                "assumptions": list(assumptions_value),
                "constraints": list(constraints_value),
                "excluded_alternatives": list(excluded),
                "owner_ref": owner,
                "authority_ref": authority,
                "supersedes_snapshot_id": None,
                "created_at": now,
            }
            snapshot_digest = canonical_digest(snapshot_payload)

            oracle_id = f"oracle-{uuid.uuid5(uuid.NAMESPACE_OID, binding_id)}"
            oracle_payload = {
                "schema": "noruct.oracle-contract.v1",
                "oracle_contract_id": oracle_id,
                "binding_id": binding_id,
                "request_id": str(binding["request_id"]),
                "job_id": str(binding["job_id"]),
                "revision": 1,
                "acceptance_criteria": list(acceptance),
                "failure_criteria": list(failures),
                "observable_signals": list(signals),
                "observation_channel": channel,
                "validator_type": validator.value,
                "independence_class": independence.value,
                "accountable_owner_ref": owner,
                "authority_ref": authority,
                "feedback_due_at": due_at,
                "reversibility_class": reversibility,
                "risk_class": risk,
                "proxy_metric": proxy,
                "proxy_failure_modes": list(proxy_failures),
                "inconclusive_policy": inconclusive,
                "max_attempts": max_attempts,
                "max_evidence_items": max_evidence_items,
                "created_at": now,
            }
            oracle_digest = canonical_digest(oracle_payload)

            existing_snapshot = conn.execute(
                "SELECT * FROM knowledge_decision_contexts WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            existing_oracle = conn.execute(
                "SELECT * FROM knowledge_oracle_contracts WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if existing_snapshot is not None or existing_oracle is not None:
                if existing_snapshot is None or existing_oracle is None:
                    raise ValueError("Epistemic admission is partially persisted")
                snapshot = self._decision_context(existing_snapshot)
                oracle = self._oracle_contract(existing_oracle)
                # created_at is part of both signed payloads; compare the
                # caller-owned fields while preserving the original timestamp.
                comparable_snapshot = dict(snapshot.canonical_payload())
                comparable_snapshot["created_at"] = now
                comparable_oracle = dict(oracle.canonical_payload())
                comparable_oracle["created_at"] = now
                if (
                    canonical_digest(comparable_snapshot) != snapshot_digest
                    or canonical_digest(comparable_oracle) != oracle_digest
                ):
                    raise ValueError("Execution binding already has different epistemic admission")
                return snapshot, oracle

            conn.execute(
                """
                INSERT INTO knowledge_decision_contexts(
                    snapshot_id, binding_id, request_id, job_id, intent_id,
                    intent_revision, intent_hash, evidence_pack_id,
                    evidence_pack_revision, evidence_pack_digest, known_refs_json,
                    unknown_refs_json, assumptions_json, constraints_json,
                    excluded_alternatives_json, owner_ref, authority_ref,
                    content_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    binding_id,
                    binding["request_id"],
                    binding["job_id"],
                    binding["intent_id"],
                    binding["intent_revision"],
                    binding["intent_hash"],
                    binding["pack_id"],
                    binding["pack_revision"],
                    binding["pack_digest"],
                    _json(list(known)),
                    _json(list(unknown)),
                    _json(list(assumptions_value)),
                    _json(list(constraints_value)),
                    _json(list(excluded)),
                    owner,
                    authority,
                    snapshot_digest,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO knowledge_oracle_contracts(
                    oracle_contract_id, binding_id, request_id, job_id, revision,
                    acceptance_criteria_json, failure_criteria_json,
                    observable_signals_json, observation_channel, validator_type,
                    independence_class, accountable_owner_ref, authority_ref,
                    feedback_due_at, reversibility_class, risk_class, proxy_metric,
                    proxy_failure_modes_json, inconclusive_policy, max_attempts,
                    max_evidence_items, content_digest, created_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    oracle_id,
                    binding_id,
                    binding["request_id"],
                    binding["job_id"],
                    _json(list(acceptance)),
                    _json(list(failures)),
                    _json(list(signals)),
                    channel,
                    validator.value,
                    independence.value,
                    owner,
                    authority,
                    due_at,
                    reversibility,
                    risk,
                    proxy,
                    _json(list(proxy_failures)),
                    inconclusive,
                    max_attempts,
                    max_evidence_items,
                    oracle_digest,
                    now,
                ),
            )
            self._event(  # type: ignore[attr-defined]
                conn,
                "EPISTEMIC_ADMISSION_FROZEN",
                "execution_binding",
                binding_id,
                {"snapshot_id": snapshot_id, "oracle_contract_id": oracle_id},
            )
        snapshot = self.decision_context_for_binding(binding_id)
        oracle = self.oracle_contract_for_binding(binding_id)
        assert snapshot is not None and oracle is not None
        return snapshot, oracle

    @staticmethod
    def _decision_context(row: sqlite3.Row) -> DecisionContextSnapshot:
        value = DecisionContextSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            binding_id=str(row["binding_id"]),
            request_id=str(row["request_id"]),
            job_id=str(row["job_id"]),
            intent_id=str(row["intent_id"]),
            intent_revision=int(row["intent_revision"]),
            intent_hash=str(row["intent_hash"]),
            decision_id=(str(row["decision_id"]) if row["decision_id"] else None),
            decision_revision=(int(row["decision_revision"]) if row["decision_revision"] else None),
            evidence_pack_id=str(row["evidence_pack_id"]),
            evidence_pack_revision=int(row["evidence_pack_revision"]),
            evidence_pack_digest=str(row["evidence_pack_digest"]),
            known_refs=tuple(str(item) for item in _loads(row["known_refs_json"])),
            unknown_refs=tuple(str(item) for item in _loads(row["unknown_refs_json"])),
            assumptions=tuple(str(item) for item in _loads(row["assumptions_json"])),
            constraints=tuple(str(item) for item in _loads(row["constraints_json"])),
            excluded_alternatives=tuple(str(item) for item in _loads(row["excluded_alternatives_json"])),
            owner_ref=str(row["owner_ref"]),
            authority_ref=str(row["authority_ref"]),
            supersedes_snapshot_id=(
                str(row["supersedes_snapshot_id"]) if row["supersedes_snapshot_id"] else None
            ),
            content_digest=str(row["content_digest"]),
            created_at=str(row["created_at"]),
        )
        value.verify()
        return value

    def decision_context_for_binding(self, binding_id: str) -> DecisionContextSnapshot | None:
        with self._lock:  # type: ignore[attr-defined]
            row = self._conn.execute(
                "SELECT * FROM knowledge_decision_contexts WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        return None if row is None else self._decision_context(row)

    @staticmethod
    def _oracle_contract(row: sqlite3.Row) -> OracleContract:
        value = OracleContract(
            oracle_contract_id=str(row["oracle_contract_id"]),
            binding_id=str(row["binding_id"]),
            request_id=str(row["request_id"]),
            job_id=str(row["job_id"]),
            revision=int(row["revision"]),
            acceptance_criteria=tuple(str(item) for item in _loads(row["acceptance_criteria_json"])),
            failure_criteria=tuple(str(item) for item in _loads(row["failure_criteria_json"])),
            observable_signals=tuple(str(item) for item in _loads(row["observable_signals_json"])),
            observation_channel=str(row["observation_channel"]),
            validator_type=OracleValidatorType(str(row["validator_type"])),
            independence_class=ValidatorIndependence(str(row["independence_class"])),
            accountable_owner_ref=str(row["accountable_owner_ref"]),
            authority_ref=str(row["authority_ref"]),
            feedback_due_at=(str(row["feedback_due_at"]) if row["feedback_due_at"] else None),
            reversibility_class=str(row["reversibility_class"]),
            risk_class=str(row["risk_class"]),
            proxy_metric=(str(row["proxy_metric"]) if row["proxy_metric"] else None),
            proxy_failure_modes=tuple(str(item) for item in _loads(row["proxy_failure_modes_json"])),
            inconclusive_policy=str(row["inconclusive_policy"]),
            max_attempts=int(row["max_attempts"]),
            max_evidence_items=int(row["max_evidence_items"]),
            content_digest=str(row["content_digest"]),
            created_at=str(row["created_at"]),
        )
        value.verify()
        return value

    def oracle_contract_for_binding(self, binding_id: str) -> OracleContract | None:
        with self._lock:  # type: ignore[attr-defined]
            row = self._conn.execute(
                "SELECT * FROM knowledge_oracle_contracts WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        return None if row is None else self._oracle_contract(row)

    def ensure_pending_outcome(
        self,
        *,
        binding_id: str,
        job_status: str,
        result_summary: str,
    ) -> OutcomeObservation:
        binding_id = _text(binding_id, "Knowledge execution binding id", 256)
        terminal_status = _text(job_status, "Firm terminal status", 64).upper()
        summary = _text(result_summary, "Firm result summary", 64_000, required=False)
        result_digest = hashlib.sha256(
            _json({"job_status": terminal_status, "summary": summary}).encode("utf-8")
        ).hexdigest()
        now = _now()
        with self._transaction() as conn:  # type: ignore[attr-defined]
            binding = conn.execute(
                "SELECT * FROM knowledge_execution_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            oracle_row = conn.execute(
                "SELECT * FROM knowledge_oracle_contracts WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if binding is None or str(binding["status"]) != "TERMINAL":
                raise ValueError("Pending outcome requires a terminal execution binding")
            if str(binding["job_status"]) != terminal_status:
                raise ValueError("Pending outcome status does not match its execution binding")
            if oracle_row is None:
                raise ValueError("Pending outcome requires an Oracle Contract")
            existing = conn.execute(
                "SELECT * FROM knowledge_outcome_observations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if existing is not None:
                value = self._outcome(existing)
                if value.result_digest != result_digest:
                    raise ValueError("Execution binding already has a different outcome result")
                return value
            oracle = self._oracle_contract(oracle_row)
            expected_signal = "; ".join(oracle.acceptance_criteria)[:8192]
            outcome_id = f"outcome-{uuid.uuid5(uuid.NAMESPACE_DNS, binding_id)}"
            conn.execute(
                """
                INSERT INTO knowledge_outcome_observations(
                    outcome_id, oracle_contract_id, binding_id, request_id, job_id,
                    result_digest, expected_signal, observed_signal, verdict,
                    confounders_json, attribution_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', 'NOT_YET_OBSERVED', '[]',
                          'UNASSESSED', ?, ?)
                """,
                (
                    outcome_id,
                    oracle.oracle_contract_id,
                    binding_id,
                    binding["request_id"],
                    binding["job_id"],
                    result_digest,
                    expected_signal,
                    now,
                    now,
                ),
            )
            self._event(  # type: ignore[attr-defined]
                conn,
                "OUTCOME_PENDING_OBSERVATION",
                "outcome",
                outcome_id,
                {"binding_id": binding_id, "job_status": terminal_status},
            )
        value = self.outcome_for_binding(binding_id)
        assert value is not None
        return value

    @staticmethod
    def _outcome(row: sqlite3.Row) -> OutcomeObservation:
        return OutcomeObservation(
            outcome_id=str(row["outcome_id"]),
            oracle_contract_id=str(row["oracle_contract_id"]),
            binding_id=str(row["binding_id"]),
            request_id=str(row["request_id"]),
            job_id=str(row["job_id"]),
            result_digest=str(row["result_digest"]),
            expected_signal=str(row["expected_signal"]),
            observed_signal=str(row["observed_signal"]),
            observed_at=(str(row["observed_at"]) if row["observed_at"] else None),
            source_ref=(str(row["source_ref"]) if row["source_ref"] else None),
            verdict=OutcomeVerdict(str(row["verdict"])),
            confounders=tuple(str(item) for item in _loads(row["confounders_json"])),
            attribution_status=AttributionStatus(str(row["attribution_status"])),
            reviewer_ref=(str(row["reviewer_ref"]) if row["reviewer_ref"] else None),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def outcome_for_binding(self, binding_id: str) -> OutcomeObservation | None:
        with self._lock:  # type: ignore[attr-defined]
            row = self._conn.execute(
                "SELECT * FROM knowledge_outcome_observations WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        return None if row is None else self._outcome(row)

    def outcome(self, outcome_id: str) -> OutcomeObservation | None:
        normalized = _text(outcome_id, "Outcome identity", 256)
        with self._lock:  # type: ignore[attr-defined]
            row = self._conn.execute(
                "SELECT * FROM knowledge_outcome_observations WHERE outcome_id = ?",
                (normalized,),
            ).fetchone()
        return None if row is None else self._outcome(row)

    def list_outcomes(
        self,
        *,
        verdict: OutcomeVerdict | str | None = None,
        limit: int = 100,
    ) -> tuple[OutcomeObservation, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Knowledge outcome list limit must be between 1 and 1000")
        query = "SELECT * FROM knowledge_outcome_observations"
        parameters: list[object] = []
        if verdict is not None:
            normalized = OutcomeVerdict(verdict).value
            query += " WHERE verdict = ?"
            parameters.append(normalized)
        query += " ORDER BY updated_at DESC, outcome_id LIMIT ?"
        parameters.append(limit)
        with self._lock:  # type: ignore[attr-defined]
            rows = self._conn.execute(query, tuple(parameters)).fetchall()
        return tuple(self._outcome(row) for row in rows)

    def observe_outcome(
        self,
        outcome_id: str,
        *,
        verdict: OutcomeVerdict,
        observed_signal: str,
        source_ref: str,
        reviewer_ref: str,
        observed_at: str | None = None,
        confounders: Sequence[str] = (),
        attribution_status: AttributionStatus = AttributionStatus.UNASSESSED,
    ) -> OutcomeObservation:
        outcome_id = _text(outcome_id, "Outcome identity", 256)
        verdict_value = OutcomeVerdict(verdict)
        if verdict_value is OutcomeVerdict.NOT_YET_OBSERVED:
            raise ValueError("An observation must provide a terminal Oracle verdict")
        signal = _text(observed_signal, "Observed signal", 8192)
        source = _text(source_ref, "Outcome source reference", 2048)
        reviewer = _text(reviewer_ref, "Outcome reviewer reference", 256)
        requested_observed_at = _timestamp(observed_at)
        confounder_values = _items(confounders, "Outcome confounder")
        attribution = AttributionStatus(attribution_status)
        now = _now()
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT * FROM knowledge_outcome_observations WHERE outcome_id = ?",
                (outcome_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Outcome observation was not found")
            observed = requested_observed_at or (
                str(row["observed_at"])
                if str(row["verdict"]) != OutcomeVerdict.NOT_YET_OBSERVED.value
                and row["observed_at"]
                else _now()
            )
            expected = (
                verdict_value.value,
                signal,
                observed,
                source,
                _json(list(confounder_values)),
                attribution.value,
                reviewer,
            )
            if str(row["verdict"]) != OutcomeVerdict.NOT_YET_OBSERVED.value:
                observed_existing = (
                    str(row["verdict"]),
                    str(row["observed_signal"]),
                    str(row["observed_at"]),
                    str(row["source_ref"]),
                    str(row["confounders_json"]),
                    str(row["attribution_status"]),
                    str(row["reviewer_ref"]),
                )
                if observed_existing != expected:
                    raise ValueError("Outcome already has a different observation")
                return self._outcome(row)
            conn.execute(
                """
                UPDATE knowledge_outcome_observations
                SET verdict = ?, observed_signal = ?, observed_at = ?, source_ref = ?,
                    confounders_json = ?, attribution_status = ?, reviewer_ref = ?,
                    updated_at = ?
                WHERE outcome_id = ? AND verdict = 'NOT_YET_OBSERVED'
                """,
                (*expected, now, outcome_id),
            )
            self._event(  # type: ignore[attr-defined]
                conn,
                "OUTCOME_OBSERVED",
                "outcome",
                outcome_id,
                {"verdict": verdict_value.value, "attribution_status": attribution.value},
            )
        with self._lock:  # type: ignore[attr-defined]
            row = self._conn.execute(
                "SELECT * FROM knowledge_outcome_observations WHERE outcome_id = ?",
                (outcome_id,),
            ).fetchone()
        assert row is not None
        return self._outcome(row)
