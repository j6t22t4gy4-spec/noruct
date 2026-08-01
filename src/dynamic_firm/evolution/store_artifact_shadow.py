"""Append-only local shadow-evaluation receipts for Evolution Artifacts."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Mapping

from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.runtime.models import utc_now

from .shadow_evaluation import (
    ARTIFACT_SHADOW_RECEIPT_SCHEMA,
    SHADOW_TERMINAL_STATES,
    ShadowEvaluationIntegrityError,
    artifact_contract_digest,
    artifact_required_capabilities,
    artifact_required_capabilities_digest,
    build_shadow_slot,
    canonical_decimal,
    decide_shadow_result,
    receipt_digest,
    require_safe_id,
    require_semver,
    require_sha256,
    shadow_slot_digest,
)


class EvolutionArtifactShadowMixin:
    """Own immutable evaluation evidence without provider or Network access."""

    @staticmethod
    def _shadow_receipt_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "schema": ARTIFACT_SHADOW_RECEIPT_SCHEMA,
            "receipt_id": str(row["receipt_id"]),
            "slot_digest": str(row["slot_digest"]),
            "scope_key": str(row["scope_key"]),
            "kind": str(row["kind"]),
            "artifact_id": str(row["artifact_id"]),
            "base_version": str(row["base_version"]),
            "base_manifest_digest": str(row["base_manifest_digest"]),
            "base_contract_digest": str(row["base_contract_digest"]),
            "base_required_capabilities_digest": str(
                row["base_required_capabilities_digest"]
            ),
            "candidate_version": str(row["candidate_version"]),
            "candidate_manifest_digest": str(row["candidate_manifest_digest"]),
            "candidate_contract_digest": str(row["candidate_contract_digest"]),
            "candidate_required_capabilities_digest": str(
                row["candidate_required_capabilities_digest"]
            ),
            "fixture_kind": str(row["fixture_kind"]),
            "fixture_id": str(row["fixture_id"]),
            "fixture_version": str(row["fixture_version"]),
            "fixture_digest": str(row["fixture_digest"]),
            "baseline_quality": str(row["baseline_quality"]),
            "candidate_quality": str(row["candidate_quality"]),
            "baseline_safety": str(row["baseline_safety"]),
            "candidate_safety": str(row["candidate_safety"]),
            "baseline_cost": str(row["baseline_cost"]),
            "candidate_cost": str(row["candidate_cost"]),
            "cost_ceiling": str(row["cost_ceiling"]),
            "terminal_state": str(row["terminal_state"]),
            "complete": bool(row["complete"]),
            "attempt_count": int(row["attempt_count"]),
            "failure_count": int(row["failure_count"]),
            "failure_history_digest": str(row["failure_history_digest"]),
            "result": str(row["result"]),
            "recorded_at": str(row["recorded_at"]),
        }

    @classmethod
    def _shadow_receipt_row(
        cls, row: sqlite3.Row | Mapping[str, Any] | None
    ) -> Mapping[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        if result.get("schema") != ARTIFACT_SHADOW_RECEIPT_SCHEMA:
            raise ShadowEvaluationIntegrityError(
                "Artifact shadow receipt schema verification failed"
            )
        slot = build_shadow_slot(
            scope_key=str(result["scope_key"]),
            kind=str(result["kind"]),
            artifact_id=str(result["artifact_id"]),
            base_version=str(result["base_version"]),
            base_manifest_digest=str(result["base_manifest_digest"]),
            base_contract_digest=str(result["base_contract_digest"]),
            base_required_capabilities_digest=str(
                result["base_required_capabilities_digest"]
            ),
            candidate_version=str(result["candidate_version"]),
            candidate_manifest_digest=str(result["candidate_manifest_digest"]),
            candidate_contract_digest=str(result["candidate_contract_digest"]),
            candidate_required_capabilities_digest=str(
                result["candidate_required_capabilities_digest"]
            ),
            fixture_kind=str(result["fixture_kind"]),
            fixture_id=str(result["fixture_id"]),
            fixture_version=str(result["fixture_version"]),
            fixture_digest=str(result["fixture_digest"]),
        )
        if shadow_slot_digest(slot) != str(result.get("slot_digest", "")):
            raise ShadowEvaluationIntegrityError(
                "Artifact shadow slot digest verification failed"
            )
        expected = receipt_digest(cls._shadow_receipt_payload(result))
        if expected != str(result.get("receipt_digest", "")):
            raise ShadowEvaluationIntegrityError(
                "Artifact shadow receipt digest verification failed"
            )
        result["sequence"] = int(result["sequence"])
        result["complete"] = bool(result["complete"])
        return result

    def record_artifact_shadow_evaluation(
        self,
        *,
        scope_key: str,
        base: Mapping[str, Any],
        candidate: Mapping[str, Any],
        fixture_kind: str,
        fixture_id: str,
        fixture_version: str,
        fixture_digest: str,
        baseline_quality: object,
        candidate_quality: object,
        baseline_safety: object,
        candidate_safety: object,
        baseline_cost: object,
        candidate_cost: object,
        cost_ceiling: object,
        terminal_state: str,
        complete: bool,
        attempt_count: int,
        failure_count: int,
        failure_history_digest: str,
    ) -> Mapping[str, Any]:
        """Append one exact receipt; its decision is derived, never supplied."""

        scope = require_safe_id(scope_key, "scope_key")
        if base.get("artifact_id") != candidate.get("artifact_id"):
            raise ValueError("Shadow evaluation base and candidate artifact ids differ")
        if base.get("kind") != candidate.get("kind"):
            raise ValueError("Shadow evaluation base and candidate kinds differ")
        artifact_id = require_safe_id(base.get("artifact_id"), "artifact_id")
        kind = str(base.get("kind", ""))
        base_version = require_semver(base.get("version"), "base_version")
        candidate_version = require_semver(
            candidate.get("version"), "candidate_version"
        )
        base_manifest_digest = require_sha256(
            base.get("manifest_digest"), "base_manifest_digest"
        )
        candidate_manifest_digest = require_sha256(
            candidate.get("manifest_digest"), "candidate_manifest_digest"
        )
        base_manifest = base.get("manifest")
        candidate_manifest = candidate.get("manifest")
        if not isinstance(base_manifest, Mapping) or not isinstance(
            candidate_manifest, Mapping
        ):
            raise ValueError("Shadow evaluation requires exact catalog manifests")

        base_contract = artifact_contract_digest(base_manifest)
        candidate_contract = artifact_contract_digest(candidate_manifest)
        base_capabilities = artifact_required_capabilities(base_manifest)
        candidate_capabilities = artifact_required_capabilities(candidate_manifest)
        base_capabilities_digest = artifact_required_capabilities_digest(base_manifest)
        candidate_capabilities_digest = artifact_required_capabilities_digest(
            candidate_manifest
        )

        quality_before = canonical_decimal(
            baseline_quality, "baseline_quality", maximum=1
        )
        quality_after = canonical_decimal(
            candidate_quality, "candidate_quality", maximum=1
        )
        safety_before = canonical_decimal(
            baseline_safety, "baseline_safety", maximum=1
        )
        safety_after = canonical_decimal(
            candidate_safety, "candidate_safety", maximum=1
        )
        cost_before = canonical_decimal(baseline_cost, "baseline_cost")
        cost_after = canonical_decimal(candidate_cost, "candidate_cost")
        ceiling = canonical_decimal(cost_ceiling, "cost_ceiling")
        if terminal_state not in SHADOW_TERMINAL_STATES:
            raise ValueError("Shadow terminal state is invalid")
        if not isinstance(complete, bool):
            raise ValueError("Shadow complete must be a boolean")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or not 1 <= attempt_count <= 100
        ):
            raise ValueError("Shadow attempt_count must be from 1 to 100")
        if (
            isinstance(failure_count, bool)
            or not isinstance(failure_count, int)
            or not 0 <= failure_count <= attempt_count
        ):
            raise ValueError("Shadow failure_count must be from 0 to attempt_count")
        history_digest = require_sha256(
            failure_history_digest, "failure_history_digest"
        )
        slot = build_shadow_slot(
            scope_key=scope,
            kind=kind,
            artifact_id=artifact_id,
            base_version=base_version,
            base_manifest_digest=base_manifest_digest,
            base_contract_digest=base_contract,
            base_required_capabilities_digest=base_capabilities_digest,
            candidate_version=candidate_version,
            candidate_manifest_digest=candidate_manifest_digest,
            candidate_contract_digest=candidate_contract,
            candidate_required_capabilities_digest=candidate_capabilities_digest,
            fixture_kind=fixture_kind,
            fixture_id=fixture_id,
            fixture_version=fixture_version,
            fixture_digest=fixture_digest,
        )
        result = decide_shadow_result(
            base_contract_digest=base_contract,
            candidate_contract_digest=candidate_contract,
            base_required_capabilities=base_capabilities,
            candidate_required_capabilities=candidate_capabilities,
            terminal_state=terminal_state,
            complete=complete,
            baseline_quality=quality_before,
            candidate_quality=quality_after,
            baseline_safety=safety_before,
            candidate_safety=safety_after,
            candidate_cost=cost_after,
            cost_ceiling=ceiling,
        )
        receipt_id = f"artifact-shadow-receipt-{uuid.uuid4()}"
        recorded_at = utc_now().isoformat()
        payload = {
            "schema": ARTIFACT_SHADOW_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "slot_digest": shadow_slot_digest(slot),
            "scope_key": scope,
            "kind": kind,
            "artifact_id": artifact_id,
            "base_version": base_version,
            "base_manifest_digest": base_manifest_digest,
            "base_contract_digest": base_contract,
            "base_required_capabilities_digest": base_capabilities_digest,
            "candidate_version": candidate_version,
            "candidate_manifest_digest": candidate_manifest_digest,
            "candidate_contract_digest": candidate_contract,
            "candidate_required_capabilities_digest": candidate_capabilities_digest,
            "fixture_kind": fixture_kind,
            "fixture_id": fixture_id,
            "fixture_version": fixture_version,
            "fixture_digest": require_sha256(fixture_digest, "fixture_digest"),
            "baseline_quality": quality_before,
            "candidate_quality": quality_after,
            "baseline_safety": safety_before,
            "candidate_safety": safety_after,
            "baseline_cost": cost_before,
            "candidate_cost": cost_after,
            "cost_ceiling": ceiling,
            "terminal_state": terminal_state,
            "complete": complete,
            "attempt_count": attempt_count,
            "failure_count": failure_count,
            "failure_history_digest": history_digest,
            "result": result,
            "recorded_at": recorded_at,
        }
        digest = receipt_digest(payload)
        columns = tuple(payload)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO evolution_artifact_shadow_receipts("
                + ", ".join(columns)
                + ", receipt_digest) VALUES("
                + ", ".join("?" for _ in range(len(columns) + 1))
                + ")",
                tuple(int(value) if key == "complete" else value for key, value in payload.items())
                + (digest,),
            )
            event = {
                "receipt_id": receipt_id,
                "slot_digest": payload["slot_digest"],
                "artifact_id": artifact_id,
                "base_version": base_version,
                "candidate_version": candidate_version,
                "result": result,
                "receipt_digest": digest,
            }
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(
                    event_type, subject_id, payload_json, payload_digest, recorded_at
                ) VALUES('EVOLUTION_ARTIFACT_SHADOW_EVALUATED', ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    canonical_json(event),
                    content_digest(event),
                    recorded_at,
                ),
            )
        return self.get_artifact_shadow_receipt(receipt_id)

    def get_artifact_shadow_receipt(self, receipt_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evolution_artifact_shadow_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        result = self._shadow_receipt_row(row)
        if result is None:
            raise KeyError(f"Unknown Artifact shadow receipt: {receipt_id}")
        return result

    def list_artifact_shadow_receipts(
        self,
        *,
        scope_key: str | None = None,
        artifact_id: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if scope_key is not None:
            clauses.append("scope_key = ?")
            parameters.append(scope_key)
        if artifact_id is not None:
            clauses.append("artifact_id = ?")
            parameters.append(artifact_id)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM evolution_artifact_shadow_receipts"
                + where
                + " ORDER BY sequence",
                tuple(parameters),
            ).fetchall()
        return tuple(self._shadow_receipt_row(row) for row in rows)

    def latest_exact_artifact_shadow_receipt(
        self,
        *,
        scope_key: str,
        base: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any] | None, bool]:
        """Return latest exact receipt and whether stale candidate history exists."""

        receipts = self.list_artifact_shadow_receipts(
            scope_key=scope_key, artifact_id=str(candidate["artifact_id"])
        )
        exact = tuple(
            receipt
            for receipt in receipts
            if receipt["base_version"] == base["version"]
            and receipt["base_manifest_digest"] == base["manifest_digest"]
            and receipt["candidate_version"] == candidate["version"]
            and receipt["candidate_manifest_digest"] == candidate["manifest_digest"]
            and receipt["base_contract_digest"]
            == artifact_contract_digest(base["manifest"])
            and receipt["candidate_contract_digest"]
            == artifact_contract_digest(candidate["manifest"])
            and receipt["base_required_capabilities_digest"]
            == artifact_required_capabilities_digest(base["manifest"])
            and receipt["candidate_required_capabilities_digest"]
            == artifact_required_capabilities_digest(candidate["manifest"])
        )
        stale_history = any(
            receipt["candidate_version"] == candidate["version"] for receipt in receipts
        )
        return (None if not exact else exact[-1], stale_history and not exact)
