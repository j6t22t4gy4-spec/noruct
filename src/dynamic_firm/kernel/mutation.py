from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Mapping

from dynamic_firm.runtime.models import (
    ActionPolicy,
    EmployeeRunResult,
    FailureCategory,
    RunStatus,
    SignalCode,
    ToolEffect,
    Usage,
    to_primitive,
)

from .models import (
    AttemptBudgetEvidence,
    AttemptFailureKind,
    CompanyRunRequest,
    EmployeeRecord,
    GraphPatch,
    GraphPatchEvent,
    GraphPatchExpectedImpact,
    GraphPatchProposalEvent,
    GraphPatchProposalStatus,
    GraphPatchValidationReceipt,
    GraphMutationLease,
    JobGraph,
    JobMutationEvent,
    JobTask,
    PatchOperationKind,
    SemanticOperation,
    TaskStatus,
    TaskAttemptRecord,
    TaskMutationType,
)


def graph_patch_from_primitive(value: Mapping[str, Any]) -> GraphPatch:
    """Rebuild a persisted proposal candidate without accepting runtime state.

    The durable proposal is a data-only candidate.  Resumption reconstructs
    only pending task topology; it never imports a previous assignee, result,
    or arbitrary execution-replica object from SQLite.
    """

    operations: list[Any] = []
    raw_operations = value.get("operations")
    if not isinstance(raw_operations, list):
        raise ValueError("Persisted Graph proposal operations are malformed")
    from .models import GraphPatchOperation  # avoid widening module import surface
    for raw in raw_operations:
        if not isinstance(raw, Mapping):
            raise ValueError("Persisted Graph proposal operation is malformed")
        raw_task = raw.get("task")
        task = None
        if raw_task is not None:
            if not isinstance(raw_task, Mapping) or raw_task.get("execution_replica") is not None:
                raise ValueError("Persisted Graph proposal task is unsupported")
            if raw_task.get("runtime_result") is not None or raw_task.get("assignee_id") is not None:
                raise ValueError("Persisted Graph proposal task carries runtime state")
            task = JobTask(
                task_id=str(raw_task.get("task_id", "")),
                objective=str(raw_task.get("objective", "")),
                depends_on=tuple(str(item) for item in raw_task.get("depends_on", ())),
                required_capabilities=tuple(str(item) for item in raw_task.get("required_capabilities", ())),
                acceptance_criteria=tuple(str(item) for item in raw_task.get("acceptance_criteria", ())),
                risk_level=str(raw_task.get("risk_level", "LOW")),
                status=TaskStatus(str(raw_task.get("status", "PENDING"))),
                attempt=int(raw_task.get("attempt", 1)),
            )
        operations.append(GraphPatchOperation(
            kind=PatchOperationKind(str(raw.get("kind", ""))), task=task,
            task_id=str(raw.get("task_id", "")), dependency_id=str(raw.get("dependency_id", "")),
            dependencies=tuple(str(item) for item in raw.get("dependencies", ())),
        ))
    raw_refs = value.get("semantic_evidence_refs", ())
    if not isinstance(raw_refs, (list, tuple)):
        raise ValueError("Persisted Graph proposal semantic evidence is malformed")
    return GraphPatch(
        patch_id=str(value.get("patch_id", "")),
        base_graph_version=int(value.get("base_graph_version", 0)),
        trigger_task_id=str(value.get("trigger_task_id", "")),
        semantic_operation=SemanticOperation(str(value.get("semantic_operation", ""))),
        rationale=str(value.get("rationale", "")),
        expected_gain=str(value.get("expected_gain", "")),
        operations=tuple(operations),
        semantic_evidence_refs=tuple(str(item) for item in raw_refs),
    )


RECOVERABLE_FAILURE_KINDS = frozenset(
    {
        AttemptFailureKind.RECOVERABLE_MODEL,
        AttemptFailureKind.RECOVERABLE_TOOL,
        AttemptFailureKind.RECOVERABLE_TIMEOUT,
        AttemptFailureKind.RECOVERABLE_LIVENESS,
    }
)


