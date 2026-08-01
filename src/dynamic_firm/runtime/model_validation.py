"""Runtime persistence codecs and request-admission validation.

The public model module owns immutable contract definitions.  This component
owns restoration of those contracts from durable data and the admission checks
performed before a request reaches a runtime implementation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from .models import (
    CostEfficiencyMode,
    EmployeeRunRequest,
    EmployeeRunResult,
    EmployeeSessionRetention,
    Failure,
    FailureCategory,
    PolicyDecision,
    RunSignal,
    RunStatus,
    SemanticReplanDirective,
    SemanticReplanOperation,
    SignalCode,
    ToolEffect,
    Usage,
    is_reserved_employee_tool_name,
)

def usage_from_dict(value: Mapping[str, Any] | None) -> Usage:
    value = value or {}
    return Usage(
        model_calls=int(value.get("model_calls", 0)),
        tool_calls=int(value.get("tool_calls", 0)),
        input_tokens=int(value.get("input_tokens", 0)),
        cached_input_tokens=int(value.get("cached_input_tokens", 0)),
        output_tokens=int(value.get("output_tokens", 0)),
        cost_usd=float(value.get("cost_usd", 0.0)),
    )


def failure_from_dict(value: Mapping[str, Any] | None) -> Failure | None:
    if not value:
        return None
    return Failure(
        code=str(value["code"]),
        category=FailureCategory(value["category"]),
        message_safe=str(value["message_safe"]),
        retryable=bool(value.get("retryable", False)),
        origin=str(value.get("origin", "native-runtime")),
        details_ref=value.get("details_ref"),
    )


def semantic_replan_directive_from_dict(value: Mapping[str, Any]) -> SemanticReplanDirective:
    """Restore a bounded semantic replan proposal from persisted runtime data.

    A directive is evidence, not an executable patch.  It must therefore pass
    the exact same narrow shape validation after a RunStore round trip as it
    did at model-output ingress.  This helper intentionally does not coerce
    strings, mappings, or arbitrary sequences into identifier lists: doing so
    would let a malformed persisted payload change the meaning of a completed
    Employee result during later Kernel reconciliation.
    """

    if not isinstance(value, Mapping):
        raise TypeError("Semantic replan directive must be a mapping")

    raw_operation = value.get("operation")
    if not isinstance(raw_operation, str):
        raise ValueError("Semantic replan operation must be a string")

    def string_tuple(field: str) -> tuple[str, ...]:
        raw = value.get(field, ())
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise ValueError(f"Semantic replan {field} must be a sequence")
        if not all(isinstance(item, str) for item in raw):
            raise ValueError(f"Semantic replan {field} must contain strings only")
        return tuple(raw)

    directive = SemanticReplanDirective(
        operation=SemanticReplanOperation(raw_operation),
        task_ids=string_tuple("task_ids"),
        capability_ids=string_tuple("capability_ids"),
        assumption_refs=string_tuple("assumption_refs"),
        constraint_refs=string_tuple("constraint_refs"),
    )
    directive.verify()
    return directive


def signal_from_dict(value: Mapping[str, Any]) -> RunSignal:
    # A malformed optional directive must never invalidate an otherwise
    # auditable terminal receipt or silently fall back to a legacy free-text
    # grammar.  Keep the signal but drop the untrusted proposal; the Kernel
    # will simply not mutate the graph for it.
    semantic_replan = None
    raw_directive = value.get("semantic_replan")
    if raw_directive is not None:
        try:
            semantic_replan = semantic_replan_directive_from_dict(raw_directive)
        except (KeyError, TypeError, ValueError):
            semantic_replan = None
    return RunSignal(
        code=SignalCode(value["code"]),
        value=str(value.get("value", "")),
        evidence=tuple(str(item) for item in value.get("evidence", ())),
        semantic_replan=semantic_replan,
    )


def result_from_dict(value: Mapping[str, Any]) -> EmployeeRunResult:
    return EmployeeRunResult(
        run_id=str(value["run_id"]),
        request_id=str(value["request_id"]),
        job_id=str(value["job_id"]),
        task_id=str(value["task_id"]),
        employee_id=str(value["employee_id"]),
        status=RunStatus(value["status"]),
        summary=str(value.get("summary", "")),
        output_artifact_refs=tuple(value.get("output_artifact_refs", ())),
        acceptance_evidence=tuple(value.get("acceptance_evidence", ())),
        unresolved_issues=tuple(value.get("unresolved_issues", ())),
        observations=tuple(value.get("observations", ())),
        suggested_followups=tuple(value.get("suggested_followups", ())),
        signals=tuple(signal_from_dict(item) for item in value.get("signals", ())),
        partial_result=bool(value.get("partial_result", False)),
        usage=usage_from_dict(value.get("usage")),
        last_event_seq=int(value.get("last_event_seq", 0)),
        started_at=datetime.fromisoformat(value["started_at"]) if value.get("started_at") else None,
        finished_at=datetime.fromisoformat(value["finished_at"]),
        failure=failure_from_dict(value.get("failure")),
    )


def validate_request(request: EmployeeRunRequest) -> None:
    required_strings = {
        "request_id": request.request_id,
        "employee_id": request.employee.employee_id,
        "employee_role": request.employee.role,
        "job_id": request.task.job_id,
        "task_id": request.task.task_id,
        "objective": request.task.objective,
    }
    missing = [name for name, value in required_strings.items() if not value.strip()]
    if missing:
        raise ValueError(f"Missing required request fields: {', '.join(missing)}")
    if len(request.session_key.encode("utf-8")) > 512:
        raise ValueError("session_key must be at most 512 UTF-8 bytes")
    if request.context.task_evidence is not None:
        request.context.task_evidence.verify()
        if request.session_retention != EmployeeSessionRetention.RUN_ONLY:
            raise ValueError("Task Evidence Pack requires RUN_ONLY employee-session retention")
    if request.task.job_graph_version < 1 or request.task.attempt < 1:
        raise ValueError("job_graph_version and attempt must be positive")
    skills = request.employee.skills
    selected_memory = request.context.selected_memory
    if len(skills) > 8:
        raise ValueError("Employee execution accepts at most 8 frozen skills")
    if len(selected_memory) > 8:
        raise ValueError("Employee execution accepts at most 8 selected memory items")
    skill_prefix = f"employee-skill:{request.employee.employee_id}:"
    external_skill_prefix = "external-skill:"
    if request.employee.temporary and any(
        item.content_id.startswith(skill_prefix) for item in skills
    ):
        raise ValueError("Temporary employees cannot receive persistent Employee Skills")
    memory_prefix = f"employee-memory:{request.employee.employee_id}:"
    for label, items, allowed_prefixes, maximum_bytes in (
        (
            "Employee Skill",
            skills,
            (skill_prefix, external_skill_prefix),
            32_768,
        ),
        ("selected memory", selected_memory, (memory_prefix, "company-memory:"), 12_000),
    ):
        identities = tuple((item.content_id, item.revision) for item in items)
        if len(identities) != len(set(identities)):
            raise ValueError(f"{label} snapshot identities must be unique")
        if any(not item.content_id.strip() or not item.revision.strip() for item in items):
            raise ValueError(f"{label} snapshot identity and revision must be non-empty")
        if any(
            not any(item.content_id.startswith(prefix) for prefix in allowed_prefixes)
            for item in items
        ):
            raise ValueError(f"{label} snapshot crossed its employee namespace")
        if sum(len(item.content.encode("utf-8")) for item in items) > maximum_bytes:
            raise ValueError(f"{label} snapshot exceeds its byte limit")
    memory_ids = tuple(item.content_id for item in selected_memory)
    if request.employee.selected_memory_refs != memory_ids:
        raise ValueError("selected_memory_refs must exactly identify the frozen memory projection")
    numeric_limits = {
        "max_wall_time_ms": request.limits.max_wall_time_ms,
        "max_model_calls": request.limits.max_model_calls,
        "max_tool_calls": request.limits.max_tool_calls,
        "max_input_tokens": request.limits.max_input_tokens,
        "max_output_tokens": request.limits.max_output_tokens,
        "max_consecutive_errors": request.limits.max_consecutive_errors,
        "max_result_bytes": request.limits.max_result_bytes,
        "max_tool_output_bytes": request.limits.max_tool_output_bytes,
        "max_context_messages": request.limits.max_context_messages,
        "max_context_chars": request.limits.max_context_chars,
        "context_keep_recent_messages": request.limits.context_keep_recent_messages,
    }
    invalid = [name for name, value in numeric_limits.items() if value <= 0]
    if invalid or request.limits.max_cost_usd < 0:
        names = invalid + (["max_cost_usd"] if request.limits.max_cost_usd < 0 else [])
        raise ValueError(f"Run limits must be bounded non-negative values: {', '.join(names)}")
    if not isinstance(request.limits.cost_efficiency_mode, CostEfficiencyMode):
        raise ValueError("cost_efficiency_mode must be a supported mode")
    if request.action_policy.default_decision != PolicyDecision.DENY:
        raise ValueError("Employee execution requires a default-deny action policy")
    if request.action_policy.network_policy not in {"DENY", "EXTERNAL_READ_ONLY"}:
        raise ValueError("Network policy must be DENY or EXTERNAL_READ_ONLY")
    if request.action_policy.filesystem_policy not in {"READ_ONLY", "WORKSPACE_WRITE", "DENY"}:
        raise ValueError("Filesystem policy must be READ_ONLY, WORKSPACE_WRITE, or DENY")
    if request.action_policy.capability_trust_mode not in {"strict", "trusted", "autonomous"}:
        raise ValueError("Action policy capability trust mode is invalid")
    grant_names = [grant.tool_name for grant in request.action_policy.tool_grants]
    if len(grant_names) != len(set(grant_names)):
        raise ValueError("Tool grants must have unique tool names")
    auto_approved_names = request.action_policy.auto_approved_tool_names
    if len(auto_approved_names) != len(set(auto_approved_names)):
        raise ValueError("Auto-approved tool names must be unique")
    if not set(auto_approved_names).issubset(grant_names):
        raise ValueError("Auto-approved tools must already have an explicit grant")
    for grant in request.action_policy.tool_grants:
        if not grant.tool_name or grant.max_calls <= 0 or not grant.allowed_effects:
            raise ValueError(f"Invalid tool grant: {grant.tool_name!r}")
        if is_reserved_employee_tool_name(grant.tool_name):
            raise ValueError(
                "Employee execution cannot grant native delegation or MCP discovery tools"
            )
        if ToolEffect.WRITE in grant.allowed_effects:
            if request.action_policy.filesystem_policy != "WORKSPACE_WRITE":
                raise ValueError("Write grants require the WORKSPACE_WRITE filesystem policy")
            if not grant.requires_approval and not request.action_policy.auto_approves(grant.tool_name):
                raise ValueError("Write grants require dynamic approval")
        if ToolEffect.EXECUTE in grant.allowed_effects:
            remote_worker_grant = (
                grant.tool_name == "run_remote_workspace_program"
                and request.action_policy.sandbox_profile in {"remote-workspace-approved", "remote-and-browser-approved"}
                and len(grant.resource_patterns) == 1
                and grant.resource_patterns[0].startswith("remote-workspace:")
            )
            browser_control_grant = (
                grant.tool_name in {"navigate_browser_tab", "click_browser_element", "type_browser_text", "capture_browser_screenshot"}
                and request.action_policy.sandbox_profile in {"browser-control-approved", "remote-and-browser-approved"}
                and len(grant.resource_patterns) == 1
                and grant.resource_patterns[0].startswith("browser:local:tab:")
            )
            container_workspace_grant = (
                grant.tool_name == "run_container_workspace_program"
                and request.action_policy.sandbox_profile == "host-workspace-approved"
                and len(grant.resource_patterns) == 1
                and grant.resource_patterns[0].startswith("container-workspace:")
            )
            computer_use_grant = (
                grant.tool_name == "computer_use"
                and request.action_policy.sandbox_profile in {"computer-use-approved", "computer-and-browser-approved"}
                and len(grant.resource_patterns) == 1
                and grant.resource_patterns[0] == "computer:local:*"
            )
            if (
                request.action_policy.sandbox_profile != "host-workspace-approved"
                and not remote_worker_grant
                and not browser_control_grant
                and not container_workspace_grant
                and not computer_use_grant
            ):
                raise ValueError(
                    "Execute grants require host-workspace-approved, a bounded remote worker profile, or browser-control-approved"
                )
            if not grant.requires_approval and not request.action_policy.auto_approves(grant.tool_name):
                raise ValueError("Execute grants require dynamic approval")
        if ToolEffect.EXTERNAL_COMMUNICATION in grant.allowed_effects:
            raise ValueError("External communication grants are unsupported")
    network_grants = [
        grant
        for grant in request.action_policy.tool_grants
        if ToolEffect.NETWORK in grant.allowed_effects
    ]
    if request.action_policy.network_policy == "DENY" and network_grants:
        raise ValueError("Network grants require the EXTERNAL_READ_ONLY policy")
    if request.action_policy.network_policy == "EXTERNAL_READ_ONLY":
        # A configured capability surface can legitimately combine bounded
        # MCP reads, web read/search, Home Assistant state reads and the
        # operator-approved media lane.  The former implementation only
        # admitted the first private MCP name family and also rejected
        # approval-gated reads.  That made the public `external-read=ask`
        # setting impossible to execute: request validation failed before an
        # approval could be shown.  Every candidate still has to be a
        # first-party registered tool, one resource pattern, and a bounded
        # per-job call limit; approval remains valid (and required by HIGH
        # risk definitions such as media).
        if not 1 <= len(network_grants) <= 24:
            raise ValueError("EXTERNAL_READ_ONLY requires between one and twenty-four network grants")
        for network_grant in network_grants:
            if (
                network_grant.allowed_effects != (ToolEffect.NETWORK,)
                or not 1 <= network_grant.max_calls <= request.limits.max_tool_calls
                or len(network_grant.resource_patterns) != 1
            ):
                raise ValueError("Network grants must be bounded and resource-specific")
            resource = network_grant.resource_patterns[0]
            if not (
                resource.startswith("external-read:")
                or resource.startswith("home-assistant:")
                or resource.startswith("workspace:") and ":media:" in resource
            ):
                raise ValueError("Network grants must use an approved first-party resource family")
