"""Company roster, hiring, retention, and Employee Skill domain contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from dynamic_firm.runtime.models import to_primitive, utc_now

from .models import (
    EmployeeSkillAssessmentDecision,
    EmployeeSkillEvidenceKind,
    EmployeeSkillPatchEventType,
    EmployeeSkillPatchStatus,
    EvidenceSource,
    HireAssessmentDecision,
    RetentionReviewDecision,
    RetentionReviewMode,
    RosterPatchEventType,
    RosterPatchOperation,
    RosterPatchStatus,
    WorkflowPattern,
    content_digest,
)

@dataclass(frozen=True, slots=True)
class RosterPatchCandidate:
    patch_id: str
    status: RosterPatchStatus
    operation: RosterPatchOperation
    base_roster_revision: int
    employee_id: str
    before_employee: Mapping[str, Any] | None
    after_employee: Mapping[str, Any]
    rationale: str
    proposed_by: str
    content_hash: str
    created_at: str
    updated_at: str
    applied_revision: int | None = None
    evidence_ids: tuple[str, ...] = ()
    assessment_ids: tuple[str, ...] = ()

    def content_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "operation": self.operation,
            "base_roster_revision": self.base_roster_revision,
            "employee_id": self.employee_id,
            "before_employee": self.before_employee,
            "after_employee": self.after_employee,
            "rationale": self.rationale,
            "proposed_by": self.proposed_by,
        }
        # Preserve the content hash of manual Phase 24 candidates created before
        # evidence-backed recommendations existed.
        if self.evidence_ids:
            payload["evidence_ids"] = self.evidence_ids
        if self.assessment_ids:
            payload["assessment_ids"] = self.assessment_ids
        return payload

    def with_status(
        self,
        status: RosterPatchStatus,
        *,
        applied_revision: int | None = None,
    ) -> RosterPatchCandidate:
        return replace(
            self,
            status=status,
            applied_revision=(
                self.applied_revision if applied_revision is None else applied_revision
            ),
            updated_at=utc_now().isoformat(),
        )


@dataclass(frozen=True, slots=True)
class RosterPatchEvent:
    event_id: str
    patch_id: str
    seq: int
    event_type: RosterPatchEventType
    actor: str
    payload: Mapping[str, Any]
    occurred_at: str


@dataclass(frozen=True, slots=True)
class StaffingDemandEvidence:
    evidence_id: str
    episode_id: str
    job_id: str
    source: EvidenceSource
    context_fingerprint: str
    execution_profile: str
    base_roster_revision: int
    task_id: str
    capability: str
    role_label: str
    job_succeeded: bool
    validation_attempts: tuple[bool, ...]
    safety_violations: tuple[str, ...]
    writer_count: int
    approvals_requested: int
    approvals_granted: int
    preapproval_mutations: int
    ledger_digest: str
    content_hash: str
    recorded_at: str

    @classmethod
    def create(
        cls,
        *,
        episode_id: str,
        job_id: str,
        source: EvidenceSource,
        context_fingerprint: str,
        execution_profile: str,
        base_roster_revision: int,
        task_id: str,
        capability: str,
        role_label: str,
        job_succeeded: bool,
        validation_attempts: tuple[bool, ...],
        safety_violations: tuple[str, ...],
        writer_count: int,
        approvals_requested: int,
        approvals_granted: int,
        preapproval_mutations: int,
        ledger_digest: str,
        recorded_at: str | None = None,
    ) -> StaffingDemandEvidence:
        immutable = {
            "episode_id": episode_id,
            "job_id": job_id,
            "source": source,
            "context_fingerprint": context_fingerprint,
            "execution_profile": execution_profile,
            "base_roster_revision": base_roster_revision,
            "task_id": task_id,
            "capability": capability,
            "role_label": role_label,
            "job_succeeded": job_succeeded,
            "validation_attempts": validation_attempts,
            "safety_violations": safety_violations,
            "writer_count": writer_count,
            "approvals_requested": approvals_requested,
            "approvals_granted": approvals_granted,
            "preapproval_mutations": preapproval_mutations,
            "ledger_digest": ledger_digest,
        }
        digest = content_digest(immutable)
        return cls(
            evidence_id=f"staffing-demand-{digest[:24]}",
            **immutable,
            content_hash=digest,
            recorded_at=recorded_at or utc_now().isoformat(),
        )

    @property
    def production_eligible(self) -> bool:
        return self.source.production_eligible

    @property
    def safety_passed(self) -> bool:
        return (
            self.job_succeeded
            and bool(self.validation_attempts)
            and all(self.validation_attempts)
            and not self.safety_violations
            and self.writer_count <= 1
            and self.approvals_requested == self.approvals_granted
            and self.preapproval_mutations == 0
        )

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("evidence_id", None)
        payload.pop("content_hash", None)
        payload.pop("recorded_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class HireObservationContract:
    patch_id: str
    applied_roster_revision: int
    employee_id: str
    capability: str
    context_fingerprint: str
    execution_profile: str
    source_evidence_ids: tuple[str, ...]
    minimum_observations: int
    maximum_observations: int
    fail_on_safety_violation: bool
    content_hash: str
    created_at: str

    @classmethod
    def create(
        cls,
        candidate: RosterPatchCandidate,
        evidence: tuple[StaffingDemandEvidence, ...],
        *,
        created_at: str | None = None,
    ) -> HireObservationContract:
        if candidate.status != RosterPatchStatus.APPLIED:
            raise ValueError("Hire observation contract requires an applied Roster Patch")
        if candidate.operation != RosterPatchOperation.ADD_EMPLOYEE:
            raise ValueError("Hire observation contract requires ADD_EMPLOYEE")
        if candidate.applied_revision is None:
            raise ValueError("Hire observation contract requires an applied ROSTER revision")
        if not evidence or tuple(item.evidence_id for item in evidence) != candidate.evidence_ids:
            raise ValueError("Hire observation contract requires exact staffing evidence")
        contexts = {item.context_fingerprint for item in evidence}
        profiles = {item.execution_profile for item in evidence}
        capabilities = {item.capability for item in evidence}
        if len(contexts) != 1 or len(profiles) != 1 or len(capabilities) != 1:
            raise ValueError("Hire observation evidence must share context, profile, and capability")
        capability = next(iter(capabilities))
        after_capabilities = tuple(
            str(item).strip().casefold()
            for item in candidate.after_employee.get("capabilities", ())
        )
        if after_capabilities != (capability,):
            raise ValueError("Hire observation capability must match the applied employee")
        immutable = {
            "patch_id": candidate.patch_id,
            "applied_roster_revision": candidate.applied_revision,
            "employee_id": candidate.employee_id,
            "capability": capability,
            "context_fingerprint": next(iter(contexts)),
            "execution_profile": next(iter(profiles)),
            "source_evidence_ids": candidate.evidence_ids,
            "minimum_observations": 3,
            "maximum_observations": 5,
            "fail_on_safety_violation": True,
        }
        return cls(
            **immutable,
            content_hash=content_digest(immutable),
            created_at=created_at or utc_now().isoformat(),
        )

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("content_hash", None)
        payload.pop("created_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class HireObservation:
    observation_id: str
    patch_id: str
    episode_id: str
    job_id: str
    source: EvidenceSource
    base_roster_revision: int
    context_fingerprint: str
    execution_profile: str
    capability_task_ids: tuple[str, ...]
    measured_task_ids: tuple[str, ...]
    persistent_employee_assigned: bool
    temporary_fallback_used: bool
    job_succeeded: bool
    validation_attempts: tuple[bool, ...]
    safety_violations: tuple[str, ...]
    writer_count: int
    approvals_requested: int
    approvals_granted: int
    preapproval_mutations: int
    attribution_eligible: bool
    cohort_eligible: bool
    ineligibility_reasons: tuple[str, ...]
    assignment_ledger_digest: str
    organization_ledger_digest: str
    content_hash: str
    recorded_at: str

    @property
    def safety_passed(self) -> bool:
        return (
            self.job_succeeded
            and bool(self.validation_attempts)
            and all(self.validation_attempts)
            and not self.safety_violations
            and self.writer_count <= 1
            and self.approvals_requested == self.approvals_granted
            and self.preapproval_mutations == 0
        )

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("observation_id", None)
        payload.pop("content_hash", None)
        payload.pop("recorded_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class HireAssessment:
    assessment_id: str
    patch_id: str
    seq: int
    decision: HireAssessmentDecision
    reasons: tuple[str, ...]
    attributable_observation_ids: tuple[str, ...]
    cohort_observation_ids: tuple[str, ...]
    persistent_assignment_count: int
    temporary_fallback_count: int
    content_hash: str
    assessed_at: str

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("assessment_id", None)
        payload.pop("seq", None)
        payload.pop("content_hash", None)
        payload.pop("assessed_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class RosterRetentionReview:
    review_id: str
    roster_patch_id: str
    hire_patch_id: str
    assessment_id: str
    company_revision: int
    mode: RetentionReviewMode
    decision: RetentionReviewDecision
    reasons: tuple[str, ...]
    content_hash: str
    reviewed_at: str

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("review_id", None)
        payload.pop("content_hash", None)
        payload.pop("reviewed_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class RetentionRecommendationResult:
    mode: RetentionReviewMode
    patch: RosterPatchCandidate
    review: RosterRetentionReview
    roster_revision_before: int
    roster_revision_after: int
    applied: bool


@dataclass(frozen=True, slots=True)
class EmployeeSkillProcedure:
    employee_id: str
    skill_key: str
    context_key: str
    purpose: str
    steps: tuple[str, ...]
    verification_steps: tuple[str, ...]
    prohibitions: tuple[str, ...] = ()
    authority_scope: str = "INHERIT_ONLY"
    workflow_scope: str = "INHERIT_ONLY"

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        return payload


@dataclass(frozen=True, slots=True)
class EmployeeSkillEvidence:
    evidence_id: str
    kind: EmployeeSkillEvidenceKind
    source_ref: str
    source: EvidenceSource
    employee_id: str
    skill_key: str
    context_key: str
    procedure_hash: str
    confirmed_by_user: bool
    job_succeeded: bool
    validation_passed: bool
    safety_passed: bool
    content_hash: str
    recorded_at: str

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("evidence_id", None)
        payload.pop("content_hash", None)
        payload.pop("recorded_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class EmployeeSkillPatchCandidate:
    patch_id: str
    status: EmployeeSkillPatchStatus
    base_company_revision: int
    base_playbook_revision: int
    base_roster_revision: int
    base_skill_revision: int
    procedure: EmployeeSkillProcedure
    before_procedure: EmployeeSkillProcedure | None
    evidence_ids: tuple[str, ...]
    rationale: str
    proposed_by: str
    content_hash: str
    created_at: str
    updated_at: str
    applied_skill_revision: int | None = None
    rolled_back_skill_revision: int | None = None

    def content_payload(self) -> Mapping[str, Any]:
        return {
            "base_company_revision": self.base_company_revision,
            "base_playbook_revision": self.base_playbook_revision,
            "base_roster_revision": self.base_roster_revision,
            "base_skill_revision": self.base_skill_revision,
            "procedure": self.procedure,
            "before_procedure": self.before_procedure,
            "evidence_ids": self.evidence_ids,
            "rationale": self.rationale,
            "proposed_by": self.proposed_by,
        }

    def with_status(
        self,
        status: EmployeeSkillPatchStatus,
        *,
        applied_skill_revision: int | None = None,
        rolled_back_skill_revision: int | None = None,
    ) -> EmployeeSkillPatchCandidate:
        return replace(
            self,
            status=status,
            applied_skill_revision=(
                self.applied_skill_revision
                if applied_skill_revision is None
                else applied_skill_revision
            ),
            rolled_back_skill_revision=(
                self.rolled_back_skill_revision
                if rolled_back_skill_revision is None
                else rolled_back_skill_revision
            ),
            updated_at=utc_now().isoformat(),
        )


@dataclass(frozen=True, slots=True)
class EmployeeSkillPatchEvent:
    event_id: str
    patch_id: str
    seq: int
    event_type: EmployeeSkillPatchEventType
    actor: str
    payload: Mapping[str, Any]
    occurred_at: str


@dataclass(frozen=True, slots=True)
class EmployeeSkillVersion:
    version_id: str
    employee_id: str
    skill_key: str
    context_key: str
    revision: int
    active: bool
    procedure: EmployeeSkillProcedure | None
    source_patch_id: str
    content_hash: str
    created_at: str

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("version_id", None)
        payload.pop("content_hash", None)
        payload.pop("created_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class EmployeeSkillObservationContract:
    patch_id: str
    employee_id: str
    skill_key: str
    context_key: str
    applied_skill_revision: int
    version_content_hash: str
    minimum_observations: int
    maximum_observations: int
    content_hash: str
    created_at: str

    @classmethod
    def create(
        cls,
        candidate: EmployeeSkillPatchCandidate,
        version: EmployeeSkillVersion,
        *,
        created_at: str,
    ) -> EmployeeSkillObservationContract:
        immutable = {
            "patch_id": candidate.patch_id,
            "employee_id": candidate.procedure.employee_id,
            "skill_key": candidate.procedure.skill_key,
            "context_key": candidate.procedure.context_key,
            "applied_skill_revision": version.revision,
            "version_content_hash": version.content_hash,
            "minimum_observations": 2,
            "maximum_observations": 5,
        }
        return cls(
            **immutable,
            content_hash=content_digest(immutable),
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class EmployeeSkillObservation:
    observation_id: str
    patch_id: str
    episode_id: str
    job_id: str
    skill_exposed: bool
    attribution_eligible: bool
    cohort_eligible: bool
    ineligibility_reasons: tuple[str, ...]
    job_succeeded: bool
    validation_attempts: tuple[bool, ...]
    safety_violations: tuple[str, ...]
    writer_count: int
    approvals_requested: int
    approvals_granted: int
    preapproval_mutations: int
    request_ledger_digest: str
    content_hash: str
    recorded_at: str

    @property
    def safety_passed(self) -> bool:
        return (
            self.job_succeeded
            and bool(self.validation_attempts)
            and all(self.validation_attempts)
            and not self.safety_violations
            and self.writer_count <= 1
            and self.approvals_requested == self.approvals_granted
            and self.preapproval_mutations == 0
        )

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("observation_id", None)
        payload.pop("content_hash", None)
        payload.pop("recorded_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class EmployeeSkillAssessment:
    assessment_id: str
    patch_id: str
    seq: int
    decision: EmployeeSkillAssessmentDecision
    reasons: tuple[str, ...]
    observation_ids: tuple[str, ...]
    exposed_count: int
    content_hash: str
    assessed_at: str

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("assessment_id", None)
        payload.pop("seq", None)
        payload.pop("content_hash", None)
        payload.pop("assessed_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class PlaybookVersion:
    revision: int
    parent_revision: int | None
    patterns: tuple[WorkflowPattern, ...]
    source_patch_id: str | None
    rolled_back_from_revision: int | None
    created_at: str