def content_digest(value: object) -> str:
    encoded = json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def frozen_snapshot_digest(request: CompanyRunRequest) -> str:
    payload = {
            "company_revision": request.company_revision,
            "roster_revision": request.roster_revision,
            "playbook_revision": request.playbook_revision,
            "roster": request.roster,
            "employee_skill_snapshots": request.employee_skill_snapshots,
            "job_local_skill_snapshots": request.job_local_skill_snapshots,
            "context_snapshot": request.context_snapshot,
            "action_policy": request.action_policy,
            "workflow_context_fingerprint": request.workflow_context_fingerprint,
            "workspace_identity_revision": request.workspace_identity_revision,
            "workspace_identity_status": request.workspace_identity_status,
            "workspace_identity_failure_code": request.workspace_identity_failure_code,
            "execution_origin": request.execution_origin,
            "manager_employee_id": request.manager_employee_id,
            "manager_assignment_digest": request.manager_assignment_digest,
            "manager_session_key": request.manager_session_key,
            "manager_employee": request.manager_employee,
            "manager_delegation_payload": request.manager_delegation_payload,
            "manager_delegation_digest": request.manager_delegation_digest,
            "planning_mode": request.planning_mode,
            "planning_reason": request.planning_reason,
            "compiler_usage": request.compiler_usage,
            "compiler_provider_request_id": request.compiler_provider_request_id,
            "work_order_id": request.work_order_id,
            "work_order_digest": request.work_order_digest,
            "work_order_authority_digest": request.work_order_authority_digest,
            "company_work_mode": request.company_work_mode,
            "coordination_policy": request.coordination_policy,
            "requested_effect": request.requested_effect,
            "operating_reason": request.operating_reason,
            "graph_blueprint_id": request.graph_blueprint_id,
            "graph_blueprint_version": request.graph_blueprint_version,
            "graph_blueprint_digest": request.graph_blueprint_digest,
            "graph_mutation_policy": request.graph_mutation_policy,
            "graph_constraints_digest": request.graph_constraints_digest,
            "graph_pinned_employee_ids": request.graph_pinned_employee_ids,
            "graph_excluded_employee_ids": request.graph_excluded_employee_ids,
            "graph_require_independent_review": request.graph_require_independent_review,
            "graph_max_concurrency": request.graph_max_concurrency,
            "graph_max_cost_usd": request.graph_max_cost_usd,
            "graph_max_wall_time_ms": request.graph_max_wall_time_ms,
        }
    # Preserve replay identity for historical/non-product requests that were
    # created before FirmAdmission existed. Production Company Front Door
    # requests always carry the digest and therefore bind it into the frozen
    # snapshot.
    if request.firm_admission_digest:
        payload["firm_admission_digest"] = request.firm_admission_digest
    # Runtime rebinding was added after the original frozen-snapshot schema.
    # Bind every present value for new Jobs while preserving the historical
    # digest of envelopes that predate all three fields; those envelopes are
    # rejected by continuation preflight rather than silently upgraded.
    for key, value in (
        ("runtime_provider_binding_digest", request.runtime_provider_binding_digest),
        ("runtime_tool_contract_digest", request.runtime_tool_contract_digest),
        (
            "runtime_company_coordination_digest",
            request.runtime_company_coordination_digest,
        ),
    ):
        if value:
            payload[key] = value
    return content_digest(payload)


def attempt_identity(
    *,
    request: CompanyRunRequest,
    task: JobTask,
    employee_id: str,
    graph_version: int,
    frozen_snapshot_hash: str,
) -> str:
    digest = content_digest(
        {
            "job_id": request.job_id,
            "task_id": task.task_id,
            "attempt": task.attempt,
            "employee_id": employee_id,
            "graph_version": graph_version,
            "frozen_snapshot_hash": frozen_snapshot_hash,
        }
    )
    return f"attempt-{digest[:24]}"


