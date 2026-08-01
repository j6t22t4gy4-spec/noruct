from __future__ import annotations

import json
from collections import Counter
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from dynamic_firm.coding import APPLY_CHANGE_SET_TOOL
from dynamic_firm.kernel.models import JobResult, JobStatus, TaskStatus
from dynamic_firm.runtime.models import ApprovalDecision, EventType, RunEvent, to_primitive

from .models import (
    EvidenceSource,
    OrganizationEpisode,
    StaffingDemandEvidence,
    WorkflowTaskTemplate,
    content_digest,
)
from .organization_metrics import OrganizationOutcomeMetrics


def staffing_demands_from_runtime_ledger(
    result: JobResult,
    runs: Sequence[Mapping[str, object]],
    *,
    episode: OrganizationEpisode,
    base_roster_revision: int,
) -> tuple[StaffingDemandEvidence, ...]:
    """Project explicit temporary assignments without persisting employee ids."""

    if result.job_id != episode.job_id:
        raise ValueError("Staffing demand job and episode must match")
    if base_roster_revision < 1:
        raise ValueError("Staffing demand requires a positive ROSTER revision")
    tasks = {task.task_id: task for task in result.final_tasks}
    selected: dict[str, tuple[str, str]] = {}
    for row in runs:
        if str(row.get("job_id", "")) != result.job_id:
            raise ValueError("Staffing demand ledger contains a different job")
        raw_request = row.get("request_json")
        if not isinstance(raw_request, str):
            raise ValueError("Staffing demand ledger request must be immutable JSON")
        try:
            request = json.loads(raw_request)
        except json.JSONDecodeError as exc:
            raise ValueError("Staffing demand ledger request is malformed") from exc
        if not isinstance(request, Mapping):
            raise ValueError("Staffing demand ledger request must be an object")
        employee = request.get("employee")
        task_payload = request.get("task")
        if not isinstance(employee, Mapping) or not isinstance(task_payload, Mapping):
            raise ValueError("Staffing demand ledger lacks employee or task snapshot")
        if employee.get("temporary") is not True:
            continue
        employee_id = str(employee.get("employee_id", "")).strip()
        task_id = str(task_payload.get("task_id", "")).strip()
        task = tasks.get(task_id)
        if (
            not employee_id
            or task is None
            or task.assignee_id != employee_id
            or task.status != TaskStatus.SUCCEEDED
        ):
            continue
        raw_capabilities = task_payload.get("required_capabilities")
        if not isinstance(raw_capabilities, list) or len(raw_capabilities) != 1:
            continue
        capability = str(raw_capabilities[0]).strip().casefold()
        expected = tuple(item.strip().casefold() for item in task.required_capabilities)
        if not capability or expected != (capability,):
            continue
        role_label = str(employee.get("role", "")).strip()
        if not role_label:
            raise ValueError("Temporary staffing ledger role must be non-empty")
        current = selected.get(capability)
        if current is None or task_id < current[0]:
            selected[capability] = (task_id, role_label)

    return tuple(
        StaffingDemandEvidence.create(
            episode_id=episode.episode_id,
            job_id=episode.job_id,
            source=episode.source,
            context_fingerprint=episode.context_fingerprint,
            execution_profile=episode.execution_profile,
            base_roster_revision=base_roster_revision,
            task_id=task_id,
            capability=capability,
            role_label=role_label,
            job_succeeded=episode.success,
            validation_attempts=episode.validation_attempts,
            safety_violations=episode.safety_violations,
            writer_count=episode.writer_count,
            approvals_requested=episode.approvals_requested,
            approvals_granted=episode.approvals_granted,
            preapproval_mutations=episode.preapproval_mutations,
            ledger_digest=episode.ledger_digest,
            recorded_at=episode.recorded_at,
        )
        for capability, (task_id, role_label) in sorted(selected.items())
    )


