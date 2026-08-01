"""Immutable verified live-evidence import and audit projection."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from dynamic_firm.runtime.models import utc_now

from .models import OrganizationEpisode, canonical_json, content_digest


_IDENTITY_FIELDS = (
    "campaign_id", "baseline_run_id", "dynamic_run_id", "baseline_evidence_id",
    "dynamic_evidence_id", "baseline_content_hash", "dynamic_content_hash",
    "source_revision", "fixture", "provider_kind", "model_id",
    "baseline_quality_score", "dynamic_quality_score", "baseline_model_calls", "dynamic_model_calls",
)


class CompanyLiveEvidenceMixin:
    @staticmethod
    def _live_evidence_conflicts_in(conn: sqlite3.Connection, pair) -> tuple[str, ...]:
        checks = (
            ("pair_id", ("pair_id",), (pair.pair_id,)),
            ("evaluation_run_id", ("baseline_run_id", "dynamic_run_id"), (pair.baseline_run_id, pair.dynamic_run_id)),
            ("evidence_id", ("baseline_evidence_id", "dynamic_evidence_id"), (pair.baseline_evidence_id, pair.dynamic_evidence_id)),
            ("record_content_hash", ("baseline_content_hash", "dynamic_content_hash"), (pair.baseline_content_hash, pair.dynamic_content_hash)),
            ("pair_content_hash", ("content_hash",), (pair.content_hash,)),
        )
        conflicts: list[str] = []
        for label, fields, values in checks:
            predicate = " OR ".join(f"{field} = ?" for field in fields)
            for value in values:
                row = conn.execute(
                    f"SELECT pair_id FROM verified_live_evidence_pairs WHERE {predicate}",
                    tuple(value for _ in fields),
                ).fetchone()
                if row:
                    conflicts.append(f"{label}:{row['pair_id']}")
        return tuple(conflicts)

    def live_evidence_conflicts(self, pair) -> tuple[str, ...]:
        with self._lock:
            return self._live_evidence_conflicts_in(self._conn, pair)

    def import_live_evidence_pair(self, pair) -> OrganizationEpisode:
        if content_digest(pair.content_payload()) != pair.content_hash:
            raise ValueError("Verified live evidence pair content hash does not match payload")
        episode = pair.episode
        episode_payload = canonical_json(episode)
        episode_digest = content_digest(episode.content_payload())
        pair_payload = canonical_json({"pair_id": pair.pair_id, "content_hash": pair.content_hash, **pair.content_payload(), "episode_id": episode.episode_id})
        with self._transaction() as conn:
            conflicts = self._live_evidence_conflicts_in(conn, pair)
            if conflicts:
                raise ValueError("Live evidence pair is duplicate: " + ", ".join(conflicts))
            existing_episode = conn.execute("SELECT episode_id FROM organization_episodes WHERE job_id = ? OR episode_id = ?", (episode.job_id, episode.episode_id)).fetchone()
            if existing_episode:
                raise ValueError("Live evidence episode identity already exists")
            conn.execute(
                "INSERT INTO organization_episodes(episode_id, job_id, source_kind, task_family, context_fingerprint, execution_profile, plan_digest, payload_json, content_hash, recorded_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (episode.episode_id, episode.job_id, episode.source.value, episode.task_family, episode.context_fingerprint, episode.execution_profile, episode.plan_digest, episode_payload, episode_digest, episode.recorded_at),
            )
            conn.execute(
                "INSERT INTO verified_live_evidence_pairs(pair_id, campaign_id, baseline_run_id, dynamic_run_id, baseline_evidence_id, dynamic_evidence_id, baseline_content_hash, dynamic_content_hash, source_revision, fixture, provider_kind, model_id, episode_id, payload_json, content_hash, imported_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pair.pair_id, pair.campaign_id, pair.baseline_run_id, pair.dynamic_run_id, pair.baseline_evidence_id, pair.dynamic_evidence_id, pair.baseline_content_hash, pair.dynamic_content_hash, pair.source_revision, pair.fixture, pair.provider_kind, pair.model_id, episode.episode_id, pair_payload, pair.content_hash, utc_now().isoformat()),
            )
        return episode

    def list_live_evidence_pairs(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute("SELECT payload_json, content_hash FROM verified_live_evidence_pairs ORDER BY imported_at, pair_id").fetchall()
        values: list[Mapping[str, Any]] = []
        for row in rows:
            payload = self._loads_live_evidence_payload(row["payload_json"])
            identity = {key: payload[key] for key in _IDENTITY_FIELDS}
            if content_digest(identity) != row["content_hash"]:
                raise RuntimeError("Verified live evidence pair integrity check failed")
            values.append(payload)
        return tuple(values)

    @staticmethod
    def _loads_live_evidence_payload(raw: str) -> Mapping[str, Any]:
        import json

        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError("Verified live evidence pair payload is malformed")
        return value
