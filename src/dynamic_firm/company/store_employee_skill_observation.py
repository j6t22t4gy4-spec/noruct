"""Employee Skill post-apply observation and assessment lifecycle."""
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


class CompanyEmployeeSkillObservationMixin:
    @staticmethod
    def _employee_skill_observation_from_row(
        row: sqlite3.Row,
    ) -> EmployeeSkillObservation:
        observation = employee_skill_observation_from_dict(_loads(row["payload_json"]))
        if (
            content_digest(observation.content_payload()) != observation.content_hash
            or observation.content_hash != row["content_hash"]
            or observation.observation_id
            != f"employee-skill-observation-{observation.content_hash[:24]}"
        ):
            raise RuntimeError("Employee Skill observation integrity failed")
        return observation

    def record_employee_skill_observation(
        self,
        observation: EmployeeSkillObservation,
    ) -> tuple[EmployeeSkillObservation, bool]:
        with self._transaction() as conn:
            contract = self._get_employee_skill_observation_contract_in(
                conn, observation.patch_id
            )
            episode = self.get_episode(observation.episode_id)
            if (
                episode.job_id != observation.job_id
                or episode.success != observation.job_succeeded
                or episode.validation_attempts != observation.validation_attempts
                or episode.safety_violations != observation.safety_violations
                or episode.writer_count != observation.writer_count
                or episode.approvals_requested != observation.approvals_requested
                or episode.approvals_granted != observation.approvals_granted
                or episode.preapproval_mutations != observation.preapproval_mutations
            ):
                raise ValueError("Employee Skill observation differs from source episode")
            if observation.attribution_eligible and (
                episode.context_fingerprint != contract.context_key
                or not observation.skill_exposed
            ):
                raise ValueError("Employee Skill attribution is inconsistent")
            if content_digest(observation.content_payload()) != observation.content_hash:
                raise ValueError("Employee Skill observation content hash differs")
            existing = conn.execute(
                """
                SELECT * FROM employee_skill_observations
                WHERE patch_id = ? AND episode_id = ?
                """,
                (observation.patch_id, observation.episode_id),
            ).fetchone()
            if existing is not None:
                stored = self._employee_skill_observation_from_row(existing)
                if stored.content_hash != observation.content_hash:
                    raise ValueError("Employee Skill episode already has different observation")
                return stored, False
            conn.execute(
                """
                INSERT INTO employee_skill_observations(
                    observation_id, patch_id, episode_id, job_id,
                    attribution_eligible, cohort_eligible, payload_json,
                    content_hash, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.patch_id,
                    observation.episode_id,
                    observation.job_id,
                    int(observation.attribution_eligible),
                    int(observation.cohort_eligible),
                    canonical_json(observation),
                    observation.content_hash,
                    observation.recorded_at,
                ),
            )
        return observation, True

    def list_employee_skill_observations(
        self,
        patch_id: str,
    ) -> tuple[EmployeeSkillObservation, ...]:
        self.get_employee_skill_observation_contract(patch_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM employee_skill_observations
                WHERE patch_id = ? ORDER BY recorded_at, observation_id
                """,
                (patch_id,),
            ).fetchall()
        return tuple(self._employee_skill_observation_from_row(row) for row in rows)

    @staticmethod
    def _employee_skill_assessment_from_row(
        row: sqlite3.Row,
    ) -> EmployeeSkillAssessment:
        assessment = employee_skill_assessment_from_dict(_loads(row["payload_json"]))
        if (
            content_digest(assessment.content_payload()) != assessment.content_hash
            or assessment.content_hash != row["content_hash"]
            or assessment.assessment_id
            != f"employee-skill-assessment-{assessment.content_hash[:24]}"
        ):
            raise RuntimeError("Employee Skill assessment integrity failed")
        return assessment

    def record_employee_skill_assessment(
        self,
        assessment: EmployeeSkillAssessment,
    ) -> tuple[EmployeeSkillAssessment, bool]:
        if content_digest(assessment.content_payload()) != assessment.content_hash:
            raise ValueError("Employee Skill assessment content hash differs")
        with self._transaction() as conn:
            contract = self._get_employee_skill_observation_contract_in(
                conn, assessment.patch_id
            )
            existing = conn.execute(
                """
                SELECT * FROM employee_skill_assessments
                WHERE patch_id = ? AND content_hash = ?
                """,
                (assessment.patch_id, assessment.content_hash),
            ).fetchone()
            if existing is not None:
                return self._employee_skill_assessment_from_row(existing), False
            rows = conn.execute(
                """
                SELECT * FROM employee_skill_observations
                WHERE patch_id = ? ORDER BY recorded_at, observation_id
                """,
                (assessment.patch_id,),
            ).fetchall()
            cohort = tuple(
                self._employee_skill_observation_from_row(row)
                for row in rows
                if bool(row["cohort_eligible"])
            )[: contract.maximum_observations]
            if assessment.observation_ids != tuple(item.observation_id for item in cohort):
                raise ValueError("Employee Skill assessment observation set differs")
            exposed_count = sum(item.skill_exposed for item in cohort)
            if exposed_count != assessment.exposed_count:
                raise ValueError("Employee Skill assessment exposed count differs")
            if any(not item.safety_passed for item in cohort):
                expected_decision = EmployeeSkillAssessmentDecision.ROLLBACK_CANDIDATE
                expected_reasons = ("attributed_failure_or_safety_violation",)
            elif len(cohort) < contract.minimum_observations:
                expected_decision = EmployeeSkillAssessmentDecision.INSUFFICIENT_OBSERVATION
                expected_reasons = ("minimum_observation_count_not_reached",)
            elif all(item.skill_exposed for item in cohort):
                expected_decision = EmployeeSkillAssessmentDecision.KEEP
                expected_reasons = ("exact_skill_version_used_safely",)
            elif len(cohort) >= contract.maximum_observations:
                expected_decision = EmployeeSkillAssessmentDecision.ROLLBACK_CANDIDATE
                expected_reasons = ("skill_not_exposed_within_observation_limit",)
            else:
                expected_decision = EmployeeSkillAssessmentDecision.INSUFFICIENT_OBSERVATION
                expected_reasons = ("skill_exposure_not_yet_proven",)
            if (
                assessment.decision != expected_decision
                or assessment.reasons != expected_reasons
            ):
                raise ValueError("Employee Skill assessment decision is inconsistent")
            seq_row = conn.execute(
                """
                SELECT COALESCE(MAX(seq), 0) + 1 AS seq
                FROM employee_skill_assessments WHERE patch_id = ?
                """,
                (assessment.patch_id,),
            ).fetchone()
            if assessment.seq != int(seq_row["seq"]):
                raise ValueError("Employee Skill assessment sequence differs")
            conn.execute(
                """
                INSERT INTO employee_skill_assessments(
                    assessment_id, patch_id, seq, decision, payload_json,
                    content_hash, assessed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.patch_id,
                    assessment.seq,
                    assessment.decision.value,
                    canonical_json(assessment),
                    assessment.content_hash,
                    assessment.assessed_at,
                ),
            )
        return assessment, True

    def list_employee_skill_assessments(
        self,
        patch_id: str,
    ) -> tuple[EmployeeSkillAssessment, ...]:
        self.get_employee_skill_observation_contract(patch_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM employee_skill_assessments
                WHERE patch_id = ? ORDER BY seq
                """,
                (patch_id,),
            ).fetchall()
        return tuple(self._employee_skill_assessment_from_row(row) for row in rows)
