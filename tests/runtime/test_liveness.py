from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from dynamic_firm.runtime.liveness import (
    EmployeeCompletionLivenessState,
    assess_employee_completion,
    enforce_employee_completion_liveness,
)
from dynamic_firm.runtime.models import EmployeeRunResult, RunStatus, Usage


def _result(
    summary: str,
    *,
    status: RunStatus = RunStatus.SUCCEEDED,
    tool_calls: int = 0,
    artifacts: tuple[str, ...] = (),
) -> EmployeeRunResult:
    return EmployeeRunResult(
        run_id="run-1",
        request_id="request-1",
        job_id="job-1",
        task_id="task-1",
        employee_id="employee-1",
        status=status,
        summary=summary,
        output_artifact_refs=artifacts,
        acceptance_evidence=("model-authored claim",),
        unresolved_issues=(),
        observations=(),
        suggested_followups=(),
        signals=(),
        partial_result=False,
        usage=Usage(model_calls=1, tool_calls=tool_calls),
        last_event_seq=1,
        started_at=None,
        finished_at=datetime.now(UTC),
    )


class EmployeeCompletionLivenessTests(unittest.TestCase):
    def test_useful_terminal_answer_is_completed(self) -> None:
        assessment = assess_employee_completion(
            objective="Explain the repository architecture",
            result=_result("The runtime has three bounded execution layers."),
        )

        self.assertEqual(assessment.state, EmployeeCompletionLivenessState.COMPLETED)

    def test_english_and_korean_future_action_are_plan_only(self) -> None:
        cases = (
            "I will inspect the repository and then implement the fix.",
            "먼저 저장소를 살펴보겠습니다.",
        )
        for summary in cases:
            with self.subTest(summary=summary):
                assessment = assess_employee_completion(
                    objective="Fix the runtime defect",
                    result=_result(summary),
                )
                self.assertEqual(
                    assessment.state,
                    EmployeeCompletionLivenessState.PLAN_ONLY,
                )

    def test_runtime_observed_tool_or_artifact_is_concrete_progress(self) -> None:
        for candidate in (
            _result("I will inspect the repository.", tool_calls=1),
            _result("I will inspect the repository.", artifacts=("artifact:patch",)),
        ):
            with self.subTest(candidate=candidate):
                assessment = assess_employee_completion(
                    objective="Fix the runtime defect",
                    result=candidate,
                )
                self.assertEqual(
                    assessment.state,
                    EmployeeCompletionLivenessState.ADVANCED,
                )

    def test_planning_deliverable_is_not_retried_for_being_a_plan(self) -> None:
        assessment = assess_employee_completion(
            objective="Draft a plan for the runtime migration",
            result=_result("Next steps: inspect, port, validate."),
        )

        self.assertEqual(assessment.state, EmployeeCompletionLivenessState.ADVANCED)

    def test_approval_or_external_blocker_is_not_automatic_continuation(self) -> None:
        validated, assessment = enforce_employee_completion_liveness(
            objective="Deploy the runtime",
            result=_result("I cannot proceed because I need user approval and credentials."),
        )

        self.assertEqual(assessment.state, EmployeeCompletionLivenessState.BLOCKED)
        self.assertEqual(assessment.actionability, "approval_required")
        self.assertEqual(validated.status, RunStatus.FAILED)
        assert validated.failure is not None
        self.assertEqual(validated.failure.code, "EMPLOYEE_APPROVAL_REQUIRED")
        self.assertFalse(validated.failure.retryable)

    def test_production_future_action_requires_review(self) -> None:
        validated, assessment = enforce_employee_completion_liveness(
            objective="Prepare the runtime release",
            result=_result("I will deploy to production next."),
        )

        self.assertEqual(assessment.state, EmployeeCompletionLivenessState.NEEDS_REVIEW)
        self.assertEqual(validated.status, RunStatus.FAILED)
        assert validated.failure is not None
        self.assertEqual(validated.failure.code, "EMPLOYEE_HUMAN_REVIEW_REQUIRED")
        self.assertFalse(validated.failure.retryable)

    def test_empty_success_becomes_typed_retryable_validation_failure(self) -> None:
        original = _result("")
        validated, assessment = enforce_employee_completion_liveness(
            objective="Inspect the repository",
            result=original,
        )

        self.assertEqual(assessment.state, EmployeeCompletionLivenessState.EMPTY_RESPONSE)
        self.assertEqual(validated.status, RunStatus.FAILED)
        self.assertEqual(validated.acceptance_evidence, ())
        self.assertIsNotNone(validated.failure)
        assert validated.failure is not None
        self.assertEqual(validated.failure.code, "EMPLOYEE_NO_CONCRETE_PROGRESS")
        self.assertTrue(validated.failure.retryable)

    def test_failed_result_remains_failed_without_reclassification(self) -> None:
        failed = replace(_result("runtime failed"), status=RunStatus.FAILED)
        validated, assessment = enforce_employee_completion_liveness(
            objective="Inspect the repository",
            result=failed,
        )

        self.assertIs(validated, failed)
        self.assertEqual(assessment.state, EmployeeCompletionLivenessState.FAILED)


if __name__ == "__main__":
    unittest.main()
