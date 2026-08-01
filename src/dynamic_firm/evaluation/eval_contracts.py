from __future__ import annotations

from dataclasses import dataclass

from dynamic_firm.company.models import content_digest
from dynamic_firm.kernel.models import JobLimits, JobResult
from dynamic_firm.runtime.models import RunLimits


EVALUATION_IDENTITY_SCHEMA = "noruct.evaluation-identity.v1"


@dataclass(frozen=True, slots=True)
class EvaluationBudgetContract:
    max_tasks: int
    max_concurrency: int
    max_graph_patches: int
    max_task_mutations: int
    max_temporary_roles: int
    max_total_model_calls: int
    max_total_tool_calls: int
    max_total_cost_usd: float
    max_wall_time_ms: int
    max_input_tokens_per_employee: int
    max_output_tokens_per_employee: int


@dataclass(frozen=True, slots=True)
class EvaluationIdentity:
    schema_version: str
    benchmark_revision: str
    case_id: str
    strategy: str
    fixture_revision: str
    model_profile: str
    authority_profile: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    memory_revision: str
    budget: EvaluationBudgetContract
    workload_hash: str
    run_id: str


@dataclass(frozen=True, slots=True)
class EvaluationAttemptProjection:
    sequence: int
    task_attempt: int
    task_id: str
    employee_id: str
    source_attempt_id: str | None
    graph_version: int
    status: str
    failure_kind: str
    failure_code: str
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class EvaluationTrajectoryProjection:
    final_graph_version: int
    final_task_id: str
    unique_employee_count: int
    maximum_parallelism: int
    graph_patch_count: int
    task_mutation_count: int
    organization_admission_count: int
    attempts: tuple[EvaluationAttemptProjection, ...]


def evaluation_budget_contract(
    job_limits: JobLimits,
    runtime_limits: RunLimits,
) -> EvaluationBudgetContract:
    return EvaluationBudgetContract(
        max_tasks=job_limits.max_tasks,
        max_concurrency=job_limits.max_concurrency,
        max_graph_patches=job_limits.max_graph_patches,
        max_task_mutations=job_limits.max_task_mutations,
        max_temporary_roles=job_limits.max_temporary_roles,
        max_total_model_calls=job_limits.max_total_model_calls,
        max_total_tool_calls=job_limits.max_total_tool_calls,
        max_total_cost_usd=job_limits.max_total_cost_usd,
        max_wall_time_ms=job_limits.max_wall_time_ms,
        max_input_tokens_per_employee=runtime_limits.max_input_tokens,
        max_output_tokens_per_employee=runtime_limits.max_output_tokens,
    )


def evaluation_identity(
    *,
    benchmark_revision: str,
    case_id: str,
    strategy: str,
    fixture_revision: str,
    model_profile: str,
    authority_profile: str,
    company_revision: int,
    roster_revision: int,
    playbook_revision: int,
    memory_revision: str,
    budget: EvaluationBudgetContract,
) -> EvaluationIdentity:
    if not all(
        value.strip()
        for value in (
            benchmark_revision,
            case_id,
            strategy,
            fixture_revision,
            model_profile,
            authority_profile,
            memory_revision,
        )
    ):
        raise ValueError("Evaluation identity fields must be non-empty")
    revisions = (company_revision, roster_revision, playbook_revision)
    if any(type(value) is not int or value < 0 for value in revisions):
        raise ValueError("Evaluation identity revisions must be non-negative integers")
    workload_payload = {
        "schema_version": EVALUATION_IDENTITY_SCHEMA,
        "benchmark_revision": benchmark_revision,
        "case_id": case_id,
        "fixture_revision": fixture_revision,
        "model_profile": model_profile,
        "authority_profile": authority_profile,
        "company_revision": company_revision,
        "roster_revision": roster_revision,
        "playbook_revision": playbook_revision,
        "memory_revision": memory_revision,
        "budget": budget,
    }
    workload_hash = content_digest(workload_payload)
    run_id = "evaluation-run-" + content_digest(
        {
            "workload_hash": workload_hash,
            "strategy": strategy,
        }
    )[:24]
    return EvaluationIdentity(
        schema_version=EVALUATION_IDENTITY_SCHEMA,
        benchmark_revision=benchmark_revision,
        case_id=case_id,
        strategy=strategy,
        fixture_revision=fixture_revision,
        model_profile=model_profile,
        authority_profile=authority_profile,
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        memory_revision=memory_revision,
        budget=budget,
        workload_hash=workload_hash,
        run_id=run_id,
    )


def project_job_trajectory(result: JobResult) -> EvaluationTrajectoryProjection:
    final_task_id = result.final_task_id
    if not final_task_id:
        final_task_id = next(
            (
                attempt.task_id
                for attempt in reversed(result.attempt_records)
                if attempt.status.value == "SUCCEEDED"
            ),
            "",
        )
    attempts = tuple(
        EvaluationAttemptProjection(
            sequence=sequence,
            task_attempt=attempt.sequence,
            task_id=attempt.task_id,
            employee_id=attempt.employee_id,
            source_attempt_id=attempt.source_attempt_id,
            graph_version=attempt.graph_version,
            status=attempt.status.value,
            failure_kind=attempt.failure_kind.value,
            failure_code=attempt.failure_code,
            model_calls=attempt.usage.model_calls,
            tool_calls=attempt.usage.tool_calls,
            input_tokens=attempt.usage.input_tokens,
            output_tokens=attempt.usage.output_tokens,
        )
        for sequence, attempt in enumerate(result.attempt_records, start=1)
    )
    return EvaluationTrajectoryProjection(
        final_graph_version=result.final_graph_version,
        final_task_id=final_task_id,
        unique_employee_count=result.metrics.unique_employee_count,
        maximum_parallelism=result.metrics.maximum_parallelism,
        graph_patch_count=result.metrics.graph_patch_count,
        task_mutation_count=result.metrics.task_mutation_count,
        organization_admission_count=result.metrics.organization_admission_count,
        attempts=attempts,
    )