def classify_attempt_failure(result: EmployeeRunResult) -> AttemptFailureKind:
    if result.status == RunStatus.SUCCEEDED:
        return AttemptFailureKind.NONE
    if result.status == RunStatus.BUDGET_EXHAUSTED:
        return AttemptFailureKind.BUDGET_EXHAUSTED
    if result.status in {RunStatus.CANCELLED, RunStatus.CANCELLING}:
        return AttemptFailureKind.CANCELLED

    failure = result.failure
    if failure is None:
        return AttemptFailureKind.UNKNOWN

    code = failure.code.upper()
    if code == "EMPLOYEE_NO_CONCRETE_PROGRESS" and failure.retryable:
        return AttemptFailureKind.RECOVERABLE_LIVENESS
    if "SAFETY" in code:
        return AttemptFailureKind.SAFETY_VIOLATION
    if "APPROVAL" in code and any(token in code for token in ("DENIED", "REJECTED")):
        return AttemptFailureKind.APPROVAL_REJECTED
    if failure.category == FailureCategory.POLICY:
        return AttemptFailureKind.POLICY_DENIED
    if failure.category == FailureCategory.CANCEL:
        return AttemptFailureKind.CANCELLED
    if failure.category == FailureCategory.INTERNAL:
        return AttemptFailureKind.INTERNAL_ERROR

    mismatch = any(
        signal.code == SignalCode.ASSIGNEE_MISMATCH for signal in result.signals
    )
    if mismatch:
        return AttemptFailureKind.ASSIGNEE_MISMATCH
    if failure.category == FailureCategory.INPUT:
        return AttemptFailureKind.INPUT_INVALID
    if failure.retryable and failure.category == FailureCategory.MODEL:
        return AttemptFailureKind.RECOVERABLE_MODEL
    if failure.retryable and failure.category == FailureCategory.TOOL:
        return AttemptFailureKind.RECOVERABLE_TOOL
    if failure.retryable and failure.category == FailureCategory.TIMEOUT:
        return AttemptFailureKind.RECOVERABLE_TIMEOUT
    return AttemptFailureKind.NON_RETRYABLE


def structurally_read_only(policy: ActionPolicy) -> bool:
    return (
        policy.filesystem_policy == "READ_ONLY"
        and policy.network_policy == "DENY"
        and policy.sandbox_profile == "none"
        and not policy.approval_grants
        and all(
            set(grant.allowed_effects).issubset({ToolEffect.READ})
            and not grant.requires_approval
            for grant in policy.tool_grants
        )
    )


def structurally_replica_safe(policy: ActionPolicy) -> bool:
    """Allow only local reads and the existing allowlisted external-read lane."""

    if (
        policy.filesystem_policy != "READ_ONLY"
        or policy.network_policy not in {"DENY", "EXTERNAL_READ_ONLY"}
        or policy.sandbox_profile != "none"
        or policy.approval_grants
    ):
        return False
    for grant in policy.tool_grants:
        if grant.requires_approval:
            return False
        effects = set(grant.allowed_effects)
        if effects.issubset({ToolEffect.READ}):
            continue
        if (
            effects == {ToolEffect.NETWORK}
            and grant.resource_patterns
            and all(
                resource.startswith("external-read:")
                for resource in grant.resource_patterns
            )
        ):
            continue
        return False
    return True


def reroute_candidate(
    task: JobTask,
    roster: tuple[EmployeeRecord, ...],
    *,
    current_employee_id: str,
    attempted_employee_ids: set[str],
    pinned_employee_ids: set[str] | None = None,
    excluded_employee_ids: set[str] | None = None,
) -> EmployeeRecord | None:
    pinned = pinned_employee_ids or set()
    excluded = excluded_employee_ids or set()
    required = set(task.required_capabilities)
    eligible = [
        employee
        for employee in roster
        if employee.active
        and not employee.temporary
        and employee.employee_id != current_employee_id
        and employee.employee_id not in attempted_employee_ids
        and employee.employee_id not in excluded
        and required.issubset(employee.capabilities)
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            0 if item.employee_id in pinned else 1,
            len(item.capabilities),
            item.employee_id,
        ),
    )


