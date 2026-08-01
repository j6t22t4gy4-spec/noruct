"""Employee Skill proposal, approval, version, apply, and rollback lifecycle."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping, Sequence

from dynamic_firm.runtime.models import VersionedContent, to_primitive, utc_now

from .models import (
    EmployeeSkillAssessment,
    EmployeeSkillAssessmentDecision,
    EmployeeSkillEvidence,
    EmployeeSkillEvidenceKind,
    EmployeeSkillObservation,
    EmployeeSkillObservationContract,
    EmployeeSkillPatchCandidate,
    EmployeeSkillPatchEvent,
    EmployeeSkillPatchEventType,
    EmployeeSkillPatchStatus,
    EmployeeSkillVersion,
    EvidenceSource,
    canonical_json,
    content_digest,
    employee_skill_assessment_from_dict,
    employee_skill_evidence_from_dict,
    employee_skill_observation_contract_from_dict,
    employee_skill_observation_from_dict,
    employee_skill_patch_from_dict,
    employee_skill_version_from_dict,
    organization_episode_from_dict,
)


def _loads(raw: str) -> Any:
    return json.loads(raw)


class CompanyEmployeeSkillLifecycleMixin:
    def create_employee_skill_patch(
        self,
        candidate: EmployeeSkillPatchCandidate,
        *,
        actor: str,
    ) -> tuple[EmployeeSkillPatchCandidate, bool]:
        if not actor.strip() or actor.strip() != candidate.proposed_by:
            raise ValueError("Employee Skill proposal actor must be explicit and consistent")
        if candidate.status != EmployeeSkillPatchStatus.PROPOSED:
            raise ValueError("New Employee Skill Patch must be PROPOSED")
        self._validate_employee_skill_patch_content(candidate)
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM employee_skill_patch_candidates WHERE content_hash = ?",
                (candidate.content_hash,),
            ).fetchone()
            if existing is not None:
                stored = employee_skill_patch_from_dict(_loads(existing["payload_json"]))
                self._validate_employee_skill_patch_content(stored)
                return stored, False
            open_row = conn.execute(
                """
                SELECT patch_id FROM employee_skill_patch_candidates
                WHERE employee_id = ? AND skill_key = ? AND context_key = ?
                  AND status IN (?, ?)
                """,
                (
                    candidate.procedure.employee_id,
                    candidate.procedure.skill_key,
                    candidate.procedure.context_key,
                    EmployeeSkillPatchStatus.PROPOSED.value,
                    EmployeeSkillPatchStatus.APPROVED.value,
                ),
            ).fetchone()
            if open_row is not None:
                raise ValueError(
                    "Employee Skill already has an open patch: " + str(open_row["patch_id"])
                )
            for evidence_id in candidate.evidence_ids:
                if conn.execute(
                    "SELECT 1 FROM employee_skill_evidence WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone() is None:
                    raise ValueError(f"Employee Skill evidence does not exist: {evidence_id}")
            conn.execute(
                """
                INSERT INTO employee_skill_patch_candidates(
                    patch_id, status, employee_id, skill_key, context_key,
                    base_skill_revision, payload_json, content_hash,
                    applied_skill_revision, rolled_back_skill_revision,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.patch_id,
                    candidate.status.value,
                    candidate.procedure.employee_id,
                    candidate.procedure.skill_key,
                    candidate.procedure.context_key,
                    candidate.base_skill_revision,
                    canonical_json(candidate),
                    candidate.content_hash,
                    None,
                    None,
                    candidate.created_at,
                    candidate.updated_at,
                ),
            )
            for evidence_id in candidate.evidence_ids:
                conn.execute(
                    "INSERT INTO employee_skill_patch_evidence(patch_id, evidence_id) VALUES(?, ?)",
                    (candidate.patch_id, evidence_id),
                )
            self._validate_employee_skill_patch_fresh_in(conn, candidate)
            self._employee_skill_patch_event(
                conn,
                patch_id=candidate.patch_id,
                event_type=EmployeeSkillPatchEventType.PROPOSED,
                actor=actor,
                payload={
                    "content_hash": candidate.content_hash,
                    "evidence_ids": candidate.evidence_ids,
                    "review_mode": "approval",
                },
            )
        return candidate, True

    def get_employee_skill_patch(self, patch_id: str) -> EmployeeSkillPatchCandidate:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM employee_skill_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Employee Skill Patch: {patch_id}")
            candidate = employee_skill_patch_from_dict(_loads(row["payload_json"]))
            self._validate_employee_skill_patch_content(candidate)
            self._employee_skill_patch_evidence_in(self._conn, candidate)
        return candidate

    def list_employee_skill_patches(
        self,
        status: EmployeeSkillPatchStatus | None = None,
    ) -> tuple[EmployeeSkillPatchCandidate, ...]:
        sql = "SELECT payload_json FROM employee_skill_patch_candidates"
        parameters: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            parameters = (status.value,)
        sql += " ORDER BY created_at, patch_id"
        with self._lock:
            rows = self._conn.execute(sql, parameters).fetchall()
            candidates = tuple(
                employee_skill_patch_from_dict(_loads(row["payload_json"]))
                for row in rows
            )
            for candidate in candidates:
                self._validate_employee_skill_patch_content(candidate)
                self._employee_skill_patch_evidence_in(self._conn, candidate)
        return candidates

    @staticmethod
    def _update_employee_skill_patch(
        conn: sqlite3.Connection,
        candidate: EmployeeSkillPatchCandidate,
    ) -> None:
        conn.execute(
            """
            UPDATE employee_skill_patch_candidates
            SET status = ?, payload_json = ?, applied_skill_revision = ?,
                rolled_back_skill_revision = ?, updated_at = ?
            WHERE patch_id = ?
            """,
            (
                candidate.status.value,
                canonical_json(candidate),
                candidate.applied_skill_revision,
                candidate.rolled_back_skill_revision,
                candidate.updated_at,
                candidate.patch_id,
            ),
        )

    def approve_employee_skill_patch(
        self,
        patch_id: str,
        actor: str,
    ) -> EmployeeSkillPatchCandidate:
        if not actor.strip():
            raise ValueError("Employee Skill approval actor must be explicit")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM employee_skill_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Employee Skill Patch: {patch_id}")
            candidate = employee_skill_patch_from_dict(_loads(row["payload_json"]))
            self._validate_employee_skill_patch_content(candidate)
            if candidate.status == EmployeeSkillPatchStatus.APPROVED:
                return candidate
            if candidate.status != EmployeeSkillPatchStatus.PROPOSED:
                raise ValueError("Only proposed Employee Skill Patches can be approved")
            self._validate_employee_skill_patch_fresh_in(conn, candidate)
            updated = candidate.with_status(EmployeeSkillPatchStatus.APPROVED)
            self._update_employee_skill_patch(conn, updated)
            self._employee_skill_patch_event(
                conn,
                patch_id=patch_id,
                event_type=EmployeeSkillPatchEventType.APPROVED,
                actor=actor,
                payload={"content_hash": candidate.content_hash},
            )
        return updated

    @staticmethod
    def _new_employee_skill_version(
        candidate: EmployeeSkillPatchCandidate,
        *,
        revision: int,
        procedure,
        active: bool,
        created_at: str,
    ) -> EmployeeSkillVersion:
        immutable = {
            "employee_id": candidate.procedure.employee_id,
            "skill_key": candidate.procedure.skill_key,
            "context_key": candidate.procedure.context_key,
            "revision": revision,
            "active": active,
            "procedure": procedure,
            "source_patch_id": candidate.patch_id,
        }
        digest = content_digest(immutable)
        return EmployeeSkillVersion(
            version_id=f"employee-skill-version-{digest[:24]}",
            **immutable,
            content_hash=digest,
            created_at=created_at,
        )

    @staticmethod
    def _insert_employee_skill_version(
        conn: sqlite3.Connection,
        version: EmployeeSkillVersion,
    ) -> None:
        conn.execute(
            """
            INSERT INTO employee_skill_versions(
                version_id, employee_id, skill_key, context_key, revision,
                active, source_patch_id, payload_json, content_hash, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.version_id,
                version.employee_id,
                version.skill_key,
                version.context_key,
                version.revision,
                int(version.active),
                version.source_patch_id,
                canonical_json(version),
                version.content_hash,
                version.created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO employee_skill_heads(
                employee_id, skill_key, context_key, current_version_id
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(employee_id, skill_key, context_key)
            DO UPDATE SET current_version_id = excluded.current_version_id
            """,
            (
                version.employee_id,
                version.skill_key,
                version.context_key,
                version.version_id,
            ),
        )

    @staticmethod
    def _insert_employee_skill_observation_contract(
        conn: sqlite3.Connection,
        contract: EmployeeSkillObservationContract,
    ) -> None:
        conn.execute(
            """
            INSERT INTO employee_skill_observation_contracts(
                patch_id, employee_id, skill_key, context_key,
                payload_json, content_hash, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract.patch_id,
                contract.employee_id,
                contract.skill_key,
                contract.context_key,
                canonical_json(contract),
                contract.content_hash,
                contract.created_at,
            ),
        )

    def apply_employee_skill_patch(
        self,
        patch_id: str,
        actor: str,
    ) -> EmployeeSkillPatchCandidate:
        if not actor.strip():
            raise ValueError("Employee Skill apply actor must be explicit")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM employee_skill_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Employee Skill Patch: {patch_id}")
            candidate = employee_skill_patch_from_dict(_loads(row["payload_json"]))
            self._validate_employee_skill_patch_content(candidate)
            if candidate.status == EmployeeSkillPatchStatus.APPLIED:
                self._get_employee_skill_observation_contract_in(conn, patch_id)
                return candidate
            if candidate.status != EmployeeSkillPatchStatus.APPROVED:
                raise ValueError("Employee Skill Patch must be approved before apply")
            self._validate_employee_skill_patch_fresh_in(conn, candidate)
            revision = candidate.base_skill_revision + 1
            now = utc_now().isoformat()
            version = self._new_employee_skill_version(
                candidate,
                revision=revision,
                procedure=candidate.procedure,
                active=True,
                created_at=now,
            )
            self._insert_employee_skill_version(conn, version)
            updated = candidate.with_status(
                EmployeeSkillPatchStatus.APPLIED,
                applied_skill_revision=revision,
            )
            self._update_employee_skill_patch(conn, updated)
            contract = EmployeeSkillObservationContract.create(
                updated,
                version,
                created_at=now,
            )
            self._insert_employee_skill_observation_contract(conn, contract)
            self._employee_skill_patch_event(
                conn,
                patch_id=patch_id,
                event_type=EmployeeSkillPatchEventType.APPLIED,
                actor=actor,
                payload={
                    "from_skill_revision": candidate.base_skill_revision,
                    "to_skill_revision": revision,
                    "version_content_hash": version.content_hash,
                },
            )
        return updated

    def reject_employee_skill_patch(
        self,
        patch_id: str,
        actor: str,
        reason: str,
    ) -> EmployeeSkillPatchCandidate:
        if not actor.strip() or not reason.strip():
            raise ValueError("Employee Skill rejection requires actor and reason")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM employee_skill_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Employee Skill Patch: {patch_id}")
            candidate = employee_skill_patch_from_dict(_loads(row["payload_json"]))
            if candidate.status == EmployeeSkillPatchStatus.REJECTED:
                return candidate
            if candidate.status not in {
                EmployeeSkillPatchStatus.PROPOSED,
                EmployeeSkillPatchStatus.APPROVED,
            }:
                raise ValueError("Employee Skill Patch can no longer be rejected")
            updated = candidate.with_status(EmployeeSkillPatchStatus.REJECTED)
            self._update_employee_skill_patch(conn, updated)
            self._employee_skill_patch_event(
                conn,
                patch_id=patch_id,
                event_type=EmployeeSkillPatchEventType.REJECTED,
                actor=actor,
                payload={"reason": reason.strip()},
            )
        return updated

    def rollback_employee_skill_patch(
        self,
        patch_id: str,
        actor: str,
    ) -> EmployeeSkillPatchCandidate:
        if not actor.strip():
            raise ValueError("Employee Skill rollback actor must be explicit")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM employee_skill_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Employee Skill Patch: {patch_id}")
            candidate = employee_skill_patch_from_dict(_loads(row["payload_json"]))
            self._validate_employee_skill_patch_content(candidate)
            if candidate.status == EmployeeSkillPatchStatus.ROLLED_BACK:
                return candidate
            if candidate.status != EmployeeSkillPatchStatus.APPLIED:
                raise ValueError("Only applied Employee Skill Patches can be rolled back")
            current = self._current_employee_skill_in(
                conn,
                candidate.procedure.employee_id,
                candidate.procedure.skill_key,
                candidate.procedure.context_key,
            )
            if (
                current is None
                or current.revision != candidate.applied_skill_revision
                or current.procedure != candidate.procedure
            ):
                raise ValueError("Employee Skill changed after this patch was applied")
            revision = current.revision + 1
            now = utc_now().isoformat()
            restored = self._new_employee_skill_version(
                candidate,
                revision=revision,
                procedure=candidate.before_procedure,
                active=candidate.before_procedure is not None,
                created_at=now,
            )
            self._insert_employee_skill_version(conn, restored)
            updated = candidate.with_status(
                EmployeeSkillPatchStatus.ROLLED_BACK,
                rolled_back_skill_revision=revision,
            )
            self._update_employee_skill_patch(conn, updated)
            self._employee_skill_patch_event(
                conn,
                patch_id=patch_id,
                event_type=EmployeeSkillPatchEventType.ROLLED_BACK,
                actor=actor,
                payload={
                    "from_skill_revision": current.revision,
                    "to_skill_revision": revision,
                    "active": restored.active,
                },
            )
        return updated

    def list_employee_skill_patch_events(
        self,
        patch_id: str,
    ) -> tuple[EmployeeSkillPatchEvent, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM employee_skill_patch_events WHERE patch_id = ? ORDER BY seq",
                (patch_id,),
            ).fetchall()
        return tuple(
            EmployeeSkillPatchEvent(
                event_id=str(row["event_id"]),
                patch_id=str(row["patch_id"]),
                seq=int(row["seq"]),
                event_type=EmployeeSkillPatchEventType(row["event_type"]),
                actor=str(row["actor"]),
                payload=_loads(row["payload_json"]),
                occurred_at=str(row["occurred_at"]),
            )
            for row in rows
        )

    def _get_employee_skill_observation_contract_in(
        self,
        conn: sqlite3.Connection,
        patch_id: str,
    ) -> EmployeeSkillObservationContract:
        row = conn.execute(
            "SELECT * FROM employee_skill_observation_contracts WHERE patch_id = ?",
            (patch_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Employee Skill observation contract does not exist: {patch_id}")
        contract = employee_skill_observation_contract_from_dict(
            _loads(row["payload_json"])
        )
        if (
            content_digest(
                {
                    "patch_id": contract.patch_id,
                    "employee_id": contract.employee_id,
                    "skill_key": contract.skill_key,
                    "context_key": contract.context_key,
                    "applied_skill_revision": contract.applied_skill_revision,
                    "version_content_hash": contract.version_content_hash,
                    "minimum_observations": contract.minimum_observations,
                    "maximum_observations": contract.maximum_observations,
                }
            )
            != contract.content_hash
            or contract.content_hash != row["content_hash"]
        ):
            raise RuntimeError("Employee Skill observation contract integrity failed")
        return contract

    def get_employee_skill_observation_contract(
        self,
        patch_id: str,
    ) -> EmployeeSkillObservationContract:
        with self._lock:
            return self._get_employee_skill_observation_contract_in(self._conn, patch_id)

    def list_employee_skill_observation_contracts(
        self,
    ) -> tuple[EmployeeSkillObservationContract, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT patch_id FROM employee_skill_observation_contracts ORDER BY created_at, patch_id"
            ).fetchall()
            return tuple(
                self._get_employee_skill_observation_contract_in(
                    self._conn, str(row["patch_id"])
                )
                for row in rows
            )
