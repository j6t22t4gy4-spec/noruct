from __future__ import annotations

import tempfile
import unittest
import sqlite3
import io
import json
from dataclasses import replace
from pathlib import Path

from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    EvidenceSource,
    OrganizationEpisode,
    WorkflowPatchAssessmentDecision,
    WorkflowPatchStatus,
    WorkflowTaskTemplate,
)
from dynamic_firm.compiler import CompilerExecutionProfile
from dynamic_firm.cli import EXIT_OK, main


def episode(
    job_id: str,
    *,
    source: EvidenceSource = EvidenceSource.REAL_JOB,
    family: str = "coding.parallel-evidence",
    context: str = "tiny-python-repository",
    quality: float = 1.0,
    baseline_quality: float | None = 0.7,
    safety_violations: tuple[str, ...] = (),
    validation: tuple[bool, ...] = (True,),
    writers: int = 1,
    preapproval: int = 0,
    success: bool = True,
    model_calls: int = 3,
    baseline_model_calls: int | None = 4,
    plan_template: tuple[WorkflowTaskTemplate, ...] | None = None,
) -> OrganizationEpisode:
    tasks = plan_template or (
        WorkflowTaskTemplate("spec_evidence", ("analysis",)),
        WorkflowTaskTemplate("test_evidence", ("analysis",)),
        WorkflowTaskTemplate(
            "implement_change",
            ("implementation",),
            depends_on=("spec_evidence", "test_evidence"),
            final=True,
        ),
    )
    return OrganizationEpisode.create(
        job_id=job_id,
        source=source,
        task_family=family,
        context_fingerprint=context,
        execution_profile=CompilerExecutionProfile.SHADOW_CODING.value,
        planning_mode="DYNAMIC",
        plan_template=tasks,
        success=success,
        quality_score=quality,
        baseline_quality_score=baseline_quality,
        model_calls=model_calls,
        baseline_model_calls=baseline_model_calls,
        employee_count=2,
        maximum_parallelism=2,
        writer_count=writers,
        approvals_requested=1,
        approvals_granted=1,
        preapproval_mutations=preapproval,
        validation_attempts=validation,
        safety_violations=safety_violations,
        ledger_digest=f"ledger-{job_id}",
    )


class PersistentCompanyKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "runtime.db"
        self.store = CompanyStateStore(self.path)
        self.learning = CompanyLearningService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_initial_state_is_versioned_and_roster_baseline_is_write_once(self) -> None:
        initial = self.store.summary()
        self.assertEqual(initial.company_revision, 1)
        self.assertEqual(initial.roster_revision, 1)
        self.assertEqual(initial.playbook_revision, 1)
        self.assertEqual(initial.workflow_pattern_count, 0)

        first = self.store.ensure_roster_baseline(
            ({"employee_id": "employee-generalist", "role": "Generalist"},)
        )
        second = self.store.ensure_roster_baseline(
            ({"employee_id": "different", "role": "Ignored"},)
        )

        self.assertEqual(first.revision, 2)
        self.assertEqual(second.revision, 2)
        self.assertEqual(second.employees[0]["employee_id"], "employee-generalist")

    def test_episode_recording_is_idempotent_and_detects_job_key_reuse(self) -> None:
        evidence = episode("job-one")
        first, created = self.store.record_episode(evidence)
        duplicate, created_again = self.store.record_episode(
            replace(evidence, recorded_at="2099-01-01T00:00:00+00:00")
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.episode_id, duplicate.episode_id)
        with self.assertRaisesRegex(ValueError, "reused with different content"):
            self.store.record_episode(replace(evidence, quality_score=0.95))

    def test_default_is_no_patch_until_repetition_effect_and_safety_all_pass(self) -> None:
        self.store.record_episode(episode("job-one"))
        one = self.learning.curate()
        self.assertEqual(one.decision, "NO_PATCH")

        self.store.record_episode(
            episode("job-unsafe", safety_violations=("unexpected_mutation",))
        )
        unsafe = self.learning.curate()
        self.assertEqual(unsafe.decision, "NO_PATCH")

        self.store.record_episode(episode("job-two"))
        repeated = self.learning.curate()
        self.assertEqual(repeated.decision, "CANDIDATE_AVAILABLE")
        self.assertEqual(len(repeated.candidates), 1)
        self.assertTrue(repeated.candidates[0].eligible_for_apply)
        self.assertEqual(self.store.playbook().revision, 1)
        self.assertEqual(self.store.playbook().patterns, ())

    def test_curator_groups_renamed_and_reordered_equivalent_workflows(self) -> None:
        renamed = (
            WorkflowTaskTemplate(
                "finish",
                ("implementation",),
                depends_on=("tests", "requirements"),
                final=True,
            ),
            WorkflowTaskTemplate("tests", ("analysis",)),
            WorkflowTaskTemplate("requirements", ("analysis",)),
        )
        first = episode("job-one")
        second = episode("job-two", plan_template=renamed)
        self.assertNotEqual(first.plan_digest, second.plan_digest)
        self.store.record_episode(first)
        self.store.record_episode(second)

        result = self.learning.curate()

        self.assertEqual(result.decision, "CANDIDATE_AVAILABLE")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            result.candidates[0].evidence_episode_ids,
            (first.episode_id, second.episode_id),
        )
        self.assertEqual(result.candidates[0].pattern.evidence_count, 2)
        self.assertEqual(self.store.playbook().revision, 1)

    def test_curator_does_not_merge_different_dependency_shapes(self) -> None:
        chain = (
            WorkflowTaskTemplate("spec_evidence", ("analysis",)),
            WorkflowTaskTemplate(
                "test_evidence",
                ("analysis",),
                depends_on=("spec_evidence",),
            ),
            WorkflowTaskTemplate(
                "implement_change",
                ("implementation",),
                depends_on=("test_evidence",),
                final=True,
            ),
        )
        self.store.record_episode(episode("job-one"))
        self.store.record_episode(episode("job-two", plan_template=chain))

        result = self.learning.curate()

        self.assertEqual(result.decision, "NO_PATCH")
        self.assertEqual(result.reasons, ("insufficient_repeated_evidence",))
        self.assertEqual(self.store.list_patches(), ())

    def test_curator_falls_back_to_exact_digest_above_six_tasks(self) -> None:
        def chain(prefix: str) -> tuple[WorkflowTaskTemplate, ...]:
            return tuple(
                WorkflowTaskTemplate(
                    f"{prefix}-{index}",
                    ("implementation",) if index == 6 else ("analysis",),
                    () if index == 0 else (f"{prefix}-{index - 1}",),
                    final=index == 6,
                )
                for index in range(7)
            )

        first = episode("job-one", plan_template=chain("a"))
        second = episode("job-two", plan_template=chain("b"))
        self.assertNotEqual(first.plan_digest, second.plan_digest)
        self.store.record_episode(first)
        self.store.record_episode(second)

        result = self.learning.curate()

        self.assertEqual(result.decision, "NO_PATCH")
        self.assertEqual(result.reasons, ("insufficient_repeated_evidence",))
        self.assertEqual(self.store.list_patches(), ())

    def test_offline_evidence_can_be_previewed_but_never_approved_or_applied(self) -> None:
        self.store.record_episode(episode("offline-one", source=EvidenceSource.OFFLINE_FIXTURE))
        self.store.record_episode(episode("offline-two", source=EvidenceSource.OFFLINE_FIXTURE))

        result = self.learning.curate()
        candidate = result.candidates[0]

        self.assertFalse(candidate.eligible_for_apply)
        self.assertIn("synthetic_or_offline_evidence_present", candidate.ineligibility_reasons)
        with self.assertRaisesRegex(ValueError, "preview-only"):
            self.learning.approve(candidate.patch_id, actor="user:test")
        self.assertEqual(self.store.playbook().revision, 1)

    def test_explicit_approve_apply_prior_replay_and_append_only_rollback(self) -> None:
        self.store.record_episode(episode("job-one"))
        self.store.record_episode(episode("job-two"))
        candidate = self.learning.curate().candidates[0]

        with self.assertRaisesRegex(ValueError, "approved before apply"):
            self.learning.apply(candidate.patch_id, actor="user:test")
        approved = self.learning.approve(candidate.patch_id, actor="user:test")
        applied = self.learning.apply(candidate.patch_id, actor="user:test")

        self.assertEqual(approved.status, WorkflowPatchStatus.APPROVED)
        self.assertEqual(applied.status, WorkflowPatchStatus.APPLIED)
        self.assertEqual(applied.applied_revision, 2)
        self.assertEqual(self.store.playbook().revision, 2)
        self.assertTrue(self.learning.replay(candidate.patch_id))
        priors = self.learning.compiler_priors(CompilerExecutionProfile.SHADOW_CODING)
        self.assertEqual(len(priors), 1)
        self.assertEqual(priors[0].pattern_id, candidate.pattern.pattern_id)
        self.assertEqual(priors[0].evidence_count, 2)
        self.assertEqual(
            self.learning.compiler_priors(
                CompilerExecutionProfile.SHADOW_CODING,
                context_fingerprint="different-workspace",
            ),
            (),
        )

        rolled_back = self.learning.rollback(candidate.patch_id, actor="user:test")

        self.assertEqual(rolled_back.status, WorkflowPatchStatus.ROLLED_BACK)
        self.assertEqual(rolled_back.rolled_back_revision, 3)
        self.assertEqual(self.store.playbook().revision, 3)
        self.assertEqual(self.store.playbook().patterns, ())
        self.assertEqual(self.learning.compiler_priors(CompilerExecutionProfile.SHADOW_CODING), ())
        self.assertEqual(
            [event.event_type.value for event in self.store.list_patch_events(candidate.patch_id)],
            ["PROPOSED", "APPROVED", "APPLIED", "ROLLED_BACK"],
        )

    def test_stale_patch_cannot_overwrite_a_newer_playbook_revision(self) -> None:
        for job_id in ("a-one", "a-two"):
            self.store.record_episode(episode(job_id, family="family-a"))
        for job_id in ("b-one", "b-two"):
            self.store.record_episode(episode(job_id, family="family-b"))
        candidates = self.learning.curate().candidates
        self.assertEqual(len(candidates), 2)
        for candidate in candidates:
            self.learning.approve(candidate.patch_id, actor="user:test")

        self.learning.apply(candidates[0].patch_id, actor="user:test")
        with self.assertRaisesRegex(ValueError, "Playbook changed since proposal"):
            self.learning.apply(candidates[1].patch_id, actor="user:test")

    def test_offline_preview_does_not_poison_later_production_evidence(self) -> None:
        self.store.record_episode(episode("offline-one", source=EvidenceSource.OFFLINE_FIXTURE))
        self.store.record_episode(episode("offline-two", source=EvidenceSource.OFFLINE_FIXTURE))
        preview = self.learning.curate().candidates[0]
        self.assertFalse(preview.eligible_for_apply)

        self.store.record_episode(episode("real-one"))
        self.store.record_episode(episode("real-two"))
        candidates = self.learning.curate().candidates

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].eligible_for_apply)
        self.assertNotEqual(candidates[0].patch_id, preview.patch_id)

    def _apply_patch(self):
        self.store.record_episode(episode("seed-one"))
        self.store.record_episode(episode("seed-two"))
        candidate = self.learning.curate().candidates[0]
        self.learning.approve(candidate.patch_id, actor="user:test")
        return self.learning.apply(candidate.patch_id, actor="user:test")

    def test_observation_separates_prior_exposure_alignment_and_cohort(self) -> None:
        patch = self._apply_patch()
        follow_up, _ = self.store.record_episode(episode("follow-up"))

        observation = self.learning.observe(
            patch.patch_id,
            follow_up,
            prior_exposed=True,
            proposal_aligned=False,
        )
        assessment = self.learning.assess(patch.patch_id)

        self.assertTrue(observation.prior_exposed)
        self.assertFalse(observation.proposal_aligned)
        self.assertFalse(observation.attribution_eligible)
        self.assertFalse(observation.cohort_eligible)
        self.assertIn("proposal_not_aligned", observation.ineligibility_reasons)
        self.assertEqual(
            assessment.decision,
            WorkflowPatchAssessmentDecision.INSUFFICIENT_OBSERVATION,
        )
        self.assertEqual(assessment.cohort_observation_ids, ())

    def test_three_exact_measured_observations_keep_without_mutating_playbook(self) -> None:
        patch = self._apply_patch()
        before_revision = self.store.playbook().revision
        decisions = []
        for index in range(3):
            follow_up, _ = self.store.record_episode(episode(f"keep-{index}"))
            self.learning.observe(
                patch.patch_id,
                follow_up,
                prior_exposed=True,
                proposal_aligned=True,
            )
            decisions.append(self.learning.assess(patch.patch_id).decision)

        self.assertEqual(
            decisions,
            [
                WorkflowPatchAssessmentDecision.INSUFFICIENT_OBSERVATION,
                WorkflowPatchAssessmentDecision.INSUFFICIENT_OBSERVATION,
                WorkflowPatchAssessmentDecision.KEEP,
            ],
        )
        repeated = self.learning.assess(patch.patch_id)
        self.assertEqual(repeated.seq, 3)
        self.assertEqual(len(self.store.list_assessments(patch.patch_id)), 3)
        self.assertEqual(self.store.playbook().revision, before_revision)
        self.assertEqual(self.store.get_patch(patch.patch_id).status, WorkflowPatchStatus.APPLIED)

    def test_attributed_failure_or_safety_violation_is_rollback_candidate_only(self) -> None:
        patch = self._apply_patch()
        before_revision = self.store.playbook().revision
        unsafe, _ = self.store.record_episode(
            episode(
                "unsafe-follow-up",
                baseline_quality=None,
                baseline_model_calls=None,
                safety_violations=("unexpected_mutation",),
            )
        )
        self.learning.observe(
            patch.patch_id,
            unsafe,
            prior_exposed=True,
            proposal_aligned=True,
        )

        assessment = self.learning.assess(patch.patch_id)

        self.assertEqual(
            assessment.decision,
            WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE,
        )
        self.assertIn("attributed_safety_violation", assessment.reasons)
        self.assertEqual(self.store.playbook().revision, before_revision)
        self.assertEqual(self.store.get_patch(patch.patch_id).status, WorkflowPatchStatus.APPLIED)

    def test_neutral_effect_reaches_bound_then_recommends_rollback(self) -> None:
        patch = self._apply_patch()
        for index in range(10):
            follow_up, _ = self.store.record_episode(
                episode(
                    f"neutral-{index}",
                    quality=0.7,
                    baseline_quality=0.7,
                    model_calls=4,
                    baseline_model_calls=4,
                )
            )
            self.learning.observe(
                patch.patch_id,
                follow_up,
                prior_exposed=True,
                proposal_aligned=True,
            )

        assessment = self.learning.assess(patch.patch_id)
        self.assertEqual(
            assessment.decision,
            WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE,
        )
        self.assertIn(
            "effect_not_reproduced_within_observation_limit", assessment.reasons
        )
        self.assertEqual(len(assessment.cohort_observation_ids), 10)

    def test_schema_v1_migrates_in_place_and_backfills_applied_contract(self) -> None:
        path = Path(self.temporary.name) / "legacy.db"
        legacy = CompanyStateStore(path)
        legacy_learning = CompanyLearningService(legacy)
        legacy.record_episode(episode("legacy-one"))
        legacy.record_episode(episode("legacy-two"))
        candidate = legacy_learning.curate().candidates[0]
        legacy_learning.approve(candidate.patch_id, actor="user:test")
        legacy_learning.apply(candidate.patch_id, actor="user:test")
        legacy.close()

        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                DROP TABLE workflow_patch_assessments;
                DROP TABLE workflow_patch_observations;
                DROP TABLE workflow_patch_observation_contracts;
                UPDATE company_state_meta SET value = '1' WHERE key = 'schema_version';
                """
            )

        migrated = CompanyStateStore(path)
        try:
            self.assertEqual(migrated.schema_version(), 9)
            self.assertEqual(len(migrated.list_episodes()), 2)
            self.assertEqual(migrated.playbook().revision, 2)
            self.assertEqual(
                migrated.get_patch(candidate.patch_id).status,
                WorkflowPatchStatus.APPLIED,
            )
            self.assertEqual(
                migrated.get_observation_contract(candidate.patch_id).pattern_id,
                candidate.pattern.pattern_id,
            )
        finally:
            migrated.close()

    def test_company_observe_and_assess_cli_are_explicit_and_machine_readable(self) -> None:
        patch = self._apply_patch()
        observed, _ = self.store.record_episode(episode("cli-observation"))
        self.learning.observe(
            patch.patch_id,
            observed,
            prior_exposed=True,
            proposal_aligned=True,
        )
        observe_output = io.StringIO()
        assess_output = io.StringIO()
        error = io.StringIO()

        observe_exit = main(
            [
                "company",
                "observe",
                patch.patch_id,
                "--state",
                str(self.path),
                "--json",
            ],
            stdout=observe_output,
            stderr=error,
        )
        assess_exit = main(
            [
                "company",
                "assess",
                patch.patch_id,
                "--state",
                str(self.path),
                "--json",
            ],
            stdout=assess_output,
            stderr=error,
        )
        observed_payload = json.loads(observe_output.getvalue())
        assessed_payload = json.loads(assess_output.getvalue())

        self.assertEqual(observe_exit, EXIT_OK, error.getvalue())
        self.assertEqual(assess_exit, EXIT_OK, error.getvalue())
        self.assertEqual(observed_payload["attribution"]["proposal_aligned"], 1)
        self.assertEqual(
            assessed_payload["assessment"]["decision"],
            "INSUFFICIENT_OBSERVATION",
        )
        self.assertFalse(assessed_payload["automatic_rollback"])
        self.assertEqual(
            assessed_payload["playbook_revision_before"],
            assessed_payload["playbook_revision_after"],
        )


if __name__ == "__main__":
    unittest.main()
