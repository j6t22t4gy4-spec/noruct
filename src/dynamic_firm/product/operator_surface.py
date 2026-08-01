"""Surface-neutral, privacy-bounded Company operator projection.

The terminal and a future GUI must not each reconstruct a different story from
Company and ACTIVE JOB tables.  This module owns the read-only projection used
by product surfaces.  It deliberately carries operational facts rather than
prompts, tool arguments, employee output, Knowledge content, or hidden model
reasoning.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from dynamic_firm.company.manager_report import ManagerOperatingReport
from dynamic_firm.product.cross_plane_attention import SupplementalOperatorAttention
from dynamic_firm.runtime.operator_attention import CompanyAttention
from dynamic_firm.runtime.job_ledger import ActiveJobInspection


OPERATOR_SURFACE_SCHEMA = "noruct.operator-surface.v1"


@dataclass(frozen=True, slots=True)
class OperatorSurfaceSnapshot:
    """A bounded explanation of the latest Company operating state.

    The snapshot is presentation data only.  It cannot resume a Job, grant an
    approval, change a budget, or mutate a graph.  ``decision`` means an
    observable lifecycle decision, never model chain-of-thought.
    """

    schema: str
    manager: Mapping[str, object]
    execution: Mapping[str, object]
    assignments: tuple[Mapping[str, object], ...]
    hold: Mapping[str, object]
    approval: Mapping[str, object]
    budget: Mapping[str, object]
    attention: Mapping[str, object]
    next_action: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def lines(self) -> tuple[str, ...]:
        """Return short stable terminal lines without leaking opaque payloads."""

        manager = self.manager
        execution = self.execution
        hold = self.hold
        approval = self.approval
        budget = self.budget
        attention = self.attention
        lines = [
            "Manager  " + str(manager.get("status", "not configured")),
            "Graph    " + str(execution.get("decision", "no active job")),
            "Proposals "
            + str(execution.get("graph_proposal_status", "none recorded")),
            "Hold     " + str(hold.get("reason", "none")),
            "Approval " + str(approval.get("status", "none pending")),
            "Budget   " + str(budget.get("summary", "applied at next Work Order admission")),
            "Attention " + str(attention.get("summary", "not scanned")),
        ]
        if self.assignments:
            rendered = ", ".join(
                f"{item.get('employee_id', 'employee')}:{item.get('status', 'pending')}"
                for item in self.assignments[:4]
            )
            lines.append("Working  " + rendered)
        return tuple(lines)


def assessment_projection(
    snapshot: Mapping[str, object],
    *,
    current_objective: str,
) -> tuple[str, str, str, str]:
    """Return the four explainable facts suitable for a live operator surface.

    This is deliberately *not* a reasoning trace.  ``observation`` comes from
    recorded lifecycle evidence, ``decision`` comes from the graph admission
    or current Job projection, and ``next_action`` is an explicit operator
    action.  Keeping it here means the terminal and a future GUI cannot drift
    into separate, more revealing interpretations of the same Company state.
    """

    execution = snapshot.get("execution", {})
    manager = snapshot.get("manager", {})
    hold = snapshot.get("hold", {})
    if not all(isinstance(value, Mapping) for value in (execution, manager, hold)):
        return (
            current_objective or "No active objective.",
            "No trusted Company observation is available.",
            "No active execution decision.",
            "Refresh the Company surface before taking action.",
        )
    evidence = str(manager.get("evidence", "")).strip()
    hold_reason = str(hold.get("reason", "none")).strip()
    observation = evidence or "No new Manager outcome evidence is recorded."
    if hold_reason and hold_reason != "none":
        observation = f"{observation} · Hold: {hold_reason}."
    return (
        current_objective or "No active objective.",
        observation,
        str(execution.get("decision", "No active execution decision.")).strip(),
        str(snapshot.get("next_action", "Inspect Company state before taking action.")).strip(),
    )


def build_operator_surface_snapshot(
    *,
    manager_report: ManagerOperatingReport | None,
    inspection: ActiveJobInspection | None,
    attention: CompanyAttention | None = None,
    supplemental_attention: SupplementalOperatorAttention | None = None,
) -> OperatorSurfaceSnapshot:
    """Build one safe projection from existing read-only Company evidence."""

    manager = _manager_projection(manager_report)
    if inspection is None:
        return OperatorSurfaceSnapshot(
            schema=OPERATOR_SURFACE_SCHEMA,
            manager=manager,
            execution={
                "status": "IDLE",
                "decision": "No active Job; the next Work Order will choose DIRECT, SOLO, or TEAM.",
                "work_mode": "",
                "planning_reason": "",
            },
            assignments=(),
            hold={"status": "CLEAR", "reason": "none"},
            approval={"pending_count": 0, "status": "none pending"},
            budget={"status": "NEXT_JOB", "summary": "applied at next Work Order admission"},
            attention=_attention_projection(attention, supplemental_attention),
            next_action=_attention_next_action(attention, supplemental_attention) or "Submit a goal, inspect Graph controls, or open Settings.",
        )

    assignments = _assignments(inspection)
    pending_approval_count = sum(item.pending_approval_count for item in inspection.runtime_runs)
    hold = _hold_projection(inspection, pending_approval_count)
    limits = inspection.job_limits
    max_calls = limits.get("max_total_model_calls", "?")
    max_tools = limits.get("max_total_tool_calls", "?")
    max_cost = limits.get("max_total_cost_usd", "?")
    compiler_usage = inspection.compiler_usage
    budget_summary = (
        f"calls {compiler_usage.model_calls}/{max_calls} · "
        f"tools ceiling {max_tools} · cost ceiling ${max_cost}"
    )
    execution_status = inspection.job_status or inspection.audit_status.value
    decision = (
        f"{inspection.company_work_mode} · {inspection.coordination_policy} · "
        f"{inspection.planning_reason}"
    )
    graph_proposals = tuple(
        item
        for item in getattr(inspection, "graph_proposal_decisions", ())
        if isinstance(item, Mapping)
    )
    latest_graph_proposal_status = (
        str(graph_proposals[-1].get("status", "unknown"))
        if graph_proposals
        else "none recorded"
    )
    return OperatorSurfaceSnapshot(
        schema=OPERATOR_SURFACE_SCHEMA,
        manager=manager,
        execution={
            "status": execution_status,
            "decision": decision,
            "work_mode": inspection.company_work_mode,
            "planning_reason": inspection.planning_reason,
            "requested_effect": inspection.requested_effect,
            "graph_patch_count": inspection.graph_patch_count,
            "graph_proposal_count": len(graph_proposals),
            "graph_proposal_status": latest_graph_proposal_status,
            "audit_status": inspection.audit_status.value,
        },
        assignments=assignments,
        hold=hold,
        approval={
            "pending_count": pending_approval_count,
            "status": "operator decision required" if pending_approval_count else "none pending",
            "latest_graph_proposal_status": latest_graph_proposal_status,
        },
        budget={
            "status": "FROZEN_JOB_LIMITS",
            "summary": budget_summary,
            "max_model_calls": max_calls,
            "max_tool_calls": max_tools,
            "max_cost_usd": max_cost,
            "compiler_model_calls": compiler_usage.model_calls,
            "compiler_tool_calls": compiler_usage.tool_calls,
            "compiler_cost_usd": compiler_usage.cost_usd,
        },
        attention=_attention_projection(attention, supplemental_attention),
        next_action=_attention_next_action(attention, supplemental_attention)
        or _next_action(inspection, pending_approval_count),
    )


def _attention_projection(
    attention: CompanyAttention | None,
    supplemental_attention: SupplementalOperatorAttention | None,
) -> Mapping[str, object]:
    """Preserve one existing Company-wide attention view without new authority."""

    supplemental_count = (
        len(supplemental_attention.items) if supplemental_attention is not None else 0
    )
    supplemental_payload: Mapping[str, object] = (
        {
            "knowledge_pending_candidate_count": supplemental_attention.knowledge_pending_candidate_count,
            "artifact_review_count": supplemental_attention.artifact_review_count,
            "knowledge_state": supplemental_attention.knowledge_state,
            "evolution_state": supplemental_attention.evolution_state,
            "truncated": supplemental_attention.truncated,
        }
        if supplemental_attention is not None
        else {}
    )
    if attention is None:
        if supplemental_count:
            assert supplemental_attention is not None
            first = supplemental_attention.items[0]
            return {
                "status": "ACTION_REQUIRED",
                "item_count": supplemental_count,
                "summary": f"{supplemental_count} cross-plane item(s) require operator review",
                "next_action": first.recommended_action,
                "kinds": tuple(
                    item.kind.value for item in supplemental_attention.items[:4]
                ),
                "supplemental": supplemental_payload,
            }
        return {
            "status": "NOT_SCANNED",
            "item_count": 0,
            "summary": "not scanned",
            "next_action": "",
            "supplemental": supplemental_payload,
        }
    items = tuple(attention.items)
    if not items and not supplemental_count:
        return {
            "status": "CLEAR",
            "item_count": 0,
            "summary": "none pending",
            "next_action": "",
            "jobs_truncated": attention.jobs_truncated,
            "supplemental": supplemental_payload,
        }
    assert items or supplemental_attention is not None
    first = items[0] if items else supplemental_attention.items[0]
    total = len(items) + supplemental_count
    return {
        "status": "ACTION_REQUIRED",
        "item_count": total,
        "summary": f"{total} item(s) require operator review",
        "next_action": first.recommended_action,
        "kinds": tuple(
            item.kind.value
            for item in (*items, *(supplemental_attention.items if supplemental_attention is not None else ()))[:4]
        ),
        "jobs_truncated": attention.jobs_truncated,
        "supplemental": supplemental_payload,
    }


def _attention_next_action(
    attention: CompanyAttention | None,
    supplemental_attention: SupplementalOperatorAttention | None,
) -> str:
    projection = _attention_projection(attention, supplemental_attention)
    next_action = projection.get("next_action", "")
    return str(next_action) if next_action else ""


def _manager_projection(report: ManagerOperatingReport | None) -> Mapping[str, object]:
    if report is None:
        return {"status": "not configured", "employee_id": "", "evidence": ""}
    skill_heads = tuple(getattr(report, "skill_heads", ()))
    status = (
        f"{report.manager_employee_id} · supervised {report.supervised_job_count}"
    )
    return {
        "status": status,
        "employee_id": report.manager_employee_id,
        "roster_revision": report.roster_revision,
        "model_profile": report.model_profile,
        "supervised_job_count": report.supervised_job_count,
        "specialist_job_count": report.specialist_job_count,
        "replanned_job_count": report.replanned_job_count,
        "skill_head_count": len(skill_heads),
        "skill_heads": tuple(
            {
                "skill_key": item.skill_key,
                "context_key": item.context_key,
                "revision": item.revision,
                "source_patch_id": item.source_patch_id,
            }
            for item in skill_heads[:8]
        ),
        "evidence": report.pending_reason or "bounded outcome evidence available",
    }


def _assignments(inspection: ActiveJobInspection) -> tuple[Mapping[str, object], ...]:
    assignments: list[Mapping[str, object]] = []
    for task in inspection.reconstructed_tasks:
        employee_id = task.get("assignee_id")
        if not isinstance(employee_id, str) or not employee_id:
            continue
        assignments.append(
            {
                "task_id": str(task.get("task_id", "")),
                "employee_id": employee_id,
                "status": str(task.get("status", "PENDING")),
                "attempt": int(task.get("attempt", 1) or 1),
            }
        )
    return tuple(assignments[:8])


def _hold_projection(
    inspection: ActiveJobInspection,
    pending_approval_count: int,
) -> Mapping[str, object]:
    if pending_approval_count:
        return {"status": "HELD", "reason": "approval pending"}
    if inspection.audit_status.value == "INTERRUPTED":
        return {"status": "HELD", "reason": "interrupted Job requires operator inspection"}
    if inspection.audit_status.value == "INVALID":
        return {"status": "HELD", "reason": "ledger validation requires operator inspection"}
    if inspection.errors:
        return {"status": "HELD", "reason": "bounded audit errors recorded"}
    return {"status": "CLEAR", "reason": "none"}


def _next_action(inspection: ActiveJobInspection, pending_approval_count: int) -> str:
    if pending_approval_count:
        return "Resolve the pending approval; the protected action remains unexecuted."
    if inspection.audit_status.value == "INTERRUPTED":
        return f"Inspect {inspection.job_id} before choosing recovery or cancellation."
    if inspection.audit_status.value == "INVALID":
        return f"Inspect {inspection.job_id}; its audit projection is not trusted for resume."
    if inspection.job_status:
        return "Review the terminal result or start a new Work Order."
    return "Wait for the next runnable task; no additional authority is granted."
