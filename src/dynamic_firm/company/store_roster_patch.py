"""ROSTER proposal, evidence, approval, apply, and hire-observation contract lifecycle."""
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


class CompanyRosterPatchMixin:
    @staticmethod
    def _insert_observation_contract(
        conn: sqlite3.Connection,
        contract: WorkflowPatchObservationContract,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO workflow_patch_observation_contracts(
                patch_id, pattern_id, context_fingerprint, execution_profile,
                payload_json, content_hash, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract.patch_id,
                contract.pattern_id,
                contract.context_fingerprint,
                contract.execution_profile,
                canonical_json(contract),
                contract.content_hash,
                contract.created_at,
            ),
        )

    @staticmethod
    def _insert_hire_observation_contract(
        conn: sqlite3.Connection,
        contract: HireObservationContract,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO hire_observation_contracts(
                patch_id, applied_roster_revision, employee_id, capability,
                context_fingerprint, payload_json, content_hash, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract.patch_id,
                contract.applied_roster_revision,
                contract.employee_id,
                contract.capability,
                contract.context_fingerprint,
                canonical_json(contract),
                contract.content_hash,
                contract.created_at,
            ),
        )

    def _active_revision(self, key: str, conn: sqlite3.Connection | None = None) -> int:
        target = conn or self._conn
        row = target.execute(
            "SELECT value FROM company_state_meta WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            raise RuntimeError(f"Missing company state pointer: {key}")
        return int(row["value"])

    def ensure_roster_baseline(self, employees: Sequence[object]) -> RosterVersion:
        normalized = tuple(to_primitive(item) for item in employees)
        with self._transaction() as conn:
            active = self._active_revision("active_roster_revision", conn)
            row = conn.execute(
                "SELECT employees_json FROM roster_versions WHERE revision = ?", (active,)
            ).fetchone()
            assert row is not None
            current = tuple(_loads(row["employees_json"]))
            if current:
                return self.roster()
            revision = active + 1
            now = utc_now().isoformat()
            conn.execute(
                """
                INSERT INTO roster_versions(revision, parent_revision, employees_json, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (revision, active, canonical_json(normalized), now),
            )
            conn.execute(
                "UPDATE company_state_meta SET value = ? WHERE key = 'active_roster_revision'",
                (str(revision),),
            )
        return self.roster()

    @staticmethod
    def _validate_roster_patch_content(candidate: RosterPatchCandidate) -> None:
        if candidate.evidence_ids != tuple(sorted(set(candidate.evidence_ids))):
            raise ValueError("Roster Patch evidence ids must be unique and sorted")
        if candidate.assessment_ids != tuple(sorted(set(candidate.assessment_ids))):
            raise ValueError("Roster Patch assessment ids must be unique and sorted")
        if candidate.evidence_ids and candidate.assessment_ids:
            raise ValueError("Roster Patch cannot mix staffing and retention evidence")
        if content_digest(candidate.content_payload()) != candidate.content_hash:
            raise ValueError(f"Roster Patch content hash mismatch: {candidate.patch_id}")

    def _roster_patch_evidence_in(
        self,
        conn: sqlite3.Connection,
        candidate: RosterPatchCandidate,
    ) -> tuple[StaffingDemandEvidence, ...]:
        linked = tuple(
            str(row["evidence_id"])
            for row in conn.execute(
                """
                SELECT evidence_id FROM roster_patch_staffing_evidence
                WHERE patch_id = ? ORDER BY evidence_id
                """,
                (candidate.patch_id,),
            ).fetchall()
        )
        if linked != candidate.evidence_ids:
            raise RuntimeError(
                f"Roster Patch evidence links mismatch: {candidate.patch_id}"
            )
        evidence: list[StaffingDemandEvidence] = []
        for evidence_id in linked:
            row = conn.execute(
                "SELECT * FROM staffing_demand_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Roster Patch evidence is missing: {evidence_id}")
            evidence.append(self._staffing_demand_from_row(row))
        result = tuple(evidence)
        if result:
            if candidate.operation != RosterPatchOperation.ADD_EMPLOYEE:
                raise ValueError("Staffing evidence can only support ADD_EMPLOYEE")
            if len({item.job_id for item in result}) < 2:
                raise ValueError("Roster recommendation requires two independent jobs")
            contexts = {item.context_fingerprint for item in result}
            capabilities = {item.capability for item in result}
            if len(contexts) != 1 or len(capabilities) != 1:
                raise ValueError(
                    "Roster recommendation evidence must share one context and capability"
                )
            if any(not item.safety_passed for item in result):
                raise ValueError("Roster recommendation evidence failed safety gates")
            after_capabilities = tuple(
                str(item).strip().casefold()
                for item in candidate.after_employee.get("capabilities", ())
            )
            if after_capabilities != (next(iter(capabilities)),):
                raise ValueError(
                    "Roster recommendation capability does not match its evidence"
                )
        return result

    def roster_patch_evidence(
        self,
        patch_id: str,
    ) -> tuple[StaffingDemandEvidence, ...]:
        candidate = self.get_roster_patch(patch_id)
        with self._lock:
            return self._roster_patch_evidence_in(self._conn, candidate)

    def _roster_patch_assessments_in(
        self,
        conn: sqlite3.Connection,
        candidate: RosterPatchCandidate,
        *,
        require_fresh: bool = False,
    ) -> tuple[HireAssessment, ...]:
        linked = tuple(
            str(row["assessment_id"])
            for row in conn.execute(
                """
                SELECT assessment_id FROM roster_patch_hire_assessments
                WHERE patch_id = ? ORDER BY assessment_id
                """,
                (candidate.patch_id,),
            ).fetchall()
        )
        if linked != candidate.assessment_ids:
            raise RuntimeError(
                f"Roster Patch assessment links mismatch: {candidate.patch_id}"
            )
        if not linked:
            return ()
        if len(linked) != 1 or candidate.operation != RosterPatchOperation.SET_ACTIVE:
            raise ValueError("Retention Roster Patch requires one SET_ACTIVE assessment")
        if candidate.before_employee is None:
            raise ValueError("Retention Roster Patch requires an existing employee")
        expected_after = {**candidate.before_employee, "active": False}
        if (
            candidate.before_employee.get("active") is not True
            or candidate.after_employee != expected_after
        ):
            raise ValueError("Retention Roster Patch must only change active true to false")
        row = conn.execute(
            "SELECT * FROM hire_assessments WHERE assessment_id = ?", (linked[0],)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Roster Patch assessment is missing: {linked[0]}")
        assessment = self._hire_assessment_from_row(row)
        if assessment.decision != HireAssessmentDecision.DORMANCY_CANDIDATE:
            raise ValueError("Retention Roster Patch requires DORMANCY_CANDIDATE")
        contract = self._get_hire_observation_contract_in(conn, assessment.patch_id)
        if contract.employee_id != candidate.employee_id:
            raise ValueError("Retention Roster Patch employee differs from hire contract")
        capabilities = tuple(
            str(item).strip().casefold()
            for item in candidate.before_employee.get("capabilities", ())
        )
        if contract.capability not in capabilities:
            raise ValueError("Retention Roster Patch capability differs from hire contract")
        if require_fresh:
            latest_row = conn.execute(
                """
                SELECT * FROM hire_assessments
                WHERE patch_id = ? ORDER BY seq DESC LIMIT 1
                """,
                (assessment.patch_id,),
            ).fetchone()
            if (
                latest_row is None
                or str(latest_row["assessment_id"]) != assessment.assessment_id
            ):
                raise ValueError("Retention Roster Patch assessment is no longer latest")
            observations = tuple(
                self._hire_observation_from_row(item)
                for item in conn.execute(
                    """
                    SELECT * FROM hire_observations
                    WHERE patch_id = ? ORDER BY recorded_at, observation_id
                    """,
                    (assessment.patch_id,),
                ).fetchall()
            )
            attributable = tuple(
                item for item in observations if item.attribution_eligible
            )
            cohort = tuple(item for item in observations if item.cohort_eligible)[
                : contract.maximum_observations
            ]
            if assessment.attributable_observation_ids != tuple(
                item.observation_id for item in attributable
            ) or assessment.cohort_observation_ids != tuple(
                item.observation_id for item in cohort
            ):
                raise ValueError(
                    "Retention Roster Patch assessment is stale after observation"
                )
            expected_decision, expected_reasons = self._expected_hire_assessment_decision(
                contract,
                cohort,
            )
            if (
                assessment.decision.value != expected_decision
                or assessment.reasons != expected_reasons
            ):
                raise ValueError("Retention Roster Patch assessment no longer replays")
        return (assessment,)

    def roster_patch_assessments(
        self,
        patch_id: str,
    ) -> tuple[HireAssessment, ...]:
        candidate = self.get_roster_patch(patch_id)
        with self._lock:
            return self._roster_patch_assessments_in(self._conn, candidate)

    @staticmethod
    def _roster_patch_event(
        conn: sqlite3.Connection,
        *,
        patch_id: str,
        event_type: RosterPatchEventType,
        actor: str,
        payload: Mapping[str, Any],
    ) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM roster_patch_events WHERE patch_id = ?",
            (patch_id,),
        ).fetchone()
        seq = int(row["seq"])
        conn.execute(
            """
            INSERT INTO roster_patch_events(
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

    def create_roster_patch(
        self,
        candidate: RosterPatchCandidate,
        *,
        actor: str,
    ) -> tuple[RosterPatchCandidate, bool]:
        if not actor.strip() or actor.strip() != candidate.proposed_by:
            raise ValueError("Roster Patch proposal actor must be explicit and consistent")
        self._validate_roster_patch_content(candidate)
        payload = canonical_json(candidate)
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM roster_patch_candidates WHERE content_hash = ?",
                (candidate.content_hash,),
            ).fetchone()
            if existing:
                stored = roster_patch_from_dict(_loads(existing["payload_json"]))
                self._validate_roster_patch_content(stored)
                self._roster_patch_evidence_in(conn, stored)
                self._roster_patch_assessments_in(conn, stored, require_fresh=True)
                return stored, False
            for evidence_id in candidate.evidence_ids:
                evidence_row = conn.execute(
                    "SELECT * FROM staffing_demand_evidence WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()
                if evidence_row is None:
                    raise ValueError(f"Roster Patch evidence does not exist: {evidence_id}")
                self._staffing_demand_from_row(evidence_row)
            for assessment_id in candidate.assessment_ids:
                assessment_row = conn.execute(
                    "SELECT * FROM hire_assessments WHERE assessment_id = ?",
                    (assessment_id,),
                ).fetchone()
                if assessment_row is None:
                    raise ValueError(
                        f"Roster Patch assessment does not exist: {assessment_id}"
                    )
                self._hire_assessment_from_row(assessment_row)
            conn.execute(
                """
                INSERT INTO roster_patch_candidates(
                    patch_id, status, operation_type, base_roster_revision,
                    employee_id, payload_json, content_hash, applied_revision,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.patch_id,
                    candidate.status.value,
                    candidate.operation.value,
                    candidate.base_roster_revision,
                    candidate.employee_id,
                    payload,
                    candidate.content_hash,
                    candidate.applied_revision,
                    candidate.created_at,
                    candidate.updated_at,
                ),
            )
            for evidence_id in candidate.evidence_ids:
                conn.execute(
                    """
                    INSERT INTO roster_patch_staffing_evidence(patch_id, evidence_id)
                    VALUES(?, ?)
                    """,
                    (candidate.patch_id, evidence_id),
                )
            for assessment_id in candidate.assessment_ids:
                conn.execute(
                    """
                    INSERT INTO roster_patch_hire_assessments(patch_id, assessment_id)
                    VALUES(?, ?)
                    """,
                    (candidate.patch_id, assessment_id),
                )
            self._roster_patch_evidence_in(conn, candidate)
            self._roster_patch_assessments_in(conn, candidate, require_fresh=True)
            self._roster_patch_event(
                conn,
                patch_id=candidate.patch_id,
                event_type=RosterPatchEventType.PROPOSED,
                actor=actor,
                payload={
                    "operation": candidate.operation,
                    "base_roster_revision": candidate.base_roster_revision,
                    "employee_id": candidate.employee_id,
                    "content_hash": candidate.content_hash,
                    "evidence_ids": candidate.evidence_ids,
                    "assessment_ids": candidate.assessment_ids,
                },
            )
        return candidate, True

    def get_roster_patch(self, patch_id: str) -> RosterPatchCandidate:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM roster_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown roster patch: {patch_id}")
            candidate = roster_patch_from_dict(_loads(row["payload_json"]))
            self._validate_roster_patch_content(candidate)
            self._roster_patch_evidence_in(self._conn, candidate)
            self._roster_patch_assessments_in(self._conn, candidate)
        return candidate

    def list_roster_patches(
        self,
        status: RosterPatchStatus | None = None,
    ) -> tuple[RosterPatchCandidate, ...]:
        sql = "SELECT payload_json FROM roster_patch_candidates"
        parameters: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            parameters = (status.value,)
        sql += " ORDER BY created_at, patch_id"
        with self._lock:
            rows = self._conn.execute(sql, parameters).fetchall()
            patches = tuple(
                roster_patch_from_dict(_loads(row["payload_json"])) for row in rows
            )
            for candidate in patches:
                self._validate_roster_patch_content(candidate)
                self._roster_patch_evidence_in(self._conn, candidate)
                self._roster_patch_assessments_in(self._conn, candidate)
        return patches

    @staticmethod
    def _update_roster_patch(
        conn: sqlite3.Connection,
        candidate: RosterPatchCandidate,
    ) -> None:
        conn.execute(
            """
            UPDATE roster_patch_candidates
            SET status = ?, payload_json = ?, applied_revision = ?, updated_at = ?
            WHERE patch_id = ?
            """,
            (
                candidate.status.value,
                canonical_json(candidate),
                candidate.applied_revision,
                candidate.updated_at,
                candidate.patch_id,
            ),
        )

    def approve_roster_patch(self, patch_id: str, actor: str) -> RosterPatchCandidate:
        if not actor.strip():
            raise ValueError("Roster Patch approval actor must be explicit")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM roster_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown roster patch: {patch_id}")
            candidate = roster_patch_from_dict(_loads(row["payload_json"]))
            self._validate_roster_patch_content(candidate)
            evidence = self._roster_patch_evidence_in(conn, candidate)
            self._roster_patch_assessments_in(conn, candidate, require_fresh=True)
            if any(not item.production_eligible for item in evidence):
                raise ValueError(
                    "Roster Patch recommendation evidence is offline and cannot be approved"
                )
            if candidate.status == RosterPatchStatus.APPROVED:
                return candidate
            if candidate.status != RosterPatchStatus.PROPOSED:
                raise ValueError(
                    f"Only proposed Roster Patches can be approved: {candidate.status.value}"
                )
            updated = candidate.with_status(RosterPatchStatus.APPROVED)
            self._update_roster_patch(conn, updated)
            self._roster_patch_event(
                conn,
                patch_id=patch_id,
                event_type=RosterPatchEventType.APPROVED,
                actor=actor,
                payload={"content_hash": candidate.content_hash},
            )
        return updated

    def reject_roster_patch(
        self,
        patch_id: str,
        actor: str,
        reason: str,
    ) -> RosterPatchCandidate:
        if not actor.strip() or not reason.strip():
            raise ValueError("Roster Patch rejection requires an actor and reason")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM roster_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown roster patch: {patch_id}")
            candidate = roster_patch_from_dict(_loads(row["payload_json"]))
            self._validate_roster_patch_content(candidate)
            self._roster_patch_evidence_in(conn, candidate)
            self._roster_patch_assessments_in(conn, candidate)
            if candidate.status == RosterPatchStatus.REJECTED:
                return candidate
            if candidate.status not in {
                RosterPatchStatus.PROPOSED,
                RosterPatchStatus.APPROVED,
            }:
                raise ValueError(
                    f"Roster Patch cannot be rejected from {candidate.status.value}"
                )
            updated = candidate.with_status(RosterPatchStatus.REJECTED)
            self._update_roster_patch(conn, updated)
            self._roster_patch_event(
                conn,
                patch_id=patch_id,
                event_type=RosterPatchEventType.REJECTED,
                actor=actor,
                payload={"reason": reason.strip()},
            )
        return updated

    def apply_roster_patch(self, patch_id: str, actor: str) -> RosterPatchCandidate:
        if not actor.strip():
            raise ValueError("Roster Patch apply actor must be explicit")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM roster_patch_candidates WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown roster patch: {patch_id}")
            candidate = roster_patch_from_dict(_loads(row["payload_json"]))
            self._validate_roster_patch_content(candidate)
            evidence = self._roster_patch_evidence_in(conn, candidate)
            self._roster_patch_assessments_in(conn, candidate, require_fresh=True)
            if any(not item.production_eligible for item in evidence):
                raise ValueError(
                    "Roster Patch recommendation evidence is offline and cannot be applied"
                )
            if candidate.status == RosterPatchStatus.APPLIED:
                if evidence:
                    self._get_hire_observation_contract_in(conn, patch_id)
                return candidate
            if candidate.status != RosterPatchStatus.APPROVED:
                raise ValueError("Roster Patch must be explicitly approved before apply")
            active = self._active_revision("active_roster_revision", conn)
            if active != candidate.base_roster_revision:
                raise ValueError(
                    "ROSTER changed since proposal: "
                    f"expected {candidate.base_roster_revision}, got {active}"
                )
            current_row = conn.execute(
                "SELECT employees_json FROM roster_versions WHERE revision = ?",
                (active,),
            ).fetchone()
            assert current_row is not None
            employees = list(_loads(current_row["employees_json"]))
            matches = tuple(
                index
                for index, employee in enumerate(employees)
                if employee.get("employee_id") == candidate.employee_id
            )
            if candidate.operation == RosterPatchOperation.ADD_EMPLOYEE:
                if candidate.before_employee is not None or matches:
                    raise ValueError("ADD_EMPLOYEE no longer matches the base ROSTER")
                employees.append(dict(candidate.after_employee))
            else:
                if len(matches) != 1:
                    raise ValueError("Roster Patch target no longer exists exactly once")
                index = matches[0]
                if employees[index] != candidate.before_employee:
                    raise ValueError("Roster Patch before_employee no longer matches")
                employees[index] = dict(candidate.after_employee)

            revision = active + 1
            now = utc_now().isoformat()
            version = RosterVersion(
                revision=revision,
                parent_revision=active,
                employees=tuple(employees),
                created_at=now,
            )
            from .roster import decode_active_roster

            decode_active_roster(version)
            conn.execute(
                """
                INSERT INTO roster_versions(
                    revision, parent_revision, employees_json, created_at
                ) VALUES(?, ?, ?, ?)
                """,
                (revision, active, canonical_json(employees), now),
            )
            conn.execute(
                "UPDATE company_state_meta SET value = ? WHERE key = 'active_roster_revision'",
                (str(revision),),
            )
            updated = candidate.with_status(
                RosterPatchStatus.APPLIED,
                applied_revision=revision,
            )
            self._update_roster_patch(conn, updated)
            if evidence:
                self._insert_hire_observation_contract(
                    conn,
                    HireObservationContract.create(
                        updated,
                        evidence,
                        created_at=now,
                    ),
                )
            self._roster_patch_event(
                conn,
                patch_id=patch_id,
                event_type=RosterPatchEventType.APPLIED,
                actor=actor,
                payload={
                    "from_revision": active,
                    "to_revision": revision,
                    "hire_observation_contract_created": bool(evidence),
                },
            )
        return updated

    def list_roster_patch_events(self, patch_id: str) -> tuple[RosterPatchEvent, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM roster_patch_events WHERE patch_id = ? ORDER BY seq",
                (patch_id,),
            ).fetchall()
        return tuple(
            RosterPatchEvent(
                event_id=str(row["event_id"]),
                patch_id=str(row["patch_id"]),
                seq=int(row["seq"]),
                event_type=RosterPatchEventType(row["event_type"]),
                actor=str(row["actor"]),
                payload=_loads(row["payload_json"]),
                occurred_at=str(row["occurred_at"]),
            )
            for row in rows
        )