def attempt_record(
    *,
    attempt_id: str,
    request: CompanyRunRequest,
    task: JobTask,
    employee: EmployeeRecord,
    source_attempt_id: str | None,
    graph_version: int,
    result: EmployeeRunResult,
    frozen_snapshot_hash: str,
    capability_profile_digest: str,
    capability_material_digest: str,
) -> TaskAttemptRecord:
    kind = classify_attempt_failure(result)
    failure_code = result.failure.code if result.failure is not None else ""
    failure_detail = (
        result.failure.message_safe[:512] if result.failure is not None else ""
    )
    capability_evidence = tuple(
        f"{capability}:{'matched' if capability in employee.capabilities else 'missing'}"
        for capability in task.required_capabilities
    )
    record = TaskAttemptRecord(
        attempt_id=attempt_id,
        task_id=task.task_id,
        sequence=task.attempt,
        employee_id=employee.employee_id,
        source_attempt_id=source_attempt_id,
        graph_version=graph_version,
        status=result.status,
        failure_kind=kind,
        failure_code=failure_code,
        failure_detail=failure_detail,
        company_revision=request.company_revision,
        roster_revision=request.roster_revision,
        playbook_revision=request.playbook_revision,
        frozen_snapshot_hash=frozen_snapshot_hash,
        capability_evidence=capability_evidence,
        capability_profile_digest=capability_profile_digest,
        capability_material_digest=capability_material_digest,
        usage=result.usage,
        content_hash="",
        execution_instance_id=(
            f"{request.job_id}:{task.task_id}:attempt-{task.attempt}"
            if task.execution_replica is None
            else (
                f"{request.job_id}:{task.execution_replica.group_id}:"
                f"{task.execution_replica.replica_id}:attempt-{task.attempt}"
            )
        ),
        replica_group_id=(
            "" if task.execution_replica is None else task.execution_replica.group_id
        ),
    )
    return replace(record, content_hash=content_digest(record))


def mutation_event(
    *,
    sequence: int,
    mutation_type: TaskMutationType,
    task: JobTask,
    source_attempt_id: str,
    source_attempt_content_hash: str,
    target_attempt_id: str,
    from_employee_id: str,
    to_employee_id: str,
    failure_kind: AttemptFailureKind,
    downstream_task_ids: tuple[str, ...],
    mutation_budget_before: int,
    reservation: AttemptBudgetEvidence,
    frozen_snapshot_hash: str,
) -> JobMutationEvent:
    if failure_kind == AttemptFailureKind.RECOVERABLE_LIVENESS:
        rationale = (
            "Run ended at a plan without concrete progress; retrying the same "
            "frozen employee once with a safe execution instruction."
        )
    else:
        rationale = {
            TaskMutationType.RETRY: "Typed recoverable read-only failure; retrying the same frozen employee once.",
            TaskMutationType.REROUTE: "Typed assignee mismatch; rerouting once to another frozen exact-capable employee.",
        }[mutation_type]
    event = JobMutationEvent(
        event_id="",
        sequence=sequence,
        mutation_type=mutation_type,
        task_id=task.task_id,
        source_attempt_id=source_attempt_id,
        source_attempt_content_hash=source_attempt_content_hash,
        target_attempt_id=target_attempt_id,
        source_attempt_sequence=task.attempt,
        target_attempt_sequence=task.attempt + 1,
        from_employee_id=from_employee_id,
        to_employee_id=to_employee_id,
        failure_kind=failure_kind,
        rationale=rationale,
        matched_capabilities=tuple(sorted(task.required_capabilities)),
        downstream_task_ids=downstream_task_ids,
        mutation_budget_before=mutation_budget_before,
        mutation_budget_after=mutation_budget_before - 1,
        next_attempt_reservation=reservation,
        frozen_snapshot_hash=frozen_snapshot_hash,
        content_hash="",
    )
    identity_digest = content_digest(event)
    identified = replace(event, event_id=f"mutation-{identity_digest[:24]}")
    return replace(identified, content_hash=content_digest(identified))


