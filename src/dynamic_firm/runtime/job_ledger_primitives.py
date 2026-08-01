from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    GraphPatchEvent,
    GraphPatchProposalEvent,
    GraphPatchProposalStatus,
    JobGraph,
    JobMutationEvent,
    JobResult,
    TaskStatus,
    TaskAttemptRecord,
)
from dynamic_firm.company.frontdoor import WorkOrder
from dynamic_firm.kernel.graph import apply_patch, graph_from_proposal, replace_task
from dynamic_firm.kernel.mutation import (
    frozen_snapshot_digest,
    graph_patch_from_primitive,
    graph_patch_proposal_event_from_primitive,
    graph_structure_digest,
    structurally_read_only,
)
from dynamic_firm._vendor.paperclip_runtime.timeline import (
    TimelineWindow,
    normalize_event_limit,
    normalize_timeline_window,
)
from dynamic_firm._vendor.paperclip_runtime.run_summary import summarize_terminal_result
from dynamic_firm.runtime.models import (
    EmployeeRunResult,
    RunEvent,
    RunSignal,
    RunStatus,
    SignalCode,
    Usage,
    to_primitive,
    usage_from_dict,
)
from dynamic_firm.runtime.interruption import (
    InterruptionCause,
    RecoveryActionPreview,
    RecoveryDisposition,
)

from .store import RunStore, job_chain_digest
from .company_coordination import (
    CompanyCoordinationError,
    RemoteCompanyCoordinationClient,
)


SNAPSHOT_SCHEMA_V1 = "noruct.active-job-snapshot.v1"
SNAPSHOT_SCHEMA = "noruct.active-job-snapshot.v2"
TERMINAL_SCHEMA_V1 = "noruct.active-job-terminal.v1"
TERMINAL_SCHEMA = "noruct.active-job-terminal.v2"
_SUPPORTED_SNAPSHOT_SCHEMAS = frozenset({SNAPSHOT_SCHEMA_V1, SNAPSHOT_SCHEMA})
_SUPPORTED_TERMINAL_SCHEMAS = frozenset({TERMINAL_SCHEMA_V1, TERMINAL_SCHEMA})

_COMPANY_WORK_MODES = frozenset(
    {"DIRECT", "SOLO_JOB", "TEAM_JOB", "UNSPECIFIED"}
)
_COORDINATION_POLICIES = frozenset(
    {"DIRECT", "SOLO_FIRST", "PLAN_FIRST", "PRECOMPILED"}
)
_REQUESTED_EFFECTS = frozenset(
    {"READ", "WORKSPACE_CHANGE", "HOST_ACTION", "UNSPECIFIED"}
)
_PLANNING_MODES = frozenset(
    {"DIRECT", "BLUEPRINT", "DYNAMIC", "SOLO", "SOLO_FALLBACK", "PRECOMPILED"}
)


class ActiveJobAuditStatus(StrEnum):
    TERMINAL = "TERMINAL"
    INTERRUPTED = "INTERRUPTED"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ActiveJobRuntimeRun:
    """Privacy-bounded live Employee Runtime projection for one ACTIVE JOB."""

    run_id: str
    task_id: str
    employee_id: str
    status: str
    created_at: str
    updated_at: str
    pending_approval_count: int


