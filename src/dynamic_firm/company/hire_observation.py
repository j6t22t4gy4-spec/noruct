from __future__ import annotations

import json
from typing import Mapping, Sequence

from dynamic_firm.kernel.models import JobResult
from dynamic_firm.runtime.models import utc_now

from .models import (
    HireAssessment,
    HireAssessmentDecision,
    HireObservation,
    HireObservationContract,
    OrganizationEpisode,
    content_digest,
)
from .store import CompanyStateStore


def _capabilities(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"Hire observation {field} must be a capability sequence")
    normalized = tuple(str(item).strip().casefold() for item in value)
    if not normalized or any(not item for item in normalized):
        raise ValueError(f"Hire observation {field} contains an empty capability")
    return normalized


def hire_observation_from_runtime_ledger(
    result: JobResult,
    runs: Sequence[Mapping[str, object]],
    *,
    episode: OrganizationEpisode,
    contract: HireObservationContract,
    base_roster_revision: int,
    existing_cohort_count: int,
) -> HireObservation:
    """Project exact post-hire assignment facts from immutable run requests."""

    if result.job_id != episode.job_id:
        raise ValueError("Hire observation job and episode must match")
    if base_roster_revision < 1:
        raise ValueError("Hire observation requires a positive ROSTER revision")
    if existing_cohort_count < 0:
        raise ValueError("Hire observation cohort count cannot be negative")

    final_tasks = {task.task_id: task for task in result.final_tasks}
    capability_task_ids = tuple(
        sorted(
            task.task_id
            for task in result.final_tasks
            if contract.capability
            in tuple(item.strip().casefold() for item in task.required_capabilities)
        )
    )
    assignments: dict[str, str] = {}
    for row in runs:
        if str(row.get("job_id", "")) != result.job_id:
            raise ValueError("Hire observation ledger contains a different job")
        raw_request = row.get("request_json")
        if not isinstance(raw_request, str):
            raise ValueError("Hire observation ledger request must be immutable JSON")
        try:
            request = json.loads(raw_request)
        except json.JSONDecodeError as exc:
            raise ValueError("Hire observation ledger request is malformed") from exc
        if not isinstance(request, Mapping):
            raise ValueError("Hire observation ledger request must be an object")
        employee = request.get("employee")
        task_payload = request.get("task")
        if not isinstance(employee, Mapping) or not isinstance(task_payload, Mapping):
            raise ValueError("Hire observation ledger lacks employee or task snapshot")

        employee_id = str(employee.get("employee_id", "")).strip()
        task_id = str(task_payload.get("task_id", "")).strip()
        if (
            not employee_id
            or task_id != str(row.get("task_id", ""))
            or employee_id != str(row.get("employee_id", ""))
        ):
            raise ValueError("Hire observation ledger row and request identity differ")
        task = final_tasks.get(task_id)
        if task is None:
            raise ValueError("Hire observation request task is absent from the final graph")
        request_capabilities = _capabilities(
            task_payload.get("required_capabilities"),
            field="request task",
        )
        task_capabilities = tuple(
            item.strip().casefold() for item in task.required_capabilities
        )
        if request_capabilities != task_capabilities:
            raise ValueError("Hire observation request and final task capabilities differ")
        if contract.capability not in task_capabilities:
            continue
        if task.assignee_id != employee_id:
            continue
        temporary = employee.get("temporary")
        if type(temporary) is not bool:
            raise ValueError("Hire observation requires explicit employee temporary identity")
        if employee_id == contract.employee_id and not temporary:
            classification = "PERSISTENT_HIRE"
        elif temporary:
            classification = "TEMPORARY_FALLBACK"
        else:
            classification = "OTHER_PERSISTENT"
        previous = assignments.get(task_id)
        if previous is not None and previous != classification:
            raise ValueError("Hire observation retries disagree on final assignment identity")
        assignments[task_id] = classification

    measured_task_ids = tuple(sorted(assignments))
    assignment_projection = tuple(
        {
            "task_id": task_id,
            "assignment": assignments[task_id],
        }
        for task_id in measured_task_ids
    )
    persistent_employee_assigned = "PERSISTENT_HIRE" in assignments.values()
    temporary_fallback_used = "TEMPORARY_FALLBACK" in assignments.values()

    reasons: list[str] = []
    if not episode.production_eligible:
        reasons.append("non_production_evidence")
    if episode.context_fingerprint != contract.context_fingerprint:
        reasons.append("context_fingerprint_mismatch")
    if episode.execution_profile != contract.execution_profile:
        reasons.append("execution_profile_mismatch")
    if base_roster_revision < contract.applied_roster_revision:
        reasons.append("pre_hire_roster_revision")
    if not capability_task_ids:
        reasons.append("capability_task_missing")
    if not measured_task_ids:
        reasons.append("assignment_not_measured")
    attribution_eligible = not reasons
    if attribution_eligible and existing_cohort_count >= contract.maximum_observations:
        reasons.append("observation_limit_reached")
    cohort_eligible = attribution_eligible and not reasons

    immutable = {
        "patch_id": contract.patch_id,
        "episode_id": episode.episode_id,
        "job_id": episode.job_id,
        "source": episode.source,
        "base_roster_revision": base_roster_revision,
        "context_fingerprint": episode.context_fingerprint,
        "execution_profile": episode.execution_profile,
        "capability_task_ids": capability_task_ids,
        "measured_task_ids": measured_task_ids,
        "persistent_employee_assigned": persistent_employee_assigned,
        "temporary_fallback_used": temporary_fallback_used,
        "job_succeeded": episode.success,
        "validation_attempts": episode.validation_attempts,
        "safety_violations": episode.safety_violations,
        "writer_count": episode.writer_count,
        "approvals_requested": episode.approvals_requested,
        "approvals_granted": episode.approvals_granted,
        "preapproval_mutations": episode.preapproval_mutations,
        "attribution_eligible": attribution_eligible,
        "cohort_eligible": cohort_eligible,
        "ineligibility_reasons": tuple(reasons),
        "assignment_ledger_digest": content_digest(assignment_projection),
        "organization_ledger_digest": episode.ledger_digest,
    }
    digest = content_digest(immutable)
    return HireObservation(
        observation_id=f"hire-observation-{digest[:24]}",
        **immutable,
        content_hash=digest,
        recorded_at=episode.recorded_at,
    )