def graph_structure_digest(graph: JobGraph) -> str:
    """Digest graph structure without retaining Employee result content in audit data."""

    return content_digest(
        {
            "version": graph.version,
            "final_task_id": graph.final_task_id,
            "tasks": tuple(
                {
                    "task_id": task.task_id,
                    "objective": task.objective,
                    "depends_on": task.depends_on,
                    "required_capabilities": task.required_capabilities,
                    "acceptance_criteria": task.acceptance_criteria,
                    "risk_level": task.risk_level,
                    "status": task.status,
                    "assignee_id": task.assignee_id,
                    "attempt": task.attempt,
                    "execution_replica": task.execution_replica,
                }
                for task in graph.tasks
            ),
        }
    )


def graph_patch_event(
    *,
    sequence: int,
    patch: GraphPatch,
    before: JobGraph,
    after: JobGraph,
    mutation_lease: GraphMutationLease | None = None,
) -> GraphPatchEvent:
    """Bind an already validated graph patch to exact before/after structures."""

    before_ids = {task.task_id for task in before.tasks}
    added_task_ids = tuple(sorted(task.task_id for task in after.tasks if task.task_id not in before_ids))
    cancelled_task_ids = tuple(
        sorted(
            task.task_id
            for task in after.tasks
            if task.task_id in before_ids
            and task.status.value == "CANCELLED"
            and next(item for item in before.tasks if item.task_id == task.task_id).status.value != "CANCELLED"
        )
    )
    expected_impact = {
        "INSERT": GraphPatchExpectedImpact.CAPABILITY_COVERAGE,
        "SPLIT": GraphPatchExpectedImpact.WORK_PARTITIONING,
        "JOIN": GraphPatchExpectedImpact.RESULT_INTEGRATION,
        "MERGE": GraphPatchExpectedImpact.TOPOLOGY_CONSOLIDATION,
        "CANCEL": GraphPatchExpectedImpact.WORK_REMOVAL,
    }[patch.semantic_operation.value]
    event = GraphPatchEvent(
        event_id="",
        sequence=sequence,
        patch=patch,
        target_graph_version=after.version,
        before_graph_digest=graph_structure_digest(before),
        after_graph_digest=graph_structure_digest(after),
        added_task_ids=added_task_ids,
        cancelled_task_ids=cancelled_task_ids,
        mutation_lease=mutation_lease or GraphMutationLease(),
        expected_impact=expected_impact,
        validation_receipt=(
            GraphPatchValidationReceipt.KERNEL_GRAPH_AND_LEASE_VALIDATED
        ),
        content_hash="",
    )
    identified = replace(event, event_id=f"graph-patch-{content_digest(event)[:24]}")
    return replace(identified, content_hash=content_digest(identified))


def graph_patch_proposal_event(
    *,
    patch: GraphPatch,
    before: JobGraph,
    after: JobGraph,
    proposed_lease: GraphMutationLease,
    status: GraphPatchProposalStatus,
) -> GraphPatchProposalEvent:
    """Bind a PROPOSE decision to exact candidate structure and lease.

    A rejected or unavailable proposal is intentionally retained in the
    terminal result for audit, but does not reserve capacity or mutate the
    JobGraph.  An approved candidate is followed by the ordinary append-only
    ``GraphPatchEvent`` before it becomes executable.
    """

    proposal_identity = {
        "patch": patch,
        "before_graph_digest": graph_structure_digest(before),
        "after_graph_digest": graph_structure_digest(after),
        "proposed_lease": proposed_lease,
    }
    proposal_id = f"graph-proposal-{content_digest(proposal_identity)[:24]}"
    unsigned = {
        **proposal_identity,
        "status": status.value,
    }
    event_id = f"graph-proposal-event-{content_digest(unsigned)[:24]}"
    event = GraphPatchProposalEvent(
        proposal_id=proposal_id,
        event_id=event_id,
        patch=patch,
        before_graph_digest=graph_structure_digest(before),
        after_graph_digest=graph_structure_digest(after),
        proposed_lease=proposed_lease,
        status=status,
        content_hash="",
    )
    return replace(event, content_hash=content_digest(event))


