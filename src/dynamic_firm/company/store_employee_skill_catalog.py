"""Employee Skill evidence, current-version, and proposal validation lifecycle."""
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


class CompanyEmployeeSkillCatalogMixin:
    @staticmethod
    def _employee_skill_evidence_from_row(row: sqlite3.Row) -> EmployeeSkillEvidence:
        evidence = employee_skill_evidence_from_dict(_loads(row["payload_json"]))
        if (
            content_digest(evidence.content_payload()) != evidence.content_hash
            or evidence.content_hash != row["content_hash"]
            or evidence.evidence_id != f"employee-skill-evidence-{evidence.content_hash[:24]}"
        ):
            raise RuntimeError(
                f"Employee Skill evidence integrity check failed: {evidence.evidence_id}"
            )
        return evidence

    def _validate_employee_skill_evidence_source(
        self,
        conn: sqlite3.Connection,
        evidence: EmployeeSkillEvidence,
    ) -> None:
        if evidence.kind == EmployeeSkillEvidenceKind.USER_CORRECTION:
            if (
                evidence.source != EvidenceSource.USER_CORRECTION
                or not evidence.confirmed_by_user
                or not evidence.job_succeeded
                or not evidence.validation_passed
                or not evidence.safety_passed
            ):
                raise ValueError("Employee Skill correction evidence is not confirmed")
            return
        if evidence.confirmed_by_user:
            raise ValueError("Employee Skill job evidence cannot claim user confirmation")
        row = conn.execute(
            "SELECT payload_json FROM organization_episodes WHERE episode_id = ?",
            (evidence.source_ref,),
        ).fetchone()
        if row is None:
            raise ValueError("Employee Skill job evidence source episode does not exist")
        episode = organization_episode_from_dict(_loads(row["payload_json"]))
        if (
            episode.source != evidence.source
            or episode.context_fingerprint != evidence.context_key
            or episode.success != evidence.job_succeeded
            or (bool(episode.validation_attempts) and all(episode.validation_attempts))
            != evidence.validation_passed
            or episode.safety_passed != evidence.safety_passed
        ):
            raise ValueError("Employee Skill job evidence differs from source episode")

    def record_employee_skill_evidence(
        self,
        evidence: EmployeeSkillEvidence,
    ) -> tuple[EmployeeSkillEvidence, bool]:
        payload = canonical_json(evidence)
        if content_digest(evidence.content_payload()) != evidence.content_hash:
            raise ValueError("Employee Skill evidence content hash does not match")
        if evidence.evidence_id != f"employee-skill-evidence-{evidence.content_hash[:24]}":
            raise ValueError("Employee Skill evidence id does not match content hash")
        with self._transaction() as conn:
            self._validate_employee_skill_evidence_source(conn, evidence)
            existing = conn.execute(
                "SELECT * FROM employee_skill_evidence WHERE content_hash = ?",
                (evidence.content_hash,),
            ).fetchone()
            if existing is not None:
                return self._employee_skill_evidence_from_row(existing), False
            conn.execute(
                """
                INSERT INTO employee_skill_evidence(
                    evidence_id, kind, source_ref, source_kind, employee_id,
                    skill_key, context_key, procedure_hash, payload_json,
                    content_hash, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.kind.value,
                    evidence.source_ref,
                    evidence.source.value,
                    evidence.employee_id,
                    evidence.skill_key,
                    evidence.context_key,
                    evidence.procedure_hash,
                    payload,
                    evidence.content_hash,
                    evidence.recorded_at,
                ),
            )
        return evidence, True

    def get_employee_skill_evidence(self, evidence_id: str) -> EmployeeSkillEvidence:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM employee_skill_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Employee Skill evidence: {evidence_id}")
            evidence = self._employee_skill_evidence_from_row(row)
            self._validate_employee_skill_evidence_source(self._conn, evidence)
        return evidence

    def list_employee_skill_evidence(self) -> tuple[EmployeeSkillEvidence, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM employee_skill_evidence ORDER BY recorded_at, evidence_id"
            ).fetchall()
            evidence = tuple(self._employee_skill_evidence_from_row(row) for row in rows)
            for item in evidence:
                self._validate_employee_skill_evidence_source(self._conn, item)
        return evidence

    @staticmethod
    def _employee_skill_version_from_row(row: sqlite3.Row) -> EmployeeSkillVersion:
        version = employee_skill_version_from_dict(_loads(row["payload_json"]))
        if (
            content_digest(version.content_payload()) != version.content_hash
            or version.content_hash != row["content_hash"]
            or version.version_id != f"employee-skill-version-{version.content_hash[:24]}"
        ):
            raise RuntimeError(
                f"Employee Skill version integrity check failed: {version.version_id}"
            )
        if version.active != (version.procedure is not None):
            raise RuntimeError("Employee Skill active version/procedure invariant failed")
        return version

    def _current_employee_skill_in(
        self,
        conn: sqlite3.Connection,
        employee_id: str,
        skill_key: str,
        context_key: str,
    ) -> EmployeeSkillVersion | None:
        row = conn.execute(
            """
            SELECT versions.* FROM employee_skill_heads AS heads
            JOIN employee_skill_versions AS versions
              ON versions.version_id = heads.current_version_id
            WHERE heads.employee_id = ? AND heads.skill_key = ? AND heads.context_key = ?
            """,
            (employee_id, skill_key, context_key),
        ).fetchone()
        return None if row is None else self._employee_skill_version_from_row(row)

    def current_employee_skill(
        self,
        employee_id: str,
        skill_key: str,
        context_key: str,
    ) -> EmployeeSkillVersion | None:
        with self._lock:
            return self._current_employee_skill_in(
                self._conn, employee_id, skill_key, context_key
            )

    def list_employee_skills(
        self,
        *,
        employee_id: str | None = None,
        context_key: str | None = None,
        active_only: bool = False,
    ) -> tuple[EmployeeSkillVersion, ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        if employee_id is not None:
            clauses.append("heads.employee_id = ?")
            parameters.append(employee_id)
        if context_key is not None:
            clauses.append("heads.context_key = ?")
            parameters.append(context_key)
        if active_only:
            clauses.append("versions.active = 1")
        sql = """
            SELECT versions.* FROM employee_skill_heads AS heads
            JOIN employee_skill_versions AS versions
              ON versions.version_id = heads.current_version_id
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY heads.employee_id, heads.context_key, heads.skill_key"
        with self._lock:
            rows = self._conn.execute(sql, tuple(parameters)).fetchall()
        return tuple(self._employee_skill_version_from_row(row) for row in rows)

    def employee_skill_runtime_snapshots(
        self,
        employee_ids: Sequence[str],
        context_key: str,
    ) -> Mapping[str, tuple[VersionedContent, ...]]:
        normalized = tuple(dict.fromkeys(str(item).strip() for item in employee_ids))
        result: dict[str, tuple[VersionedContent, ...]] = {}
        for employee_id in normalized:
            versions = self.list_employee_skills(
                employee_id=employee_id,
                context_key=context_key,
                active_only=True,
            )
            result[employee_id] = tuple(
                VersionedContent(
                    content_id=(
                        f"employee-skill:{item.employee_id}:{item.skill_key}:{item.context_key}"
                    ),
                    revision=str(item.revision),
                    content=canonical_json(
                        {
                            "contract": "noruct.employee-skill.v1",
                            "precedence": (
                                "COMPANY_AND_ACTION_POLICY_THEN_PLAYBOOK_THEN_EMPLOYEE_SKILL"
                            ),
                            "procedure": item.procedure,
                        }
                    ),
                    content_hash=item.content_hash,
                )
                for item in versions
            )
        return result

    @staticmethod
    def _employee_skill_patch_event(
        conn: sqlite3.Connection,
        *,
        patch_id: str,
        event_type: EmployeeSkillPatchEventType,
        actor: str,
        payload: Mapping[str, Any],
    ) -> None:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(seq), 0) + 1 AS seq
            FROM employee_skill_patch_events WHERE patch_id = ?
            """,
            (patch_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO employee_skill_patch_events(
                event_id, patch_id, seq, event_type, actor, payload_json, occurred_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                patch_id,
                int(row["seq"]),
                event_type.value,
                actor,
                canonical_json(payload),
                utc_now().isoformat(),
            ),
        )

    @staticmethod
    def _validate_employee_skill_patch_content(
        candidate: EmployeeSkillPatchCandidate,
    ) -> None:
        if candidate.patch_id != f"employee-skill-patch-{candidate.content_hash[:24]}":
            raise ValueError("Employee Skill Patch id does not match content hash")
        if content_digest(candidate.content_payload()) != candidate.content_hash:
            raise ValueError("Employee Skill Patch content hash does not match payload")
        if (
            candidate.procedure.authority_scope != "INHERIT_ONLY"
            or candidate.procedure.workflow_scope != "INHERIT_ONLY"
        ):
            raise ValueError("Employee Skill Patch cannot override authority or workflow")
        if candidate.status == EmployeeSkillPatchStatus.APPLIED:
            if candidate.applied_skill_revision is None:
                raise ValueError("Applied Employee Skill Patch lacks a revision")
        elif candidate.status == EmployeeSkillPatchStatus.ROLLED_BACK:
            if (
                candidate.applied_skill_revision is None
                or candidate.rolled_back_skill_revision is None
            ):
                raise ValueError("Rolled-back Employee Skill Patch lacks revisions")

    def _employee_skill_patch_evidence_in(
        self,
        conn: sqlite3.Connection,
        candidate: EmployeeSkillPatchCandidate,
    ) -> tuple[EmployeeSkillEvidence, ...]:
        rows = conn.execute(
            """
            SELECT evidence.* FROM employee_skill_patch_evidence AS links
            JOIN employee_skill_evidence AS evidence
              ON evidence.evidence_id = links.evidence_id
            WHERE links.patch_id = ? ORDER BY evidence.evidence_id
            """,
            (candidate.patch_id,),
        ).fetchall()
        evidence = tuple(self._employee_skill_evidence_from_row(row) for row in rows)
        if tuple(item.evidence_id for item in evidence) != tuple(sorted(candidate.evidence_ids)):
            raise ValueError("Employee Skill Patch evidence relation differs from payload")
        procedure_hash = content_digest(candidate.procedure.content_payload())
        for item in evidence:
            self._validate_employee_skill_evidence_source(conn, item)
            if (
                item.employee_id,
                item.skill_key,
                item.context_key,
                item.procedure_hash,
            ) != (
                candidate.procedure.employee_id,
                candidate.procedure.skill_key,
                candidate.procedure.context_key,
                procedure_hash,
            ):
                raise ValueError("Employee Skill Patch evidence no longer matches")
        user_path = (
            len(evidence) == 1
            and evidence[0].kind == EmployeeSkillEvidenceKind.USER_CORRECTION
            and evidence[0].confirmed_by_user
        )
        job_path = (
            len(evidence) >= 2
            and len({item.source_ref for item in evidence}) == len(evidence)
            and all(
                item.kind == EmployeeSkillEvidenceKind.VERIFIED_JOB_PROCEDURE
                and item.source.production_eligible
                and item.job_succeeded
                and item.validation_passed
                and item.safety_passed
                for item in evidence
            )
        )
        if not user_path and not job_path:
            raise ValueError("Employee Skill Patch evidence gate failed")
        return evidence

    def _validate_employee_skill_patch_fresh_in(
        self,
        conn: sqlite3.Connection,
        candidate: EmployeeSkillPatchCandidate,
    ) -> None:
        if self._active_revision("active_company_revision", conn) != candidate.base_company_revision:
            raise ValueError("COMPANY changed since Employee Skill proposal")
        if self._active_revision("active_playbook_revision", conn) != candidate.base_playbook_revision:
            raise ValueError("PLAYBOOK changed since Employee Skill proposal")
        if self._active_revision("active_roster_revision", conn) != candidate.base_roster_revision:
            raise ValueError("ROSTER changed since Employee Skill proposal")
        roster_row = conn.execute(
            "SELECT employees_json FROM roster_versions WHERE revision = ?",
            (candidate.base_roster_revision,),
        ).fetchone()
        assert roster_row is not None
        employees = _loads(roster_row["employees_json"])
        targets = tuple(
            item
            for item in employees
            if item.get("employee_id") == candidate.procedure.employee_id
            and item.get("active") is True
            and item.get("temporary") is not True
        )
        if len(targets) != 1:
            raise ValueError("Employee Skill target is no longer one active employee")
        current = self._current_employee_skill_in(
            conn,
            candidate.procedure.employee_id,
            candidate.procedure.skill_key,
            candidate.procedure.context_key,
        )
        current_revision = 0 if current is None else current.revision
        current_procedure = (
            None if current is None or not current.active else current.procedure
        )
        if (
            current_revision != candidate.base_skill_revision
            or current_procedure != candidate.before_procedure
        ):
            raise ValueError("Employee Skill changed since proposal")
        if current_procedure == candidate.procedure:
            raise ValueError("Employee Skill proposal does not change the active procedure")
        self._employee_skill_patch_evidence_in(conn, candidate)
