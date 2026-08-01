"""Evidence, write-candidate, and execution-binding lifecycle for KnowledgeStore."""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from typing import Mapping, Sequence

from .delivery import runtime_delivery_from_evidence_pack
from .epistemic import ContentTrustClass, EpistemicStatus
from .folder_models import KnowledgeFolderEntryStatus
from .models import (
    EvidenceItem,
    EvidencePack,
    KnowledgeExecutionBinding,
    KnowledgeRecord,
    KnowledgeWriteCandidate,
)
from .store_primitives import (
    _bounded_mapping,
    _bounded_text,
    _json,
    _loads,
    _normalized_scope,
    _normalized_timestamp,
    _now,
)


class KnowledgeEvidenceExecutionMixin:
    def create_record(
        self,
        *,
        kind: str,
        statement: str,
        confidence: float = 1.0,
        source_asset_id: str | None = None,
        source_representation_id: str | None = None,
        source_span: Mapping[str, object] | None = None,
        supersedes_record_id: str | None = None,
        source_candidate_id: str | None = None,
        source_job_id: str | None = None,
        evidence_pack_id: str | None = None,
        access_scope: str = "private",
        epistemic_status: EpistemicStatus = EpistemicStatus.UNKNOWN,
        trust_class: ContentTrustClass = ContentTrustClass.UNSPECIFIED,
        freshness_expires_at: str | None = None,
        conflict_refs: Sequence[str] = (),
        unknown_refs: Sequence[str] = (),
    ) -> KnowledgeRecord:
        normalized = _bounded_text(statement, "Knowledge statement", 64_000)
        normalized_kind = _bounded_text(kind or "NOTE", "Knowledge record kind", 128).upper()
        scope = _normalized_scope(access_scope)
        span = _bounded_mapping(source_span, "Knowledge source span", 8192)
        for label, value in (
            ("source Asset id", source_asset_id),
            ("source representation id", source_representation_id),
            ("superseded record id", supersedes_record_id),
            ("source candidate id", source_candidate_id),
            ("source Job id", source_job_id),
            ("Evidence Pack id", evidence_pack_id),
        ):
            if value is not None:
                _bounded_text(value, f"Knowledge {label}", 256)
        if not 0 <= confidence <= 1:
            raise ValueError("Knowledge confidence must be between 0 and 1")
        epistemic = EpistemicStatus(epistemic_status)
        trust = ContentTrustClass(trust_class)
        freshness = _normalized_timestamp(freshness_expires_at)
        normalized_conflicts = tuple(
            _bounded_text(value, "Knowledge conflict reference", 256)
            for value in conflict_refs
        )
        normalized_unknowns = tuple(
            _bounded_text(value, "Knowledge unknown reference", 256)
            for value in unknown_refs
        )
        if len(normalized_conflicts) > 100 or len(normalized_unknowns) > 100:
            raise ValueError("Knowledge epistemic references exceed their item bound")
        record_id = f"record-{uuid.uuid4()}"
        now = _now()
        with self._transaction() as conn:
            source_asset_scope: str | None = None
            if source_asset_id:
                source_asset = conn.execute(
                    "SELECT access_scope FROM knowledge_assets WHERE asset_id = ?",
                    (source_asset_id,),
                ).fetchone()
                if source_asset is None:
                    raise ValueError(f"Knowledge Asset was not found: {source_asset_id}")
                source_asset_scope = str(source_asset["access_scope"])
                if source_asset_scope != scope:
                    raise ValueError("Knowledge record and source Asset must use the same scope")
            if source_representation_id:
                source_representation = conn.execute(
                    """
                    SELECT representation.asset_id, asset.access_scope
                    FROM knowledge_representations representation
                    JOIN knowledge_assets asset ON asset.asset_id = representation.asset_id
                    WHERE representation.representation_id = ?
                    """,
                    (source_representation_id,),
                ).fetchone()
                if source_representation is None:
                    raise ValueError(
                        f"Knowledge representation was not found: {source_representation_id}"
                    )
                if str(source_representation["access_scope"]) != scope:
                    raise ValueError(
                        "Knowledge record and source representation must use the same scope"
                    )
                if source_asset_id and str(source_representation["asset_id"]) != source_asset_id:
                    raise ValueError(
                        "Knowledge source representation does not belong to the source Asset"
                    )
            if evidence_pack_id:
                pack_row = conn.execute(
                    "SELECT access_scope FROM evidence_packs WHERE pack_id = ?",
                    (evidence_pack_id,),
                ).fetchone()
                if pack_row is None:
                    raise ValueError(f"Evidence Pack was not found: {evidence_pack_id}")
                if str(pack_row["access_scope"]) != scope:
                    raise ValueError("Knowledge record and Evidence Pack must use the same scope")
            if source_candidate_id:
                candidate_row = conn.execute(
                    """
                    SELECT candidate.evidence_pack_id, pack.access_scope
                    FROM knowledge_write_candidates candidate
                    LEFT JOIN evidence_packs pack ON pack.pack_id = candidate.evidence_pack_id
                    WHERE candidate.candidate_id = ?
                    """,
                    (source_candidate_id,),
                ).fetchone()
                if candidate_row is None:
                    raise ValueError(
                        f"Knowledge write candidate was not found: {source_candidate_id}"
                    )
                candidate_scope = str(candidate_row["access_scope"] or "private")
                if candidate_scope != scope:
                    raise ValueError(
                        "Knowledge record and source candidate must use the same scope"
                    )
                if (
                    evidence_pack_id
                    and candidate_row["evidence_pack_id"] != evidence_pack_id
                ):
                    raise ValueError(
                        "Knowledge source candidate does not belong to the Evidence Pack"
                    )
            revision = 1
            if supersedes_record_id:
                previous = conn.execute(
                    "SELECT revision, access_scope FROM knowledge_records WHERE record_id = ?",
                    (supersedes_record_id,),
                ).fetchone()
                if previous is None:
                    raise ValueError(f"Knowledge record was not found: {supersedes_record_id}")
                if str(previous["access_scope"]) != scope:
                    raise ValueError(
                        "Knowledge record corrections cannot cross an access scope"
                    )
                revision = int(previous["revision"]) + 1
                conn.execute(
                    "UPDATE knowledge_records SET status = 'SUPERSEDED', updated_at = ? WHERE record_id = ?",
                    (now, supersedes_record_id),
                )
            conn.execute(
                """
                INSERT INTO knowledge_records(
                    record_id, kind, statement, status, confidence, source_asset_id,
                    source_representation_id, source_span_json, revision,
                    supersedes_record_id, source_candidate_id, source_job_id,
                    evidence_pack_id, access_scope, created_at, updated_at
                ) VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    normalized_kind,
                    normalized,
                    confidence,
                    source_asset_id,
                    source_representation_id,
                    _json(span),
                    revision,
                    supersedes_record_id,
                    source_candidate_id,
                    source_job_id,
                    evidence_pack_id,
                    scope,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO knowledge_epistemic_annotations(
                    subject_type, subject_id, epistemic_status, trust_class,
                    freshness_expires_at, conflict_refs_json, unknown_refs_json,
                    source_revision, created_at, updated_at
                ) VALUES ('RECORD', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    epistemic.value,
                    trust.value,
                    freshness,
                    _json(list(dict.fromkeys(normalized_conflicts))),
                    _json(list(dict.fromkeys(normalized_unknowns))),
                    str(revision),
                    now,
                    now,
                ),
            )
            self._event(conn, "RECORD_CREATED", "record", record_id, {"revision": revision})
        value = self.record(record_id)
        assert value is not None
        return value

    def save_evidence_pack(self, pack: EvidencePack) -> None:
        pack.verify()
        payload = pack.canonical_payload()
        with self._transaction() as conn:
            for item in pack.items:
                if item.source_type == "representation_chunk":
                    source = conn.execute(
                        """
                        SELECT chunk.asset_id, chunk.representation_id, chunk.content_hash,
                               asset.access_scope, asset.revision AS asset_revision,
                               representation.revision AS representation_revision
                        FROM knowledge_chunks chunk
                        JOIN knowledge_assets asset ON asset.asset_id = chunk.asset_id
                        JOIN knowledge_representations representation
                          ON representation.representation_id = chunk.representation_id
                        WHERE chunk.chunk_id = ?
                        """,
                        (item.source_id,),
                    ).fetchone()
                    if source is None:
                        raise ValueError(
                            f"Evidence Pack source chunk was not found: {item.source_id}"
                        )
                    expected_revision = (
                        f"asset-r{int(source['asset_revision'])}:"
                        f"repr-r{int(source['representation_revision'])}"
                    )
                    if (
                        str(source["access_scope"]) != pack.access_scope
                        or str(source["asset_id"]) != item.asset_id
                        or str(source["representation_id"]) != item.representation_id
                        or str(source["content_hash"]) != item.content_hash
                        or expected_revision != item.source_revision
                    ):
                        raise ValueError(
                            "Evidence Pack source chunk does not match its scope or provenance"
                        )
                elif item.source_type == "knowledge_record":
                    source = conn.execute(
                        """
                        SELECT access_scope, source_asset_id, source_representation_id,
                               revision, statement
                        FROM knowledge_records WHERE record_id = ? AND status = 'ACTIVE'
                        """,
                        (item.source_id,),
                    ).fetchone()
                    if source is None:
                        raise ValueError(
                            f"Evidence Pack source record was not found: {item.source_id}"
                        )
                    source_hash = hashlib.sha256(
                        str(source["statement"]).encode("utf-8")
                    ).hexdigest()
                    if (
                        str(source["access_scope"]) != pack.access_scope
                        or source["source_asset_id"] != item.asset_id
                        or source["source_representation_id"] != item.representation_id
                        or str(source["revision"]) != item.source_revision
                        or source_hash != item.content_hash
                    ):
                        raise ValueError(
                            "Evidence Pack source record does not match its scope or provenance"
                        )
                elif item.source_type == "folder_file":
                    source = conn.execute(
                        """
                        SELECT entry.content_hash, entry.revision, entry.snapshot_asset_id,
                               entry.index_status, folder.access_scope
                        FROM knowledge_folder_entries entry
                        JOIN knowledge_folders folder ON folder.folder_id = entry.folder_id
                        WHERE entry.entry_id = ?
                        """,
                        (item.source_id,),
                    ).fetchone()
                    representation = (
                        conn.execute(
                            """
                            SELECT asset_id FROM knowledge_representations
                            WHERE representation_id = ?
                            """,
                            (item.representation_id,),
                        ).fetchone()
                        if item.representation_id is not None
                        else None
                    )
                    expected_revision = (
                        ""
                        if source is None
                        else (
                            f"folder-entry-r{int(source['revision'])}:"
                            f"{str(source['content_hash'])}"
                        )
                    )
                    if (
                        source is None
                        or str(source["index_status"])
                        != KnowledgeFolderEntryStatus.READY.value
                        or str(source["access_scope"]) != pack.access_scope
                        or str(source["content_hash"]) != item.content_hash
                        or expected_revision != item.source_revision
                        or source["snapshot_asset_id"] != item.asset_id
                        or item.asset_id is None
                        or (
                            item.representation_id is not None
                            and (
                                representation is None
                                or str(representation["asset_id"]) != item.asset_id
                            )
                        )
                    ):
                        raise ValueError(
                            "Evidence Pack folder source does not match its current snapshot"
                        )
                else:
                    raise ValueError(
                        f"Evidence Pack source type is unsupported: {item.source_type}"
                    )
            conn.execute(
                """
                INSERT INTO evidence_packs(
                    pack_id, query, item_count, selected_bytes, candidate_count,
                    payload_json, digest, access_scope, revision,
                    conflict_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack.pack_id,
                    pack.query,
                    len(pack.items),
                    pack.selected_bytes,
                    pack.candidate_count,
                    _json(payload),
                    pack.digest,
                    pack.access_scope,
                    pack.revision,
                    _json(list(pack.conflict_refs)),
                    pack.created_at,
                ),
            )
            for item in pack.items:
                conn.execute(
                    """
                    INSERT INTO evidence_pack_sources(
                        pack_id, evidence_id, asset_id, representation_id,
                        source_type, source_id, source_revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pack.pack_id,
                        item.evidence_id,
                        item.asset_id,
                        item.representation_id,
                        item.source_type,
                        item.source_id,
                        item.source_revision,
                    ),
                )
            self._event(conn, "EVIDENCE_PACK_CREATED", "evidence_pack", pack.pack_id, {"item_count": len(pack.items)})

    @staticmethod
    def _evidence_pack(row: sqlite3.Row) -> EvidencePack:
        value = _loads(row["payload_json"], {})
        pack = EvidencePack(
            pack_id=str(value["pack_id"]),
            query=str(value["query"]),
            items=tuple(
                EvidenceItem(
                    evidence_id=str(item["evidence_id"]),
                    source_type=str(item["source_type"]),
                    source_id=str(item["source_id"]),
                    asset_id=item.get("asset_id"),
                    representation_id=item.get("representation_id"),
                    title=str(item["title"]),
                    excerpt=str(item["excerpt"]),
                    content_hash=str(item["content_hash"]),
                    excerpt_hash=str(item["excerpt_hash"]),
                    source_revision=str(item["source_revision"]),
                    source_created_at=str(item["source_created_at"]),
                    location=dict(item.get("location", {})),
                    confidence=float(item["confidence"]),
                    epistemic_status=EpistemicStatus(
                        str(item.get("epistemic_status", EpistemicStatus.UNKNOWN.value))
                    ),
                    trust_class=ContentTrustClass(
                        str(item.get("trust_class", ContentTrustClass.UNSPECIFIED.value))
                    ),
                    freshness_expires_at=(
                        str(item["freshness_expires_at"])
                        if item.get("freshness_expires_at")
                        else None
                    ),
                    conflict_refs=tuple(
                        str(value) for value in item.get("conflict_refs", [])
                    ),
                    unknown_refs=tuple(
                        str(value) for value in item.get("unknown_refs", [])
                    ),
                    retrieval_basis=tuple(
                        str(value) for value in item.get("retrieval_basis", [])
                    ),
                )
                for item in value.get("items", [])
            ),
            selected_bytes=int(value["selected_bytes"]),
            candidate_count=int(value["candidate_count"]),
            created_at=str(value["created_at"]),
            access_scope=str(value["access_scope"]),
            digest=str(row["digest"]),
            revision=int(value.get("revision", 1)),
            conflict_refs=tuple(str(item) for item in value.get("conflict_refs", [])),
            schema_version=str(value.get("schema_version", "noruct.evidence-pack.v1")),
        )
        pack.verify()
        return pack

    def evidence_pack(self, pack_id: str) -> EvidencePack | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evidence_packs WHERE pack_id = ?", (pack_id,)
            ).fetchone()
        if row is None:
            return None
        return self._evidence_pack(row)

    def create_write_candidate(
        self,
        *,
        job_id: str,
        statement: str,
        kind: str = "JOB_RESULT",
        evidence_pack_id: str | None = None,
    ) -> KnowledgeWriteCandidate:
        normalized_job_id = _bounded_text(job_id, "Knowledge candidate Job id", 256)
        normalized = _bounded_text(
            statement, "Knowledge write candidate statement", 64_000
        )
        normalized_kind = _bounded_text(
            kind or "JOB_RESULT", "Knowledge write candidate kind", 128
        ).upper()
        if evidence_pack_id is not None:
            evidence_pack_id = _bounded_text(
                evidence_pack_id, "Knowledge candidate Evidence Pack id", 256
            )
        candidate_id = f"candidate-{uuid.uuid4()}"
        now = _now()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM knowledge_write_candidates WHERE job_id = ? AND kind = ?",
                (normalized_job_id, normalized_kind),
            ).fetchone()
            if existing is not None:
                if str(existing["statement"]) != normalized or existing["evidence_pack_id"] != evidence_pack_id:
                    raise ValueError("Job already has a different Knowledge write candidate")
                return self._write_candidate(existing)
            conn.execute(
                """
                INSERT INTO knowledge_write_candidates(
                    candidate_id, job_id, kind, statement, evidence_pack_id,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    candidate_id,
                    normalized_job_id,
                    normalized_kind,
                    normalized,
                    evidence_pack_id,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO knowledge_epistemic_annotations(
                    subject_type, subject_id, epistemic_status, trust_class,
                    conflict_refs_json, unknown_refs_json, source_revision,
                    created_at, updated_at
                ) VALUES ('WRITE_CANDIDATE', ?, 'INFERRED', 'MODEL_GENERATED',
                          '[]', '[]', '1', ?, ?)
                """,
                (candidate_id, now, now),
            )
            self._event(
                conn,
                "WRITE_CANDIDATE_CREATED",
                "candidate",
                candidate_id,
                {"job_id": normalized_job_id},
            )
        value = self.write_candidate(candidate_id)
        assert value is not None
        return value

    @staticmethod
    def _execution_binding(row: sqlite3.Row) -> KnowledgeExecutionBinding:
        return KnowledgeExecutionBinding(
            binding_id=str(row["binding_id"]),
            request_id=str(row["request_id"]),
            job_id=str(row["job_id"]),
            intent_id=str(row["intent_id"]),
            intent_revision=int(row["intent_revision"]),
            intent_hash=str(row["intent_hash"]),
            pack_id=str(row["pack_id"]),
            pack_revision=int(row["pack_revision"]),
            pack_digest=str(row["pack_digest"]),
            delivery_digest=str(row["delivery_digest"]),
            item_count=int(row["item_count"]),
            selected_bytes=int(row["selected_bytes"]),
            access_scope=str(row["access_scope"]),
            status=str(row["status"]),
            job_status=str(row["job_status"]),
            candidate_id=(str(row["candidate_id"]) if row["candidate_id"] else None),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def prepare_execution_binding(
        self,
        *,
        request_id: str,
        job_id: str,
        intent_id: str,
        intent_revision: int,
        intent_hash: str,
        pack_id: str,
        pack_revision: int,
        pack_digest: str,
        delivery_digest: str,
        item_count: int,
        selected_bytes: int,
        access_scope: str,
    ) -> KnowledgeExecutionBinding:
        """Persist a content-free PREPARED record before Company execution starts."""

        if not request_id.startswith("request-") or not job_id.startswith("job-"):
            raise ValueError("Knowledge execution binding request or Job identity is invalid")
        if intent_revision < 1 or pack_revision < 1 or item_count < 0 or selected_bytes < 0:
            raise ValueError("Knowledge execution binding counters are invalid")
        for digest in (intent_hash, pack_digest, delivery_digest):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("Knowledge execution binding digest is invalid")
        scope = access_scope.strip()
        if not scope:
            raise ValueError("Knowledge execution binding scope is required")
        binding_id = f"binding-{uuid.uuid4()}"
        now = _now()
        expected = (
            request_id,
            job_id,
            intent_id,
            intent_revision,
            intent_hash,
            pack_id,
            pack_revision,
            pack_digest,
            delivery_digest,
            item_count,
            selected_bytes,
            scope,
        )
        with self._transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM knowledge_execution_bindings
                WHERE request_id = ? OR job_id = ?
                """,
                (request_id, job_id),
            ).fetchone()
            if existing is not None:
                observed = (
                    existing["request_id"],
                    existing["job_id"],
                    existing["intent_id"],
                    existing["intent_revision"],
                    existing["intent_hash"],
                    existing["pack_id"],
                    existing["pack_revision"],
                    existing["pack_digest"],
                    existing["delivery_digest"],
                    existing["item_count"],
                    existing["selected_bytes"],
                    existing["access_scope"],
                )
                if observed != expected:
                    raise ValueError("Request or Job is already bound to different knowledge")
                return self._execution_binding(existing)
            intent_revision_row = conn.execute(
                """
                SELECT current.*, history.payload_json AS revision_payload_json,
                       history.content_hash AS revision_content_hash
                FROM knowledge_intents current
                JOIN knowledge_intent_revisions history
                  ON history.intent_id = current.intent_id
                 AND history.revision = current.revision
                WHERE current.intent_id = ? AND current.revision = ?
                """,
                (intent_id, intent_revision),
            ).fetchone()
            if intent_revision_row is None:
                raise ValueError("Knowledge execution binding Intent revision is unavailable or changed")
            history_payload = _loads(intent_revision_row["revision_payload_json"], {})
            history_hash = hashlib.sha256(_json(history_payload).encode("utf-8")).hexdigest()
            if (
                not isinstance(history_payload, dict)
                or history_hash != str(intent_revision_row["revision_content_hash"])
                or history_hash != intent_hash
                or history_payload != self._intent_revision_payload(intent_revision_row)
            ):
                raise ValueError("Knowledge execution binding Intent revision is unavailable or changed")
            pack = conn.execute(
                """
                SELECT *
                FROM evidence_packs WHERE pack_id = ?
                """,
                (pack_id,),
            ).fetchone()
            persisted_pack = self._evidence_pack(pack) if pack is not None else None
            expected_delivery = (
                runtime_delivery_from_evidence_pack(persisted_pack)
                if persisted_pack is not None
                else None
            )
            if persisted_pack is None or (
                persisted_pack.revision,
                persisted_pack.digest,
                expected_delivery.delivery_digest,
                len(expected_delivery.items),
                expected_delivery.selected_bytes,
                persisted_pack.access_scope,
            ) != (
                pack_revision,
                pack_digest,
                delivery_digest,
                item_count,
                selected_bytes,
                scope,
            ):
                raise ValueError("Knowledge execution binding Evidence Pack is unavailable or changed")
            conn.execute(
                """
                INSERT INTO knowledge_execution_bindings(
                    binding_id, request_id, job_id, intent_id, intent_revision,
                    intent_hash, pack_id, pack_revision, pack_digest, delivery_digest,
                    item_count, selected_bytes, access_scope, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?)
                """,
                (binding_id, *expected, now, now),
            )
            self._event(
                conn,
                "KNOWLEDGE_EXECUTION_PREPARED",
                "execution_binding",
                binding_id,
                {"request_id": request_id, "job_id": job_id},
            )
        value = self.execution_binding(binding_id)
        assert value is not None
        return value

    def execution_binding(self, binding_id: str) -> KnowledgeExecutionBinding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_execution_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        return None if row is None else self._execution_binding(row)

    def execution_binding_for_job(self, job_id: str) -> KnowledgeExecutionBinding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_execution_bindings WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return None if row is None else self._execution_binding(row)

    def complete_execution_binding(
        self,
        binding_id: str,
        *,
        job_status: str,
        candidate_id: str | None = None,
    ) -> KnowledgeExecutionBinding:
        normalized_status = job_status.strip().upper()
        if normalized_status not in {
            "SUCCEEDED",
            "FAILED",
            "STALLED",
            "BUDGET_EXHAUSTED",
            "CANCELLED",
            "INTERRUPTED",
        }:
            raise ValueError("Knowledge execution binding Job status is invalid")
        now = _now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_execution_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Knowledge execution binding was not found: {binding_id}")
            if str(row["status"]) != "PREPARED":
                if (
                    str(row["status"]) == "TERMINAL"
                    and str(row["job_status"]) == normalized_status
                    and row["candidate_id"] == candidate_id
                ):
                    return self._execution_binding(row)
                raise ValueError("Knowledge execution binding is already terminal")
            if candidate_id is not None:
                candidate = conn.execute(
                    "SELECT job_id FROM knowledge_write_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
                if candidate is None or str(candidate["job_id"]) != str(row["job_id"]):
                    raise ValueError("Knowledge write candidate does not belong to the bound Job")
            conn.execute(
                """
                UPDATE knowledge_execution_bindings
                SET status = 'TERMINAL', job_status = ?, candidate_id = ?, updated_at = ?
                WHERE binding_id = ?
                """,
                (normalized_status, candidate_id, now, binding_id),
            )
            self._event(
                conn,
                "KNOWLEDGE_EXECUTION_TERMINAL",
                "execution_binding",
                binding_id,
                {"job_status": normalized_status, "candidate_created": candidate_id is not None},
            )
        value = self.execution_binding(binding_id)
        assert value is not None
        return value

    def finalize_execution(
        self,
        binding_id: str,
        *,
        job_status: str,
        candidate_statement: str = "",
    ) -> tuple[KnowledgeExecutionBinding, KnowledgeWriteCandidate | None]:
        """Atomically terminalize a binding and create its optional result candidate."""

        normalized_status = job_status.strip().upper()
        if normalized_status not in {
            "SUCCEEDED",
            "FAILED",
            "STALLED",
            "BUDGET_EXHAUSTED",
            "CANCELLED",
            "INTERRUPTED",
        }:
            raise ValueError("Knowledge execution binding Job status is invalid")
        statement = candidate_statement.strip() if normalized_status == "SUCCEEDED" else ""
        if len(statement.encode("utf-8")) > 64_000:
            raise ValueError("Knowledge write candidate exceeds the 64000 byte limit")
        now = _now()
        candidate_id: str | None = None
        with self._transaction() as conn:
            binding = conn.execute(
                "SELECT * FROM knowledge_execution_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if binding is None:
                raise ValueError(f"Knowledge execution binding was not found: {binding_id}")
            if statement:
                candidate = conn.execute(
                    """
                    SELECT * FROM knowledge_write_candidates
                    WHERE job_id = ? AND kind = 'JOB_RESULT'
                    """,
                    (binding["job_id"],),
                ).fetchone()
                if candidate is None:
                    candidate_id = f"candidate-{uuid.uuid4()}"
                    conn.execute(
                        """
                        INSERT INTO knowledge_write_candidates(
                            candidate_id, job_id, kind, statement, evidence_pack_id,
                            status, created_at
                        ) VALUES (?, ?, 'JOB_RESULT', ?, ?, 'PENDING', ?)
                        """,
                        (
                            candidate_id,
                            binding["job_id"],
                            statement,
                            binding["pack_id"],
                            now,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO knowledge_epistemic_annotations(
                            subject_type, subject_id, epistemic_status, trust_class,
                            conflict_refs_json, unknown_refs_json, source_revision,
                            created_at, updated_at
                        ) VALUES ('WRITE_CANDIDATE', ?, 'INFERRED',
                                  'MODEL_GENERATED', '[]', '[]', '1', ?, ?)
                        """,
                        (candidate_id, now, now),
                    )
                    self._event(
                        conn,
                        "WRITE_CANDIDATE_CREATED",
                        "candidate",
                        candidate_id,
                        {"job_id": str(binding["job_id"])},
                    )
                else:
                    if (
                        str(candidate["statement"]) != statement
                        or candidate["evidence_pack_id"] != binding["pack_id"]
                    ):
                        raise ValueError("Job already has a different Knowledge write candidate")
                    candidate_id = str(candidate["candidate_id"])
            if str(binding["status"]) == "TERMINAL":
                if (
                    str(binding["job_status"]) != normalized_status
                    or binding["candidate_id"] != candidate_id
                ):
                    raise ValueError("Knowledge execution binding is already terminal")
            else:
                conn.execute(
                    """
                    UPDATE knowledge_execution_bindings
                    SET status = 'TERMINAL', job_status = ?, candidate_id = ?, updated_at = ?
                    WHERE binding_id = ? AND status = 'PREPARED'
                    """,
                    (normalized_status, candidate_id, now, binding_id),
                )
                self._event(
                    conn,
                    "KNOWLEDGE_EXECUTION_TERMINAL",
                    "execution_binding",
                    binding_id,
                    {"job_status": normalized_status, "candidate_created": candidate_id is not None},
                )
        terminal = self.execution_binding(binding_id)
        assert terminal is not None
        candidate_value = self.write_candidate(candidate_id) if candidate_id is not None else None
        return terminal, candidate_value

    def list_execution_bindings(
        self, *, status: str | None = None, limit: int = 100
    ) -> tuple[KnowledgeExecutionBinding, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Knowledge execution binding limit must be between 1 and 1000")
        query = "SELECT * FROM knowledge_execution_bindings"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.strip().upper())
        query += " ORDER BY created_at DESC, binding_id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return tuple(self._execution_binding(row) for row in rows)