@dataclass(frozen=True, slots=True)
class ActiveJobInspection:
    job_id: str
    request_id: str
    initial_company_work_mode: str
    company_work_mode: str
    coordination_policy: str
    requested_effect: str
    operating_reason: str
    planning_mode: str
    planning_reason: str
    compiler_usage: Usage
    compiler_provider_request_id: str | None
    work_order_id: str
    work_order_digest: str
    work_order_authority_digest: str
    firm_admission_digest: str
    graph_blueprint_id: str
    graph_blueprint_version: int
    graph_blueprint_digest: str
    graph_mutation_policy: str
    graph_constraints_digest: str
    initial_graph_digest: str
    audit_status: ActiveJobAuditStatus
    job_status: str | None
    created_at: str
    frozen_snapshot_hash: str
    chain_head: str
    final_graph_version: int
    attempt_count: int
    mutation_count: int
    graph_patch_count: int
    replay_matches: bool
    reconstructed_tasks: tuple[Mapping[str, Any], ...]
    attempts: tuple[Mapping[str, Any], ...]
    mutations: tuple[Mapping[str, Any], ...]
    graph_patches: tuple[Mapping[str, Any], ...]
    graph_proposal_decisions: tuple[Mapping[str, Any], ...]
    terminal: Mapping[str, Any] | None
    job_limits: Mapping[str, Any]
    execution_replica_groups: tuple[Mapping[str, Any], ...]
    errors: tuple[str, ...]
    evolution_artifact_pins: tuple[Mapping[str, Any], ...] = ()
    automatic_resume: bool = False
    runtime_runs: tuple[ActiveJobRuntimeRun, ...] = ()
    # Content-free terminal tool facts are a read model only. Tool result
    # bodies, arguments, previews, resource keys and error details remain in
    # their owning runtime tables and never cross into product summaries.
    tool_receipts: tuple[Mapping[str, str], ...] = ()
    # A failed same-Job continuation must be visible without exposing the
    # removed package path, manifest, provider config, or error text.
    continuation_preflight_receipts: tuple[Mapping[str, str], ...] = ()
    # Static capability identity and coding validation receipts are separately
    # bounded so product reporting never reads task prose or validation detail.
    final_task_id: str = ""
    final_task_capabilities: tuple[str, ...] = ()
    validation_receipts: tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ActiveJobSummary:
    job_id: str
    request_id: str
    company_work_mode: str
    coordination_policy: str
    requested_effect: str
    planning_mode: str
    work_order_id: str
    audit_status: ActiveJobAuditStatus
    job_status: str | None
    created_at: str
    attempt_count: int
    mutation_count: int
    graph_patch_count: int
    final_graph_version: int
    chain_head: str


_GRAPH_PROPOSAL_STATUSES = frozenset({"PENDING", "APPROVED", "REJECTED", "UNAVAILABLE"})
_GRAPH_PROPOSAL_OPERATIONS = frozenset({"SPLIT", "JOIN", "MERGE", "INSERT", "CANCEL"})


def _terminal_graph_proposal_payload(result: JobResult) -> tuple[Mapping[str, object], ...]:
    """Persist only the operator-safe portion of resolved PROPOSE decisions.

    The typed event itself carries a complete patch candidate, including task
    topology and rationale.  That is useful inside the Kernel but must not be
    copied into a retained product/audit projection.  Accepted rewrites remain
    represented by the separate append-only ``GraphPatchEvent`` chain.
    """

    return tuple(
        {
            "status": event.status.value,
            "semantic_operation": event.patch.semantic_operation.value,
            "base_graph_version": event.patch.base_graph_version,
            "proposed_lease": {
                "model_calls": event.proposed_lease.model_calls,
                "tool_calls": event.proposed_lease.tool_calls,
                "cost_usd": event.proposed_lease.cost_usd,
            },
        }
        for event in result.graph_patch_proposal_events
    )


