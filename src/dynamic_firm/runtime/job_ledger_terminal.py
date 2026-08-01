"""Terminal ACTIVE JOB persistence stage separated from its writer facade."""

from __future__ import annotations

from dynamic_firm.kernel.models import JobResult
from .job_ledger_primitives import TERMINAL_SCHEMA, _terminal_graph_proposal_payload


def finish_job(ledger, job_id: str, result: JobResult) -> None:
    # A receipt-bound partial continuation contributes new records to an
        # existing immutable chain.  The store, rather than this one Kernel
        # invocation's in-memory slice, is the only authoritative aggregate.
        rows = ledger.store.get_job_ledger_rows(job_id)
        if rows is None:
            raise KeyError(f"ACTIVE JOB snapshot does not exist: {job_id}")
        payload = {
            "schema_version": TERMINAL_SCHEMA,
            "job_id": result.job_id,
            "request_id": result.request_id,
            "status": result.status.value,
            "final_graph_version": result.final_graph_version,
            "final_task_id": result.final_task_id,
            "task_attempt_count": len(rows["attempts"]),
            "task_mutation_count": len(rows["mutations"]),
            "graph_patch_count": len(rows["graph_patches"]),
            "graph_proposal_decision_count": len(
                rows["graph_proposals"]
            ),
            "graph_proposal_decisions": _terminal_graph_proposal_payload(result),
            "failure_reason": result.failure_reason[:256],
            "operating_decision": {
                "initial_company_work_mode": result.initial_company_work_mode,
                "company_work_mode": result.company_work_mode,
                "coordination_policy": result.coordination_policy,
                "requested_effect": result.requested_effect,
                "operating_reason": result.operating_reason,
            },
            "planning": {
                "planning_mode": result.planning_mode,
                "planning_reason": result.planning_reason,
                "compiler_usage": result.compiler_usage,
                "compiler_provider_request_id": (
                    result.compiler_provider_request_id
                ),
            },
            "work_order": {
                "work_order_id": result.work_order_id,
                "work_order_digest": result.work_order_digest,
                "work_order_authority_digest": (
                    result.work_order_authority_digest
                ),
                "firm_admission_digest": result.firm_admission_digest,
            },
            "graph_blueprint": {
                "blueprint_id": result.graph_blueprint_id,
                "blueprint_version": result.graph_blueprint_version,
                "blueprint_digest": result.graph_blueprint_digest,
                "mutation_policy": result.graph_mutation_policy,
                "constraints_digest": result.graph_constraints_digest,
                "constraints": {
                    "pinned_employee_ids": result.graph_pinned_employee_ids,
                    "excluded_employee_ids": result.graph_excluded_employee_ids,
                    "require_independent_review": result.graph_require_independent_review,
                    "max_concurrency": result.graph_max_concurrency,
                    "max_cost_usd": result.graph_max_cost_usd,
                    "max_wall_time_ms": result.graph_max_wall_time_ms,
                },
            },
            "tasks": tuple(
                {
                    "task_id": task.task_id,
                    "status": task.status.value,
                    "assignee_id": task.assignee_id,
                    "attempt": task.attempt,
                }
                for task in result.final_tasks
            ),
            "metrics": {
                "unique_employee_count": result.metrics.unique_employee_count,
                "temporary_role_count": result.metrics.temporary_role_count,
                "maximum_parallelism": result.metrics.maximum_parallelism,
                "graph_patch_count": result.metrics.graph_patch_count,
                "task_mutation_count": result.metrics.task_mutation_count,
                "organization_admission_count": (
                    result.metrics.organization_admission_count
                ),
                "execution_replica_count": result.metrics.execution_replica_count,
                "replica_group_count": result.metrics.replica_group_count,
                "usage": result.metrics.usage,
            },
        }
        ledger.store.append_job_terminal(job_id, payload)
        # The terminal audit remains the source of execution truth.  This
        # separate lifecycle projection makes terminalization idempotent and
        # leaves a bounded operator-visible state transition without exposing
        # request content, artifacts, or tool payloads.
        ledger.store.transition_job_lifecycle(
            job_id=job_id,
            operation="TERMINALIZE",
            reason=f"JOB_{result.status.value}",
        )
        ledger.store.settle_job_lifecycle_leases(
            job_id=job_id,
            reason=f"JOB_{result.status.value}",
        )
        ledger.store.finalize_local_resume_envelope(job_id)
