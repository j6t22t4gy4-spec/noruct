from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from dynamic_firm.company import (
    CompanyStateStore,
    EvidenceSource,
    HireObservationService,
    HiringRecommendationService,
    OrganizationEpisode,
    RosterPatchService,
    StaffingDemandEvidence,
    WorkflowTaskTemplate,
)
from dynamic_firm.kernel.models import (
    EmployeeRecord,
    JobMetrics,
    JobResult,
    JobStatus,
    JobTask,
    TaskStatus,
)
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ContextBundle,
    EmployeeRunRequest,
    EmployeeSnapshot,
    RunLimits,
    TaskEnvelope,
    Usage,
)
from dynamic_firm.runtime.store import RunStore


CAPABILITY = "security_review"
CONTEXT = "python-repository"
PROFILE = "SHADOW_CODING"


@dataclass(frozen=True, slots=True)
class HireObservationEvaluationCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class HireObservationEvaluationRecord:
    schema_version: str
    evidence_class: str
    patch_id: str
    contract_content_hash: str
    source_evidence_count: int
    applied_roster_revision: int
    two_observation_decision: str
    three_observation_decision: str
    safety_decision: str
    cohort_count: int
    unrelated_observation_count: int
    final_roster_revision: int
    employee_active: bool
    automatic_dormancy: bool
    automatic_roster_patch: bool
    provider_calls: int
    quota_consumed: bool
    checks: tuple[HireObservationEvaluationCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def _episode(
    job_id: str,
    *,
    capability: str = CAPABILITY,
    context: str = CONTEXT,
    validation: tuple[bool, ...] = (True,),
    violations: tuple[str, ...] = (),
) -> OrganizationEpisode:
    return OrganizationEpisode.create(
        job_id=job_id,
        source=EvidenceSource.REAL_JOB,
        task_family=f"hire-observation.{capability}",
        context_fingerprint=context,
        execution_profile=PROFILE,
        planning_mode="DYNAMIC",
        plan_template=(WorkflowTaskTemplate("specialist", (capability,), final=True),),
        success=True,
        quality_score=1.0,
        baseline_quality_score=None,
        model_calls=1,
        baseline_model_calls=None,
        employee_count=2,
        maximum_parallelism=1,
        writer_count=1,
        approvals_requested=1,
        approvals_granted=1,
        preapproval_mutations=0,
        validation_attempts=validation,
        safety_violations=violations,
        ledger_digest=f"ledger-{job_id}",
    )


def _record_demand(store: CompanyStateStore, job_id: str) -> StaffingDemandEvidence:
    episode = _episode(job_id)
    store.record_episode(episode)
    evidence = StaffingDemandEvidence.create(
        episode_id=episode.episode_id,
        job_id=episode.job_id,
        source=episode.source,
        context_fingerprint=episode.context_fingerprint,
        execution_profile=episode.execution_profile,
        base_roster_revision=store.roster().revision,
        task_id="specialist",
        capability=CAPABILITY,
        role_label="Temporary Security Review Specialist",
        job_succeeded=True,
        validation_attempts=episode.validation_attempts,
        safety_violations=episode.safety_violations,
        writer_count=episode.writer_count,
        approvals_requested=episode.approvals_requested,
        approvals_granted=episode.approvals_granted,
        preapproval_mutations=episode.preapproval_mutations,
        ledger_digest=episode.ledger_digest,
        recorded_at=episode.recorded_at,
    )
    return store.record_staffing_demand(evidence)[0]


def _observe(
    store: CompanyStateStore,
    runtime: RunStore,
    *,
    patch_id: str,
    employee_id: str,
    job_id: str,
    capability: str = CAPABILITY,
    context: str = CONTEXT,
    base_roster_revision: int = 3,
    temporary: bool = False,
    validation: tuple[bool, ...] = (True,),
    violations: tuple[str, ...] = (),
):
    episode = _episode(
        job_id,
        capability=capability,
        context=context,
        validation=validation,
        violations=violations,
    )
    store.record_episode(episode)
    if not runtime.list_job_runs(job_id):
        runtime.create_run(
            EmployeeRunRequest(
                request_id=f"request-{job_id}",
                employee=EmployeeSnapshot(
                    employee_id=employee_id,
                    role="Assigned Specialist",
                    capabilities=(capability,),
                    temporary=temporary,
                ),
                task=TaskEnvelope(
                    job_id=job_id,
                    job_graph_version=1,
                    task_id="specialist",
                    attempt=1,
                    objective="Review the target.",
                    required_capabilities=(capability,),
                    acceptance_criteria=("Return review evidence.",),
                ),
                context=ContextBundle(),
                limits=RunLimits(),
                action_policy=ActionPolicy(),
            )
        )
    result = JobResult(
        job_id=job_id,
        request_id=f"company-{job_id}",
        status=JobStatus.SUCCEEDED,
        summary="Observed",
        acceptance_evidence=("observed",),
        unresolved_issues=(),
        task_results=(),
        final_graph_version=1,
        final_tasks=(
            JobTask(
                task_id="specialist",
                objective="Review the target.",
                depends_on=(),
                required_capabilities=(capability,),
                acceptance_criteria=("Return review evidence.",),
                status=TaskStatus.SUCCEEDED,
                assignee_id=employee_id,
            ),
        ),
        metrics=JobMetrics(1, int(temporary), 1, 0, Usage(model_calls=1)),
    )
    return HireObservationService(store).observe(
        patch_id,
        result,
        runtime.list_job_runs(job_id),
        episode=episode,
        base_roster_revision=base_roster_revision,
    )


def run_hire_observation_evaluation() -> HireObservationEvaluationRecord:
    """Exercise bounded post-hire attribution without a provider or network."""

    with tempfile.TemporaryDirectory(prefix="noruct-hire-observation-") as directory:
        path = Path(directory) / "runtime.db"
        with CompanyStateStore(path) as store:
            runtime = RunStore(path)
            try:
                store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            "employee-generalist",
                            "Generalist",
                            ("conversation",),
                            model_profile="company-default",
                        ),
                    )
                )
                first = _record_demand(store, "demand-one")
                second = _record_demand(store, "demand-two")
                candidate = HiringRecommendationService(store).curate().candidates[0]
                patches = RosterPatchService(store)
                patches.approve(candidate.patch_id, actor="user:evaluation")
                applied = patches.apply(candidate.patch_id, actor="user:evaluation")
                contract = store.get_hire_observation_contract(candidate.patch_id)
                roster_patch_count = len(store.list_roster_patches())

                unrelated = (
                    _observe(
                        store,
                        runtime,
                        patch_id=candidate.patch_id,
                        employee_id=contract.employee_id,
                        job_id="other-context",
                        context="other-repository",
                    ),
                    _observe(
                        store,
                        runtime,
                        patch_id=candidate.patch_id,
                        employee_id="employee-other",
                        job_id="other-capability",
                        capability="compliance_review",
                    ),
                    _observe(
                        store,
                        runtime,
                        patch_id=candidate.patch_id,
                        employee_id=contract.employee_id,
                        job_id="pre-hire-revision",
                        base_roster_revision=2,
                    ),
                )
                first_observation = _observe(
                    store,
                    runtime,
                    patch_id=candidate.patch_id,
                    employee_id=contract.employee_id,
                    job_id="matching-one",
                )
                duplicate = _observe(
                    store,
                    runtime,
                    patch_id=candidate.patch_id,
                    employee_id=contract.employee_id,
                    job_id="matching-one",
                )
                _observe(
                    store,
                    runtime,
                    patch_id=candidate.patch_id,
                    employee_id=contract.employee_id,
                    job_id="matching-two",
                )
                observation_service = HireObservationService(store)
                two = observation_service.assess(candidate.patch_id)
                _observe(
                    store,
                    runtime,
                    patch_id=candidate.patch_id,
                    employee_id=contract.employee_id,
                    job_id="matching-three",
                )
                three = observation_service.assess(candidate.patch_id)
                _observe(
                    store,
                    runtime,
                    patch_id=candidate.patch_id,
                    employee_id=contract.employee_id,
                    job_id="matching-unsafe",
                    validation=(),
                    violations=("no_validation_evidence",),
                )
                safety = observation_service.assess(candidate.patch_id)
                replayed_safety = observation_service.assess(candidate.patch_id)
                observations = store.list_hire_observations(candidate.patch_id)
                final_revision = store.roster().revision
                employee_payload = next(
                    employee
                    for employee in store.roster().employees
                    if employee["employee_id"] == contract.employee_id
                )
                final_roster_patch_count = len(store.list_roster_patches())
            finally:
                runtime.close()

        with CompanyStateStore(path) as restarted:
            restarted_contract = restarted.get_hire_observation_contract(candidate.patch_id)
            restarted_assessment = restarted.latest_hire_assessment(candidate.patch_id)
            restarted_observation_count = len(
                restarted.list_hire_observations(candidate.patch_id)
            )

        cohort_count = sum(item.cohort_eligible for item in observations)
        checks = (
            HireObservationEvaluationCheck(
                "apply_creates_exact_contract_atomically",
                contract.patch_id == applied.patch_id
                and contract.applied_roster_revision == 3
                and contract.source_evidence_ids
                == tuple(sorted((first.evidence_id, second.evidence_id))),
                f"patch={contract.patch_id},revision={contract.applied_roster_revision}",
            ),
            HireObservationEvaluationCheck(
                "unrelated_jobs_are_evidence_not_cohort",
                all(not item.cohort_eligible for item in unrelated),
                ",".join(reason for item in unrelated for reason in item.ineligibility_reasons),
            ),
            HireObservationEvaluationCheck(
                "two_matching_jobs_are_insufficient",
                two.decision.value == "INSUFFICIENT_OBSERVATION"
                and len(two.cohort_observation_ids) == 2,
                f"{two.decision.value},cohort={len(two.cohort_observation_ids)}",
            ),
            HireObservationEvaluationCheck(
                "three_exact_assignments_keep",
                three.decision.value == "KEEP"
                and three.persistent_assignment_count == 3
                and three.temporary_fallback_count == 0,
                f"{three.decision.value},persistent={three.persistent_assignment_count}",
            ),
            HireObservationEvaluationCheck(
                "attributed_safety_failure_recommends_dormancy",
                safety.decision.value == "DORMANCY_CANDIDATE"
                and "attributed_safety_violation" in safety.reasons,
                safety.decision.value,
            ),
            HireObservationEvaluationCheck(
                "duplicate_projection_and_assessment_are_idempotent",
                duplicate.observation_id == first_observation.observation_id
                and replayed_safety.assessment_id == safety.assessment_id,
                f"observation={duplicate.observation_id},assessment={replayed_safety.assessment_id}",
            ),
            HireObservationEvaluationCheck(
                "assessment_never_changes_roster_or_employee_state",
                final_revision == 3
                and len(observations) == 7
                and len(observations) == restarted_observation_count
                and final_roster_patch_count == roster_patch_count
                and employee_payload["active"] is True,
                f"ROSTER=r{final_revision},active={employee_payload['active']}",
            ),
            HireObservationEvaluationCheck(
                "restart_replays_contract_observations_and_assessment",
                restarted_contract.content_hash == contract.content_hash
                and restarted_assessment is not None
                and restarted_assessment.assessment_id == safety.assessment_id,
                f"contract={restarted_contract.content_hash[:12]},assessment={restarted_assessment.assessment_id if restarted_assessment else 'none'}",
            ),
        )
        return HireObservationEvaluationRecord(
            schema_version="noruct.hire-observation-evaluation.v1",
            evidence_class="offline-production-shaped-attribution-fixture",
            patch_id=candidate.patch_id,
            contract_content_hash=contract.content_hash,
            source_evidence_count=len(contract.source_evidence_ids),
            applied_roster_revision=contract.applied_roster_revision,
            two_observation_decision=two.decision.value,
            three_observation_decision=three.decision.value,
            safety_decision=safety.decision.value,
            cohort_count=cohort_count,
            unrelated_observation_count=len(unrelated),
            final_roster_revision=final_revision,
            employee_active=bool(employee_payload["active"]),
            automatic_dormancy=False,
            automatic_roster_patch=False,
            provider_calls=0,
            quota_consumed=False,
            checks=checks,
        )
