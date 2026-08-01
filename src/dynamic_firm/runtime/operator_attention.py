"""Read-only Company operator attention projection.

This module joins existing, first-party runtime facts without creating a new
incident state machine.  It is intentionally a projection: ACTIVE JOB audit,
approval decisions, and Company budget incidents retain their own authorities.

The shape follows the waiting-path principle observed in Paperclip's
``issue-graph-liveness`` classifier.  A pending approval attached to an
interrupted or invalid Company job is not emitted as a second, independently
actionable item: only the owning job recovery guidance is safe in that case.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .company_budget import CompanyCostBudgetPolicy
from .job_ledger import ActiveJobAuditStatus, ActiveJobInspector
from .store import RunStore


DEFAULT_ATTENTION_JOB_LIMIT = 100
MAX_ATTENTION_JOB_LIMIT = 500


class CompanyAttentionKind(StrEnum):
    BUDGET_INCIDENT = "BUDGET_INCIDENT"
    INVALID_JOB_AUDIT = "INVALID_JOB_AUDIT"
    INTERRUPTED_JOB = "INTERRUPTED_JOB"
    EFFECT_RECOVERY = "EFFECT_RECOVERY"
    PENDING_APPROVAL = "PENDING_APPROVAL"


@dataclass(frozen=True, slots=True)
class CompanyAttentionItem:
    """One privacy-bounded operator item; never a command or a resume token."""

    kind: CompanyAttentionKind
    subject_id: str
    job_id: str | None
    run_id: str | None
    task_id: str | None
    employee_id: str | None
    state: str
    created_at: str
    recommended_action: str
    automatic_action: bool = False


@dataclass(frozen=True, slots=True)
class CompanyAttention:
    """Bounded, deterministic read model for the local Company operator."""

    job_scan_limit: int
    scanned_job_count: int
    jobs_truncated: bool
    open_budget_incident_count: int
    invalid_job_count: int
    interrupted_job_count: int
    blocking_effect_recovery_count: int
    pending_approval_count: int
    suppressed_pending_approval_count: int
    items: tuple[CompanyAttentionItem, ...]
    state_changed: bool = False
    automatic_resolution: bool = False


def normalize_attention_job_limit(value: object) -> int:
    """Accept a small, explicit inspection bound without hiding its cap."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Attention job limit must be an integer")
    if value < 1 or value > MAX_ATTENTION_JOB_LIMIT:
        raise ValueError(
            f"Attention job limit must be between 1 and {MAX_ATTENTION_JOB_LIMIT}"
        )
    return value


