"""Strict local codec for explicit same-Job continuation envelopes.

The ACTIVE JOB ledger deliberately contains only redacted execution evidence.
An operator who elects the narrow receipt-bound continuation path needs the
original immutable request as well, but that request must remain in a
user-local authority.  This codec is intentionally a typed JSON boundary, not
pickle: a modified local envelope is rejected by the frozen request digest and
can never construct arbitrary Python objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dynamic_firm.runtime.models import (
    ActionPolicy,
    ContextBundle,
    CostEfficiencyMode,
    PolicyDecision,
    RunLimits,
    TaskEvidenceItem,
    TaskEvidencePack,
    ToolEffect,
    ToolGrant,
    Usage,
    VersionedContent,
    to_primitive,
)

from .models import (
    CompanyRunRequest,
    EmployeeRecord,
    ExecutionReplicaAggregation,
    ExecutionReplicaSpec,
    ExecutionReplicaStrategy,
    ExecutionOriginBinding,
    JobLimits,
    JobTask,
    PlanProposal,
)


REQUEST_ENVELOPE_SCHEMA = "noruct.company-run-request.v1"


def request_envelope_payload(request: CompanyRunRequest) -> dict[str, object]:
    """Return the full user-local request payload for a future explicit resume.

    The payload is never suitable for ACTIVE JOB, graph, remote coordination,
    Community Blueprint, or Shared Evolution storage.  It may contain the
    user's goal and bounded Evidence content, so callers must retain it only
    in the local Work Order authority.
    """

    return {
        "schema": REQUEST_ENVELOPE_SCHEMA,
        "request": to_primitive(request),
    }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list")
    return tuple(str(item) for item in value)


def _versioned(value: object, label: str) -> VersionedContent:
    item = _mapping(value, label)
    return VersionedContent(
        content_id=str(item["content_id"]),
        revision=str(item["revision"]),
        content=str(item["content"]),
        content_hash=str(item.get("content_hash", "")),
    )


def _employee(value: object, label: str) -> EmployeeRecord:
    item = _mapping(value, label)
    return EmployeeRecord(
        employee_id=str(item["employee_id"]),
        role=str(item["role"]),
        capabilities=_strings(item.get("capabilities", ()), f"{label}.capabilities"),
        active=bool(item.get("active", True)),
        temporary=bool(item.get("temporary", False)),
        model_profile=str(item.get("model_profile", "scripted")),
    )


def _replica(value: object) -> ExecutionReplicaSpec | None:
    if value is None:
        return None
    item = _mapping(value, "execution_replica")
    return ExecutionReplicaSpec(
        group_id=str(item["group_id"]),
        replica_id=str(item["replica_id"]),
        strategy=ExecutionReplicaStrategy(str(item["strategy"])),
        scope=str(item["scope"]),
        aggregation_task_id=str(item["aggregation_task_id"]),
        aggregation=ExecutionReplicaAggregation(str(item["aggregation"])),
        marginal_value_reason=str(item["marginal_value_reason"]),
    )


def _task(value: object) -> JobTask:
    item = _mapping(value, "plan task")
    return JobTask(
        task_id=str(item["task_id"]),
        objective=str(item["objective"]),
        depends_on=_strings(item.get("depends_on", ()), "plan task.depends_on"),
        required_capabilities=_strings(
            item.get("required_capabilities", ()), "plan task.required_capabilities"
        ),
        acceptance_criteria=_strings(
            item.get("acceptance_criteria", ()), "plan task.acceptance_criteria"
        ),
        risk_level=str(item.get("risk_level", "LOW")),
        # An immutable request envelope always represents the initial graph.
        # Runtime task state and result bodies are rehydrated only from the
        # receipt-aware ledger after it has consumed its one-shot claim.
        execution_replica=_replica(item.get("execution_replica")),
    )


def _evidence(value: object) -> TaskEvidencePack | None:
    if value is None:
        return None
    payload = _mapping(value, "task_evidence")
    items = tuple(
        TaskEvidenceItem(
            citation_id=str(_mapping(item, "evidence item")["citation_id"]),
            source_id=str(_mapping(item, "evidence item")["source_id"]),
            source_revision=str(_mapping(item, "evidence item")["source_revision"]),
            title=str(_mapping(item, "evidence item")["title"]),
            content=str(_mapping(item, "evidence item")["content"]),
            source_hash=str(_mapping(item, "evidence item")["source_hash"]),
            content_hash=str(_mapping(item, "evidence item")["content_hash"]),
            location=dict(_mapping(item, "evidence item").get("location", {})),
        )
        for item in payload.get("items", ())
    )
    pack = TaskEvidencePack(
        pack_id=str(payload["pack_id"]),
        revision=int(payload["revision"]),
        pack_digest=str(payload["pack_digest"]),
        delivery_digest=str(payload["delivery_digest"]),
        access_scope=str(payload["access_scope"]),
        items=items,
    )
    pack.verify(max_items=20, max_bytes=64_000)
    return pack


def _context(value: object) -> ContextBundle:
    payload = _mapping(value, "context_snapshot")
    return ContextBundle(
        company_policy_excerpt=str(payload.get("company_policy_excerpt", "")),
        task_dependencies=tuple(
            _versioned(item, "context task dependency")
            for item in payload.get("task_dependencies", ())
        ),
        selected_facts=tuple(
            _versioned(item, "context selected fact")
            for item in payload.get("selected_facts", ())
        ),
        selected_memory=tuple(
            _versioned(item, "context selected memory")
            for item in payload.get("selected_memory", ())
        ),
        ephemeral_instructions=_strings(
            payload.get("ephemeral_instructions", ()), "context ephemeral instructions"
        ),
        task_evidence=_evidence(payload.get("task_evidence")),
        workspace_id=(
            None if payload.get("workspace_id") is None else str(payload["workspace_id"])
        ),
    )


def _limits(value: object) -> RunLimits:
    payload = _mapping(value, "runtime_limits")
    return RunLimits(
        max_wall_time_ms=int(payload.get("max_wall_time_ms", 30_000)),
        max_model_calls=int(payload.get("max_model_calls", 8)),
        max_tool_calls=int(payload.get("max_tool_calls", 16)),
        max_input_tokens=int(payload.get("max_input_tokens", 100_000)),
        max_output_tokens=int(payload.get("max_output_tokens", 20_000)),
        max_cost_usd=float(payload.get("max_cost_usd", 5.0)),
        max_consecutive_errors=int(payload.get("max_consecutive_errors", 2)),
        max_result_bytes=int(payload.get("max_result_bytes", 256_000)),
        max_tool_output_bytes=int(payload.get("max_tool_output_bytes", 256_000)),
        max_context_messages=int(payload.get("max_context_messages", 32)),
        max_context_chars=int(payload.get("max_context_chars", 120_000)),
        context_keep_recent_messages=int(payload.get("context_keep_recent_messages", 12)),
        cost_efficiency_mode=CostEfficiencyMode(
            str(payload.get("cost_efficiency_mode", CostEfficiencyMode.STANDARD.value))
        ),
    )


def _action_policy(value: object) -> ActionPolicy:
    payload = _mapping(value, "action_policy")
    grants = tuple(
        ToolGrant(
            tool_name=str(_mapping(item, "tool grant")["tool_name"]),
            allowed_effects=tuple(
                ToolEffect(str(effect))
                for effect in _mapping(item, "tool grant").get("allowed_effects", ())
            ),
            resource_patterns=_strings(
                _mapping(item, "tool grant").get("resource_patterns", ("*",)),
                "tool grant.resource_patterns",
            ),
            max_calls=int(_mapping(item, "tool grant").get("max_calls", 1)),
            max_cost_usd=(
                None
                if _mapping(item, "tool grant").get("max_cost_usd") is None
                else float(_mapping(item, "tool grant")["max_cost_usd"])
            ),
            requires_approval=bool(_mapping(item, "tool grant").get("requires_approval", False)),
        )
        for item in payload.get("tool_grants", ())
    )
    return ActionPolicy(
        default_decision=PolicyDecision(str(payload.get("default_decision", "DENY"))),
        tool_grants=grants,
        approval_grants=_strings(payload.get("approval_grants", ()), "approval grants"),
        network_policy=str(payload.get("network_policy", "DENY")),
        filesystem_policy=str(payload.get("filesystem_policy", "READ_ONLY")),
        secret_refs=_strings(payload.get("secret_refs", ()), "secret refs"),
        sandbox_profile=str(payload.get("sandbox_profile", "none")),
        capability_trust_mode=str(payload.get("capability_trust_mode", "strict")),
        auto_approved_tool_names=_strings(
            payload.get("auto_approved_tool_names", ()), "auto approved tool names"
        ),
    )


def _job_limits(value: object) -> JobLimits:
    payload = _mapping(value, "job_limits")
    return JobLimits(
        max_tasks=int(payload.get("max_tasks", 16)),
        max_concurrency=int(payload.get("max_concurrency", 4)),
        max_graph_patches=int(payload.get("max_graph_patches", 3)),
        max_task_mutations=int(payload.get("max_task_mutations", 2)),
        max_temporary_roles=int(payload.get("max_temporary_roles", 2)),
        max_total_model_calls=int(payload.get("max_total_model_calls", 64)),
        max_total_tool_calls=int(payload.get("max_total_tool_calls", 128)),
        max_total_cost_usd=float(payload.get("max_total_cost_usd", 20.0)),
        max_wall_time_ms=int(payload.get("max_wall_time_ms", 300_000)),
    )


def company_run_request_from_envelope(value: object) -> CompanyRunRequest:
    """Rebuild a typed request only from the local envelope schema."""

    envelope = _mapping(value, "continuation envelope")
    if envelope.get("schema") != REQUEST_ENVELOPE_SCHEMA:
        raise ValueError("Unsupported Company continuation envelope schema")
    payload = _mapping(envelope.get("request"), "continuation request")
    proposal_payload = _mapping(payload.get("plan_proposal"), "plan_proposal")
    skills = _mapping(payload.get("employee_skill_snapshots", {}), "employee skills")
    origin = payload.get("execution_origin")
    origin_payload = None if origin is None else _mapping(origin, "execution origin")
    manager = payload.get("manager_employee")
    return CompanyRunRequest(
        request_id=str(payload["request_id"]),
        job_id=str(payload["job_id"]),
        goal=str(payload["goal"]),
        plan_proposal=PlanProposal(
            proposal_id=str(proposal_payload["proposal_id"]),
            goal=str(proposal_payload["goal"]),
            tasks=tuple(_task(item) for item in proposal_payload.get("tasks", ())),
            final_task_id=str(proposal_payload["final_task_id"]),
            assumptions=_strings(proposal_payload.get("assumptions", ()), "plan assumptions"),
        ),
        roster=tuple(_employee(item, "roster employee") for item in payload.get("roster", ())),
        employee_skill_snapshots={
            str(employee_id): tuple(_versioned(item, "employee skill") for item in values)
            for employee_id, values in skills.items()
        },
        job_local_skill_snapshots=tuple(
            _versioned(item, "job local skill")
            for item in payload.get("job_local_skill_snapshots", ())
        ),
        context_snapshot=_context(payload.get("context_snapshot", {})),
        execution_origin=(
            None
            if origin_payload is None
            else ExecutionOriginBinding(
                binding_id=str(origin_payload["binding_id"]),
                intent_id=str(origin_payload["intent_id"]),
                intent_revision=int(origin_payload["intent_revision"]),
                intent_hash=str(origin_payload["intent_hash"]),
                pack_id=str(origin_payload["pack_id"]),
                pack_revision=int(origin_payload["pack_revision"]),
                pack_digest=str(origin_payload["pack_digest"]),
                delivery_digest=str(origin_payload["delivery_digest"]),
                item_count=int(origin_payload["item_count"]),
                selected_bytes=int(origin_payload["selected_bytes"]),
                access_scope=str(origin_payload["access_scope"]),
                decision_context_id=str(origin_payload.get("decision_context_id", "")),
                decision_context_digest=str(origin_payload.get("decision_context_digest", "")),
                oracle_contract_id=str(origin_payload.get("oracle_contract_id", "")),
                oracle_contract_digest=str(origin_payload.get("oracle_contract_digest", "")),
            )
        ),
        runtime_limits=_limits(payload.get("runtime_limits", {})),
        action_policy=_action_policy(payload.get("action_policy", {})),
        job_limits=_job_limits(payload.get("job_limits", {})),
        company_revision=int(payload.get("company_revision", 0)),
        roster_revision=int(payload.get("roster_revision", 0)),
        playbook_revision=int(payload.get("playbook_revision", 0)),
        workflow_context_fingerprint=str(payload.get("workflow_context_fingerprint", "")),
        workspace_identity_revision=str(payload.get("workspace_identity_revision", "")),
        workspace_identity_status=str(payload.get("workspace_identity_status", "NOT_APPLICABLE")),
        workspace_identity_failure_code=str(payload.get("workspace_identity_failure_code", "")),
        session_key=str(payload.get("session_key", "")),
        manager_employee_id=str(payload.get("manager_employee_id", "")),
        manager_assignment_digest=str(payload.get("manager_assignment_digest", "")),
        manager_session_key=str(payload.get("manager_session_key", "")),
        manager_employee=None if manager is None else _employee(manager, "manager employee"),
        manager_delegation_payload=dict(
            _mapping(payload.get("manager_delegation_payload", {}), "manager delegation")
        ),
        manager_delegation_digest=str(payload.get("manager_delegation_digest", "")),
        planning_mode=str(payload.get("planning_mode", "PRECOMPILED")),
        planning_reason=str(payload.get("planning_reason", "LEGACY_PRECOMPILED")),
        compiler_usage=Usage(**{
            key: value
            for key, value in _mapping(payload.get("compiler_usage", {}), "compiler usage").items()
            if key in {"model_calls", "tool_calls", "input_tokens", "cached_input_tokens", "output_tokens", "cost_usd"}
        }),
        compiler_provider_request_id=(
            None
            if payload.get("compiler_provider_request_id") is None
            else str(payload["compiler_provider_request_id"])
        ),
        work_order_id=str(payload.get("work_order_id", "")),
        work_order_digest=str(payload.get("work_order_digest", "")),
        work_order_authority_digest=str(payload.get("work_order_authority_digest", "")),
        firm_admission_digest=str(payload.get("firm_admission_digest", "")),
        runtime_provider_binding_digest=str(
            payload.get("runtime_provider_binding_digest", "")
        ),
        runtime_tool_contract_digest=str(
            payload.get("runtime_tool_contract_digest", "")
        ),
        runtime_company_coordination_digest=str(
            payload.get("runtime_company_coordination_digest", "")
        ),
        company_work_mode=str(payload.get("company_work_mode", "UNSPECIFIED")),
        coordination_policy=str(payload.get("coordination_policy", "PRECOMPILED")),
        requested_effect=str(payload.get("requested_effect", "UNSPECIFIED")),
        operating_reason=str(payload.get("operating_reason", "LEGACY_PRECOMPILED")),
        graph_blueprint_id=str(payload.get("graph_blueprint_id", "")),
        graph_blueprint_version=int(payload.get("graph_blueprint_version", 0)),
        graph_blueprint_digest=str(payload.get("graph_blueprint_digest", "")),
        graph_mutation_policy=str(payload.get("graph_mutation_policy", "BOUNDED_AUTO")),
        graph_constraints_digest=str(payload.get("graph_constraints_digest", "")),
        graph_pinned_employee_ids=_strings(payload.get("graph_pinned_employee_ids", ()), "pinned employees"),
        graph_excluded_employee_ids=_strings(payload.get("graph_excluded_employee_ids", ()), "excluded employees"),
        graph_require_independent_review=bool(payload.get("graph_require_independent_review", False)),
        graph_max_concurrency=(
            None if payload.get("graph_max_concurrency") is None else int(payload["graph_max_concurrency"])
        ),
        graph_max_cost_usd=(
            None if payload.get("graph_max_cost_usd") is None else float(payload["graph_max_cost_usd"])
        ),
        graph_max_wall_time_ms=(
            None if payload.get("graph_max_wall_time_ms") is None else int(payload["graph_max_wall_time_ms"])
        ),
    )
