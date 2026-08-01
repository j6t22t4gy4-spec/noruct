"""Read-only Company operator-state assembly for the Modern terminal.

The terminal controller owns session orchestration; this component only joins
the existing ACTIVE JOB audit and Company-wide attention projection.  It never
changes a Job, budget, approval, or Company record.
"""

from __future__ import annotations

from pathlib import Path

from dynamic_firm.runtime.company_budget import CompanyCostBudgetPolicy
from dynamic_firm.runtime.job_ledger import ActiveJobInspection, ActiveJobInspector
from dynamic_firm.runtime.operator_attention import (
    CompanyAttention,
    CompanyAttentionInspector,
)
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.product.cross_plane_attention import (
    SupplementalOperatorAttention,
    inspect_supplemental_operator_attention,
)


def inspect_operator_state(
    state_path: Path,
    *,
    budget_policy: CompanyCostBudgetPolicy,
) -> tuple[ActiveJobInspection | None, CompanyAttention, SupplementalOperatorAttention]:
    """Read the latest Job plus bounded Company attention from one store."""

    audit_store = RunStore(state_path)
    try:
        inspector = ActiveJobInspector(audit_store)
        latest_jobs = inspector.list(1)
        latest_inspection = (
            inspector.inspect(latest_jobs[0].job_id) if latest_jobs else None
        )
        attention = CompanyAttentionInspector(audit_store, budget_policy).inspect(
            job_limit=20
        )
    finally:
        audit_store.close()
    supplemental_attention = inspect_supplemental_operator_attention(state_path)
    return latest_inspection, attention, supplemental_attention


def latest_interrupted_job_id(state_path: Path) -> str | None:
    """Return one bounded interrupted Job identifier for terminal startup guidance."""

    audit_store = RunStore(state_path)
    try:
        interrupted = next(
            (
                item
                for item in ActiveJobInspector(audit_store).list(20)
                if item.audit_status.value == "INTERRUPTED"
            ),
            None,
        )
    finally:
        audit_store.close()
    return None if interrupted is None else interrupted.job_id
