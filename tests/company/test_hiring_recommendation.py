from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.company import (
    CompanyStateStore,
    EvidenceSource,
    HiringRecommendationService,
    OrganizationEpisode,
    RosterPatchService,
    StaffingDemandEvidence,
    WorkflowTaskTemplate,
    decode_active_roster,
    staffing_demands_from_runtime_ledger,
)
from dynamic_firm.company.models import canonical_json
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


def employee() -> EmployeeRecord:
    return EmployeeRecord(
        employee_id="employee-generalist",
        role="Generalist",
        capabilities=("conversation",),
        model_profile="company-default",
    )


def episode(
    job_id: str,
    *,
    source: EvidenceSource = EvidenceSource.REAL_JOB,
    context: str = "python-repository",
    success: bool = True,
    validation: tuple[bool, ...] = (True,),
    violations: tuple[str, ...] = (),
) -> OrganizationEpisode:
    return OrganizationEpisode.create(
        job_id=job_id,
        source=source,
        task_family="coding.security-review",
        context_fingerprint=context,
        execution_profile="SHADOW_CODING",
        planning_mode="DYNAMIC",
        plan_template=(
            WorkflowTaskTemplate(
                "security",
                ("security_review",),
                final=True,
            ),
        ),
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


def demand(
    source_episode: OrganizationEpisode,
    *,
    capability: str = "security_review",
) -> StaffingDemandEvidence:
    return StaffingDemandEvidence.create(
        episode_id=source_episode.episode_id,
        job_id=source_episode.job_id,
        source=source_episode.source,
        context_fingerprint=source_episode.context_fingerprint,
        execution_profile=source_episode.execution_profile,
        base_roster_revision=2,
        task_id="security",
        capability=capability,
        role_label="Temporary Security Review Specialist",
        job_succeeded=source_episode.success,
        validation_attempts=source_episode.validation_attempts,
        safety_violations=source_episode.safety_violations,
        writer_count=source_episode.writer_count,
        approvals_requested=source_episode.approvals_requested,
        approvals_granted=source_episode.approvals_granted,
        preapproval_mutations=source_episode.preapproval_mutations,
        ledger_digest=source_episode.ledger_digest,
        recorded_at=source_episode.recorded_at,
    )


class HiringRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "runtime.db"
        self.store = CompanyStateStore(self.path)
        self.store.ensure_roster_baseline((employee(),))

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _record(self, item: OrganizationEpisode) -> StaffingDemandEvidence:
        self.store.record_episode(item)
        evidence = demand(item)
        self.store.record_staffing_demand(evidence)
        return evidence

    def test_runtime_projection_uses_explicit_temporary_flag_and_omits_employee_id(self) -> None:
        runtime_path = Path(self.temporary.name) / "ledger.db"
        runtime = RunStore(runtime_path)
        request = EmployeeRunRequest(
            request_id="request-security",
            employee=EmployeeSnapshot(
                employee_id="temp-job-security-1",
                role="Temporary Security Review Specialist",
                capabilities=("security_review",),
                temporary=True,
            ),
            task=TaskEnvelope(
                job_id="job-security",
                job_graph_version=1,
                task_id="security",
                attempt=1,
                objective="Review security controls.",
                required_capabilities=("security_review",),
                acceptance_criteria=("Return review evidence.",),
            ),
            context=ContextBundle(),
            limits=RunLimits(),
            action_policy=ActionPolicy(),
        )
        runtime.create_run(request)
        result = JobResult(
            job_id="job-security",
            request_id="company-request",
            status=JobStatus.SUCCEEDED,
            summary="Reviewed",
            acceptance_evidence=("reviewed",),
            unresolved_issues=(),
            task_results=(),
            final_graph_version=1,
            final_tasks=(
                JobTask(
                    task_id="security",
                    objective="Review security controls.",
                    depends_on=(),
                    required_capabilities=("security_review",),
                    acceptance_criteria=("Return review evidence.",),
                    status=TaskStatus.SUCCEEDED,
                    assignee_id="temp-job-security-1",
                ),
            ),
            metrics=JobMetrics(1, 1, 1, 0, Usage(model_calls=1)),
        )
        source_episode = episode("job-security")

        projected = staffing_demands_from_runtime_ledger(
            result,
            runtime.list_job_runs("job-security"),
            episode=source_episode,
            base_roster_revision=2,
        )
        runtime.close()

        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0].capability, "security_review")
        self.assertTrue(projected[0].safety_passed)
        self.assertNotIn("temp-job-security-1", canonical_json(projected[0]))
        self.store.record_episode(source_episode)
        stored, created = self.store.record_staffing_demand(projected[0])
        duplicate, created_again = self.store.record_staffing_demand(projected[0])
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(stored.evidence_id, duplicate.evidence_id)
        with self.assertRaisesRegex(ValueError, "content hash mismatch"):
            self.store.record_staffing_demand(
                replace(projected[0], role_label="Tampered role")
            )

    def test_two_independent_safe_demands_create_one_deterministic_candidate(self) -> None:
        first = self._record(episode("job-one"))
        before = HiringRecommendationService(self.store).curate()
        self.assertEqual(before.decision, "NO_PATCH")
        self.assertEqual(self.store.roster().revision, 2)

        second = self._record(episode("job-two"))
        service = HiringRecommendationService(self.store)
        recommended = service.curate()
        replayed = service.curate()

        self.assertEqual(recommended.decision, "CANDIDATE_AVAILABLE")
        self.assertEqual(len(recommended.candidates), 1)
        candidate = recommended.candidates[0]
        self.assertEqual(candidate.patch_id, replayed.candidates[0].patch_id)
        self.assertEqual(candidate.evidence_ids, tuple(sorted((first.evidence_id, second.evidence_id))))
        self.assertEqual(candidate.after_employee["capabilities"], ["security_review"])
        self.assertEqual(candidate.proposed_by, "system:staffing-demand-curator")
        self.assertEqual(self.store.roster().revision, 2)

        patches = RosterPatchService(self.store)
        patches.approve(candidate.patch_id, actor="user:test")
        applied = patches.apply(candidate.patch_id, actor="user:test")
        self.assertEqual(applied.applied_revision, 3)
        snapshot = decode_active_roster(self.store.roster())
        self.assertIn("security_review", snapshot.available_capabilities)
        after = service.curate()
        self.assertEqual(after.decision, "NO_PATCH")
        self.assertIn(
            "capability_already_covered:security_review",
            after.reasons,
        )

    def test_offline_recommendation_is_preview_only(self) -> None:
        self._record(episode("offline-one", source=EvidenceSource.OFFLINE_FIXTURE))
        self._record(episode("offline-two", source=EvidenceSource.OFFLINE_FIXTURE))
        result = HiringRecommendationService(self.store).curate()
        candidate = result.candidates[0]

        with self.assertRaisesRegex(ValueError, "offline.*cannot be approved"):
            RosterPatchService(self.store).approve(candidate.patch_id, actor="user:test")
        self.assertEqual(self.store.roster().revision, 2)

    def test_evidence_contract_rejects_single_job_and_capability_mismatch(self) -> None:
        first = self._record(episode("contract-one"))
        second = self._record(episode("contract-two"))
        patches = RosterPatchService(self.store)
        proposed = EmployeeRecord(
            "employee-contract",
            "Contract Specialist",
            ("security_review",),
            model_profile="company-default",
        )

        with self.assertRaisesRegex(ValueError, "two independent jobs"):
            patches.propose_add_employee(
                proposed,
                rationale="Invalid one-job recommendation.",
                actor="system:test",
                evidence_ids=(first.evidence_id,),
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            patches.propose_add_employee(
                replace(proposed, capabilities=("other_capability",)),
                rationale="Invalid capability recommendation.",
                actor="system:test",
                evidence_ids=(first.evidence_id, second.evidence_id),
            )
        self.assertEqual(self.store.list_roster_patches(), ())

    def test_unsafe_or_different_context_does_not_fill_repetition_gate(self) -> None:
        self._record(episode("safe-one"))
        self._record(
            episode(
                "unsafe-two",
                violations=("preapproval_mutation",),
            )
        )
        self._record(episode("other-context", context="other-repository"))

        result = HiringRecommendationService(self.store).curate()

        self.assertEqual(result.decision, "NO_PATCH")
        self.assertEqual(result.qualified_evidence_count, 2)
        self.assertIn("insufficient_repeated_demand:security_review", result.reasons)
        self.assertEqual(self.store.list_roster_patches(), ())

    def test_schema_v5_migrates_additively_to_current_schema(self) -> None:
        original = self.store.summary()
        self.store.close()
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute("DROP TABLE roster_patch_staffing_evidence")
            connection.execute("DROP TABLE staffing_demand_evidence")
            connection.execute(
                "UPDATE company_state_meta SET value = '5' WHERE key = 'schema_version'"
            )
        connection.close()

        self.store = CompanyStateStore(self.path)

        self.assertEqual(self.store.schema_version(), 9)
        self.assertEqual(self.store.summary().company_revision, original.company_revision)
        self.assertEqual(self.store.summary().roster_revision, original.roster_revision)
        self.assertEqual(self.store.summary().playbook_revision, original.playbook_revision)
        self.assertEqual(self.store.summary().staffing_demand_count, 0)


if __name__ == "__main__":
    unittest.main()