def graph_patch_proposal_event_from_primitive(
    value: Mapping[str, Any],
) -> GraphPatchProposalEvent:
    """Restore one persisted Graph proposal without trusting SQLite payloads.

    A durable external decision must bind to the *exact* pending proposal, not
    merely a proposal id supplied by a terminal surface.  This reader
    reconstructs the closed candidate shape and re-derives every stable
    identity before handing it to the continuation path.
    """

    raw_patch = value.get("patch")
    raw_lease = value.get("proposed_lease")
    if not isinstance(raw_patch, Mapping) or not isinstance(raw_lease, Mapping):
        raise ValueError("Persisted Graph proposal is malformed")
    patch = graph_patch_from_primitive(raw_patch)
    try:
        lease = GraphMutationLease(
            model_calls=int(raw_lease["model_calls"]),
            tool_calls=int(raw_lease["tool_calls"]),
            cost_usd=float(raw_lease["cost_usd"]),
        )
        status = GraphPatchProposalStatus(str(value["status"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Persisted Graph proposal identity is malformed") from exc
    before_digest = str(value.get("before_graph_digest", ""))
    after_digest = str(value.get("after_graph_digest", ""))
    if any(
        len(item) != 64 or any(character not in "0123456789abcdef" for character in item)
        for item in (before_digest, after_digest)
    ):
        raise ValueError("Persisted Graph proposal digest is malformed")
    proposal_identity = {
        "patch": patch,
        "before_graph_digest": before_digest,
        "after_graph_digest": after_digest,
        "proposed_lease": lease,
    }
    expected_proposal_id = f"graph-proposal-{content_digest(proposal_identity)[:24]}"
    unsigned = {**proposal_identity, "status": status.value}
    expected_event_id = f"graph-proposal-event-{content_digest(unsigned)[:24]}"
    candidate = GraphPatchProposalEvent(
        proposal_id=str(value.get("proposal_id", "")),
        event_id=str(value.get("event_id", "")),
        patch=patch,
        before_graph_digest=before_digest,
        after_graph_digest=after_digest,
        proposed_lease=lease,
        status=status,
        content_hash=str(value.get("content_hash", "")),
    )
    unsigned_candidate = replace(candidate, content_hash="")
    if (
        candidate.proposal_id != expected_proposal_id
        or candidate.event_id != expected_event_id
        or candidate.content_hash != content_digest(unsigned_candidate)
    ):
        raise ValueError("Persisted Graph proposal content identity mismatch")
    return candidate


def graph_patch_proposal_resolution_event(
    pending: GraphPatchProposalEvent,
    *,
    status: GraphPatchProposalStatus,
) -> GraphPatchProposalEvent:
    """Create the only terminal decision for one exact pending proposal."""

    if pending.status is not GraphPatchProposalStatus.PENDING:
        raise ValueError("Only a pending Graph proposal can be resolved")
    if status not in {
        GraphPatchProposalStatus.APPROVED,
        GraphPatchProposalStatus.REJECTED,
    }:
        raise ValueError("Graph proposal resolution must approve or reject")
    proposal_identity = {
        "patch": pending.patch,
        "before_graph_digest": pending.before_graph_digest,
        "after_graph_digest": pending.after_graph_digest,
        "proposed_lease": pending.proposed_lease,
    }
    unsigned = {**proposal_identity, "status": status.value}
    event = GraphPatchProposalEvent(
        proposal_id=pending.proposal_id,
        event_id=f"graph-proposal-event-{content_digest(unsigned)[:24]}",
        patch=pending.patch,
        before_graph_digest=pending.before_graph_digest,
        after_graph_digest=pending.after_graph_digest,
        proposed_lease=pending.proposed_lease,
        status=status,
        content_hash="",
    )
    return replace(event, content_hash=content_digest(event))