class CompanyAttentionInspector:
    """Aggregate existing local runtime attention without mutating any owner."""

    def __init__(self, store: RunStore, policy: CompanyCostBudgetPolicy) -> None:
        if not isinstance(policy, CompanyCostBudgetPolicy):
            raise ValueError("Company attention requires a validated budget policy")
        self.store = store
        self.policy = policy

    def inspect(self, *, job_limit: object = DEFAULT_ATTENTION_JOB_LIMIT) -> CompanyAttention:
        limit = normalize_attention_job_limit(job_limit)
        # Read one extra immutable snapshot so the projection can disclose that
        # it is partial instead of silently claiming an exhaustive company view.
        rows = self.store.list_job_snapshot_rows(limit + 1)
        jobs_truncated = len(rows) > limit
        summaries = ActiveJobInspector(self.store).list(limit)
        items: list[CompanyAttentionItem] = []

        budget = self.store.company_budget_status(self.policy)
        incident = budget.get("incident")
        if budget.get("paused") and incident is not None:
            incident_id = str(getattr(incident, "incident_id", ""))
            if incident_id:
                items.append(
                    CompanyAttentionItem(
                        kind=CompanyAttentionKind.BUDGET_INCIDENT,
                        subject_id=incident_id,
                        job_id=None,
                        run_id=None,
                        task_id=None,
                        employee_id=None,
                        state="PAUSED",
                        created_at=str(getattr(incident, "created_at", "")),
                        recommended_action=(
                            "Inspect company budget-status, then use explicit "
                            "company budget-resolve --confirm only after the policy covers "
                            "observed and reserved cost."
                        ),
                    )
                )

        blocked_job_ids: set[str] = set()
        invalid_job_count = 0
        interrupted_job_count = 0
        blocking_effect_recovery_count = 0
        for summary in summaries:
            if summary.audit_status == ActiveJobAuditStatus.INVALID:
                invalid_job_count += 1
                blocked_job_ids.add(summary.job_id)
                items.append(
                    CompanyAttentionItem(
                        kind=CompanyAttentionKind.INVALID_JOB_AUDIT,
                        subject_id=summary.job_id,
                        job_id=summary.job_id,
                        run_id=None,
                        task_id=None,
                        employee_id=None,
                        state="AUDIT_INVALID_MANUAL_INVESTIGATION",
                        created_at=summary.created_at,
                        recommended_action=(
                            "Inspect the ACTIVE JOB audit and investigate manually. "
                            "Do not resume, replay, or resolve its approvals from this view."
                        ),
                    )
                )
            elif summary.audit_status == ActiveJobAuditStatus.INTERRUPTED:
                interrupted_job_count += 1
                blocked_job_ids.add(summary.job_id)
                blocking_cases = tuple(
                    case
                    for case in self.store.list_job_effect_recovery_cases(summary.job_id)
                    if not bool(case.get("resource_released"))
                )
                if blocking_cases:
                    blocking_effect_recovery_count += len(blocking_cases)
                    for case in blocking_cases:
                        state = (
                            "EFFECT_OUTCOME_UNKNOWN"
                            if case.get("case_status") == "OPEN"
                            else "EFFECT_OUTCOME_SEALED_UNKNOWN"
                        )
                        items.append(
                            CompanyAttentionItem(
                                kind=CompanyAttentionKind.EFFECT_RECOVERY,
                                subject_id=str(case["action_id"]),
                                job_id=summary.job_id,
                                run_id=str(case["run_id"]),
                                task_id=None,
                                employee_id=None,
                                state=state,
                                created_at=str(case["detected_at"]),
                                recommended_action=(
                                    "Inspect the Job effect recovery case. Append trustworthy "
                                    "evidence, compensation, or a permanent unknown seal with "
                                    "job effect-resolve; never replay the old external action."
                                    if case.get("case_status") == "OPEN"
                                    else "Keep the sealed-unknown external effect blocked and "
                                    "investigate manually. Do not retry, release, or infer its "
                                    "outcome from this view."
                                ),
                            )
                        )
                else:
                    items.append(
                        CompanyAttentionItem(
                            kind=CompanyAttentionKind.INTERRUPTED_JOB,
                            subject_id=summary.job_id,
                            job_id=summary.job_id,
                            run_id=None,
                            task_id=None,
                            employee_id=None,
                            state="INTERRUPTED_NEW_KERNEL_ATTEMPT_REQUIRED",
                            created_at=summary.created_at,
                            recommended_action=(
                                "Inspect job recovery and start a new Kernel-owned Company job if "
                                "the goal should continue. Do not resume the old graph or resolve "
                                "its approvals independently."
                            ),
                        )
                    )

        approvals = self.store.list_pending_approvals()
        suppressed_pending_approval_count = 0
        for approval in approvals:
            request = approval.request
            if request.job_id in blocked_job_ids:
                suppressed_pending_approval_count += 1
                continue
            items.append(
                CompanyAttentionItem(
                    kind=CompanyAttentionKind.PENDING_APPROVAL,
                    subject_id=request.action_id,
                    job_id=request.job_id or None,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    employee_id=request.employee_id,
                    state="WAITING_APPROVAL",
                    created_at=approval.created_at.isoformat(),
                    recommended_action=(
                        "Review this action in its owning interactive session. This view does "
                        "not approve, reject, or resume work."
                    ),
                )
            )

        return CompanyAttention(
            job_scan_limit=limit,
            scanned_job_count=len(summaries),
            jobs_truncated=jobs_truncated,
            open_budget_incident_count=1 if budget.get("paused") and incident is not None else 0,
            invalid_job_count=invalid_job_count,
            interrupted_job_count=interrupted_job_count,
            blocking_effect_recovery_count=blocking_effect_recovery_count,
            pending_approval_count=len(approvals),
            suppressed_pending_approval_count=suppressed_pending_approval_count,
            items=tuple(items),
        )