class HireObservationService:
    """Post-hire staffing attribution; never changes the ROSTER automatically."""

    def __init__(self, store: CompanyStateStore) -> None:
        self.store = store

    def observe(
        self,
        patch_id: str,
        result: JobResult,
        runs: Sequence[Mapping[str, object]],
        *,
        episode: OrganizationEpisode,
        base_roster_revision: int,
    ) -> HireObservation:
        contract = self.store.get_hire_observation_contract(patch_id)
        existing = next(
            (
                item
                for item in self.store.list_hire_observations(patch_id)
                if item.episode_id == episode.episode_id
            ),
            None,
        )
        existing_cohort_count = sum(
            item.cohort_eligible
            for item in self.store.list_hire_observations(patch_id)
            if existing is None or item.observation_id != existing.observation_id
        )
        observation = hire_observation_from_runtime_ledger(
            result,
            runs,
            episode=episode,
            contract=contract,
            base_roster_revision=base_roster_revision,
            existing_cohort_count=existing_cohort_count,
        )
        return self.store.record_hire_observation(observation)[0]

    def assess(self, patch_id: str) -> HireAssessment:
        """Append a deterministic recommendation without changing employee state."""

        contract = self.store.get_hire_observation_contract(patch_id)
        observations = self.store.list_hire_observations(patch_id)
        attributable = tuple(item for item in observations if item.attribution_eligible)
        cohort = tuple(item for item in observations if item.cohort_eligible)[
            : contract.maximum_observations
        ]
        persistent_count = sum(item.persistent_employee_assigned for item in cohort)
        fallback_count = sum(item.temporary_fallback_used for item in cohort)

        decision = HireAssessmentDecision.INSUFFICIENT_OBSERVATION
        if any(not item.job_succeeded for item in cohort):
            decision = HireAssessmentDecision.DORMANCY_CANDIDATE
            reasons = ("attributed_job_failure",)
        elif contract.fail_on_safety_violation and any(
            not item.safety_passed for item in cohort
        ):
            decision = HireAssessmentDecision.DORMANCY_CANDIDATE
            reasons = ("attributed_safety_violation",)
        elif len(cohort) < contract.minimum_observations:
            reasons = ("minimum_observation_count_not_reached",)
        elif all(item.persistent_employee_assigned for item in cohort) and not any(
            item.temporary_fallback_used for item in cohort
        ):
            decision = HireAssessmentDecision.KEEP
            reasons = ("persistent_hire_replaced_temporary_staffing",)
        elif len(cohort) >= contract.maximum_observations and persistent_count == 0:
            decision = HireAssessmentDecision.DORMANCY_CANDIDATE
            reasons = ("hire_unused_within_observation_limit",)
        elif len(cohort) >= contract.maximum_observations and fallback_count >= 2:
            decision = HireAssessmentDecision.DORMANCY_CANDIDATE
            reasons = ("temporary_fallback_repeated",)
        else:
            reasons = ("staffing_replacement_not_yet_proven",)

        immutable = {
            "patch_id": patch_id,
            "decision": decision,
            "reasons": reasons,
            "attributable_observation_ids": tuple(
                item.observation_id for item in attributable
            ),
            "cohort_observation_ids": tuple(item.observation_id for item in cohort),
            "persistent_assignment_count": persistent_count,
            "temporary_fallback_count": fallback_count,
        }
        digest = content_digest(immutable)
        latest = self.store.latest_hire_assessment(patch_id)
        assessment = HireAssessment(
            assessment_id=f"hire-assessment-{digest[:24]}",
            seq=1 if latest is None else latest.seq + 1,
            **immutable,
            content_hash=digest,
            assessed_at=utc_now().isoformat(),
        )
        return self.store.record_hire_assessment(assessment)[0]
