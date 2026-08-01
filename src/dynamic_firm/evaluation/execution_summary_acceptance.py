"""Provider-free identifiability checks for the bounded execution summary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


EXECUTION_SUMMARY_ACCEPTANCE_SCHEMA = "noruct.execution-summary-acceptance.v1"


@dataclass(frozen=True, slots=True)
class SummaryComprehensionExpectation:
    """Facts an operator must find from the summary alone, not from logs."""

    purpose: str
    delivery_kind: str
    responsibility_scope: str
    review_kind: str
    verification_name: str
    verification_status: str


@dataclass(frozen=True, slots=True)
class SummaryComprehensionCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ExecutionSummaryAcceptanceRecord:
    schema_version: str
    machine_passed: bool
    human_study_status: str
    review_wait_time_status: str
    rework_status: str
    approval_friction_status: str
    checks: tuple[SummaryComprehensionCheck, ...]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first(items: object) -> Mapping[str, Any]:
    if not isinstance(items, (list, tuple)) or not items:
        return {}
    return _mapping(items[0])


def evaluate_execution_summary(
    summary: Mapping[str, Any],
    expectation: SummaryComprehensionExpectation,
) -> ExecutionSummaryAcceptanceRecord:
    """Verify a deterministic comprehension questionnaire against one summary.

    This is a surface-identifiability contract, not evidence that humans have
    read the report or that review/rework/approval time improved. Those values
    remain explicitly unrecorded until a consented operator study supplies
    durable measurements.
    """

    result = _mapping(summary.get("result"))
    delivery = _mapping(summary.get("delivery"))
    responsibility = _mapping(delivery.get("ai_responsibility"))
    review = _first(delivery.get("review_focus"))
    verification = _first(delivery.get("verification"))
    checks = (
        SummaryComprehensionCheck(
            "purpose-identifiable",
            result.get("requested_purpose") == expectation.purpose,
            "summary.result.requested_purpose",
        ),
        SummaryComprehensionCheck(
            "delivery-kind-identifiable",
            delivery.get("kind") == expectation.delivery_kind,
            "summary.delivery.kind",
        ),
        SummaryComprehensionCheck(
            "ai-responsibility-identifiable",
            responsibility.get("scope") == expectation.responsibility_scope,
            "summary.delivery.ai_responsibility.scope",
        ),
        SummaryComprehensionCheck(
            "review-focus-identifiable",
            review.get("kind") == expectation.review_kind,
            "summary.delivery.review_focus[0].kind",
        ),
        SummaryComprehensionCheck(
            "verification-identifiable",
            verification.get("name") == expectation.verification_name
            and verification.get("status") == expectation.verification_status,
            "summary.delivery.verification[0]",
        ),
    )
    return ExecutionSummaryAcceptanceRecord(
        schema_version=EXECUTION_SUMMARY_ACCEPTANCE_SCHEMA,
        machine_passed=all(check.passed for check in checks),
        human_study_status="NOT_RUN",
        review_wait_time_status="NOT_RECORDED",
        rework_status="NOT_RECORDED",
        approval_friction_status="NOT_RECORDED",
        checks=checks,
    )