def _safe_terminal_graph_proposal_decisions(
    payload: Mapping[str, Any],
    *,
    errors: list[str],
) -> tuple[Mapping[str, object], ...]:
    """Validate and project durable PROPOSE state without patch content."""

    raw = payload.get("graph_proposal_decisions", ())
    if not isinstance(raw, (tuple, list)):
        errors.append("terminal graph proposal decisions malformed")
        return ()
    declared_count = payload.get("graph_proposal_decision_count", len(raw))
    if type(declared_count) is not int or declared_count != len(raw):
        errors.append("terminal graph proposal aggregate mismatch")

    projected: list[Mapping[str, object]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            errors.append("terminal graph proposal decision malformed")
            continue
        status = item.get("status")
        operation = item.get("semantic_operation")
        base_version = item.get("base_graph_version")
        lease = item.get("proposed_lease")
        if (
            not isinstance(status, str)
            or status not in _GRAPH_PROPOSAL_STATUSES
            or not isinstance(operation, str)
            or operation not in _GRAPH_PROPOSAL_OPERATIONS
            or type(base_version) is not int
            or base_version < 0
            or not isinstance(lease, Mapping)
        ):
            errors.append("terminal graph proposal decision invalid")
            continue
        model_calls = lease.get("model_calls")
        tool_calls = lease.get("tool_calls")
        cost_usd = lease.get("cost_usd")
        if (
            type(model_calls) is not int
            or model_calls < 0
            or type(tool_calls) is not int
            or tool_calls < 0
            or isinstance(cost_usd, bool)
            or not isinstance(cost_usd, (int, float))
            or not math.isfinite(float(cost_usd))
            or float(cost_usd) < 0
        ):
            errors.append("terminal graph proposal lease invalid")
            continue
        projected.append(
            {
                "sequence": index,
                "status": status,
                "operation": operation,
                "base_graph_version": base_version,
                "proposed_lease": {
                    "model_calls": model_calls,
                    "tool_calls": tool_calls,
                    "cost_usd": float(cost_usd),
                },
            }
        )
    return tuple(projected)


@dataclass(frozen=True, slots=True)
class ActiveJobTimelineEvent:
    """Safe event detail for one bounded, read-only operator timeline row."""

    run_id: str
    task_id: str
    employee_id: str
    event_type: str
    occurred_at: str
    usage_delta: Usage | None
    terminal_summary: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class ActiveJobTimeline:
    """First-party operator view; it is not the canonical audit ledger."""

    job_id: str
    audit_status: ActiveJobAuditStatus
    window_from: str
    window_to: str
    window_capped: bool
    event_limit: int
    event_count: int
    truncated: bool
    runtime_run_count: int
    job_usage: Usage
    events: tuple[ActiveJobTimelineEvent, ...]


@dataclass(frozen=True, slots=True)
class ActiveJobInterruptionEvidence:
    """Content-free evidence that makes an interrupted run non-replayable.

    A provider cancellation receipt proves only that the local runtime observed
    a cancellation request for a provider request identity.  It does not prove
    that the provider produced no completion, charged no usage, or that an
    external effect was not committed.  The projection therefore contains
    counts only, never provider request identifiers or event payloads.
    """

    provider_cancellation_receipt_count: int = 0
    malformed_provider_cancellation_event_count: int = 0
    timeout_terminal_run_count: int = 0
    nonterminal_runtime_run_count: int = 0
    causes: tuple[InterruptionCause, ...] = ()


@dataclass(frozen=True, slots=True)
class ActiveJobRecoveryAdvice:
    """Read-only operator decision support for a non-terminal ACTIVE JOB.

    This is intentionally not a scheduler or a resume token.  The original
    request is privacy-bounded and the in-memory Firm Kernel may have held
    graph, reservation and cancellation state that cannot be recreated from
    the audit.  The only safe execution path is a new Kernel-owned job.
    """

    job_id: str
    audit_status: ActiveJobAuditStatus
    recovery_state: str
    requires_new_kernel_attempt: bool
    runtime_run_statuses: tuple[str, ...]
    recommended_actions: tuple[str, ...]
    prohibited_actions: tuple[str, ...]
    disposition: RecoveryDisposition
    local_continuation_candidate: Mapping[str, Any] | None = None
    interruption_evidence: ActiveJobInterruptionEvidence | None = None
    effect_recovery: ActiveJobEffectRecovery | None = None
    effect_recovery_cases: tuple[Mapping[str, Any], ...] = ()
    remote_effect_resource_claims: tuple[Mapping[str, Any], ...] = ()
    action_previews: tuple[RecoveryActionPreview, ...] = ()


@dataclass(frozen=True, slots=True)
class ActiveJobRecoveryPreparation:
    """Verified, non-dispatchable continuation input for a new Kernel owner.

    The ACTIVE JOB audit never stores the Work Order body. The caller supplies
    it again from its user-owned local authority and this object proves only
    that its identity, source references and interrupted task boundary match
    the retained audit. It deliberately cannot start, replay or claim a run.
    """

    job_id: str
    work_order_id: str
    work_order_digest: str
    graph_version: int
    completed_task_ids: tuple[str, ...]
    pending_task_ids: tuple[str, ...]
    required_checks: tuple[str, ...]
    continuation_authority: str = "NEW_KERNEL_ATTEMPT_REQUIRED"


@dataclass(frozen=True, slots=True)
class ActiveJobSameJobContinuation:
    """One explicit, receipt-bound permission to re-enter an untouched Job.

    This is not a partial-graph resume token.  It is available only when no
    Employee attempt, graph mutation, approval-waiting run, or effect could
    have occurred.  The caller must still hold and present the original
    in-memory request and user-owned Work Order.
    """

    job_id: str
    request_id: str
    work_order_id: str
    work_order_digest: str
    graph_digest: str
    required_checks: tuple[str, ...]
    continuation_authority: str = "SAME_JOB_FRESH_START_ALLOWED"


@dataclass(frozen=True, slots=True)
class ActiveJobPartialContinuation:
    """One explicit read-only partial-graph continuation admission.

    Completed results remain in the user-local Employee Runtime store.  This
    public projection contains only task/run identities and a digest, so an
    operator can review the boundary without leaking raw result content into
    ACTIVE JOB or a future remote coordinator.
    """

    job_id: str
    request_id: str
    work_order_id: str
    work_order_digest: str
    graph_digest: str
    completed_task_ids: tuple[str, ...]
    completed_run_ids: tuple[str, ...]
    completed_results_digest: str
    required_checks: tuple[str, ...]
    continuation_authority: str = "PARTIAL_READ_ONLY_CONTINUATION_ALLOWED"


@dataclass(frozen=True, slots=True)
class ActiveJobEffectRecovery:
    """A non-dispatchable recovery decision for an interrupted effectful Job."""

    job_id: str
    disposition: str
    completed_task_ids: tuple[str, ...]
    pending_task_ids: tuple[str, ...]
    observed_cost_usd: float | None
    required_checks: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ActiveJobApprovedGraphContinuation:
    """Exact, claimed graph and successful receipts for a later Kernel entry."""

    job_id: str
    proposal_id: str
    before_graph: JobGraph
    graph: JobGraph
    completed_results: Mapping[str, EmployeeRunResult]
    prior_graph_patch_count: int
    prior_specialist_material_profiles: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ActiveJobCheckpoint:
    """One read-only state boundary reconstructed from the ACTIVE JOB audit.

    This is deliberately an operator projection, not a resumable execution
    token.  It carries only graph/task lifecycle facts that a CLI, TUI, or
    future GUI can safely inspect without receiving prompt, artifact, tool, or
    approval payloads.
    """

    checkpoint_id: str
    parent_checkpoint_id: str | None
    ledger_sequence: int
    event_type: str
    chain_hash: str
    graph_version: int
    graph_digest: str
    changed_task_ids: tuple[str, ...]
    task_states: tuple[Mapping[str, Any], ...]
    resumable: bool = False


@dataclass(frozen=True, slots=True)
class ActiveJobCheckpointHistory:
    """Bounded checkpoint lineage for one replay-verified ACTIVE JOB."""

    job_id: str
    audit_status: ActiveJobAuditStatus
    checkpoint_count: int
    checkpoints: tuple[ActiveJobCheckpoint, ...]
    automatic_resume: bool = False


def _canonical_json(value: Any) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def remote_continuation_id(
    job_id: str,
    request_snapshot_hash: str,
    graph_digest: str,
) -> str:
    """Build the stable remote identity shared by continuation writer and inspector."""

    value = hashlib.sha256(
        f"noruct.partial-continuation.v1|{job_id}|{request_snapshot_hash}|{graph_digest}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"continuation-{value}"


def _record_content_hash_valid(payload: Mapping[str, Any]) -> bool:
    expected = str(payload.get("content_hash", ""))
    unhashed = dict(payload)
    unhashed["content_hash"] = ""
    return bool(expected) and expected == _digest(unhashed)


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(str(row["payload_json"]))
    if not isinstance(value, dict):
        raise ValueError("Ledger payload must be a JSON object")
    return value


def _resolved_company_work_mode(initial_mode: str, task_count: int) -> str:
    """Resolve the effective coordination shape without inferring authority."""

    if initial_mode == "DIRECT":
        return "DIRECT"
    return "TEAM_JOB" if task_count > 1 else "SOLO_JOB"


def _operating_identity(
    payload: Mapping[str, Any],
    *,
    schema_version: str,
    task_count: int,
    label: str,
    errors: list[str],
) -> dict[str, str]:
    """Read v2 operating identity while keeping v1 audits replayable."""

    if schema_version in {SNAPSHOT_SCHEMA_V1, TERMINAL_SCHEMA_V1}:
        return {
            "initial_company_work_mode": "UNSPECIFIED",
            "company_work_mode": _resolved_company_work_mode(
                "UNSPECIFIED", task_count
            ),
            "coordination_policy": "PRECOMPILED",
            "requested_effect": "UNSPECIFIED",
            "operating_reason": "LEGACY_ACTIVE_JOB_V1",
        }

    raw = payload.get("operating_decision")
    if not isinstance(raw, Mapping):
        errors.append(f"{label} operating decision missing")
        raw = {}
    identity = {
        "initial_company_work_mode": str(
            raw.get("initial_company_work_mode", "")
        ),
        "company_work_mode": str(raw.get("company_work_mode", "")),
        "coordination_policy": str(raw.get("coordination_policy", "")),
        "requested_effect": str(raw.get("requested_effect", "")),
        "operating_reason": str(raw.get("operating_reason", "")),
    }
    if identity["initial_company_work_mode"] not in _COMPANY_WORK_MODES:
        errors.append(f"{label} initial company work mode invalid")
    if identity["company_work_mode"] not in {"DIRECT", "SOLO_JOB", "TEAM_JOB"}:
        errors.append(f"{label} company work mode invalid")
    if identity["coordination_policy"] not in _COORDINATION_POLICIES:
        errors.append(f"{label} coordination policy invalid")
    if identity["requested_effect"] not in _REQUESTED_EFFECTS:
        errors.append(f"{label} requested effect invalid")
    reason = identity["operating_reason"]
    if (
        not reason
        or len(reason) > 64
        or reason.upper() != reason
        or not reason.replace("_", "").isalnum()
    ):
        errors.append(f"{label} operating reason invalid")
    expected_mode = _resolved_company_work_mode(
        identity["initial_company_work_mode"], task_count
    )
    if identity["company_work_mode"] != expected_mode:
        errors.append(f"{label} company work mode does not match graph shape")
    return identity


def _planning_identity(
    payload: Mapping[str, Any],
    *,
    schema_version: str,
    label: str,
    errors: list[str],
) -> dict[str, Any]:
    """Read bounded planning provenance while keeping v1 audits replayable."""

    if schema_version in {SNAPSHOT_SCHEMA_V1, TERMINAL_SCHEMA_V1}:
        return {
            "planning_mode": "PRECOMPILED",
            "planning_reason": "LEGACY_ACTIVE_JOB_V1",
            "compiler_usage": Usage(),
            "compiler_provider_request_id": None,
        }

    raw = payload.get("planning")
    if not isinstance(raw, Mapping):
        errors.append(f"{label} planning provenance missing")
        raw = {}
    mode = str(raw.get("planning_mode", ""))
    reason = str(raw.get("planning_reason", ""))
    if mode not in _PLANNING_MODES:
        errors.append(f"{label} planning mode invalid")
    if (
        not reason
        or len(reason) > 64
        or reason.upper() != reason
        or not reason.replace("_", "").isalnum()
    ):
        errors.append(f"{label} planning reason invalid")

    raw_usage = raw.get("compiler_usage")
    if not isinstance(raw_usage, Mapping):
        errors.append(f"{label} compiler usage invalid")
        raw_usage = {}
    try:
        compiler_usage = usage_from_dict(raw_usage)
    except (TypeError, ValueError, OverflowError):
        errors.append(f"{label} compiler usage invalid")
        compiler_usage = Usage()
    if any(
        type(value) is not int or value < 0
        for value in (
            compiler_usage.model_calls,
            compiler_usage.tool_calls,
            compiler_usage.input_tokens,
            compiler_usage.cached_input_tokens,
            compiler_usage.output_tokens,
        )
    ) or not math.isfinite(compiler_usage.cost_usd) or compiler_usage.cost_usd < 0:
        errors.append(f"{label} compiler usage invalid")

    raw_provider_id = raw.get("compiler_provider_request_id")
    provider_id = None if raw_provider_id is None else str(raw_provider_id)
    if provider_id is not None and (
        not provider_id
        or len(provider_id) > 160
        or any(ord(char) < 32 or ord(char) == 127 for char in provider_id)
    ):
        errors.append(f"{label} compiler provider request id invalid")
    return {
        "planning_mode": mode,
        "planning_reason": reason,
        "compiler_usage": compiler_usage,
        "compiler_provider_request_id": provider_id,
    }


def _work_order_identity(
    payload: Mapping[str, Any],
    *,
    schema_version: str,
    label: str,
    errors: list[str],
) -> dict[str, str]:
    if schema_version in {SNAPSHOT_SCHEMA_V1, TERMINAL_SCHEMA_V1}:
        return {
            "work_order_id": "",
            "work_order_digest": "",
            "work_order_authority_digest": "",
            "firm_admission_digest": "",
        }
    raw = payload.get("work_order")
    if not isinstance(raw, Mapping):
        errors.append(f"{label} work order identity missing")
        raw = {}
    identity = {
        "work_order_id": str(raw.get("work_order_id", "")),
        "work_order_digest": str(raw.get("work_order_digest", "")),
        "work_order_authority_digest": str(
            raw.get("work_order_authority_digest", "")
        ),
        "firm_admission_digest": str(raw.get("firm_admission_digest", "")),
    }
    for key, maximum_bytes in (
        ("work_order_id", 256),
        ("work_order_digest", 128),
        ("work_order_authority_digest", 128),
        ("firm_admission_digest", 128),
    ):
        value = identity[key]
        if value and (
            len(value.encode("utf-8")) > maximum_bytes
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            errors.append(f"{label} {key.replace('_', ' ')} invalid")
    return identity


def _graph_blueprint_identity(
    payload: Mapping[str, Any],
    *,
    label: str,
    errors: list[str],
) -> dict[str, Any]:
    """Read inert Blueprint provenance without making historic v2 rows invalid."""

    raw = payload.get("graph_blueprint")
    if raw is None:
        return {
            "blueprint_id": "",
            "blueprint_version": 0,
            "blueprint_digest": "",
            "mutation_policy": "BOUNDED_AUTO",
            "constraints_digest": "",
            "initial_graph_digest": "",
        }
    if not isinstance(raw, Mapping):
        errors.append(f"{label} graph Blueprint provenance invalid")
        raw = {}
    blueprint_id = str(raw.get("blueprint_id", ""))
    raw_version = raw.get("blueprint_version", 0)
    version = raw_version if type(raw_version) is int else -1
    blueprint_digest = str(raw.get("blueprint_digest", ""))
    mutation_policy = str(raw.get("mutation_policy", ""))
    constraints_digest = str(raw.get("constraints_digest", ""))
    initial_graph_digest = str(raw.get("initial_graph_digest", ""))
    raw_constraints = raw.get("constraints", {})
    if raw_constraints is None:
        raw_constraints = {}
    if not isinstance(raw_constraints, Mapping):
        errors.append(f"{label} graph constraints invalid")
        raw_constraints = {}

    def employee_ids(field: str) -> list[str]:
        value = raw_constraints.get(field, ())
        if not isinstance(value, (list, tuple)):
            errors.append(f"{label} graph {field} invalid")
            return []
        normalized = [str(item) for item in value]
        if any(
            not item
            or len(item) > 160
            or not item.replace("-", "").replace("_", "").isalnum()
            for item in normalized
        ) or len(normalized) != len(set(normalized)):
            errors.append(f"{label} graph {field} invalid")
        return normalized

    pinned_employee_ids = employee_ids("pinned_employee_ids")
    excluded_employee_ids = employee_ids("excluded_employee_ids")
    if set(pinned_employee_ids) & set(excluded_employee_ids):
        errors.append(f"{label} graph employee constraints overlap")
    require_independent_review = raw_constraints.get(
        "require_independent_review", False
    )
    if type(require_independent_review) is not bool:
        errors.append(f"{label} graph independent review constraint invalid")
        require_independent_review = False
    max_concurrency = raw_constraints.get("max_concurrency")
    if max_concurrency is not None and (
        type(max_concurrency) is not int or max_concurrency < 1
    ):
        errors.append(f"{label} graph concurrency constraint invalid")
        max_concurrency = None
    max_cost_usd = raw_constraints.get("max_cost_usd")
    if max_cost_usd is not None and (
        not isinstance(max_cost_usd, (int, float))
        or not math.isfinite(float(max_cost_usd))
        or float(max_cost_usd) < 0
    ):
        errors.append(f"{label} graph cost constraint invalid")
        max_cost_usd = None
    max_wall_time_ms = raw_constraints.get("max_wall_time_ms")
    if max_wall_time_ms is not None and (
        type(max_wall_time_ms) is not int or max_wall_time_ms < 1
    ):
        errors.append(f"{label} graph wall-time constraint invalid")
        max_wall_time_ms = None
    identity = (blueprint_id, version, blueprint_digest)
    if any(identity) and not all(identity):
        errors.append(f"{label} graph Blueprint provenance incomplete")
    if blueprint_id and (
        len(blueprint_id) > 160
        or not blueprint_id.replace("-", "").replace("_", "").isalnum()
    ):
        errors.append(f"{label} graph Blueprint id invalid")
    if version and version < 1:
        errors.append(f"{label} graph Blueprint version invalid")
    for field, value in (
        ("Blueprint", blueprint_digest),
        ("constraints", constraints_digest),
        ("initial graph", initial_graph_digest),
    ):
        if value and (
            len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            errors.append(f"{label} graph {field} digest invalid")
    if mutation_policy not in {"LOCKED", "PROPOSE", "BOUNDED_AUTO"}:
        errors.append(f"{label} graph mutation policy invalid")
    return {
        "blueprint_id": blueprint_id,
        "blueprint_version": version,
        "blueprint_digest": blueprint_digest,
        "mutation_policy": mutation_policy,
        "constraints_digest": constraints_digest,
        "constraints": {
            "pinned_employee_ids": pinned_employee_ids,
            "excluded_employee_ids": excluded_employee_ids,
            "require_independent_review": require_independent_review,
            "max_concurrency": max_concurrency,
            "max_cost_usd": max_cost_usd,
            "max_wall_time_ms": max_wall_time_ms,
        },
        "initial_graph_digest": initial_graph_digest,
    }


def _operator_timeline_event(event: RunEvent) -> ActiveJobTimelineEvent:
    """Drop every event payload field except the existing safe terminal view."""

    raw_terminal = event.payload.get("terminal_summary")
    terminal_summary = None
    if isinstance(raw_terminal, Mapping):
        terminal_summary = summarize_terminal_result(
            {
                "summary": raw_terminal.get("summary"),
                "status": raw_terminal.get("status"),
                "usage": raw_terminal.get("usage"),
                "failure": {"code": raw_terminal.get("failure_code")},
            }
        )
    return ActiveJobTimelineEvent(
        run_id=event.run_id,
        task_id=event.task_id,
        employee_id=event.employee_id,
        event_type=event.type.value,
        occurred_at=event.occurred_at.isoformat(),
        usage_delta=event.usage_delta,
        terminal_summary=terminal_summary,
    )
