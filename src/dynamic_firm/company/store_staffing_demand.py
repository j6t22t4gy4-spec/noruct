"""Staffing-demand evidence lifecycle composed into CompanyStateStore."""

from __future__ import annotations

import json
import sqlite3

from .models import StaffingDemandEvidence, canonical_json, content_digest, staffing_demand_from_dict


def _loads(raw: str) -> object:
    return json.loads(raw)


class CompanyStaffingDemandMixin:
    """Persist bounded staffing-demand evidence on the owning Store connection."""


    @staticmethod
    def _validate_staffing_demand_content(evidence: StaffingDemandEvidence) -> None:
        if evidence.evidence_id != f"staffing-demand-{evidence.content_hash[:24]}":
            raise ValueError("Staffing demand id does not match its content hash")
        if (
            not evidence.task_id.strip()
            or not evidence.role_label.strip()
            or not evidence.capability.strip()
            or evidence.capability != evidence.capability.strip().casefold()
            or evidence.base_roster_revision < 1
        ):
            raise ValueError("Staffing demand contains invalid bounded identity fields")
        if content_digest(evidence.content_payload()) != evidence.content_hash:
            raise ValueError(
                f"Staffing demand content hash mismatch: {evidence.evidence_id}"
            )

    def _staffing_demand_from_row(self, row: sqlite3.Row) -> StaffingDemandEvidence:
        evidence = staffing_demand_from_dict(_loads(row["payload_json"]))
        self._validate_staffing_demand_content(evidence)
        if evidence.content_hash != row["content_hash"]:
            raise RuntimeError(
                f"Staffing demand stored hash mismatch: {evidence.evidence_id}"
            )
        return evidence

    def record_staffing_demand(
        self,
        evidence: StaffingDemandEvidence,
    ) -> tuple[StaffingDemandEvidence, bool]:
        self._validate_staffing_demand_content(evidence)
        with self._transaction() as conn:
            episode_row = conn.execute(
                "SELECT * FROM organization_episodes WHERE episode_id = ?",
                (evidence.episode_id,),
            ).fetchone()
            if episode_row is None:
                raise ValueError(
                    f"Staffing demand episode does not exist: {evidence.episode_id}"
                )
            episode = self._episode_from_row(episode_row)
            expected = (
                episode.job_id,
                episode.source,
                episode.context_fingerprint,
                episode.execution_profile,
                episode.ledger_digest,
            )
            actual = (
                evidence.job_id,
                evidence.source,
                evidence.context_fingerprint,
                evidence.execution_profile,
                evidence.ledger_digest,
            )
            if actual != expected:
                raise ValueError("Staffing demand does not match its source episode")
            existing = conn.execute(
                """
                SELECT * FROM staffing_demand_evidence
                WHERE episode_id = ? AND capability = ?
                """,
                (evidence.episode_id, evidence.capability),
            ).fetchone()
            if existing is not None:
                stored = self._staffing_demand_from_row(existing)
                if stored.content_hash != evidence.content_hash:
                    raise ValueError(
                        "Staffing demand episode/capability was reused with different content"
                    )
                return stored, False
            conn.execute(
                """
                INSERT INTO staffing_demand_evidence(
                    evidence_id, episode_id, job_id, source_kind,
                    context_fingerprint, execution_profile, base_roster_revision,
                    capability, payload_json, content_hash, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.episode_id,
                    evidence.job_id,
                    evidence.source.value,
                    evidence.context_fingerprint,
                    evidence.execution_profile,
                    evidence.base_roster_revision,
                    evidence.capability,
                    canonical_json(evidence),
                    evidence.content_hash,
                    evidence.recorded_at,
                ),
            )
        return evidence, True

    def get_staffing_demand(self, evidence_id: str) -> StaffingDemandEvidence:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM staffing_demand_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown staffing demand evidence: {evidence_id}")
        return self._staffing_demand_from_row(row)

    def list_staffing_demands(self) -> tuple[StaffingDemandEvidence, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM staffing_demand_evidence
                ORDER BY recorded_at, evidence_id
                """
            ).fetchall()
        return tuple(self._staffing_demand_from_row(row) for row in rows)

