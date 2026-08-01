"""Company organization-episode and workflow-patch domain contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from dynamic_firm.runtime.models import to_primitive, utc_now

from .review_burden_metrics import (
    NOT_RECORDED,
    NOT_RUN,
    ReviewBurdenMetrics,
    validate_review_burden_metrics,
)
from .models import (
    EvidenceSource,
    WorkflowPatchAssessmentDecision,
    WorkflowPatchEventType,
    WorkflowPatchStatus,
    WorkflowPattern,
    WorkflowTaskTemplate,
    content_digest,
)


INDEPENDENT = "INDEPENDENT"
NOT_INDEPENDENT = "NOT_INDEPENDENT"

_EXPLICIT_NON_OVERLAPPING_CONTEXT = frozenset(
    {"DIFFERENT", "DISJOINT", "NON_OVERLAPPING"}
)
_EXPLICIT_INDEPENDENT_EVIDENCE = frozenset({"DIFFERENT", "INDEPENDENT"})
_EXPLICIT_SEPARATE_ROUTE = frozenset({"DIFFERENT", "INDEPENDENT"})


def effective_verification_independence(
    *,
    context_route_difference: str,
    evidence_route_difference: str,
    model_route_difference: str,
    tool_route_difference: str,
    procedure_route_difference: str,
    reviewer_material_profile_difference: str,
) -> str:
    """Classify independence from explicit fixed-state route facts only.

    A material profile difference is intentionally not a qualifying condition.
    Missing, overlapping, or otherwise unrecognized route facts fail closed.
    """

    if not (
        isinstance(context_route_difference, str)
        and context_route_difference in _EXPLICIT_NON_OVERLAPPING_CONTEXT
        and isinstance(evidence_route_difference, str)
        and evidence_route_difference in _EXPLICIT_INDEPENDENT_EVIDENCE
    ):
        return NOT_INDEPENDENT

    separate_routes = (
        model_route_difference,
        tool_route_difference,
        procedure_route_difference,
    )
    if any(
        isinstance(route_difference, str)
        and route_difference in _EXPLICIT_SEPARATE_ROUTE
        for route_difference in separate_routes
    ):
        return INDEPENDENT
    return NOT_INDEPENDENT


@dataclass(frozen=True, slots=True)
class OrganizationEpisode:
    episode_id: str
    job_id: str
    source: EvidenceSource
    task_family: str
    context_fingerprint: str
    execution_profile: str
    planning_mode: str
    manager_employee_id: str
    manager_assignment_digest: str
    manager_delegation_digest: str
    manager_supervision_count: int
    plan_digest: str
    plan_template: tuple[WorkflowTaskTemplate, ...]
    success: bool
    quality_score: float
    baseline_quality_score: float | None
    model_calls: int
    baseline_model_calls: int | None
    employee_count: int
    temporary_role_count: int
    maximum_parallelism: int
    graph_patch_count: int
    graph_proposal_approved_count: int
    graph_proposal_rejected_count: int
    graph_proposal_unavailable_count: int
    writer_count: int
    approvals_requested: int
    approvals_granted: int
    preapproval_mutations: int
    validation_attempts: tuple[bool, ...]
    safety_violations: tuple[str, ...]
    ledger_digest: str
    recorded_at: str
    time_to_first_runnable_ms: int | None = None
    blueprint_outcome: str = "NOT_SELECTED"
    initial_final_graph_distance: int | None = None
    reserved_model_call_delta: int | None = None
    model_call_budget_variance: int | None = None
    user_override_outcome: str = "NOT_OBSERVED"
    user_override_reason: str = "NOT_OBSERVED"
    recovery_outcome: str = "NOT_OBSERVED"
    context_route_difference: str = "NOT_RECORDED"
    reviewer_material_profile_difference: str = "NOT_RECORDED"
    evidence_route_difference: str = "NOT_RECORDED"
    model_route_difference: str = "NOT_RECORDED"
    tool_route_difference: str = "NOT_RECORDED"
    procedure_route_difference: str = "NOT_RECORDED"
    error_independence: str = "NOT_INDEPENDENT"
    detected_error: str = "NOT_RECORDED"
    false_positive: str = "NOT_RECORDED"
    rework: str = "NOT_RECORDED"
    final_change_status: str = "NOT_RECORDED"
    execution_replica_count: int = 0
    replica_group_count: int = 0
    review_wait_ms: int | float | str = NOT_RECORDED
    reopened_evidence_count: int | float | str = NOT_RECORDED
    unused_subartifact_rate: int | float | str = NOT_RECORDED
    rework_count: int | float | str = NOT_RECORDED
    approval_friction_count: int | float | str = NOT_RECORDED
    unverified_item_discovery: str = NOT_RUN
    summary_comprehension_status: str = NOT_RUN

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "error_independence",
            self.effective_verification_independence,
        )
        manager_values = (
            self.manager_employee_id,
            self.manager_assignment_digest,
            self.manager_delegation_digest,
            self.manager_supervision_count,
        )
        if self.manager_employee_id:
            if (
                len(self.manager_employee_id) > 160
                or not self.manager_assignment_digest
                or len(self.manager_assignment_digest) != 64
                or any(
                    value
                    and (
                        len(value) != 64
                        or any(character not in "0123456789abcdef" for character in value)
                    )
                    for value in (
                        self.manager_assignment_digest,
                        self.manager_delegation_digest,
                    )
                )
            ):
                raise ValueError("Organization episode Manager provenance is invalid")
        elif any(manager_values):
            raise ValueError("Organization episode Manager facts require a Manager identity")
        if any(
            type(value) is not int or value < 0
            for value in (
                self.manager_supervision_count,
                self.temporary_role_count,
                self.execution_replica_count,
                self.replica_group_count,
                self.graph_patch_count,
                self.graph_proposal_approved_count,
                self.graph_proposal_rejected_count,
                self.graph_proposal_unavailable_count,
            )
        ):
            raise ValueError("Organization episode growth counters are invalid")
        from .organization_metrics import OrganizationOutcomeMetrics
        OrganizationOutcomeMetrics(
            time_to_first_runnable_ms=self.time_to_first_runnable_ms,
            blueprint_outcome=self.blueprint_outcome,
            initial_final_graph_distance=self.initial_final_graph_distance,
            reserved_model_call_delta=self.reserved_model_call_delta,
            model_call_budget_variance=self.model_call_budget_variance,
            user_override_outcome=self.user_override_outcome,
            user_override_reason=self.user_override_reason,
            recovery_outcome=self.recovery_outcome,
        )
        validate_review_burden_metrics(
            review_wait_ms=self.review_wait_ms,
            reopened_evidence_count=self.reopened_evidence_count,
            unused_subartifact_rate=self.unused_subartifact_rate,
            rework_count=self.rework_count,
            approval_friction_count=self.approval_friction_count,
            unverified_item_discovery=self.unverified_item_discovery,
            summary_comprehension_status=self.summary_comprehension_status,
        )

    @property
    def review_burden_metrics(self) -> ReviewBurdenMetrics:
        """Return the additive review facts as one immutable value object."""

        return ReviewBurdenMetrics(
            review_wait_ms=self.review_wait_ms,
            reopened_evidence_count=self.reopened_evidence_count,
            unused_subartifact_rate=self.unused_subartifact_rate,
            rework_count=self.rework_count,
            approval_friction_count=self.approval_friction_count,
            unverified_item_discovery=self.unverified_item_discovery,
            summary_comprehension_status=self.summary_comprehension_status,
        )

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        source: EvidenceSource,
        task_family: str,
        context_fingerprint: str,
        execution_profile: str,
        planning_mode: str,
        plan_template: tuple[WorkflowTaskTemplate, ...],
        success: bool,
        quality_score: float,
        baseline_quality_score: float | None,
        model_calls: int,
        baseline_model_calls: int | None,
        employee_count: int,
        maximum_parallelism: int,
        writer_count: int,
        approvals_requested: int,
        approvals_granted: int,
        preapproval_mutations: int,
        validation_attempts: tuple[bool, ...],
        safety_violations: tuple[str, ...] = (),
        ledger_digest: str,
        manager_employee_id: str = "",
        manager_assignment_digest: str = "",
        manager_delegation_digest: str = "",
        manager_supervision_count: int = 0,
        temporary_role_count: int = 0,
        execution_replica_count: int = 0,
        replica_group_count: int = 0,
        graph_patch_count: int = 0,
        graph_proposal_approved_count: int = 0,
        graph_proposal_rejected_count: int = 0,
        graph_proposal_unavailable_count: int = 0,
        time_to_first_runnable_ms: int | None = None,
        blueprint_outcome: str = "NOT_SELECTED",
        initial_final_graph_distance: int | None = None,
        reserved_model_call_delta: int | None = None,
        model_call_budget_variance: int | None = None,
        user_override_outcome: str = "NOT_OBSERVED",
        user_override_reason: str = "NOT_OBSERVED",
        recovery_outcome: str = "NOT_OBSERVED",
        context_route_difference: str = "NOT_RECORDED",
        reviewer_material_profile_difference: str = "NOT_RECORDED",
        evidence_route_difference: str = "NOT_RECORDED",
        model_route_difference: str = "NOT_RECORDED",
        tool_route_difference: str = "NOT_RECORDED",
        procedure_route_difference: str = "NOT_RECORDED",
        error_independence: str = "NOT_INDEPENDENT",
        detected_error: str = "NOT_RECORDED",
        false_positive: str = "NOT_RECORDED",
        rework: str = "NOT_RECORDED",
        final_change_status: str = "NOT_RECORDED",
        review_wait_ms: int | float | str = NOT_RECORDED,
        reopened_evidence_count: int | float | str = NOT_RECORDED,
        unused_subartifact_rate: int | float | str = NOT_RECORDED,
        rework_count: int | float | str = NOT_RECORDED,
        approval_friction_count: int | float | str = NOT_RECORDED,
        unverified_item_discovery: str = NOT_RUN,
        summary_comprehension_status: str = NOT_RUN,
        recorded_at: str | None = None,
    ) -> OrganizationEpisode:
        plan_digest = content_digest(plan_template)
        identity = {
            "job_id": job_id,
            "source": source,
            "task_family": task_family,
            "context_fingerprint": context_fingerprint,
            "execution_profile": execution_profile,
            "planning_mode": planning_mode,
            "plan_digest": plan_digest,
            "ledger_digest": ledger_digest,
        }
        if manager_employee_id:
            identity["manager_employee_id"] = manager_employee_id
            identity["manager_assignment_digest"] = manager_assignment_digest
            identity["manager_delegation_digest"] = manager_delegation_digest
        episode_id = f"episode-{content_digest(identity)[:24]}"
        return cls(
            episode_id=episode_id,
            job_id=job_id,
            source=source,
            task_family=task_family,
            context_fingerprint=context_fingerprint,
            execution_profile=execution_profile,
            planning_mode=planning_mode,
            manager_employee_id=manager_employee_id,
            manager_assignment_digest=manager_assignment_digest,
            manager_delegation_digest=manager_delegation_digest,
            manager_supervision_count=manager_supervision_count,
            plan_digest=plan_digest,
            plan_template=plan_template,
            success=success,
            quality_score=quality_score,
            baseline_quality_score=baseline_quality_score,
            model_calls=model_calls,
            baseline_model_calls=baseline_model_calls,
            employee_count=employee_count,
            temporary_role_count=temporary_role_count,
            maximum_parallelism=maximum_parallelism,
            execution_replica_count=execution_replica_count,
            replica_group_count=replica_group_count,
            graph_patch_count=graph_patch_count,
            graph_proposal_approved_count=graph_proposal_approved_count,
            graph_proposal_rejected_count=graph_proposal_rejected_count,
            graph_proposal_unavailable_count=graph_proposal_unavailable_count,
            writer_count=writer_count,
            approvals_requested=approvals_requested,
            approvals_granted=approvals_granted,
            preapproval_mutations=preapproval_mutations,
            validation_attempts=validation_attempts,
            safety_violations=safety_violations,
            ledger_digest=ledger_digest,
            recorded_at=recorded_at or utc_now().isoformat(),
            time_to_first_runnable_ms=time_to_first_runnable_ms,
            blueprint_outcome=blueprint_outcome,
            initial_final_graph_distance=initial_final_graph_distance,
            reserved_model_call_delta=reserved_model_call_delta,
            model_call_budget_variance=model_call_budget_variance,
            user_override_outcome=user_override_outcome,
            user_override_reason=user_override_reason,
            recovery_outcome=recovery_outcome,
            context_route_difference=context_route_difference,
            reviewer_material_profile_difference=reviewer_material_profile_difference,
            evidence_route_difference=evidence_route_difference,
            model_route_difference=model_route_difference,
            tool_route_difference=tool_route_difference,
            procedure_route_difference=procedure_route_difference,
            error_independence=error_independence,
            detected_error=detected_error,
            false_positive=false_positive,
            rework=rework,
            final_change_status=final_change_status,
            review_wait_ms=review_wait_ms,
            reopened_evidence_count=reopened_evidence_count,
            unused_subartifact_rate=unused_subartifact_rate,
            rework_count=rework_count,
            approval_friction_count=approval_friction_count,
            unverified_item_discovery=unverified_item_discovery,
            summary_comprehension_status=summary_comprehension_status,
        )

    @property
    def effective_verification_independence(self) -> str:
        return effective_verification_independence(
            context_route_difference=self.context_route_difference,
            evidence_route_difference=self.evidence_route_difference,
            model_route_difference=self.model_route_difference,
            tool_route_difference=self.tool_route_difference,
            procedure_route_difference=self.procedure_route_difference,
            reviewer_material_profile_difference=(
                self.reviewer_material_profile_difference
            ),
        )

    @property
    def production_eligible(self) -> bool:
        return self.source.production_eligible

    @property
    def safety_passed(self) -> bool:
        return (
            self.success
            and self.quality_score >= 0.9
            and not self.safety_violations
            and self.preapproval_mutations == 0
            and self.approvals_granted == self.approvals_requested
            and bool(self.validation_attempts)
            and all(self.validation_attempts)
            and self.writer_count <= 1
        )

    @property
    def effect_passed(self) -> bool:
        if self.baseline_quality_score is None:
            return False
        quality_gain = self.quality_score - self.baseline_quality_score
        model_savings = (
            self.baseline_model_calls - self.model_calls
            if self.baseline_model_calls is not None
            else 0
        )
        return quality_gain >= 0.1 - 1e-9 or (quality_gain >= -1e-9 and model_savings >= 1)

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("episode_id", None)
        payload.pop("recorded_at", None)
        # These fields were added after the first OrganizationEpisode records.
        # Omitting their zero-value form preserves immutable historical hashes.
        if not payload.get("manager_employee_id"):
            for key in (
                "manager_employee_id",
                "manager_assignment_digest",
                "manager_delegation_digest",
                "manager_supervision_count",
            ):
                payload.pop(key, None)
        if not payload.get("temporary_role_count"):
            payload.pop("temporary_role_count", None)
        if not payload.get("execution_replica_count"):
            payload.pop("execution_replica_count", None)
        if not payload.get("replica_group_count"):
            payload.pop("replica_group_count", None)
        if not payload.get("graph_patch_count"):
            payload.pop("graph_patch_count", None)
        for key in (
            "graph_proposal_approved_count",
            "graph_proposal_rejected_count",
            "graph_proposal_unavailable_count",
        ):
            if not payload.get(key):
                payload.pop(key, None)
        for key, default in (
            ("context_route_difference", "NOT_RECORDED"),
            ("reviewer_material_profile_difference", "NOT_RECORDED"),
            ("evidence_route_difference", "NOT_RECORDED"),
            ("model_route_difference", "NOT_RECORDED"),
            ("tool_route_difference", "NOT_RECORDED"),
            ("procedure_route_difference", "NOT_RECORDED"),
            ("error_independence", "NOT_INDEPENDENT"),
            ("detected_error", "NOT_RECORDED"),
            ("false_positive", "NOT_RECORDED"),
            ("rework", "NOT_RECORDED"),
            ("final_change_status", "NOT_RECORDED"),
            ("review_wait_ms", NOT_RECORDED),
            ("reopened_evidence_count", NOT_RECORDED),
            ("unused_subartifact_rate", NOT_RECORDED),
            ("rework_count", NOT_RECORDED),
            ("approval_friction_count", NOT_RECORDED),
            ("unverified_item_discovery", NOT_RUN),
            ("summary_comprehension_status", NOT_RUN),
        ):
            if payload.get(key) == default:
                payload.pop(key, None)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPatchCandidate:
    patch_id: str
    status: WorkflowPatchStatus
    base_playbook_revision: int
    pattern: WorkflowPattern
    evidence_episode_ids: tuple[str, ...]
    expected_quality_gain: float
    expected_model_call_savings: int
    confidence: float
    eligible_for_apply: bool
    ineligibility_reasons: tuple[str, ...]
    content_hash: str
    created_at: str
    updated_at: str
    applied_revision: int | None = None
    rolled_back_revision: int | None = None

    def with_status(
        self,
        status: WorkflowPatchStatus,
        *,
        applied_revision: int | None = None,
        rolled_back_revision: int | None = None,
    ) -> WorkflowPatchCandidate:
        return replace(
            self,
            status=status,
            applied_revision=(
                self.applied_revision if applied_revision is None else applied_revision
            ),
            rolled_back_revision=(
                self.rolled_back_revision
                if rolled_back_revision is None
                else rolled_back_revision
            ),
            updated_at=utc_now().isoformat(),
        )


@dataclass(frozen=True, slots=True)
class WorkflowPatchEvent:
    event_id: str
    patch_id: str
    seq: int
    event_type: WorkflowPatchEventType
    actor: str
    payload: Mapping[str, Any]
    occurred_at: str


@dataclass(frozen=True, slots=True)
class WorkflowPatchObservationContract:
    patch_id: str
    pattern_id: str
    context_fingerprint: str
    execution_profile: str
    minimum_observations: int
    maximum_observations: int
    minimum_quality_gain: float
    minimum_model_call_savings: int
    fail_on_safety_violation: bool
    content_hash: str
    created_at: str

    @classmethod
    def create(
        cls,
        candidate: WorkflowPatchCandidate,
        *,
        created_at: str | None = None,
    ) -> WorkflowPatchObservationContract:
        immutable = {
            "patch_id": candidate.patch_id,
            "pattern_id": candidate.pattern.pattern_id,
            "context_fingerprint": candidate.pattern.context_fingerprint,
            "execution_profile": candidate.pattern.execution_profile,
            "minimum_observations": 3,
            "maximum_observations": 10,
            "minimum_quality_gain": 0.1,
            "minimum_model_call_savings": 1,
            "fail_on_safety_violation": True,
        }
        return cls(
            **immutable,
            content_hash=content_digest(immutable),
            created_at=created_at or utc_now().isoformat(),
        )


@dataclass(frozen=True, slots=True)
class WorkflowPatchObservation:
    observation_id: str
    patch_id: str
    episode_id: str
    prior_exposed: bool
    proposal_aligned: bool
    attribution_eligible: bool
    cohort_eligible: bool
    ineligibility_reasons: tuple[str, ...]
    quality_gain: float | None
    model_call_savings: int | None
    content_hash: str
    recorded_at: str

    def content_payload(self) -> Mapping[str, Any]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("observation_id", None)
        payload.pop("content_hash", None)
        payload.pop("recorded_at", None)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPatchAssessment:
    assessment_id: str
    patch_id: str
    seq: int
    decision: WorkflowPatchAssessmentDecision
    reasons: tuple[str, ...]
    attributable_observation_ids: tuple[str, ...]
    cohort_observation_ids: tuple[str, ...]
    mean_quality_gain: float | None
    mean_model_call_savings: float | None
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
