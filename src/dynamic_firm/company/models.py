from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping

from dynamic_firm.runtime.models import to_primitive, utc_now


def canonical_json(value: object) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class EvidenceSource(StrEnum):
    USER_CORRECTION = "USER_CORRECTION"
    REAL_JOB = "REAL_JOB"
    LIVE_EVALUATION = "LIVE_EVALUATION"
    OFFLINE_FIXTURE = "OFFLINE_FIXTURE"

    @property
    def production_eligible(self) -> bool:
        return self in {
            EvidenceSource.USER_CORRECTION,
            EvidenceSource.REAL_JOB,
            EvidenceSource.LIVE_EVALUATION,
        }


class WorkflowPatchStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"


class WorkflowPatchEventType(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"


class WorkflowPatchAssessmentDecision(StrEnum):
    INSUFFICIENT_OBSERVATION = "INSUFFICIENT_OBSERVATION"
    KEEP = "KEEP"
    ROLLBACK_CANDIDATE = "ROLLBACK_CANDIDATE"


class RosterPatchOperation(StrEnum):
    ADD_EMPLOYEE = "ADD_EMPLOYEE"
    SET_ACTIVE = "SET_ACTIVE"
    SET_CAPABILITIES = "SET_CAPABILITIES"
    UPDATE_EMPLOYEE = "UPDATE_EMPLOYEE"


class RosterPatchStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


class RosterPatchEventType(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


class HireAssessmentDecision(StrEnum):
    INSUFFICIENT_OBSERVATION = "INSUFFICIENT_OBSERVATION"
    KEEP = "KEEP"
    DORMANCY_CANDIDATE = "DORMANCY_CANDIDATE"


class RetentionReviewMode(StrEnum):
    APPROVAL = "approval"
    AUTO_REVIEW = "auto-review"
    ALWAYS_APPROVE = "always-approve"


class EvolutionAutonomyMode(StrEnum):
    """The one user-facing choice for how a Company adopts improvements.

    This is deliberately separate from individual Patch lifecycle states.  A
    mode changes whether qualifying *future* evolution may advance without an
    extra prompt; it never relaxes integrity, compatibility, cost, authority,
    or active-Job pinning invariants.
    """

    NEVER = "never"
    PROPOSE = "propose"
    ALWAYS_APPROVE = "always-approve"


class RetentionReviewDecision(StrEnum):
    PENDING_USER_APPROVAL = "PENDING_USER_APPROVAL"
    AUTO_APPROVED = "AUTO_APPROVED"
    REQUIRES_USER_APPROVAL = "REQUIRES_USER_APPROVAL"
    APPROVAL_BYPASSED = "APPROVAL_BYPASSED"


class EmployeeSkillEvidenceKind(StrEnum):
    USER_CORRECTION = "USER_CORRECTION"
    VERIFIED_JOB_PROCEDURE = "VERIFIED_JOB_PROCEDURE"


class EmployeeSkillPatchStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"


class EmployeeSkillPatchEventType(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"


class EmployeeSkillAssessmentDecision(StrEnum):
    INSUFFICIENT_OBSERVATION = "INSUFFICIENT_OBSERVATION"
    KEEP = "KEEP"
    ROLLBACK_CANDIDATE = "ROLLBACK_CANDIDATE"


@dataclass(frozen=True, slots=True)
class WorkflowTaskTemplate:
    task_key: str
    required_capabilities: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    final: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowPattern:
    pattern_id: str
    task_family: str
    context_fingerprint: str
    execution_profile: str
    plan_digest: str
    tasks: tuple[WorkflowTaskTemplate, ...]
    maximum_parallelism: int
    writer_count: int
    evidence_count: int
    rationale: str


@dataclass(frozen=True, slots=True)
class CompanyVersion:
    revision: int
    parent_revision: int | None
    purpose: str
    policies: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class RosterVersion:
    revision: int
    parent_revision: int | None
    employees: tuple[Mapping[str, Any], ...]
    created_at: str


from .roster_models import (
    EmployeeSkillAssessment,
    EmployeeSkillEvidence,
    EmployeeSkillObservation,
    EmployeeSkillObservationContract,
    EmployeeSkillPatchCandidate,
    EmployeeSkillPatchEvent,
    EmployeeSkillProcedure,
    EmployeeSkillVersion,
    HireAssessment,
    HireObservation,
    HireObservationContract,
    PlaybookVersion,
    RetentionRecommendationResult,
    RosterPatchCandidate,
    RosterPatchEvent,
    RosterRetentionReview,
    StaffingDemandEvidence,
)


from .workflow_models import (
    OrganizationEpisode,
    WorkflowPatchAssessment,
    WorkflowPatchCandidate,
    WorkflowPatchEvent,
    WorkflowPatchObservation,
    WorkflowPatchObservationContract,
)


@dataclass(frozen=True, slots=True)
class CompanyStateSummary:
    company_revision: int
    roster_revision: int
    playbook_revision: int
    employee_count: int
    active_employee_count: int
    workflow_pattern_count: int
    episode_count: int
    staffing_demand_count: int
    hire_observation_contract_count: int
    hire_observation_count: int
    hire_assessment_count: int
    retention_review_mode: RetentionReviewMode
    retention_review_count: int
    company_policy_event_count: int
    employee_skill_count: int
    employee_skill_patch_counts: Mapping[str, int]
    employee_skill_observation_count: int
    employee_skill_assessment_count: int
    verified_live_pair_count: int
    patch_counts: Mapping[str, int]
    roster_patch_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class CurationResult:
    decision: str
    candidates: tuple[WorkflowPatchCandidate, ...]
    considered_episode_count: int
    qualified_episode_count: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HiringRecommendationResult:
    decision: str
    candidates: tuple[RosterPatchCandidate, ...]
    considered_evidence_count: int
    qualified_evidence_count: int
    reasons: tuple[str, ...]


def workflow_task_from_dict(value: Mapping[str, Any]) -> WorkflowTaskTemplate:
    return WorkflowTaskTemplate(
        task_key=str(value["task_key"]),
        required_capabilities=tuple(str(item) for item in value["required_capabilities"]),
        depends_on=tuple(str(item) for item in value.get("depends_on", ())),
        final=bool(value.get("final", False)),
    )


def workflow_pattern_from_dict(value: Mapping[str, Any]) -> WorkflowPattern:
    return WorkflowPattern(
        pattern_id=str(value["pattern_id"]),
        task_family=str(value["task_family"]),
        context_fingerprint=str(value["context_fingerprint"]),
        execution_profile=str(value["execution_profile"]),
        plan_digest=str(value["plan_digest"]),
        tasks=tuple(workflow_task_from_dict(item) for item in value["tasks"]),
        maximum_parallelism=int(value["maximum_parallelism"]),
        writer_count=int(value["writer_count"]),
        evidence_count=int(value["evidence_count"]),
        rationale=str(value["rationale"]),
    )


def organization_episode_from_dict(value: Mapping[str, Any]) -> OrganizationEpisode:
    return OrganizationEpisode(
        episode_id=str(value["episode_id"]),
        job_id=str(value["job_id"]),
        source=EvidenceSource(value["source"]),
        task_family=str(value["task_family"]),
        context_fingerprint=str(value["context_fingerprint"]),
        execution_profile=str(value["execution_profile"]),
        planning_mode=str(value["planning_mode"]),
        manager_employee_id=str(value.get("manager_employee_id", "")),
        manager_assignment_digest=str(value.get("manager_assignment_digest", "")),
        manager_delegation_digest=str(value.get("manager_delegation_digest", "")),
        manager_supervision_count=int(value.get("manager_supervision_count", 0)),
        plan_digest=str(value["plan_digest"]),
        plan_template=tuple(workflow_task_from_dict(item) for item in value["plan_template"]),
        success=bool(value["success"]),
        quality_score=float(value["quality_score"]),
        baseline_quality_score=(
            None
            if value.get("baseline_quality_score") is None
            else float(value["baseline_quality_score"])
        ),
        model_calls=int(value["model_calls"]),
        baseline_model_calls=(
            None
            if value.get("baseline_model_calls") is None
            else int(value["baseline_model_calls"])
        ),
        employee_count=int(value["employee_count"]),
        temporary_role_count=int(value.get("temporary_role_count", 0)),
        maximum_parallelism=int(value["maximum_parallelism"]),
        execution_replica_count=int(value.get("execution_replica_count", 0)),
        replica_group_count=int(value.get("replica_group_count", 0)),
        graph_patch_count=int(value.get("graph_patch_count", 0)),
        graph_proposal_approved_count=int(value.get("graph_proposal_approved_count", 0)),
        graph_proposal_rejected_count=int(value.get("graph_proposal_rejected_count", 0)),
        graph_proposal_unavailable_count=int(value.get("graph_proposal_unavailable_count", 0)),
        writer_count=int(value["writer_count"]),
        approvals_requested=int(value["approvals_requested"]),
        approvals_granted=int(value["approvals_granted"]),
        preapproval_mutations=int(value["preapproval_mutations"]),
        validation_attempts=tuple(bool(item) for item in value["validation_attempts"]),
        safety_violations=tuple(str(item) for item in value.get("safety_violations", ())),
        ledger_digest=str(value["ledger_digest"]),
        recorded_at=str(value["recorded_at"]),
        time_to_first_runnable_ms=(
            None
            if value.get("time_to_first_runnable_ms") is None
            else int(value["time_to_first_runnable_ms"])
        ),
        blueprint_outcome=str(value.get("blueprint_outcome", "NOT_SELECTED")),
        initial_final_graph_distance=(
            None
            if value.get("initial_final_graph_distance") is None
            else int(value["initial_final_graph_distance"])
        ),
        reserved_model_call_delta=(
            None
            if value.get("reserved_model_call_delta") is None
            else int(value["reserved_model_call_delta"])
        ),
        model_call_budget_variance=(
            None
            if value.get("model_call_budget_variance") is None
            else int(value["model_call_budget_variance"])
        ),
        user_override_outcome=str(value.get("user_override_outcome", "NOT_OBSERVED")),
        user_override_reason=str(value.get("user_override_reason", "NOT_OBSERVED")),
        recovery_outcome=str(value.get("recovery_outcome", "NOT_OBSERVED")),
        context_route_difference=str(
            value.get("context_route_difference", "NOT_RECORDED")
        ),
        reviewer_material_profile_difference=str(
            value.get("reviewer_material_profile_difference", "NOT_RECORDED")
        ),
        evidence_route_difference=str(
            value.get("evidence_route_difference", "NOT_RECORDED")
        ),
        model_route_difference=str(
            value.get("model_route_difference", "NOT_RECORDED")
        ),
        tool_route_difference=str(
            value.get("tool_route_difference", "NOT_RECORDED")
        ),
        procedure_route_difference=str(
            value.get("procedure_route_difference", "NOT_RECORDED")
        ),
        error_independence=str(
            value.get("error_independence", "NOT_INDEPENDENT")
        ),
        detected_error=str(value.get("detected_error", "NOT_RECORDED")),
        false_positive=str(value.get("false_positive", "NOT_RECORDED")),
        rework=str(value.get("rework", "NOT_RECORDED")),
        final_change_status=str(
            value.get("final_change_status", "NOT_RECORDED")
        ),
        review_wait_ms=value.get("review_wait_ms", "NOT_RECORDED"),
        reopened_evidence_count=value.get(
            "reopened_evidence_count", "NOT_RECORDED"
        ),
        unused_subartifact_rate=value.get(
            "unused_subartifact_rate", "NOT_RECORDED"
        ),
        rework_count=value.get("rework_count", "NOT_RECORDED"),
        approval_friction_count=value.get(
            "approval_friction_count", "NOT_RECORDED"
        ),
        unverified_item_discovery=value.get(
            "unverified_item_discovery", "NOT_RUN"
        ),
        summary_comprehension_status=value.get(
            "summary_comprehension_status", "NOT_RUN"
        ),
    )


def workflow_patch_from_dict(value: Mapping[str, Any]) -> WorkflowPatchCandidate:
    return WorkflowPatchCandidate(
        patch_id=str(value["patch_id"]),
        status=WorkflowPatchStatus(value["status"]),
        base_playbook_revision=int(value["base_playbook_revision"]),
        pattern=workflow_pattern_from_dict(value["pattern"]),
        evidence_episode_ids=tuple(str(item) for item in value["evidence_episode_ids"]),
        expected_quality_gain=float(value["expected_quality_gain"]),
        expected_model_call_savings=int(value["expected_model_call_savings"]),
        confidence=float(value["confidence"]),
        eligible_for_apply=bool(value["eligible_for_apply"]),
        ineligibility_reasons=tuple(str(item) for item in value["ineligibility_reasons"]),
        content_hash=str(value["content_hash"]),
        created_at=str(value["created_at"]),
        updated_at=str(value["updated_at"]),
        applied_revision=(
            None if value.get("applied_revision") is None else int(value["applied_revision"])
        ),
        rolled_back_revision=(
            None
            if value.get("rolled_back_revision") is None
            else int(value["rolled_back_revision"])
        ),
    )


def roster_patch_from_dict(value: Mapping[str, Any]) -> RosterPatchCandidate:
    before = value.get("before_employee")
    after = value["after_employee"]
    if before is not None and not isinstance(before, Mapping):
        raise ValueError("Roster Patch before_employee must be an object or null")
    if not isinstance(after, Mapping):
        raise ValueError("Roster Patch after_employee must be an object")
    return RosterPatchCandidate(
        patch_id=str(value["patch_id"]),
        status=RosterPatchStatus(value["status"]),
        operation=RosterPatchOperation(value["operation"]),
        base_roster_revision=int(value["base_roster_revision"]),
        employee_id=str(value["employee_id"]),
        before_employee=None if before is None else dict(before),
        after_employee=dict(after),
        rationale=str(value["rationale"]),
        proposed_by=str(value["proposed_by"]),
        content_hash=str(value["content_hash"]),
        created_at=str(value["created_at"]),
        updated_at=str(value["updated_at"]),
        applied_revision=(
            None if value.get("applied_revision") is None else int(value["applied_revision"])
        ),
        evidence_ids=tuple(str(item) for item in value.get("evidence_ids", ())),
        assessment_ids=tuple(str(item) for item in value.get("assessment_ids", ())),
    )


def staffing_demand_from_dict(value: Mapping[str, Any]) -> StaffingDemandEvidence:
    return StaffingDemandEvidence(
        evidence_id=str(value["evidence_id"]),
        episode_id=str(value["episode_id"]),
        job_id=str(value["job_id"]),
        source=EvidenceSource(value["source"]),
        context_fingerprint=str(value["context_fingerprint"]),
        execution_profile=str(value["execution_profile"]),
        base_roster_revision=int(value["base_roster_revision"]),
        task_id=str(value["task_id"]),
        capability=str(value["capability"]),
        role_label=str(value["role_label"]),
        job_succeeded=bool(value["job_succeeded"]),
        validation_attempts=tuple(bool(item) for item in value["validation_attempts"]),
        safety_violations=tuple(str(item) for item in value.get("safety_violations", ())),
        writer_count=int(value["writer_count"]),
        approvals_requested=int(value["approvals_requested"]),
        approvals_granted=int(value["approvals_granted"]),
        preapproval_mutations=int(value["preapproval_mutations"]),
        ledger_digest=str(value["ledger_digest"]),
        content_hash=str(value["content_hash"]),
        recorded_at=str(value["recorded_at"]),
    )


def hire_observation_contract_from_dict(
    value: Mapping[str, Any],
) -> HireObservationContract:
    return HireObservationContract(
        patch_id=str(value["patch_id"]),
        applied_roster_revision=int(value["applied_roster_revision"]),
        employee_id=str(value["employee_id"]),
        capability=str(value["capability"]),
        context_fingerprint=str(value["context_fingerprint"]),
        execution_profile=str(value["execution_profile"]),
        source_evidence_ids=tuple(str(item) for item in value["source_evidence_ids"]),
        minimum_observations=int(value["minimum_observations"]),
        maximum_observations=int(value["maximum_observations"]),
        fail_on_safety_violation=bool(value["fail_on_safety_violation"]),
        content_hash=str(value["content_hash"]),
        created_at=str(value["created_at"]),
    )


def hire_observation_from_dict(value: Mapping[str, Any]) -> HireObservation:
    return HireObservation(
        observation_id=str(value["observation_id"]),
        patch_id=str(value["patch_id"]),
        episode_id=str(value["episode_id"]),
        job_id=str(value["job_id"]),
        source=EvidenceSource(value["source"]),
        base_roster_revision=int(value["base_roster_revision"]),
        context_fingerprint=str(value["context_fingerprint"]),
        execution_profile=str(value["execution_profile"]),
        capability_task_ids=tuple(str(item) for item in value["capability_task_ids"]),
        measured_task_ids=tuple(str(item) for item in value["measured_task_ids"]),
        persistent_employee_assigned=bool(value["persistent_employee_assigned"]),
        temporary_fallback_used=bool(value["temporary_fallback_used"]),
        job_succeeded=bool(value["job_succeeded"]),
        validation_attempts=tuple(bool(item) for item in value["validation_attempts"]),
        safety_violations=tuple(str(item) for item in value.get("safety_violations", ())),
        writer_count=int(value["writer_count"]),
        approvals_requested=int(value["approvals_requested"]),
        approvals_granted=int(value["approvals_granted"]),
        preapproval_mutations=int(value["preapproval_mutations"]),
        attribution_eligible=bool(value["attribution_eligible"]),
        cohort_eligible=bool(value["cohort_eligible"]),
        ineligibility_reasons=tuple(str(item) for item in value["ineligibility_reasons"]),
        assignment_ledger_digest=str(value["assignment_ledger_digest"]),
        organization_ledger_digest=str(value["organization_ledger_digest"]),
        content_hash=str(value["content_hash"]),
        recorded_at=str(value["recorded_at"]),
    )


def hire_assessment_from_dict(value: Mapping[str, Any]) -> HireAssessment:
    return HireAssessment(
        assessment_id=str(value["assessment_id"]),
        patch_id=str(value["patch_id"]),
        seq=int(value["seq"]),
        decision=HireAssessmentDecision(value["decision"]),
        reasons=tuple(str(item) for item in value["reasons"]),
        attributable_observation_ids=tuple(
            str(item) for item in value["attributable_observation_ids"]
        ),
        cohort_observation_ids=tuple(str(item) for item in value["cohort_observation_ids"]),
        persistent_assignment_count=int(value["persistent_assignment_count"]),
        temporary_fallback_count=int(value["temporary_fallback_count"]),
        content_hash=str(value["content_hash"]),
        assessed_at=str(value["assessed_at"]),
    )


def roster_retention_review_from_dict(
    value: Mapping[str, Any],
) -> RosterRetentionReview:
    return RosterRetentionReview(
        review_id=str(value["review_id"]),
        roster_patch_id=str(value["roster_patch_id"]),
        hire_patch_id=str(value["hire_patch_id"]),
        assessment_id=str(value["assessment_id"]),
        company_revision=int(value["company_revision"]),
        mode=RetentionReviewMode(value["mode"]),
        decision=RetentionReviewDecision(value["decision"]),
        reasons=tuple(str(item) for item in value["reasons"]),
        content_hash=str(value["content_hash"]),
        reviewed_at=str(value["reviewed_at"]),
    )


def employee_skill_procedure_from_dict(
    value: Mapping[str, Any],
) -> EmployeeSkillProcedure:
    return EmployeeSkillProcedure(
        employee_id=str(value["employee_id"]),
        skill_key=str(value["skill_key"]),
        context_key=str(value["context_key"]),
        purpose=str(value["purpose"]),
        steps=tuple(str(item) for item in value["steps"]),
        verification_steps=tuple(str(item) for item in value["verification_steps"]),
        prohibitions=tuple(str(item) for item in value.get("prohibitions", ())),
        authority_scope=str(value.get("authority_scope", "INHERIT_ONLY")),
        workflow_scope=str(value.get("workflow_scope", "INHERIT_ONLY")),
    )


def employee_skill_evidence_from_dict(
    value: Mapping[str, Any],
) -> EmployeeSkillEvidence:
    return EmployeeSkillEvidence(
        evidence_id=str(value["evidence_id"]),
        kind=EmployeeSkillEvidenceKind(value["kind"]),
        source_ref=str(value["source_ref"]),
        source=EvidenceSource(value["source"]),
        employee_id=str(value["employee_id"]),
        skill_key=str(value["skill_key"]),
        context_key=str(value["context_key"]),
        procedure_hash=str(value["procedure_hash"]),
        confirmed_by_user=bool(value["confirmed_by_user"]),
        job_succeeded=bool(value["job_succeeded"]),
        validation_passed=bool(value["validation_passed"]),
        safety_passed=bool(value["safety_passed"]),
        content_hash=str(value["content_hash"]),
        recorded_at=str(value["recorded_at"]),
    )


def employee_skill_patch_from_dict(
    value: Mapping[str, Any],
) -> EmployeeSkillPatchCandidate:
    before = value.get("before_procedure")
    return EmployeeSkillPatchCandidate(
        patch_id=str(value["patch_id"]),
        status=EmployeeSkillPatchStatus(value["status"]),
        base_company_revision=int(value["base_company_revision"]),
        base_playbook_revision=int(value["base_playbook_revision"]),
        base_roster_revision=int(value["base_roster_revision"]),
        base_skill_revision=int(value["base_skill_revision"]),
        procedure=employee_skill_procedure_from_dict(value["procedure"]),
        before_procedure=(
            None if before is None else employee_skill_procedure_from_dict(before)
        ),
        evidence_ids=tuple(str(item) for item in value["evidence_ids"]),
        rationale=str(value["rationale"]),
        proposed_by=str(value["proposed_by"]),
        content_hash=str(value["content_hash"]),
        created_at=str(value["created_at"]),
        updated_at=str(value["updated_at"]),
        applied_skill_revision=(
            None
            if value.get("applied_skill_revision") is None
            else int(value["applied_skill_revision"])
        ),
        rolled_back_skill_revision=(
            None
            if value.get("rolled_back_skill_revision") is None
            else int(value["rolled_back_skill_revision"])
        ),
    )


def employee_skill_version_from_dict(
    value: Mapping[str, Any],
) -> EmployeeSkillVersion:
    procedure = value.get("procedure")
    return EmployeeSkillVersion(
        version_id=str(value["version_id"]),
        employee_id=str(value["employee_id"]),
        skill_key=str(value["skill_key"]),
        context_key=str(value["context_key"]),
        revision=int(value["revision"]),
        active=bool(value["active"]),
        procedure=(
            None if procedure is None else employee_skill_procedure_from_dict(procedure)
        ),
        source_patch_id=str(value["source_patch_id"]),
        content_hash=str(value["content_hash"]),
        created_at=str(value["created_at"]),
    )


def employee_skill_observation_contract_from_dict(
    value: Mapping[str, Any],
) -> EmployeeSkillObservationContract:
    return EmployeeSkillObservationContract(
        patch_id=str(value["patch_id"]),
        employee_id=str(value["employee_id"]),
        skill_key=str(value["skill_key"]),
        context_key=str(value["context_key"]),
        applied_skill_revision=int(value["applied_skill_revision"]),
        version_content_hash=str(value["version_content_hash"]),
        minimum_observations=int(value["minimum_observations"]),
        maximum_observations=int(value["maximum_observations"]),
        content_hash=str(value["content_hash"]),
        created_at=str(value["created_at"]),
    )


def employee_skill_observation_from_dict(
    value: Mapping[str, Any],
) -> EmployeeSkillObservation:
    return EmployeeSkillObservation(
        observation_id=str(value["observation_id"]),
        patch_id=str(value["patch_id"]),
        episode_id=str(value["episode_id"]),
        job_id=str(value["job_id"]),
        skill_exposed=bool(value["skill_exposed"]),
        attribution_eligible=bool(value["attribution_eligible"]),
        cohort_eligible=bool(value["cohort_eligible"]),
        ineligibility_reasons=tuple(
            str(item) for item in value["ineligibility_reasons"]
        ),
        job_succeeded=bool(value["job_succeeded"]),
        validation_attempts=tuple(bool(item) for item in value["validation_attempts"]),
        safety_violations=tuple(str(item) for item in value["safety_violations"]),
        writer_count=int(value["writer_count"]),
        approvals_requested=int(value["approvals_requested"]),
        approvals_granted=int(value["approvals_granted"]),
        preapproval_mutations=int(value["preapproval_mutations"]),
        request_ledger_digest=str(value["request_ledger_digest"]),
        content_hash=str(value["content_hash"]),
        recorded_at=str(value["recorded_at"]),
    )


def employee_skill_assessment_from_dict(
    value: Mapping[str, Any],
) -> EmployeeSkillAssessment:
    return EmployeeSkillAssessment(
        assessment_id=str(value["assessment_id"]),
        patch_id=str(value["patch_id"]),
        seq=int(value["seq"]),
        decision=EmployeeSkillAssessmentDecision(value["decision"]),
        reasons=tuple(str(item) for item in value["reasons"]),
        observation_ids=tuple(str(item) for item in value["observation_ids"]),
        exposed_count=int(value["exposed_count"]),
        content_hash=str(value["content_hash"]),
        assessed_at=str(value["assessed_at"]),
    )


def workflow_patch_observation_contract_from_dict(
    value: Mapping[str, Any],
) -> WorkflowPatchObservationContract:
    return WorkflowPatchObservationContract(
        patch_id=str(value["patch_id"]),
        pattern_id=str(value["pattern_id"]),
        context_fingerprint=str(value["context_fingerprint"]),
        execution_profile=str(value["execution_profile"]),
        minimum_observations=int(value["minimum_observations"]),
        maximum_observations=int(value["maximum_observations"]),
        minimum_quality_gain=float(value["minimum_quality_gain"]),
        minimum_model_call_savings=int(value["minimum_model_call_savings"]),
        fail_on_safety_violation=bool(value["fail_on_safety_violation"]),
        content_hash=str(value["content_hash"]),
        created_at=str(value["created_at"]),
    )


def workflow_patch_observation_from_dict(
    value: Mapping[str, Any],
) -> WorkflowPatchObservation:
    return WorkflowPatchObservation(
        observation_id=str(value["observation_id"]),
        patch_id=str(value["patch_id"]),
        episode_id=str(value["episode_id"]),
        prior_exposed=bool(value["prior_exposed"]),
        proposal_aligned=bool(value["proposal_aligned"]),
        attribution_eligible=bool(value["attribution_eligible"]),
        cohort_eligible=bool(value["cohort_eligible"]),
        ineligibility_reasons=tuple(str(item) for item in value["ineligibility_reasons"]),
        quality_gain=(
            None if value.get("quality_gain") is None else float(value["quality_gain"])
        ),
        model_call_savings=(
            None
            if value.get("model_call_savings") is None
            else int(value["model_call_savings"])
        ),
        content_hash=str(value["content_hash"]),
        recorded_at=str(value["recorded_at"]),
    )


def workflow_patch_assessment_from_dict(
    value: Mapping[str, Any],
) -> WorkflowPatchAssessment:
    return WorkflowPatchAssessment(
        assessment_id=str(value["assessment_id"]),
        patch_id=str(value["patch_id"]),
        seq=int(value["seq"]),
        decision=WorkflowPatchAssessmentDecision(value["decision"]),
        reasons=tuple(str(item) for item in value["reasons"]),
        attributable_observation_ids=tuple(
            str(item) for item in value["attributable_observation_ids"]
        ),
        cohort_observation_ids=tuple(str(item) for item in value["cohort_observation_ids"]),
        mean_quality_gain=(
            None
            if value.get("mean_quality_gain") is None
            else float(value["mean_quality_gain"])
        ),
        mean_model_call_savings=(
            None
            if value.get("mean_model_call_savings") is None
            else float(value["mean_model_call_savings"])
        ),
        content_hash=str(value["content_hash"]),
        assessed_at=str(value["assessed_at"]),
    )
