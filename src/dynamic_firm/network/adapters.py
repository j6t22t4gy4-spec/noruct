"""Registered, data-only adapters for Network template manifests.

Network releases are distribution data, never executable extensions.  This
module is the deliberately small allow-list between an immutable manifest and
an already-owned Noruct runtime contract.  Adding an adapter here is a product
and audit decision; a publisher cannot name a Python module, binary, URL, or
arbitrary evaluator in a manifest and have it executed locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from dynamic_firm.compiler.models import (
    CompilerExecutionProfile,
    WorkflowPrior,
    WorkflowPriorTask,
)


COMPILER_LINEAR_PLAYBOOK_ADAPTER = "compiler_linear_playbook_v1"
BLUEPRINT_DELTA_HOLDOUT_SUITE_ADAPTER = "blueprint_delta_holdout_suite_v1"
_PROFILE_BY_MANIFEST_VALUE = {
    "read_only": CompilerExecutionProfile.READ_ONLY,
    "host_action": CompilerExecutionProfile.HOST_ACTION,
    "host_direct": CompilerExecutionProfile.HOST_DIRECT,
    "shadow_coding": CompilerExecutionProfile.SHADOW_CODING,
}


@dataclass(frozen=True, slots=True)
class WorkflowPlaybookCandidate:
    artifact_id: str
    version: str
    scope_key: str
    workflow_shape: tuple[str, ...]
    reviewer_policy: str
    execution_profile: CompilerExecutionProfile
    required_capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NetworkEvaluationAdapter:
    adapter_reference: str
    benchmark_artifact_id: str
    benchmark_version: str
    evaluator_artifact_id: str
    evaluator_version: str
    fixture_ids: tuple[str, ...]


def workflow_candidate_from_manifest(
    manifest: Mapping[str, Any], *, scope_key: str
) -> WorkflowPlaybookCandidate | None:
    """Project the one registered playbook format, otherwise return ``None``.

    A legacy/generic playbook remains a valid catalog Artifact but deliberately
    has no execution effect.  The registered format describes a bounded
    linear capability chain only; it cannot inject objectives, tools,
    approval policy, code, prompts, or graph rewrites.
    """

    if manifest.get("kind") != "WORKFLOW_PLAYBOOK":
        return None
    content = manifest["content"]
    if content.get("adapter_reference") != COMPILER_LINEAR_PLAYBOOK_ADAPTER:
        return None
    profile = _PROFILE_BY_MANIFEST_VALUE.get(str(content.get("execution_profile", "")))
    if profile is None:
        return None
    return WorkflowPlaybookCandidate(
        artifact_id=str(manifest["artifact_id"]),
        version=str(manifest["version"]),
        scope_key=scope_key,
        workflow_shape=tuple(str(item) for item in content["workflow_shape"]),
        reviewer_policy=str(content["reviewer_policy"]),
        execution_profile=profile,
        required_capabilities=tuple(
            str(item)
            for item in (
                *manifest["compatibility"]["required_capabilities"],
                *content["required_capabilities"],
            )
        ),
    )


def workflow_prior_from_candidate(
    candidate: WorkflowPlaybookCandidate,
    *,
    execution_profile: CompilerExecutionProfile,
    available_capabilities: Sequence[str],
) -> tuple[WorkflowPrior | None, str]:
    """Return one advisory Compiler prior only when it is locally compatible."""

    if candidate.execution_profile is not execution_profile:
        return None, "IGNORED_WORKFLOW_ADAPTER_EXECUTION_PROFILE_MISMATCH"
    if len(candidate.workflow_shape) > 6:
        return None, "IGNORED_WORKFLOW_ADAPTER_TASK_LIMIT"
    available = frozenset(available_capabilities)
    required = frozenset((*candidate.required_capabilities, *candidate.workflow_shape))
    if not required <= available:
        return None, "IGNORED_WORKFLOW_ADAPTER_CAPABILITY_MISMATCH"
    tasks = tuple(
        WorkflowPriorTask(
            task_key=f"network_{candidate.artifact_id}_{index + 1}",
            required_capabilities=(capability,),
            depends_on=(
                ()
                if index == 0
                else (f"network_{candidate.artifact_id}_{index}",)
            ),
            final=index == len(candidate.workflow_shape) - 1,
        )
        for index, capability in enumerate(candidate.workflow_shape)
    )
    return (
        WorkflowPrior(
            pattern_id=f"network:{candidate.artifact_id}@{candidate.version}",
            task_family="network_registered_playbook",
            context_fingerprint=f"network:{candidate.artifact_id}@{candidate.version}",
            execution_profile=execution_profile,
            rationale=(
                "A signed Network playbook is advisory only; Compiler and Firm Kernel "
                "still validate the user goal, authority, budget, and graph."
            ),
            tasks=tasks,
            evidence_count=1,
        ),
        "PROJECTED_WORKFLOW_ADAPTER_COMPILER_PRIOR",
    )


def evaluation_adapter_from_manifests(
    benchmark: Mapping[str, Any], evaluator: Mapping[str, Any]
) -> NetworkEvaluationAdapter | None:
    """Recognize one fixed local offline evaluator pair.

    The fixed evaluator is intentionally the pre-existing public synthetic
    Blueprint Delta holdout suite.  It provides a real, deterministic local
    evaluation call while retaining the existing manual-review-only result;
    it is not a path to execute publisher-provided code or auto-promote an
    Employee/Tool/Workflow release.
    """

    if (
        benchmark.get("kind") != "BENCHMARK_SUITE"
        or evaluator.get("kind") != "EVALUATOR_PROFILE"
    ):
        return None
    benchmark_content = benchmark["content"]
    evaluator_content = evaluator["content"]
    if (
        benchmark_content.get("adapter_reference")
        != BLUEPRINT_DELTA_HOLDOUT_SUITE_ADAPTER
        or evaluator_content.get("adapter_reference")
        != BLUEPRINT_DELTA_HOLDOUT_SUITE_ADAPTER
        or benchmark_content.get("scorer") != "capability_route_delta"
        or evaluator_content.get("evaluator") != "capability_route_delta"
        or evaluator_content.get("threshold_profile") != "manual_review_only"
    ):
        return None
    return NetworkEvaluationAdapter(
        adapter_reference=BLUEPRINT_DELTA_HOLDOUT_SUITE_ADAPTER,
        benchmark_artifact_id=str(benchmark["artifact_id"]),
        benchmark_version=str(benchmark["version"]),
        evaluator_artifact_id=str(evaluator["artifact_id"]),
        evaluator_version=str(evaluator["version"]),
        fixture_ids=tuple(str(item) for item in benchmark_content["fixture_ids"]),
    )
