"""Workflow Patch proposal, apply, observation, assessment, and rollback lifecycle."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping, Sequence

from dynamic_firm.runtime.models import VersionedContent, to_primitive, utc_now

from .models import (
    EmployeeSkillAssessment,
    EmployeeSkillAssessmentDecision,
    EmployeeSkillObservation,
    EmployeeSkillObservationContract,
    EmployeeSkillPatchCandidate,
    EmployeeSkillPatchEvent,
    EmployeeSkillPatchEventType,
    EmployeeSkillPatchStatus,
    EmployeeSkillVersion,
    EvidenceSource,
    HireAssessment,
    HireAssessmentDecision,
    HireObservation,
    HireObservationContract,
    RosterPatchCandidate,
    RosterPatchEvent,
    RosterPatchEventType,
    RosterPatchOperation,
    RosterPatchStatus,
    RosterRetentionReview,
    RetentionReviewMode,
    RosterVersion,
    WorkflowPatchCandidate,
    WorkflowPatchAssessment,
    WorkflowPatchEvent,
    WorkflowPatchEventType,
    WorkflowPatchObservation,
    WorkflowPatchObservationContract,
    WorkflowPatchStatus,
    canonical_json,
    content_digest,
    employee_skill_assessment_from_dict,
    employee_skill_patch_from_dict,
    hire_assessment_from_dict,
    hire_observation_contract_from_dict,
    hire_observation_from_dict,
    organization_episode_from_dict,
    roster_patch_from_dict,
    roster_retention_review_from_dict,
    workflow_patch_assessment_from_dict,
    workflow_patch_from_dict,
    workflow_patch_observation_contract_from_dict,
    workflow_patch_observation_from_dict,
    workflow_pattern_from_dict,
)


def _loads(raw: str) -> Any:
    return json.loads(raw)


class CompanyWorkflowPatchMixin:
    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        *,
        patch_id: str,
        event_type: WorkflowPatchEventType,
        actor: str,
        payload: Mapping[str, Any],
    ) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM workflow_patch_events WHERE patch_id = ?",
            (patch_id,),
        ).fetchone()
        seq = int(row["seq"])
        conn.execute(
            """
            INSERT INTO workflow_patch_events(
                event_id, patch_id, seq, event_type, actor, payload_json, occurred_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                patch_id,
                seq,
                event_type.value,
                actor,
                canonical_json(payload),
                utc_now().isoformat(),
            ),
        )

    def create_candidate(
        self,
        candidate: WorkflowPatchCandidate,
        *,
        actor: str = "system:deterministic-curator",
        proposal_payload: Mapping[str, Any] | None = None,
        reject_open_pattern_conflict: bool = False,
        expected_state_revisions: tuple[int, int, int] | None = None,
    ) -> tuple[WorkflowPatchCandidate, bool]:
        if not actor.strip():
            raise ValueError("Workflow Patch proposal actor must be explicit")
        payload = canonical_json(candidate)
        with self._transaction() as conn:
            if expected_state_revisions is not None:
                current = (
                    self._active_revision("active_company_revision", conn),
                    self._active_revision("active_roster_revision", conn),
                    self._active_revision("active_playbook_revision", conn),
                )
                if current != expected_state_revisions:
                    raise ValueError(
                        "Company state changed before Workflow Patch proposal append: "
                        f"expected {expected_state_revisions}, got {current}"
                    )
            existing = conn.execute(
                "SELECT payload_json FROM workflow_patch_candidates WHERE content_hash = ?",
                (candidate.content_hash,),
            ).fetchone()
            if existing:
                return workflow_patch_from_dict(_loads(existing["payload_json"])), False
            if reject_open_pattern_conflict:
                conflict = conn.execute(
                    """
                    SELECT content_hash FROM workflow_patch_candidates
                    WHERE pattern_id = ? AND status IN (?, ?)
                    ORDER BY created_at LIMIT 1
                    """,
                    (
                        candidate.pattern.pattern_id,
                        WorkflowPatchStatus.PROPOSED.value,
                        WorkflowPatchStatus.APPROVED.value,
                    ),
                ).fetchone()
                if conflict is not None:
                    raise ValueError(
                        "Workflow Patch pattern already has a different open proposal"
                    )
            conn.execute(
                """
                INSERT INTO workflow_patch_candidates(
                    patch_id, status, base_playbook_revision, pattern_id, task_family,
                    payload_json, content_hash, eligible_for_apply, applied_revision,
                    rolled_back_revision, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.patch_id,
                    candidate.status.value,
                    candidate.base_playbook_revision,
                    candidate.pattern.pattern_id,
                    candidate.pattern.task_family,
                    payload,
                    candidate.content_hash,
                    int(candidate.eligible_for_apply),
                    candidate.applied_revision,
                    candidate.rolled_back_revision,
                    candidate.created_at,
                    candidate.updated_at,
                ),
            )
            self._event(
                conn,
                patch_id=candidate.patch_id,
                event_type=WorkflowPatchEventType.PROPOSED,
                actor=actor,
                payload=(
                    {
                        "base_playbook_revision": candidate.base_playbook_revision,
                        "eligible_for_apply": candidate.eligible_for_apply,
                        "evidence_episode_ids": candidate.evidence_episode_ids,
                    }
                    if proposal_payload is None
                    else proposal_payload
                ),
            )
        return candidate, True

    def get_patch(self, patch_id: str) -> WorkflowPatchCandidate:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM workflow_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown workflow patch: {patch_id}")
        return workflow_patch_from_dict(_loads(row["payload_json"]))

    def list_patches(
        self, status: WorkflowPatchStatus | None = None
    ) -> tuple[WorkflowPatchCandidate, ...]:
        sql = "SELECT payload_json FROM workflow_patch_candidates"
        parameters: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            parameters = (status.value,)
        sql += " ORDER BY created_at, patch_id"
        with self._lock:
            rows = self._conn.execute(sql, parameters).fetchall()
        return tuple(workflow_patch_from_dict(_loads(row["payload_json"])) for row in rows)

    def find_open_patch_for_pattern(self, pattern_id: str) -> WorkflowPatchCandidate | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT payload_json FROM workflow_patch_candidates
                WHERE pattern_id = ? AND status IN (?, ?)
                ORDER BY created_at LIMIT 1
                """,
                (
                    pattern_id,
                    WorkflowPatchStatus.PROPOSED.value,
                    WorkflowPatchStatus.APPROVED.value,
                ),
            ).fetchone()
        return workflow_patch_from_dict(_loads(row["payload_json"])) if row else None

    def find_applied_patch_for_pattern(self, pattern_id: str) -> WorkflowPatchCandidate | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT payload_json FROM workflow_patch_candidates
                WHERE pattern_id = ? AND status = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (pattern_id, WorkflowPatchStatus.APPLIED.value),
            ).fetchone()
        return workflow_patch_from_dict(_loads(row["payload_json"])) if row else None

    def _update_patch(
        self,
        conn: sqlite3.Connection,
        candidate: WorkflowPatchCandidate,
    ) -> None:
        conn.execute(
            """
            UPDATE workflow_patch_candidates
            SET status = ?, payload_json = ?, applied_revision = ?, rolled_back_revision = ?,
                updated_at = ?
            WHERE patch_id = ?
            """,
            (
                candidate.status.value,
                canonical_json(candidate),
                candidate.applied_revision,
                candidate.rolled_back_revision,
                candidate.updated_at,
                candidate.patch_id,
            ),
        )

    def approve_patch(self, patch_id: str, actor: str) -> WorkflowPatchCandidate:
        if not actor.strip():
            raise ValueError("Patch approval actor must be explicit")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM workflow_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown workflow patch: {patch_id}")
            candidate = workflow_patch_from_dict(_loads(row["payload_json"]))
            if not candidate.eligible_for_apply:
                raise ValueError(
                    "Workflow patch is preview-only: "
                    + ", ".join(candidate.ineligibility_reasons)
                )
            if candidate.status == WorkflowPatchStatus.APPROVED:
                return candidate
            if candidate.status != WorkflowPatchStatus.PROPOSED:
                raise ValueError(f"Only proposed patches can be approved: {candidate.status.value}")
            updated = candidate.with_status(WorkflowPatchStatus.APPROVED)
            self._update_patch(conn, updated)
            self._event(
                conn,
                patch_id=patch_id,
                event_type=WorkflowPatchEventType.APPROVED,
                actor=actor,
                payload={"content_hash": candidate.content_hash},
            )
        return updated

    def reject_patch(self, patch_id: str, actor: str, reason: str) -> WorkflowPatchCandidate:
        if not actor.strip() or not reason.strip():
            raise ValueError("Patch rejection requires an actor and reason")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM workflow_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown workflow patch: {patch_id}")
            candidate = workflow_patch_from_dict(_loads(row["payload_json"]))
            if candidate.status == WorkflowPatchStatus.REJECTED:
                return candidate
            if candidate.status not in {
                WorkflowPatchStatus.PROPOSED,
                WorkflowPatchStatus.APPROVED,
            }:
                raise ValueError(f"Patch cannot be rejected from {candidate.status.value}")
            updated = candidate.with_status(WorkflowPatchStatus.REJECTED)
            self._update_patch(conn, updated)
            self._event(
                conn,
                patch_id=patch_id,
                event_type=WorkflowPatchEventType.REJECTED,
                actor=actor,
                payload={"reason": reason},
            )
        return updated

    def apply_patch(self, patch_id: str, actor: str) -> WorkflowPatchCandidate:
        if not actor.strip():
            raise ValueError("Patch apply actor must be explicit")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM workflow_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown workflow patch: {patch_id}")
            candidate = workflow_patch_from_dict(_loads(row["payload_json"]))
            if candidate.status == WorkflowPatchStatus.APPLIED:
                return candidate
            if candidate.status != WorkflowPatchStatus.APPROVED:
                raise ValueError("Workflow patch must be explicitly approved before apply")
            if not candidate.eligible_for_apply:
                raise ValueError("Preview-only workflow patch cannot be applied")
            active = self._active_revision("active_playbook_revision", conn)
            if active != candidate.base_playbook_revision:
                raise ValueError(
                    f"Playbook changed since proposal: expected {candidate.base_playbook_revision}, got {active}"
                )
            current_row = conn.execute(
                "SELECT patterns_json FROM playbook_versions WHERE revision = ?", (active,)
            ).fetchone()
            assert current_row is not None
            patterns = list(_loads(current_row["patterns_json"]))
            if any(item.get("pattern_id") == candidate.pattern.pattern_id for item in patterns):
                raise ValueError("Workflow pattern is already active")
            patterns.append(to_primitive(candidate.pattern))
            revision = active + 1
            now = utc_now().isoformat()
            conn.execute(
                """
                INSERT INTO playbook_versions(
                    revision, parent_revision, patterns_json, source_patch_id,
                    rolled_back_from_revision, created_at
                ) VALUES(?, ?, ?, ?, NULL, ?)
                """,
                (revision, active, canonical_json(patterns), patch_id, now),
            )
            conn.execute(
                "UPDATE company_state_meta SET value = ? WHERE key = 'active_playbook_revision'",
                (str(revision),),
            )
            updated = candidate.with_status(
                WorkflowPatchStatus.APPLIED, applied_revision=revision
            )
            self._update_patch(conn, updated)
            self._insert_observation_contract(
                conn,
                WorkflowPatchObservationContract.create(updated, created_at=now),
            )
            self._event(
                conn,
                patch_id=patch_id,
                event_type=WorkflowPatchEventType.APPLIED,
                actor=actor,
                payload={"from_revision": active, "to_revision": revision},
            )
        return updated

    def get_observation_contract(
        self, patch_id: str
    ) -> WorkflowPatchObservationContract:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT payload_json, content_hash FROM workflow_patch_observation_contracts
                WHERE patch_id = ?
                """,
                (patch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Workflow patch has no observation contract: {patch_id}")
        contract = workflow_patch_observation_contract_from_dict(_loads(row["payload_json"]))
        immutable = {
            "patch_id": contract.patch_id,
            "pattern_id": contract.pattern_id,
            "context_fingerprint": contract.context_fingerprint,
            "execution_profile": contract.execution_profile,
            "minimum_observations": contract.minimum_observations,
            "maximum_observations": contract.maximum_observations,
            "minimum_quality_gain": contract.minimum_quality_gain,
            "minimum_model_call_savings": contract.minimum_model_call_savings,
            "fail_on_safety_violation": contract.fail_on_safety_violation,
        }
        if (
            content_digest(immutable) != contract.content_hash
            or contract.content_hash != row["content_hash"]
        ):
            raise RuntimeError(f"Observation contract integrity check failed: {patch_id}")
        return contract

    def record_observation(
        self, observation: WorkflowPatchObservation
    ) -> tuple[WorkflowPatchObservation, bool]:
        if content_digest(observation.content_payload()) != observation.content_hash:
            raise ValueError("Workflow patch observation content hash does not match payload")
        payload = canonical_json(observation)
        with self._transaction() as conn:
            contract = conn.execute(
                "SELECT patch_id FROM workflow_patch_observation_contracts WHERE patch_id = ?",
                (observation.patch_id,),
            ).fetchone()
            if contract is None:
                raise ValueError("Workflow patch must be applied before it can be observed")
            episode = conn.execute(
                "SELECT episode_id FROM organization_episodes WHERE episode_id = ?",
                (observation.episode_id,),
            ).fetchone()
            if episode is None:
                raise ValueError("Workflow patch observation requires a persisted episode")
            existing = conn.execute(
                """
                SELECT payload_json, content_hash FROM workflow_patch_observations
                WHERE patch_id = ? AND episode_id = ?
                """,
                (observation.patch_id, observation.episode_id),
            ).fetchone()
            if existing:
                if existing["content_hash"] != observation.content_hash:
                    raise ValueError(
                        "Workflow patch observation was reused with different attribution"
                    )
                return self._observation_from_row(existing), False
            conn.execute(
                """
                INSERT INTO workflow_patch_observations(
                    observation_id, patch_id, episode_id, attribution_eligible,
                    cohort_eligible, payload_json, content_hash, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.patch_id,
                    observation.episode_id,
                    int(observation.attribution_eligible),
                    int(observation.cohort_eligible),
                    payload,
                    observation.content_hash,
                    observation.recorded_at,
                ),
            )
        return observation, True

    def _observation_from_row(self, row: sqlite3.Row) -> WorkflowPatchObservation:
        observation = workflow_patch_observation_from_dict(_loads(row["payload_json"]))
        if (
            content_digest(observation.content_payload()) != row["content_hash"]
            or observation.content_hash != row["content_hash"]
        ):
            raise RuntimeError(
                f"Workflow patch observation integrity check failed: {observation.observation_id}"
            )
        return observation

    def list_observations(self, patch_id: str) -> tuple[WorkflowPatchObservation, ...]:
        self.get_observation_contract(patch_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM workflow_patch_observations
                WHERE patch_id = ? ORDER BY recorded_at, observation_id
                """,
                (patch_id,),
            ).fetchall()
        return tuple(self._observation_from_row(row) for row in rows)

    def record_assessment(
        self, assessment: WorkflowPatchAssessment
    ) -> tuple[WorkflowPatchAssessment, bool]:
        if content_digest(assessment.content_payload()) != assessment.content_hash:
            raise ValueError("Workflow patch assessment content hash does not match payload")
        payload = canonical_json(assessment)
        with self._transaction() as conn:
            contract = conn.execute(
                "SELECT patch_id FROM workflow_patch_observation_contracts WHERE patch_id = ?",
                (assessment.patch_id,),
            ).fetchone()
            if contract is None:
                raise ValueError("Workflow patch assessment requires an observation contract")
            existing = conn.execute(
                """
                SELECT payload_json, content_hash FROM workflow_patch_assessments
                WHERE patch_id = ? AND content_hash = ?
                """,
                (assessment.patch_id, assessment.content_hash),
            ).fetchone()
            if existing:
                stored = workflow_patch_assessment_from_dict(_loads(existing["payload_json"]))
                if (
                    content_digest(stored.content_payload()) != existing["content_hash"]
                    or stored.content_hash != existing["content_hash"]
                ):
                    raise RuntimeError(
                        f"Workflow patch assessment integrity check failed: {stored.assessment_id}"
                    )
                return stored, False
            row = conn.execute(
                """
                SELECT COALESCE(MAX(seq), 0) + 1 AS seq
                FROM workflow_patch_assessments WHERE patch_id = ?
                """,
                (assessment.patch_id,),
            ).fetchone()
            expected_seq = int(row["seq"])
            if assessment.seq != expected_seq:
                raise ValueError(
                    f"Workflow patch assessment sequence must be {expected_seq}"
                )
            conn.execute(
                """
                INSERT INTO workflow_patch_assessments(
                    assessment_id, patch_id, seq, decision, payload_json,
                    content_hash, assessed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.patch_id,
                    assessment.seq,
                    assessment.decision.value,
                    payload,
                    assessment.content_hash,
                    assessment.assessed_at,
                ),
            )
        return assessment, True

    def list_assessments(self, patch_id: str) -> tuple[WorkflowPatchAssessment, ...]:
        self.get_observation_contract(patch_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM workflow_patch_assessments
                WHERE patch_id = ? ORDER BY seq
                """,
                (patch_id,),
            ).fetchall()
        assessments: list[WorkflowPatchAssessment] = []
        for row in rows:
            assessment = workflow_patch_assessment_from_dict(_loads(row["payload_json"]))
            if (
                content_digest(assessment.content_payload()) != assessment.content_hash
                or assessment.content_hash != row["content_hash"]
            ):
                raise RuntimeError(
                    f"Workflow patch assessment integrity check failed: {assessment.assessment_id}"
                )
            assessments.append(assessment)
        return tuple(assessments)

    def latest_assessment(self, patch_id: str) -> WorkflowPatchAssessment | None:
        assessments = self.list_assessments(patch_id)
        return assessments[-1] if assessments else None

    def rollback_patch(self, patch_id: str, actor: str) -> WorkflowPatchCandidate:
        if not actor.strip():
            raise ValueError("Patch rollback actor must be explicit")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM workflow_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown workflow patch: {patch_id}")
            candidate = workflow_patch_from_dict(_loads(row["payload_json"]))
            if candidate.status == WorkflowPatchStatus.ROLLED_BACK:
                return candidate
            if candidate.status != WorkflowPatchStatus.APPLIED or candidate.applied_revision is None:
                raise ValueError("Only an applied workflow patch can be rolled back")
            active = self._active_revision("active_playbook_revision", conn)
            if active != candidate.applied_revision:
                raise ValueError(
                    "Rollback is stale because a later playbook revision is already active"
                )
            applied = conn.execute(
                "SELECT parent_revision FROM playbook_versions WHERE revision = ?", (active,)
            ).fetchone()
            assert applied is not None and applied["parent_revision"] is not None
            before_revision = int(applied["parent_revision"])
            before = conn.execute(
                "SELECT patterns_json FROM playbook_versions WHERE revision = ?",
                (before_revision,),
            ).fetchone()
            assert before is not None
            revision = active + 1
            now = utc_now().isoformat()
            conn.execute(
                """
                INSERT INTO playbook_versions(
                    revision, parent_revision, patterns_json, source_patch_id,
                    rolled_back_from_revision, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (revision, active, before["patterns_json"], patch_id, active, now),
            )
            conn.execute(
                "UPDATE company_state_meta SET value = ? WHERE key = 'active_playbook_revision'",
                (str(revision),),
            )
            updated = candidate.with_status(
                WorkflowPatchStatus.ROLLED_BACK,
                rolled_back_revision=revision,
            )
            self._update_patch(conn, updated)
            self._event(
                conn,
                patch_id=patch_id,
                event_type=WorkflowPatchEventType.ROLLED_BACK,
                actor=actor,
                payload={
                    "from_revision": active,
                    "restored_content_from_revision": before_revision,
                    "to_revision": revision,
                },
            )
        return updated

    def list_patch_events(self, patch_id: str) -> tuple[WorkflowPatchEvent, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_patch_events WHERE patch_id = ? ORDER BY seq",
                (patch_id,),
            ).fetchall()
        return tuple(
            WorkflowPatchEvent(
                event_id=str(row["event_id"]),
                patch_id=str(row["patch_id"]),
                seq=int(row["seq"]),
                event_type=WorkflowPatchEventType(row["event_type"]),
                actor=str(row["actor"]),
                payload=_loads(row["payload_json"]),
                occurred_at=str(row["occurred_at"]),
            )
            for row in rows
        )