def workflow_context_fingerprint(
    execution_profile: str,
    workspace_manifest: Sequence[str],
) -> str:
    """Build a bounded pre-Compiler signature from non-secret workspace structure."""

    extensions = Counter(
        PurePosixPath(path).suffix.lower() or "<none>" for path in workspace_manifest
    )
    marker_names = {
        "pyproject.toml",
        "package.json",
        "cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "requirements.txt",
    }
    markers = sorted(
        PurePosixPath(path).name.lower()
        for path in workspace_manifest
        if PurePosixPath(path).name.lower() in marker_names
    )
    signature = {
        "execution_profile": execution_profile,
        "extension_histogram": sorted(extensions.items()),
        "markers": markers,
        "file_count_bucket": min(20, len(workspace_manifest) // 10),
        "maximum_depth_bucket": min(
            10,
            max((len(PurePosixPath(path).parts) for path in workspace_manifest), default=0),
        ),
    }
    return content_digest(signature)[:24]


def episode_from_runtime_ledger(
    result: JobResult,
    events: Sequence[RunEvent],
    *,
    source: EvidenceSource = EvidenceSource.REAL_JOB,
    execution_profile: str,
    task_family: str | None = None,
    context_fingerprint: str | None = None,
    quality_score: float | None = None,
    baseline_quality_score: float | None = None,
    baseline_model_calls: int | None = None,
    manager_employee_id: str = "",
    manager_assignment_digest: str = "",
    manager_delegation_digest: str = "",
    manager_supervision_count: int = 0,
    outcome_metrics: OrganizationOutcomeMetrics | None = None,
) -> OrganizationEpisode:
    """Reduce an immutable run ledger to one bounded organization episode."""

    dependency_ids = {
        dependency
        for task in result.final_tasks
        for dependency in task.depends_on
    }
    final_candidates = {
        task.task_id for task in result.final_tasks if task.task_id not in dependency_ids
    }
    templates = tuple(
        WorkflowTaskTemplate(
            task_key=task.task_id,
            required_capabilities=tuple(sorted(task.required_capabilities)),
            depends_on=tuple(sorted(task.depends_on)),
            final=task.task_id in final_candidates and len(final_candidates) == 1,
        )
        for task in result.final_tasks
    )
    plan_digest = content_digest(templates)
    family = task_family or f"runtime.{execution_profile.lower()}.{plan_digest[:12]}"
    fingerprint = context_fingerprint or content_digest(
        {
            "execution_profile": execution_profile,
            "capability_sets": [task.required_capabilities for task in templates],
            "task_count": len(templates),
        }
    )[:24]

    approvals_requested = 0
    approvals_granted = 0
    approved_actions: set[str] = set()
    preapproval_mutations = 0
    writers: set[str] = set()
    validation_attempts: list[bool] = []
    ledger_projection: list[object] = []
    for event in events:
        ledger_projection.append(
            {
                "run_id": event.run_id,
                "seq": event.seq,
                "task_id": event.task_id,
                "employee_id": event.employee_id,
                "event_type": event.type.value,
                "payload": to_primitive(event.payload),
            }
        )
        if event.type == EventType.APPROVAL_REQUIRED:
            approvals_requested += 1
        elif event.type == EventType.APPROVAL_RESOLVED:
            if event.payload.get("decision") in {
                ApprovalDecision.ALLOW_ONCE.value,
                ApprovalDecision.ALLOW_SESSION.value,
            }:
                approvals_granted += 1
                approved_actions.add(str(event.payload.get("action_id", "")))
        elif (
            event.type == EventType.TOOL_STARTED
            and event.payload.get("tool_name") == APPLY_CHANGE_SET_TOOL
        ):
            writers.add(event.employee_id)
            if str(event.payload.get("action_id", "")) not in approved_actions:
                preapproval_mutations += 1
        elif event.type == EventType.VALIDATION_RECORDED:
            passed = event.payload.get("passed")
            if type(passed) is bool:
                validation_attempts.append(passed)

    safety_violations: list[str] = []
    if not validation_attempts:
        safety_violations.append("no_validation_evidence")
    if approvals_granted != approvals_requested:
        safety_violations.append("approval_mismatch")
    if preapproval_mutations:
        safety_violations.append("preapproval_mutation")
    if len(writers) > 1:
        safety_violations.append("multiple_writers")

    metrics = outcome_metrics or OrganizationOutcomeMetrics()
    proposal_counts = Counter(
        event.status.value for event in result.graph_patch_proposal_events
    )
    return OrganizationEpisode.create(
        job_id=result.job_id,
        source=source,
        task_family=family,
        context_fingerprint=fingerprint,
        execution_profile=execution_profile,
        planning_mode=result.planning_mode,
        manager_employee_id=manager_employee_id,
        manager_assignment_digest=manager_assignment_digest,
        manager_delegation_digest=manager_delegation_digest,
        manager_supervision_count=manager_supervision_count,
        plan_template=templates,
        success=result.status == JobStatus.SUCCEEDED,
        quality_score=(
            float(quality_score)
            if quality_score is not None
            else (1.0 if result.status == JobStatus.SUCCEEDED else 0.0)
        ),
        baseline_quality_score=baseline_quality_score,
        model_calls=result.metrics.usage.model_calls,
        baseline_model_calls=baseline_model_calls,
        employee_count=result.metrics.unique_employee_count,
        temporary_role_count=result.metrics.temporary_role_count,
        maximum_parallelism=result.metrics.maximum_parallelism,
        execution_replica_count=result.metrics.execution_replica_count,
        replica_group_count=result.metrics.replica_group_count,
        graph_patch_count=result.metrics.graph_patch_count,
        graph_proposal_approved_count=proposal_counts["APPROVED"],
        graph_proposal_rejected_count=proposal_counts["REJECTED"],
        graph_proposal_unavailable_count=proposal_counts["UNAVAILABLE"],
        writer_count=len(writers),
        approvals_requested=approvals_requested,
        approvals_granted=approvals_granted,
        preapproval_mutations=preapproval_mutations,
        validation_attempts=tuple(validation_attempts),
        safety_violations=tuple(safety_violations),
        ledger_digest=content_digest(ledger_projection),
        time_to_first_runnable_ms=metrics.time_to_first_runnable_ms,
        blueprint_outcome=metrics.blueprint_outcome,
        initial_final_graph_distance=metrics.initial_final_graph_distance,
        reserved_model_call_delta=metrics.reserved_model_call_delta,
        model_call_budget_variance=metrics.model_call_budget_variance,
        user_override_outcome=metrics.user_override_outcome,
        user_override_reason=metrics.user_override_reason,
        recovery_outcome=metrics.recovery_outcome,
    )
