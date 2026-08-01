"""Hire observation, assessment, and retention review lifecycle."""
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


class CompanyHireObservationMixin:
    def _get_hire_observation_contract_in(
        self,
        conn: sqlite3.Connection,
        patch_id: str,
    ) -> HireObservationContract:
        row = conn.execute(
            """
            SELECT payload_json, content_hash FROM hire_observation_contracts
            WHERE patch_id = ?
            """,
            (patch_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Roster Patch has no hire observation contract: {patch_id}")
        contract = hire_observation_contract_from_dict(_loads(row["payload_json"]))
        if (
            content_digest(contract.content_payload()) != contract.content_hash
            or contract.content_hash != row["content_hash"]
        ):
            raise RuntimeError(f"Hire observation contract integrity check failed: {patch_id}")
        patch_row = conn.execute(
            "SELECT payload_json FROM roster_patch_candidates WHERE patch_id = ?",
            (patch_id,),
        ).fetchone()
        if patch_row is None:
            raise RuntimeError(f"Hire observation source patch is missing: {patch_id}")
        candidate = roster_patch_from_dict(_loads(patch_row["payload_json"]))
        self._validate_roster_patch_content(candidate)
        evidence = self._roster_patch_evidence_in(conn, candidate)
        expected = HireObservationContract.create(
            candidate,
            evidence,
            created_at=contract.created_at,
        )
        if expected != contract:
            raise RuntimeError(f"Hire observation contract source mismatch: {patch_id}")
        return contract

    def get_hire_observation_contract(self, patch_id: str) -> HireObservationContract:
        with self._lock:
            return self._get_hire_observation_contract_in(self._conn, patch_id)

    def list_hire_observation_contracts(self) -> tuple[HireObservationContract, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT patch_id FROM hire_observation_contracts ORDER BY created_at, patch_id"
            ).fetchall()
            return tuple(
                self._get_hire_observation_contract_in(self._conn, str(row["patch_id"]))
                for row in rows
            )

    @staticmethod
    def _hire_observation_from_row(row: sqlite3.Row) -> HireObservation:
        observation = hire_observation_from_dict(_loads(row["payload_json"]))
        if (
            content_digest(observation.content_payload()) != observation.content_hash
            or observation.content_hash != row["content_hash"]
        ):
            raise RuntimeError(
                f"Hire observation integrity check failed: {observation.observation_id}"
            )
        return observation

    @staticmethod
    def _hire_observation_base_reasons(
        observation: HireObservation,
        contract: HireObservationContract,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if not observation.source.production_eligible:
            reasons.append("non_production_evidence")
        if observation.context_fingerprint != contract.context_fingerprint:
            reasons.append("context_fingerprint_mismatch")
        if observation.execution_profile != contract.execution_profile:
            reasons.append("execution_profile_mismatch")
        if observation.base_roster_revision < contract.applied_roster_revision:
            reasons.append("pre_hire_roster_revision")
        if not observation.capability_task_ids:
            reasons.append("capability_task_missing")
        if not observation.measured_task_ids:
            reasons.append("assignment_not_measured")
        return tuple(reasons)

    def _validate_hire_observation_source(
        self,
        conn: sqlite3.Connection,
        observation: HireObservation,
        contract: HireObservationContract,
        *,
        expected_limit_reached: bool | None = None,
    ) -> None:
        episode_row = conn.execute(
            "SELECT payload_json FROM organization_episodes WHERE episode_id = ?",
            (observation.episode_id,),
        ).fetchone()
        if episode_row is None:
            raise ValueError("Hire observation requires a persisted organization episode")
        episode = organization_episode_from_dict(_loads(episode_row["payload_json"]))
        exact_episode_fields = (
            observation.job_id == episode.job_id,
            observation.source == episode.source,
            observation.context_fingerprint == episode.context_fingerprint,
            observation.execution_profile == episode.execution_profile,
            observation.job_succeeded == episode.success,
            observation.validation_attempts == episode.validation_attempts,
            observation.safety_violations == episode.safety_violations,
            observation.writer_count == episode.writer_count,
            observation.approvals_requested == episode.approvals_requested,
            observation.approvals_granted == episode.approvals_granted,
            observation.preapproval_mutations == episode.preapproval_mutations,
            observation.organization_ledger_digest == episode.ledger_digest,
        )
        if not all(exact_episode_fields):
            raise ValueError("Hire observation does not match its organization episode")
        expected_capability_tasks = tuple(
            sorted(
                task.task_key
                for task in episode.plan_template
                if contract.capability
                in tuple(item.strip().casefold() for item in task.required_capabilities)
            )
        )
        if observation.capability_task_ids != expected_capability_tasks:
            raise ValueError("Hire observation capability tasks do not match the final graph")
        if observation.measured_task_ids != tuple(sorted(set(observation.measured_task_ids))):
            raise ValueError("Hire observation measured task ids must be unique and sorted")
        if not set(observation.measured_task_ids).issubset(observation.capability_task_ids):
            raise ValueError("Hire observation measured tasks are outside the capability cohort")
        if (
            observation.persistent_employee_assigned
            or observation.temporary_fallback_used
        ) and not observation.measured_task_ids:
            raise ValueError("Hire observation assignment claims require measured tasks")
        if len(observation.assignment_ledger_digest) != 64:
            raise ValueError("Hire observation assignment ledger digest is malformed")

        base_reasons = self._hire_observation_base_reasons(observation, contract)
        expected_attribution = not base_reasons
        if observation.attribution_eligible != expected_attribution:
            raise ValueError("Hire observation attribution eligibility is inconsistent")
        if expected_limit_reached is None:
            if expected_attribution:
                if observation.cohort_eligible:
                    expected_reasons = ()
                else:
                    expected_reasons = ("observation_limit_reached",)
            else:
                expected_reasons = base_reasons
        else:
            expected_reasons = base_reasons + (
                ("observation_limit_reached",)
                if expected_attribution and expected_limit_reached
                else ()
            )
            expected_cohort = expected_attribution and not expected_limit_reached
            if observation.cohort_eligible != expected_cohort:
                raise ValueError("Hire observation cohort eligibility is inconsistent")
        if observation.ineligibility_reasons != expected_reasons:
            raise ValueError("Hire observation ineligibility reasons are inconsistent")
        if observation.cohort_eligible and not observation.attribution_eligible:
            raise ValueError("Hire observation cohort requires attribution eligibility")

    def record_hire_observation(
        self,
        observation: HireObservation,
    ) -> tuple[HireObservation, bool]:
        if content_digest(observation.content_payload()) != observation.content_hash:
            raise ValueError("Hire observation content hash does not match payload")
        payload = canonical_json(observation)
        with self._transaction() as conn:
            contract = self._get_hire_observation_contract_in(conn, observation.patch_id)
            existing = conn.execute(
                """
                SELECT * FROM hire_observations
                WHERE patch_id = ? AND episode_id = ?
                """,
                (observation.patch_id, observation.episode_id),
            ).fetchone()
            if existing:
                if existing["content_hash"] != observation.content_hash:
                    raise ValueError(
                        "Hire observation episode was reused with different attribution"
                    )
                stored = self._hire_observation_from_row(existing)
                self._validate_hire_observation_source(conn, stored, contract)
                return stored, False
            cohort_row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM hire_observations
                WHERE patch_id = ? AND cohort_eligible = 1
                """,
                (observation.patch_id,),
            ).fetchone()
            limit_reached = int(cohort_row["count"]) >= contract.maximum_observations
            self._validate_hire_observation_source(
                conn,
                observation,
                contract,
                expected_limit_reached=limit_reached,
            )
            conn.execute(
                """
                INSERT INTO hire_observations(
                    observation_id, patch_id, episode_id, job_id,
                    attribution_eligible, cohort_eligible,
                    persistent_employee_assigned, temporary_fallback_used,
                    payload_json, content_hash, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.patch_id,
                    observation.episode_id,
                    observation.job_id,
                    int(observation.attribution_eligible),
                    int(observation.cohort_eligible),
                    int(observation.persistent_employee_assigned),
                    int(observation.temporary_fallback_used),
                    payload,
                    observation.content_hash,
                    observation.recorded_at,
                ),
            )
        return observation, True

    def list_hire_observations(self, patch_id: str) -> tuple[HireObservation, ...]:
        contract = self.get_hire_observation_contract(patch_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM hire_observations
                WHERE patch_id = ? ORDER BY recorded_at, observation_id
                """,
                (patch_id,),
            ).fetchall()
            observations = tuple(self._hire_observation_from_row(row) for row in rows)
            for observation in observations:
                self._validate_hire_observation_source(
                    self._conn,
                    observation,
                    contract,
                )
        return observations

    @staticmethod
    def _hire_assessment_from_row(row: sqlite3.Row) -> HireAssessment:
        assessment = hire_assessment_from_dict(_loads(row["payload_json"]))
        if (
            content_digest(assessment.content_payload()) != assessment.content_hash
            or assessment.content_hash != row["content_hash"]
        ):
            raise RuntimeError(
                f"Hire assessment integrity check failed: {assessment.assessment_id}"
            )
        return assessment

    @staticmethod
    def _expected_hire_assessment_decision(
        contract: HireObservationContract,
        cohort: tuple[HireObservation, ...],
    ) -> tuple[str, tuple[str, ...]]:
        if any(not item.job_succeeded for item in cohort):
            return "DORMANCY_CANDIDATE", ("attributed_job_failure",)
        if contract.fail_on_safety_violation and any(
            not item.safety_passed for item in cohort
        ):
            return "DORMANCY_CANDIDATE", ("attributed_safety_violation",)
        if len(cohort) < contract.minimum_observations:
            return "INSUFFICIENT_OBSERVATION", (
                "minimum_observation_count_not_reached",
            )
        if all(item.persistent_employee_assigned for item in cohort) and not any(
            item.temporary_fallback_used for item in cohort
        ):
            return "KEEP", ("persistent_hire_replaced_temporary_staffing",)
        persistent_count = sum(item.persistent_employee_assigned for item in cohort)
        fallback_count = sum(item.temporary_fallback_used for item in cohort)
        if len(cohort) >= contract.maximum_observations and persistent_count == 0:
            return "DORMANCY_CANDIDATE", ("hire_unused_within_observation_limit",)
        if len(cohort) >= contract.maximum_observations and fallback_count >= 2:
            return "DORMANCY_CANDIDATE", ("temporary_fallback_repeated",)
        return "INSUFFICIENT_OBSERVATION", ("staffing_replacement_not_yet_proven",)

    def record_hire_assessment(
        self,
        assessment: HireAssessment,
    ) -> tuple[HireAssessment, bool]:
        if content_digest(assessment.content_payload()) != assessment.content_hash:
            raise ValueError("Hire assessment content hash does not match payload")
        payload = canonical_json(assessment)
        with self._transaction() as conn:
            contract = self._get_hire_observation_contract_in(conn, assessment.patch_id)
            existing = conn.execute(
                """
                SELECT * FROM hire_assessments
                WHERE patch_id = ? AND content_hash = ?
                """,
                (assessment.patch_id, assessment.content_hash),
            ).fetchone()
            if existing:
                return self._hire_assessment_from_row(existing), False
            rows = conn.execute(
                """
                SELECT * FROM hire_observations
                WHERE patch_id = ? ORDER BY recorded_at, observation_id
                """,
                (assessment.patch_id,),
            ).fetchall()
            observations = tuple(self._hire_observation_from_row(row) for row in rows)
            attributable = tuple(item for item in observations if item.attribution_eligible)
            cohort = tuple(item for item in observations if item.cohort_eligible)[
                : contract.maximum_observations
            ]
            if assessment.attributable_observation_ids != tuple(
                item.observation_id for item in attributable
            ) or assessment.cohort_observation_ids != tuple(
                item.observation_id for item in cohort
            ):
                raise ValueError("Hire assessment does not reference the current observation set")
            persistent_count = sum(item.persistent_employee_assigned for item in cohort)
            fallback_count = sum(item.temporary_fallback_used for item in cohort)
            if (
                assessment.persistent_assignment_count != persistent_count
                or assessment.temporary_fallback_count != fallback_count
            ):
                raise ValueError("Hire assessment staffing counts are inconsistent")
            expected_decision, expected_reasons = self._expected_hire_assessment_decision(
                contract,
                cohort,
            )
            if (
                assessment.decision.value != expected_decision
                or assessment.reasons != expected_reasons
            ):
                raise ValueError("Hire assessment decision is inconsistent")
            seq_row = conn.execute(
                """
                SELECT COALESCE(MAX(seq), 0) + 1 AS seq
                FROM hire_assessments WHERE patch_id = ?
                """,
                (assessment.patch_id,),
            ).fetchone()
            expected_seq = int(seq_row["seq"])
            if assessment.seq != expected_seq:
                raise ValueError(f"Hire assessment sequence must be {expected_seq}")
            conn.execute(
                """
                INSERT INTO hire_assessments(
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

    def list_hire_assessments(self, patch_id: str) -> tuple[HireAssessment, ...]:
        self.get_hire_observation_contract(patch_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM hire_assessments
                WHERE patch_id = ? ORDER BY seq
                """,
                (patch_id,),
            ).fetchall()
        return tuple(self._hire_assessment_from_row(row) for row in rows)

    def latest_hire_assessment(self, patch_id: str) -> HireAssessment | None:
        assessments = self.list_hire_assessments(patch_id)
        return assessments[-1] if assessments else None

    def record_retention_review(
        self,
        review: RosterRetentionReview,
    ) -> tuple[RosterRetentionReview, bool]:
        if review.review_id != f"retention-review-{review.content_hash[:24]}":
            raise ValueError("Retention review id does not match content hash")
        if content_digest(review.content_payload()) != review.content_hash:
            raise ValueError("Retention review content hash does not match payload")
        expected_decisions = {
            RetentionReviewMode.APPROVAL: {"PENDING_USER_APPROVAL"},
            RetentionReviewMode.AUTO_REVIEW: {
                "AUTO_APPROVED",
                "REQUIRES_USER_APPROVAL",
            },
            RetentionReviewMode.ALWAYS_APPROVE: {"APPROVAL_BYPASSED"},
        }
        if review.decision.value not in expected_decisions[review.mode]:
            raise ValueError("Retention review decision does not match its mode")
        if not review.reasons:
            raise ValueError("Retention review requires at least one reason")
        payload = canonical_json(review)
        with self._transaction() as conn:
            candidate_row = conn.execute(
                "SELECT payload_json FROM roster_patch_candidates WHERE patch_id = ?",
                (review.roster_patch_id,),
            ).fetchone()
            if candidate_row is None:
                raise ValueError("Retention review requires a persisted Roster Patch")
            candidate = roster_patch_from_dict(_loads(candidate_row["payload_json"]))
            assessments = self._roster_patch_assessments_in(
                conn,
                candidate,
                require_fresh=True,
            )
            if len(assessments) != 1 or assessments[0].assessment_id != review.assessment_id:
                raise ValueError("Retention review assessment relation does not match")
            if assessments[0].patch_id != review.hire_patch_id:
                raise ValueError("Retention review hire patch does not match assessment")
            company_row = conn.execute(
                "SELECT policies_json FROM company_versions WHERE revision = ?",
                (review.company_revision,),
            ).fetchone()
            if company_row is None:
                raise ValueError("Retention review COMPANY revision does not exist")
            policies = _loads(company_row["policies_json"])
            mode = RetentionReviewMode(
                str(
                    policies.get(
                        "roster_retention_review_mode",
                        RetentionReviewMode.APPROVAL.value,
                    )
                )
            )
            if mode != review.mode:
                raise ValueError("Retention review mode differs from COMPANY revision")
            existing = conn.execute(
                "SELECT * FROM roster_retention_reviews WHERE content_hash = ?",
                (review.content_hash,),
            ).fetchone()
            if existing is not None:
                return self._retention_review_from_row(existing), False
            conn.execute(
                """
                INSERT INTO roster_retention_reviews(
                    review_id, roster_patch_id, hire_patch_id, assessment_id,
                    company_revision, mode, decision, payload_json,
                    content_hash, reviewed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.roster_patch_id,
                    review.hire_patch_id,
                    review.assessment_id,
                    review.company_revision,
                    review.mode.value,
                    review.decision.value,
                    payload,
                    review.content_hash,
                    review.reviewed_at,
                ),
            )
        return review, True

    @staticmethod
    def _retention_review_from_row(row: sqlite3.Row) -> RosterRetentionReview:
        review = roster_retention_review_from_dict(_loads(row["payload_json"]))
        if (
            content_digest(review.content_payload()) != review.content_hash
            or review.content_hash != row["content_hash"]
        ):
            raise RuntimeError(
                f"Retention review integrity check failed: {review.review_id}"
            )
        return review

    def list_retention_reviews(
        self,
        roster_patch_id: str | None = None,
    ) -> tuple[RosterRetentionReview, ...]:
        sql = "SELECT * FROM roster_retention_reviews"
        parameters: tuple[object, ...] = ()
        if roster_patch_id is not None:
            sql += " WHERE roster_patch_id = ?"
            parameters = (roster_patch_id,)
        sql += " ORDER BY reviewed_at, review_id"
        with self._lock:
            rows = self._conn.execute(sql, parameters).fetchall()
        return tuple(self._retention_review_from_row(row) for row in rows)


