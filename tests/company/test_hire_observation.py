from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.company import (
    CompanyStateStore,
    EvidenceSource,
    HireAssessmentDecision,
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


def _episode(
    job_id: str,
    *,
    capability: str = CAPABILITY,
    context: str = CONTEXT,
    source: EvidenceSource = EvidenceSource.REAL_JOB,
    success: bool = True,
    validation: tuple[bool, ...] = (True,),
    violations: tuple[str, ...] = (),
) -> OrganizationEpisode:
    return OrganizationEpisode.create(
        job_id=job_id,
        source=source,
        task_family=f"staffing.{capability}",
        context_fingerprint=context,
        execution_profile=PROFILE,
        planning_mode="DYNAMIC",
        plan_template=(WorkflowTaskTemplate("specialist", (capability,), final=True),),
        success=success,
        quality_score=1.0 if success else 0.0,
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


def _demand(episode: OrganizationEpisode, base_revision: int) -> StaffingDemandEvidence:
    capability = episode.plan_template[0].required_capabilities[0]
    return StaffingDemandEvidence.create(
        episode_id=episode.episode_id,
        job_id=episode.job_id,
        source=episode.source,
        context_fingerprint=episode.context_fingerprint,
        execution_profile=episode.execution_profile,
        base_roster_revision=base_revision,
        task_id="specialist",
        capability=capability,
        role_label="Temporary Security Review Specialist",
        job_succeeded=episode.success,
        validation_attempts=episode.validation_attempts,
        safety_violations=episode.safety_violations,
        writer_count=episode.writer_count,
        approvals_requested=episode.approvals_requested,
        approvals_granted=episode.approvals_granted,
        preapproval_mutations=episode.preapproval_mutations,
        ledger_digest=episode.ledger_digest,
        recorded_at=episode.recorded_at,
    )


class HireObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "runtime.db"
        self.store: CompanyStateStore | None = CompanyStateStore(self.path)
        self.runtime: RunStore | None = RunStore(self.path)
        self.store.ensure_roster_baseline(
            (
                EmployeeRecord(
                    "employee-generalist",
                    "Generalist",
                    ("conversation",),
                    model_profile="company-default",
                ),
            )
        )

    def tearDown(self) -> None:
        if self.runtime is not None:
            self.runtime.close()
        if self.store is not None:
            self.store.close()
        self.temporary.cleanup()

    def _apply_evidence_backed_hire(self):
        assert self.store is not None
        for job_id in ("demand-one", "demand-two"):
            episode = _episode(job_id)
            self.store.record_episode(episode)
            self.store.record_staffing_demand(
                _demand(episode, self.store.roster().revision)
            )
        candidate = HiringRecommendationService(self.store).curate().candidates[0]
        patches = RosterPatchService(self.store)
        patches.approve(candidate.patch_id, actor="user:test")
        applied = patches.apply(candidate.patch_id, actor="user:test")
        return applied, self.store.get_hire_observation_contract(candidate.patch_id)

    def _observe(
        self,
        patch_id: str,
        employee_id: str,
        job_id: str,
        *,
        capability: str = CAPABILITY,
        context: str = CONTEXT,
        base_revision: int = 3,
        temporary: bool = False,
        success: bool = True,
        validation: tuple[bool, ...] = (True,),
        violations: tuple[str, ...] = (),
    ):
        assert self.store is not None and self.runtime is not None
        episode = _episode(
            job_id,
            capability=capability,
            context=context,
            success=success,
            validation=validation,
            violations=violations,
        )
        self.store.record_episode(episode)
        request = EmployeeRunRequest(
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
        self.runtime.create_run(request)
        result = JobResult(
            job_id=job_id,
            request_id=f"company-{job_id}",
            status=JobStatus.SUCCEEDED if success else JobStatus.FAILED,
            summary="Observed",
            acceptance_evidence=("observed",) if success else (),
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
                    status=TaskStatus.SUCCEEDED if success else TaskStatus.FAILED,
                    assignee_id=employee_id,
                ),
            ),
            metrics=JobMetrics(1, int(temporary), 1, 0, Usage(model_calls=1)),
        )
        return HireObservationService(self.store).observe(
            patch_id,
            result,
            self.runtime.list_job_runs(job_id),
            episode=episode,
            base_roster_revision=base_revision,
        )

    def test_apply_creates_exact_contract_but_manual_add_does_not(self) -> None:
        assert self.store is not None
        applied, contract = self._apply_evidence_backed_hire()

        self.assertEqual(contract.patch_id, applied.patch_id)
        self.assertEqual(contract.applied_roster_revision, 3)
        self.assertEqual(contract.employee_id, applied.employee_id)
        self.assertEqual(contract.capability, CAPABILITY)
        self.assertEqual(contract.minimum_observations, 3)
        self.assertEqual(contract.maximum_observations, 5)
        self.assertEqual(contract.source_evidence_ids, applied.evidence_ids)

        manual = RosterPatchService(self.store).propose_add_employee(
            EmployeeRecord(
                "employee-manual",
                "Manual Specialist",
                ("manual_review",),
                model_profile="company-default",
            ),
            rationale="Manual operator proposal.",
            actor="user:test",
        )
        RosterPatchService(self.store).approve(manual.patch_id, actor="user:test")
        RosterPatchService(self.store).apply(manual.patch_id, actor="user:test")
        with self.assertRaisesRegex(KeyError, "no hire observation contract"):
            self.store.get_hire_observation_contract(manual.patch_id)

    def test_two_observations_are_insufficient_and_three_exact_assignments_keep(self) -> None:
        assert self.store is not None
        applied, contract = self._apply_evidence_backed_hire()
        service = HireObservationService(self.store)

        self._observe(contract.patch_id, contract.employee_id, "post-hire-one")
        self._observe(contract.patch_id, contract.employee_id, "post-hire-two")
        two = service.assess(contract.patch_id)
        roster_before = self.store.roster().revision
        self._observe(contract.patch_id, contract.employee_id, "post-hire-three")
        three = service.assess(contract.patch_id)

        self.assertEqual(
            two.decision,
            HireAssessmentDecision.INSUFFICIENT_OBSERVATION,
        )
        self.assertEqual(three.decision, HireAssessmentDecision.KEEP)
        self.assertEqual(three.persistent_assignment_count, 3)
        self.assertEqual(three.temporary_fallback_count, 0)
        self.assertEqual(self.store.roster().revision, roster_before)
        self.assertEqual(applied.status.value, "APPLIED")

        self.runtime.close()
        self.runtime = None
        self.store.close()
        self.store = CompanyStateStore(self.path)
        self.assertEqual(
            self.store.latest_hire_assessment(contract.patch_id).assessment_id,
            three.assessment_id,
        )
        self.assertEqual(len(self.store.list_hire_observations(contract.patch_id)), 3)

    def test_other_context_capability_and_pre_hire_revision_are_not_cohort(self) -> None:
        assert self.store is not None
        _, contract = self._apply_evidence_backed_hire()

        other_context = self._observe(
            contract.patch_id,
            contract.employee_id,
            "other-context",
            context="other-repository",
        )
        other_capability = self._observe(
            contract.patch_id,
            "employee-other",
            "other-capability",
            capability="compliance_review",
        )
        pre_hire = self._observe(
            contract.patch_id,
            contract.employee_id,
            "pre-hire",
            base_revision=2,
        )

        self.assertFalse(other_context.cohort_eligible)
        self.assertIn("context_fingerprint_mismatch", other_context.ineligibility_reasons)
        self.assertFalse(other_capability.cohort_eligible)
        self.assertIn("capability_task_missing", other_capability.ineligibility_reasons)
        self.assertFalse(pre_hire.cohort_eligible)
        self.assertIn("pre_hire_roster_revision", pre_hire.ineligibility_reasons)
        assessment = HireObservationService(self.store).assess(contract.patch_id)
        self.assertEqual(
            assessment.decision,
            HireAssessmentDecision.INSUFFICIENT_OBSERVATION,
        )
        self.assertEqual(assessment.cohort_observation_ids, ())

    def test_attributed_safety_failure_recommends_dormancy_without_roster_change(self) -> None:
        assert self.store is not None
        _, contract = self._apply_evidence_backed_hire()
        roster_revision = self.store.roster().revision
        self._observe(
            contract.patch_id,
            contract.employee_id,
            "unsafe-post-hire",
            validation=(),
            violations=("no_validation_evidence",),
        )

        assessment = HireObservationService(self.store).assess(contract.patch_id)

        self.assertEqual(
            assessment.decision,
            HireAssessmentDecision.DORMANCY_CANDIDATE,
        )
        self.assertIn("attributed_safety_violation", assessment.reasons)
        self.assertEqual(self.store.roster().revision, roster_revision)
        active = next(
            employee
            for employee in self.store.roster().employees
            if employee["employee_id"] == contract.employee_id
        )
        self.assertTrue(active["active"])

    def test_five_matching_jobs_with_repeated_temporary_fallback_recommend_dormancy(self) -> None:
        assert self.store is not None
        _, contract = self._apply_evidence_backed_hire()
        for index in range(5):
            temporary = index < 2
            self._observe(
                contract.patch_id,
                f"temporary-fallback-{index}" if temporary else contract.employee_id,
                f"fallback-{index}",
                temporary=temporary,
            )

        assessment = HireObservationService(self.store).assess(contract.patch_id)

        self.assertEqual(
            assessment.decision,
            HireAssessmentDecision.DORMANCY_CANDIDATE,
        )
        self.assertEqual(assessment.temporary_fallback_count, 2)
        self.assertIn("temporary_fallback_repeated", assessment.reasons)
        self.assertEqual(self.store.roster().revision, 3)

    def test_observation_tamper_is_rejected(self) -> None:
        assert self.store is not None
        _, contract = self._apply_evidence_backed_hire()
        observation = self._observe(
            contract.patch_id,
            contract.employee_id,
            "tamper-source",
        )

        with self.assertRaisesRegex(ValueError, "content hash"):
            self.store.record_hire_observation(
                replace(observation, persistent_employee_assigned=False)
            )

    def test_schema_v6_backfills_contract_for_applied_evidence_hire(self) -> None:
        assert self.store is not None and self.runtime is not None
        applied, contract = self._apply_evidence_backed_hire()
        self.runtime.close()
        self.runtime = None
        self.store.close()
        self.store = None
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute("DROP TABLE hire_assessments")
            connection.execute("DROP TABLE hire_observations")
            connection.execute("DROP TABLE hire_observation_contracts")
            connection.execute(
                "UPDATE company_state_meta SET value = '6' WHERE key = 'schema_version'"
            )
        connection.close()

        self.store = CompanyStateStore(self.path)
        migrated = self.store.get_hire_observation_contract(applied.patch_id)

        self.assertEqual(self.store.schema_version(), 9)
        self.assertEqual(migrated.content_hash, contract.content_hash)
        self.assertEqual(migrated.created_at, applied.updated_at)
        self.assertEqual(self.store.roster().revision, 3)


if __name__ == "__main__":
    unittest.main()
