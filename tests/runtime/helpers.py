from __future__ import annotations

from dataclasses import replace

from dynamic_firm.runtime.models import (
    ActionPolicy,
    CompletionEnvelope,
    ContextBundle,
    EmployeeRunRequest,
    EmployeeSnapshot,
    RunLimits,
    TaskEnvelope,
    ToolEffect,
    ToolGrant,
    VersionedContent,
)


def completion(summary: str = "Task complete") -> CompletionEnvelope:
    return CompletionEnvelope(
        summary=summary,
        acceptance_evidence=("fixture evidence",),
        observations=("candidate reusable observation",),
    )


def make_request(
    *,
    request_id: str = "request-1",
    tool_names: tuple[str, ...] = (),
    resource_patterns: tuple[str, ...] = ("*",),
    limits: RunLimits | None = None,
    workspace_id: str | None = None,
) -> EmployeeRunRequest:
    grants = tuple(
        ToolGrant(
            tool_name=name,
            allowed_effects=(ToolEffect.READ,),
            resource_patterns=resource_patterns,
            max_calls=8,
        )
        for name in tool_names
    )
    return EmployeeRunRequest(
        request_id=request_id,
        employee=EmployeeSnapshot(
            employee_id="employee-researcher",
            role="Repository Analyst",
            capabilities=("repository_analysis",),
            skills=(
                VersionedContent(
                    "employee-skill:employee-researcher:read-evidence:fixture",
                    "1",
                    "Read only the evidence needed.",
                ),
            ),
            memory_namespace="employee:researcher",
            selected_memory_refs=("employee-memory:employee-researcher:fact-1",),
        ),
        task=TaskEnvelope(
            job_id="job-1",
            job_graph_version=1,
            task_id="task-1",
            attempt=1,
            objective="Inspect the fixture and return evidence.",
            required_capabilities=("repository_analysis",),
            acceptance_criteria=("Cite relevant fixture evidence.",),
        ),
        context=ContextBundle(
            company_policy_excerpt="Do not mutate external state.",
            selected_memory=(
                VersionedContent(
                    "employee-memory:employee-researcher:fact-1",
                    "3",
                    "Prefer the smallest evidence set.",
                ),
            ),
            workspace_id=workspace_id,
        ),
        limits=limits or RunLimits(),
        action_policy=ActionPolicy(tool_grants=grants),
    )


def with_limits(request: EmployeeRunRequest, **changes) -> EmployeeRunRequest:
    return replace(request, limits=replace(request.limits, **changes))
