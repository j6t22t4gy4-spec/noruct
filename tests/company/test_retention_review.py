from __future__ import annotations

import sqlite3
import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.company import (
    CompanyStateStore,
    HireAssessmentDecision,
    HireObservationService,
    HiringRecommendationService,
    RetentionReviewDecision,
    RetentionReviewMode,
    RosterPatchService,
    RosterPatchStatus,
    RosterRetentionService,
)
from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.evaluation.hire_observation import _observe, _record_demand
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.runtime.store import RunStore


class RetentionReviewTests(unittest.TestCase):
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

    def _hire(self):
        assert self.store is not None
        _record_demand(self.store, "retention-demand-one")
        _record_demand(self.store, "retention-demand-two")
        candidate = HiringRecommendationService(self.store).curate().candidates[0]
        patches = RosterPatchService(self.store)
        patches.approve(candidate.patch_id, actor="user:test")
        patches.apply(candidate.patch_id, actor="user:test")
        return candidate, self.store.get_hire_observation_contract(candidate.patch_id)

    def _full_window_dormancy(self):
        assert self.store is not None and self.runtime is not None
        candidate, contract = self._hire()
        for index in range(contract.maximum_observations):
            _observe(
                self.store,
                self.runtime,
                patch_id=candidate.patch_id,
                employee_id=f"temporary-retention-{index}",
                job_id=f"retention-fallback-{index}",
                temporary=True,
            )
        assessment = HireObservationService(self.store).assess(candidate.patch_id)
        self.assertEqual(
            assessment.decision,
            HireAssessmentDecision.DORMANCY_CANDIDATE,
        )
        return candidate, contract, assessment

    def _safety_dormancy(self):
        assert self.store is not None and self.runtime is not None
        candidate, contract = self._hire()
        _observe(
            self.store,
            self.runtime,
            patch_id=candidate.patch_id,
            employee_id=contract.employee_id,
            job_id="retention-safety-failure",
            validation=(),
            violations=("no_validation_evidence",),
        )
        assessment = HireObservationService(self.store).assess(candidate.patch_id)
        self.assertEqual(
            assessment.decision,
            HireAssessmentDecision.DORMANCY_CANDIDATE,
        )
        return candidate, contract, assessment

    def test_default_approval_creates_proposal_only_then_manual_apply(self) -> None:
        assert self.store is not None
        candidate, contract, assessment = self._full_window_dormancy()

        result = RosterRetentionService(self.store).recommend(candidate.patch_id)

        self.assertEqual(result.mode, RetentionReviewMode.APPROVAL)
        self.assertEqual(
            result.review.decision,
            RetentionReviewDecision.PENDING_USER_APPROVAL,
        )
        self.assertEqual(result.patch.status, RosterPatchStatus.PROPOSED)
        self.assertEqual(result.patch.assessment_ids, (assessment.assessment_id,))
        self.assertFalse(result.applied)
        self.assertEqual(self.store.roster().revision, 3)
        RosterPatchService(self.store).approve(result.patch.patch_id, actor="user:test")
        applied = RosterPatchService(self.store).apply(
            result.patch.patch_id,
            actor="user:test",
        )
        self.assertEqual(applied.applied_revision, 4)
        employee = next(
            item
            for item in self.store.roster().employees
            if item["employee_id"] == contract.employee_id
        )
        self.assertFalse(employee["active"])

    def test_auto_review_applies_only_full_window_underuse(self) -> None:
        assert self.store is not None
        candidate, contract, _ = self._full_window_dormancy()
        company, changed = self.store.set_retention_review_mode(
            RetentionReviewMode.AUTO_REVIEW,
            actor="user:test",
        )

        result = RosterRetentionService(self.store).recommend(candidate.patch_id)

        self.assertTrue(changed)
        self.assertEqual(company.revision, 2)
        self.assertEqual(result.review.company_revision, 2)
        self.assertEqual(result.review.decision, RetentionReviewDecision.AUTO_APPROVED)
        self.assertTrue(result.applied)
        self.assertEqual(result.patch.status, RosterPatchStatus.APPLIED)
        self.assertEqual(self.store.roster().revision, 4)
        employee = next(
            item
            for item in self.store.roster().employees
            if item["employee_id"] == contract.employee_id
        )
        self.assertFalse(employee["active"])

    def test_auto_review_escalates_failure_or_safety_assessment(self) -> None:
        assert self.store is not None
        candidate, _, _ = self._safety_dormancy()
        self.store.set_retention_review_mode(
            RetentionReviewMode.AUTO_REVIEW,
            actor="user:test",
        )

        result = RosterRetentionService(self.store).recommend(candidate.patch_id)

        self.assertEqual(
            result.review.decision,
            RetentionReviewDecision.REQUIRES_USER_APPROVAL,
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.patch.status, RosterPatchStatus.PROPOSED)
        self.assertEqual(self.store.roster().revision, 3)

    def test_always_approve_applies_valid_safety_candidate_but_keeps_hard_gates(self) -> None:
        assert self.store is not None
        candidate, contract, _ = self._safety_dormancy()
        self.store.set_retention_review_mode(
            RetentionReviewMode.ALWAYS_APPROVE,
            actor="user:test",
        )

        result = RosterRetentionService(self.store).recommend(candidate.patch_id)

        self.assertEqual(
            result.review.decision,
            RetentionReviewDecision.APPROVAL_BYPASSED,
        )
        self.assertTrue(result.applied)
        self.assertEqual(self.store.roster().revision, 4)
        employee = next(
            item
            for item in self.store.roster().employees
            if item["employee_id"] == contract.employee_id
        )
        self.assertFalse(employee["active"])

    def test_new_observation_makes_open_proposal_stale(self) -> None:
        assert self.store is not None and self.runtime is not None
        candidate, contract, _ = self._safety_dormancy()
        result = RosterRetentionService(self.store).recommend(candidate.patch_id)
        _observe(
            self.store,
            self.runtime,
            patch_id=candidate.patch_id,
            employee_id=contract.employee_id,
            job_id="retention-new-observation",
        )

        with self.assertRaisesRegex(ValueError, "stale after observation"):
            RosterPatchService(self.store).approve(
                result.patch.patch_id,
                actor="user:test",
            )
        self.assertEqual(self.store.roster().revision, 3)

    def test_keep_and_insufficient_assessment_cannot_create_retention_patch(self) -> None:
        assert self.store is not None and self.runtime is not None
        candidate, contract = self._hire()
        _observe(
            self.store,
            self.runtime,
            patch_id=candidate.patch_id,
            employee_id=contract.employee_id,
            job_id="retention-insufficient",
        )
        HireObservationService(self.store).assess(candidate.patch_id)
        with self.assertRaisesRegex(ValueError, "DORMANCY_CANDIDATE"):
            RosterRetentionService(self.store).recommend(candidate.patch_id)

    def test_policy_is_versioned_idempotent_and_persists_across_restart(self) -> None:
        assert self.store is not None
        company, changed = self.store.set_retention_review_mode(
            RetentionReviewMode.ALWAYS_APPROVE,
            actor="user:test",
        )
        replay, replay_changed = self.store.set_retention_review_mode(
            RetentionReviewMode.ALWAYS_APPROVE,
            actor="user:test",
        )
        self.assertTrue(changed)
        self.assertFalse(replay_changed)
        self.assertEqual(company.revision, replay.revision)
        self.assertEqual(len(self.store.list_company_policy_events()), 1)

        self.runtime.close()
        self.runtime = None
        self.store.close()
        self.store = CompanyStateStore(self.path)
        self.assertEqual(
            self.store.retention_review_mode(),
            RetentionReviewMode.ALWAYS_APPROVE,
        )
        self.assertEqual(self.store.company().revision, 2)

    def test_schema_v7_migrates_to_safe_default_without_rewriting_company(self) -> None:
        assert self.store is not None and self.runtime is not None
        self.runtime.close()
        self.runtime = None
        before = self.store.company()
        self.store.close()
        self.store = None
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute(
                "UPDATE company_state_meta SET value = '7' WHERE key = 'schema_version'"
            )
            connection.execute("DROP TABLE roster_retention_reviews")
            connection.execute("DROP TABLE roster_patch_hire_assessments")
            connection.execute("DROP TABLE company_policy_events")
        connection.close()

        self.store = CompanyStateStore(self.path)
        self.assertEqual(self.store.schema_version(), 9)
        self.assertEqual(self.store.company().revision, before.revision)
        self.assertEqual(
            self.store.retention_review_mode(),
            RetentionReviewMode.APPROVAL,
        )
        self.assertEqual(self.store.list_retention_reviews(), ())

    def test_cli_exposes_policy_modes_and_one_command_auto_review(self) -> None:
        assert self.store is not None and self.runtime is not None
        candidate, _, _ = self._full_window_dormancy()
        self.runtime.close()
        self.runtime = None
        self.store.close()
        self.store = None

        denied = io.StringIO()
        self.assertEqual(
            main(
                [
                    "company",
                    "review-policy-set",
                    "auto-review",
                    "--state",
                    str(self.path),
                ],
                stderr=denied,
            ),
            EXIT_INPUT,
        )
        self.assertIn("requires --confirm", denied.getvalue())

        policy_output = io.StringIO()
        self.assertEqual(
            main(
                [
                    "company",
                    "review-policy-set",
                    "auto-review",
                    "--state",
                    str(self.path),
                    "--confirm",
                    "--json",
                ],
                stdout=policy_output,
            ),
            EXIT_OK,
        )
        self.assertEqual(json.loads(policy_output.getvalue())["mode"], "auto-review")

        retention_output = io.StringIO()
        self.assertEqual(
            main(
                [
                    "company",
                    "roster-retention-recommend",
                    candidate.patch_id,
                    "--state",
                    str(self.path),
                    "--json",
                ],
                stdout=retention_output,
            ),
            EXIT_OK,
        )
        payload = json.loads(retention_output.getvalue())
        self.assertEqual(payload["result"]["review"]["decision"], "AUTO_APPROVED")
        self.assertTrue(payload["result"]["applied"])
        self.assertFalse(payload["hard_invariants_bypassed"])

        reviews_output = io.StringIO()
        self.assertEqual(
            main(
                [
                    "company",
                    "retention-reviews",
                    "--state",
                    str(self.path),
                    "--json",
                ],
                stdout=reviews_output,
            ),
            EXIT_OK,
        )
        self.assertEqual(len(json.loads(reviews_output.getvalue())), 1)
