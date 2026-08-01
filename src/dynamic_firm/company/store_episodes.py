"""Append-only Organization Episode audit component.

Episodes are the Company learning evidence boundary.  This mixin keeps their
content-hash validation and idempotent append logic separate from ROSTER,
Skill, and Workflow Patch mutation code while using the same canonical Company
database and transaction owner.
"""

from __future__ import annotations

import json

from .models import (
    OrganizationEpisode,
    canonical_json,
    content_digest,
    organization_episode_from_dict,
)


class CompanyEpisodeAuditMixin:
    """Immutable Organization Episode append/read operations."""

    def record_episode(self, episode: OrganizationEpisode) -> tuple[OrganizationEpisode, bool]:
        payload = canonical_json(episode)
        digest = content_digest(episode.content_payload())
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT payload_json, content_hash FROM organization_episodes WHERE job_id = ?",
                (episode.job_id,),
            ).fetchone()
            if existing:
                if existing["content_hash"] != digest:
                    raise ValueError(
                        f"Job evidence {episode.job_id!r} was reused with different content"
                    )
                return organization_episode_from_dict(json.loads(existing["payload_json"])), False
            conn.execute(
                """
                INSERT INTO organization_episodes(
                    episode_id, job_id, source_kind, task_family, context_fingerprint,
                    execution_profile, plan_digest, payload_json, content_hash, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode.episode_id,
                    episode.job_id,
                    episode.source.value,
                    episode.task_family,
                    episode.context_fingerprint,
                    episode.execution_profile,
                    episode.plan_digest,
                    payload,
                    digest,
                    episode.recorded_at,
                ),
            )
        return episode, True

    def _episode_from_row(self, row) -> OrganizationEpisode:
        episode = organization_episode_from_dict(json.loads(row["payload_json"]))
        if content_digest(episode.content_payload()) != row["content_hash"]:
            raise RuntimeError(f"Organization episode integrity check failed: {episode.episode_id}")
        return episode

    def get_episode(self, episode_id: str) -> OrganizationEpisode:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM organization_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown organization episode: {episode_id}")
        return self._episode_from_row(row)

    def list_episodes(self, limit: int | None = None) -> tuple[OrganizationEpisode, ...]:
        sql = "SELECT * FROM organization_episodes ORDER BY recorded_at, episode_id"
        parameters: tuple[object, ...] = ()
        if limit is not None:
            if limit < 1:
                return ()
            sql += " LIMIT ?"
            parameters = (limit,)
        with self._lock:
            rows = self._conn.execute(sql, parameters).fetchall()
        return tuple(self._episode_from_row(row) for row in rows)
