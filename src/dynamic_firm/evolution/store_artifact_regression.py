"""Append-only, content-free post-activation regression signals.

Signals never deactivate an Artifact by themselves.  They only make an exact
rollback proposal visible while the reported activation is still current.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Mapping

from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.runtime.models import utc_now

from .shadow_evaluation import require_safe_id, require_sha256


ARTIFACT_REGRESSION_SIGNAL_SCHEMA = "noruct.artifact-regression-signal.v1"
ARTIFACT_REGRESSION_PROJECTION_SCHEMA = "noruct.artifact-regression-projection.v1"
_SIGNAL_KINDS = frozenset({
    "QUALITY_REGRESSION", "SAFETY_REGRESSION", "EFFECT_FAILURE", "OPERATOR_INTERVENTION",
})


class ArtifactRegressionIntegrityError(ValueError):
    """A persisted signal no longer matches its immutable receipt."""


class EvolutionArtifactRegressionMixin:
    """Persist observation receipts without becoming activation authority."""

    @staticmethod
    def _signal_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "schema": ARTIFACT_REGRESSION_SIGNAL_SCHEMA,
            "signal_id": str(row["signal_id"]),
            "scope_key": str(row["scope_key"]),
            "artifact_id": str(row["artifact_id"]),
            "activation_id": str(row["activation_id"]),
            "signal_kind": str(row["signal_kind"]),
            "evidence_digest": str(row["evidence_digest"]),
            "recorded_at": str(row["recorded_at"]),
        }

    def record_artifact_regression_signal(
        self,
        *,
        scope_key: str,
        artifact_id: str,
        signal_kind: str,
        evidence_digest: str,
    ) -> Mapping[str, Any]:
        """Append one operator-observed signal for the current exact activation."""

        scope = require_safe_id(scope_key, "scope_key")
        subject = require_safe_id(artifact_id, "artifact_id")
        if signal_kind not in _SIGNAL_KINDS:
            raise ValueError("Artifact regression signal kind is invalid")
        evidence = require_sha256(evidence_digest, "evidence_digest")
        with self._transaction() as connection:
            activation = connection.execute(
                """
                SELECT activation_id FROM evolution_artifact_activations
                 WHERE scope_key = ? AND artifact_id = ? AND status = 'ACTIVE'
                """,
                (scope, subject),
            ).fetchone()
            if activation is None:
                raise ValueError("Artifact regression signal requires an active exact Artifact")
            signal_id = f"artifact-regression-{uuid.uuid4()}"
            recorded_at = utc_now().isoformat()
            payload = {
                "schema": ARTIFACT_REGRESSION_SIGNAL_SCHEMA,
                "signal_id": signal_id,
                "scope_key": scope,
                "artifact_id": subject,
                "activation_id": str(activation["activation_id"]),
                "signal_kind": signal_kind,
                "evidence_digest": evidence,
                "recorded_at": recorded_at,
            }
            receipt_digest = content_digest(payload)
            connection.execute(
                """
                INSERT INTO evolution_artifact_regression_signals(
                    schema, signal_id, scope_key, artifact_id, activation_id,
                    signal_kind, evidence_digest, recorded_at, receipt_digest
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ARTIFACT_REGRESSION_SIGNAL_SCHEMA, signal_id, scope, subject,
                    payload["activation_id"], signal_kind, evidence, recorded_at,
                    receipt_digest,
                ),
            )
            event = {"signal_id": signal_id, "receipt_digest": receipt_digest}
            connection.execute(
                """
                INSERT INTO evolution_evidence_events(event_type, subject_id, payload_json, payload_digest, recorded_at)
                VALUES('EVOLUTION_ARTIFACT_REGRESSION_SIGNALED', ?, ?, ?, ?)
                """,
                (signal_id, canonical_json(event), content_digest(event), recorded_at),
            )
        return self.get_artifact_regression_signal(signal_id)

    def get_artifact_regression_signal(self, signal_id: str) -> Mapping[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evolution_artifact_regression_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown Artifact regression signal: {signal_id}")
        result = dict(row)
        payload = self._signal_payload(result)
        if result["schema"] != ARTIFACT_REGRESSION_SIGNAL_SCHEMA or result["receipt_digest"] != content_digest(payload):
            raise ArtifactRegressionIntegrityError("Artifact regression signal receipt is tampered")
        return {**payload, "receipt_digest": str(result["receipt_digest"])}

    def list_artifact_regression_signals(
        self, *, scope_key: str | None = None, artifact_id: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if scope_key is not None:
            clauses.append("scope_key = ?")
            parameters.append(require_safe_id(scope_key, "scope_key"))
        if artifact_id is not None:
            clauses.append("artifact_id = ?")
            parameters.append(require_safe_id(artifact_id, "artifact_id"))
        query = "SELECT signal_id FROM evolution_artifact_regression_signals"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence, signal_id"
        with self._lock:
            rows = self._conn.execute(query, tuple(parameters)).fetchall()
        return tuple(self.get_artifact_regression_signal(str(row["signal_id"])) for row in rows)
