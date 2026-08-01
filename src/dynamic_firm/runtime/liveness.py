"""First-party Employee completion-liveness contract."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from dynamic_firm._vendor.paperclip_runtime.liveness import classify_run_liveness

from .models import EmployeeRunResult, Failure, FailureCategory, RunStatus


class EmployeeCompletionLivenessState(StrEnum):
    COMPLETED = "COMPLETED"
    ADVANCED = "ADVANCED"
    PLAN_ONLY = "PLAN_ONLY"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    BLOCKED = "BLOCKED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class EmployeeCompletionAssessment:
    state: EmployeeCompletionLivenessState
    reason: str
    actionability: str


LIVENESS_CONTINUATION_INSTRUCTION = (
    "The prior attempt stopped at a plan without concrete progress. "
    "Take the first safe concrete action now. If blocked by approval, "
    "credentials, external state, or user input, report that blocker "
    "explicitly instead of claiming completion."
)


def assess_employee_completion(
    *,
    objective: str,
    result: EmployeeRunResult,
) -> EmployeeCompletionAssessment:
    private = classify_run_liveness(
        run_status=result.status.value,
        objective=objective,
        summary=result.summary,
        concrete_action_count=result.usage.tool_calls + len(result.output_artifact_refs),
    )
    return EmployeeCompletionAssessment(
        state=EmployeeCompletionLivenessState(private.state.upper()),
        reason=private.reason,
        actionability=private.actionability,
    )


def enforce_employee_completion_liveness(
    *,
    objective: str,
    result: EmployeeRunResult,
) -> tuple[EmployeeRunResult, EmployeeCompletionAssessment]:
    assessment = assess_employee_completion(objective=objective, result=result)
    if assessment.state in {
        EmployeeCompletionLivenessState.BLOCKED,
        EmployeeCompletionLivenessState.NEEDS_REVIEW,
    }:
        approval_required = assessment.actionability == "approval_required"
        validated = replace(
            result,
            status=RunStatus.FAILED,
            summary=(
                "Employee execution requires explicit approval or external input."
                if assessment.state == EmployeeCompletionLivenessState.BLOCKED
                else "Employee execution requires explicit human review."
            ),
            acceptance_evidence=(),
            unresolved_issues=(assessment.reason,),
            partial_result=False,
            failure=Failure(
                code=(
                    "EMPLOYEE_APPROVAL_REQUIRED"
                    if approval_required
                    else "EMPLOYEE_EXTERNAL_BLOCKER"
                    if assessment.state == EmployeeCompletionLivenessState.BLOCKED
                    else "EMPLOYEE_HUMAN_REVIEW_REQUIRED"
                ),
                category=(
                    FailureCategory.POLICY
                    if approval_required
                    or assessment.state == EmployeeCompletionLivenessState.NEEDS_REVIEW
                    else FailureCategory.INPUT
                ),
                message_safe=assessment.reason,
                retryable=False,
            ),
        )
        return validated, assessment
    if assessment.state not in {
        EmployeeCompletionLivenessState.PLAN_ONLY,
        EmployeeCompletionLivenessState.EMPTY_RESPONSE,
    }:
        return result, assessment
    validated = replace(
        result,
        status=RunStatus.FAILED,
        summary="Employee execution ended without concrete progress.",
        acceptance_evidence=(),
        unresolved_issues=(assessment.reason,),
        partial_result=False,
        failure=Failure(
            code="EMPLOYEE_NO_CONCRETE_PROGRESS",
            category=FailureCategory.VALIDATION,
            message_safe=assessment.reason,
            retryable=True,
        ),
    )
    return validated, assessment
