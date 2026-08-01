from __future__ import annotations

import unittest
from types import SimpleNamespace

from dynamic_firm.evaluation.execution_summary_acceptance import (
    EXECUTION_SUMMARY_ACCEPTANCE_SCHEMA,
    SummaryComprehensionExpectation,
    evaluate_execution_summary,
)
from dynamic_firm.product.execution_summary import execution_summary
from dynamic_firm.product.verification_truth import (
    VerificationEntry,
    project_verification_truth,
    verification_truth,
)


def _inspection(*, code: bool, validation=(), test_receipts=(), review_receipts=(), tool_receipts=(), job_status="SUCCEEDED"):
    return SimpleNamespace(
        job_id="summary-acceptance",
        audit_status=SimpleNamespace(value="TERMINAL"),
        job_status=job_status,
        replay_matches=True,
        company_work_mode="SOLO_JOB",
        planning_mode="SOLO",
        operating_reason="VALID_SOLO",
        planning_reason="COMPILER_ACCEPTED",
        requested_effect="READ",
        tool_receipts=tool_receipts,
        continuation_preflight_receipts=(),
        final_task_id="final",
        final_task_capabilities=("implementation",) if code else (),
        validation_receipts=validation,
        test_receipts=test_receipts,
        review_receipts=review_receipts,
        reconstructed_tasks=(
            {"task_id": "final", "assignee_id": "employee", "status": "SUCCEEDED"},
        ),
    )


class ExecutionSummaryAcceptanceTests(unittest.TestCase):
    def test_code_summary_identifies_purpose_ai_scope_review_and_test(self) -> None:
        summary = execution_summary(
            _inspection(
                code=True,
                validation=(
                    {"name": "pytest", "status": "PASSED", "task_id": "final", "employee_id": "employee"},
                ),
            ),
            work_order=SimpleNamespace(objective="Repair the parser", requested_outcome="A reviewed patch"),
        )
        record = evaluate_execution_summary(
            summary,
            SummaryComprehensionExpectation(
                purpose="Repair the parser",
                delivery_kind="CODE",
                responsibility_scope="IMPLEMENTATION_TASK",
                review_kind="CHANGESET_AND_VALIDATION",
                verification_name="pytest",
                verification_status="PASSED",
            ),
        )
        self.assertEqual(record.schema_version, EXECUTION_SUMMARY_ACCEPTANCE_SCHEMA)
        self.assertTrue(record.machine_passed)
        self.assertEqual(record.human_study_status, "NOT_RUN")
        self.assertEqual(record.review_wait_time_status, "NOT_RECORDED")
        self.assertEqual(record.rework_status, "NOT_RECORDED")
        self.assertEqual(record.approval_friction_status, "NOT_RECORDED")

    def test_missing_code_validation_is_identifiable_as_not_run(self) -> None:
        summary = execution_summary(
            _inspection(code=True),
            work_order=SimpleNamespace(objective="Repair the parser", requested_outcome="A reviewed patch"),
        )
        record = evaluate_execution_summary(
            summary,
            SummaryComprehensionExpectation(
                purpose="Repair the parser",
                delivery_kind="CODE",
                responsibility_scope="IMPLEMENTATION_TASK",
                review_kind="CHANGESET_AND_VALIDATION",
                verification_name="TEST_EXECUTION",
                verification_status="NOT_RUN",
            ),
        )
        self.assertTrue(record.machine_passed)

    def test_non_code_summary_uses_the_same_questionnaire_shape(self) -> None:
        summary = execution_summary(
            _inspection(code=False),
            work_order=SimpleNamespace(objective="Summarize the decision", requested_outcome="A reviewable report"),
        )
        record = evaluate_execution_summary(
            summary,
            SummaryComprehensionExpectation(
                purpose="Summarize the decision",
                delivery_kind="NON_CODE",
                responsibility_scope="TASK_RESULT",
                review_kind="EFFECT_OR_ARTIFACT_BOUNDARY",
                verification_name="ARTIFACT_OR_EFFECT_OUTCOME",
                verification_status="NOT_RUN",
            ),
        )
        self.assertTrue(record.machine_passed)

    def test_verification_truth_keeps_receipt_status_and_opaque_links(self) -> None:
        entries = project_verification_truth(
            test_receipts=(
                {"name": "pytest", "status": "PASSED", "evidence_id": "test-1"},
            ),
            validator_receipts=(
                {"name": "schema-validator", "status": "FAILED", "receipt_id": "validator-1"},
            ),
            review_receipts=(
                {"name": "human-review", "status": "PARTIAL", "id": "review-1"},
            ),
        )
        self.assertEqual(
            entries[:3],
            (
                VerificationEntry("pytest", "PASSED", ("test-1",)),
                VerificationEntry("schema-validator", "FAILED", ("validator-1",)),
                VerificationEntry("human-review", "PARTIAL", ("review-1",)),
            ),
        )
        with self.assertRaises(AttributeError):
            entries[0].status = "FAILED"

    def test_missing_test_and_validator_are_not_run_and_effect_start_is_unknown(self) -> None:
        entries = verification_truth(
            _inspection(
                code=False,
                tool_receipts=(
                    {"effect": "EXECUTE", "status": "STARTED", "evidence_id": "effect-1"},
                ),
            ),
        )
        self.assertEqual(entries[0], VerificationEntry("TEST_EXECUTION", "NOT_RUN", ()))
        self.assertEqual(entries[1], VerificationEntry("VALIDATOR_EXECUTION", "NOT_RUN", ()))
        self.assertEqual(entries[2], VerificationEntry("REVIEW", "NOT_RUN", ()))
        self.assertEqual(entries[3], VerificationEntry("EXTERNAL_EFFECT_RECEIPTS", "UNKNOWN", ("effect-1",)))

    def test_terminal_job_success_does_not_change_verification_truth(self) -> None:
        receipts = (
            {"name": "pytest", "status": "FAILED", "evidence_id": "test-2"},
        )
        succeeded = verification_truth(_inspection(code=True, validation=receipts, job_status="SUCCEEDED"))
        failed = verification_truth(_inspection(code=True, validation=receipts, job_status="FAILED"))
        self.assertEqual(succeeded, failed)
        self.assertEqual(succeeded[1].status, "FAILED")


if __name__ == "__main__":
    unittest.main()
