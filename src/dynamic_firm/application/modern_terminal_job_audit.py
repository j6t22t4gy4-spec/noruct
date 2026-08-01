from __future__ import annotations

"""Read-only ACTIVE JOB audit projection for terminal and future GUI surfaces."""

import math
from pathlib import Path
import re
from typing import Mapping

from dynamic_firm.company import graph_run_record_from_active_job
from dynamic_firm.company.model_invocation_receipt import (
    InvocationTerminalStatus,
    ModelInvocationReceipt,
    ReceiptAvailability,
)
from dynamic_firm.product.route_operator_projection import (
    CompatibilityPoint,
    CompatibilityStatus,
    EgressOperatorState,
    EgressPolicyState,
    FallbackOperatorState,
    OperatorTaskIdentity,
    build_route_operator_projection,
)
from dynamic_firm.product.modern_tui import ModernTerminalCommandResult
from dynamic_firm.runtime.job_ledger import ActiveJobInspector
from dynamic_firm.runtime.store import RunStore

_JOB_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _frozen_route_admission_projection(
    store: RunStore,
    inspection: object,
) -> tuple[Mapping[str, object], ...]:
    """Project only verified, operator-safe frozen route admissions.

    A legacy run without an admission remains absent rather than acquiring an
    implied approval.  The durable store verifies the binding/admission pair
    before returning it; an invalid or tampered pair is deliberately omitted
    from this read-only surface.  In particular, the physical run identifier
    is used only for the store lookup and never crosses the product boundary.
    """

    projected: list[Mapping[str, object]] = []
    for runtime_run in tuple(getattr(inspection, "runtime_runs", ())):
        run_id = getattr(runtime_run, "run_id", None)
        employee_id = getattr(runtime_run, "employee_id", None)
        task_id = getattr(runtime_run, "task_id", None)
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(employee_id, str)
            or not employee_id
            or len(employee_id) > 192
            or not isinstance(task_id, str)
            or not task_id
            or len(task_id) > 192
        ):
            continue
        try:
            admission = store.get_frozen_route_admission(run_id)
        except (KeyError, ValueError):
            # Storage verification is fail-closed.  This content-free audit
            # cannot safely explain a malformed receipt, so it exposes none.
            continue
        if admission is None:
            continue
        summary = admission.operator_safe_summary()
        if not isinstance(summary, Mapping):
            continue
        binding = getattr(admission, "binding", None)
        selection_receipt = getattr(admission, "selection_receipt", None)
        selected_candidate = getattr(selection_receipt, "selected_candidate", None)
        route_id = summary.get("route_id")
        binding_digest = summary.get("binding_digest")
        receipt_digest = summary.get("selection_receipt_digest")
        policy_digest = summary.get("selection_policy_digest")
        reasons = summary.get("selection_reasons")
        status_pins = (
            getattr(binding, "intelligence_snapshot_digest", None),
            getattr(binding, "compatibility_evidence_digest", None),
            getattr(binding, "egress_policy_digest", None),
            getattr(binding, "fallback_policy_digest", None),
        )
        uncertainty = getattr(selected_candidate, "uncertainty", None)
        if (
            not isinstance(route_id, str)
            or not route_id
            or len(route_id) > 192
            or not all(
                isinstance(value, str) and _HEX_DIGEST.fullmatch(value)
                for value in (binding_digest, receipt_digest, policy_digest)
            )
            or not isinstance(reasons, (tuple, list))
            or any(
                not isinstance(reason, str) or not reason or len(reason) > 96
                for reason in reasons
            )
            or not all(
                isinstance(value, str) and _HEX_DIGEST.fullmatch(value)
                for value in status_pins
            )
            or isinstance(uncertainty, bool)
            or not isinstance(uncertainty, (int, float))
            or not math.isfinite(float(uncertainty))
            or not 0 <= float(uncertainty) <= 1
        ):
            continue
        projected.append(
            {
                "employee_id": employee_id,
                "task_id": task_id,
                "route_id": route_id,
                "binding_digest": binding_digest,
                "selection_receipt_digest": receipt_digest,
                "selection_policy_digest": policy_digest,
                "selection_reasons": tuple(reasons),
                # Opaque identity pins describe the already frozen route
                # decision only.  They do not authorize egress/fallback or
                # establish that either policy was actually exercised.
                "intelligence_snapshot_digest": status_pins[0],
                "compatibility_evidence_digest": status_pins[1],
                "egress_policy_digest": status_pins[2],
                "fallback_policy_digest": status_pins[3],
                "selected_uncertainty": float(uncertainty),
            }
        )
    return tuple(
        sorted(
            projected,
            key=lambda item: (
                str(item["employee_id"]),
                str(item["task_id"]),
                str(item["selection_receipt_digest"]),
                str(item["route_id"]),
            ),
        )
    )


def _model_invocation_receipt_projection(
    store: RunStore,
    inspection: object,
) -> tuple[Mapping[str, object], ...]:
    """Project verified physical-call terminal facts without call authority.

    The store is the only source of receipt truth: it verifies canonical
    receipt content and binds it to the retained frozen route before this
    function sees it.  The admission is fetched independently and must name
    the same binding.  An operator therefore sees neither a provider/model
    identifier nor a physical run or invocation identifier, and cannot turn a
    terminal receipt into a resume, reroute, or egress permission.
    """

    projected: list[Mapping[str, object]] = []
    for runtime_run in tuple(getattr(inspection, "runtime_runs", ())):
        run_id = getattr(runtime_run, "run_id", None)
        employee_id = getattr(runtime_run, "employee_id", None)
        task_id = getattr(runtime_run, "task_id", None)
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(employee_id, str)
            or not _JOB_IDENTIFIER.fullmatch(employee_id)
            or not isinstance(task_id, str)
            or not _JOB_IDENTIFIER.fullmatch(task_id)
        ):
            continue
        try:
            admission = store.get_frozen_route_admission(run_id)
            receipts = store.list_model_invocation_receipts(run_id)
        except (KeyError, ValueError):
            # Either durable evidence failed strict canonical/binding checks.
            # Do not expose a partial explanation for an untrusted run.
            continue
        if admission is None or not isinstance(receipts, list):
            continue
        summary = admission.operator_safe_summary()
        if not isinstance(summary, Mapping):
            continue
        route_id = summary.get("route_id")
        binding_digest = summary.get("binding_digest")
        if (
            not isinstance(route_id, str)
            or not _JOB_IDENTIFIER.fullmatch(route_id)
            or not isinstance(binding_digest, str)
            or not _HEX_DIGEST.fullmatch(binding_digest)
        ):
            continue
        for receipt in receipts:
            if (
                not isinstance(receipt, ModelInvocationReceipt)
                or receipt.route_binding_digest != binding_digest
                or not isinstance(receipt.terminal_status, InvocationTerminalStatus)
                or not isinstance(receipt.usage_availability, ReceiptAvailability)
                or not isinstance(receipt.cost_availability, ReceiptAvailability)
                or not isinstance(receipt.latency_ms, float)
                or not math.isfinite(receipt.latency_ms)
                or receipt.latency_ms < 0
            ):
                continue
            cost_usd: float | None
            if receipt.cost_availability is ReceiptAvailability.AVAILABLE:
                if (
                    not isinstance(receipt.cost_usd, float)
                    or not math.isfinite(receipt.cost_usd)
                    or receipt.cost_usd < 0
                ):
                    continue
                # 0.0 remains an observed available value, not an unknown.
                cost_usd = receipt.cost_usd
            else:
                if receipt.cost_usd is not None:
                    continue
                cost_usd = None
            projected.append(
                {
                    "employee_id": employee_id,
                    "task_id": task_id,
                    "route_id": route_id,
                    "binding_digest": binding_digest,
                    "receipt_digest": receipt.digest,
                    "terminal_status": receipt.terminal_status.value,
                    "usage_availability": receipt.usage_availability.value,
                    "cost_availability": receipt.cost_availability.value,
                    "cost_usd": cost_usd,
                    "latency_ms": receipt.latency_ms,
                }
            )
    return tuple(
        sorted(
            projected,
            key=lambda item: (
                str(item["employee_id"]),
                str(item["task_id"]),
                str(item["receipt_digest"]),
                str(item["route_id"]),
            ),
        )
    )


def _route_operator_projection(
    store: RunStore,
    inspection: object,
) -> tuple[Mapping[str, object], ...]:
    """Build the one durable route/admission/invocation projection for all UIs.

    A frozen binding proves policy pins but not an egress grant execution;
    invocation fan-out also does not identify fallback versus advisory work.
    The projection therefore carries explicit unverified/unclassified states
    instead of upgrading either fact from mutable adapter state.
    """

    projected: list[Mapping[str, object]] = []
    for runtime_run in tuple(getattr(inspection, "runtime_runs", ())):
        run_id = getattr(runtime_run, "run_id", None)
        employee_id = getattr(runtime_run, "employee_id", None)
        task_id = getattr(runtime_run, "task_id", None)
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(employee_id, str)
            or not _JOB_IDENTIFIER.fullmatch(employee_id)
            or not isinstance(task_id, str)
            or not _JOB_IDENTIFIER.fullmatch(task_id)
        ):
            continue
        try:
            admission = store.get_frozen_route_admission(run_id)
            receipts = store.list_model_invocation_receipts(run_id)
        except (KeyError, ValueError):
            continue
        if admission is None:
            continue
        binding = getattr(admission, "binding", None)
        selection = getattr(admission, "selection_receipt", None)
        if binding is None or selection is None:
            continue
        roots = [item for item in receipts if item.fanout_parent_id is None]
        actual_receipt = roots[-1] if roots else None
        fallback_state = (
            FallbackOperatorState.NOT_OBSERVED
            if actual_receipt is None
            else (
                FallbackOperatorState.FANOUT_UNCLASSIFIED
                if any(item.fanout_parent_id is not None for item in receipts)
                else FallbackOperatorState.NOT_USED
            )
        )
        try:
            projection = build_route_operator_projection(
                OperatorTaskIdentity(employee_id, task_id),
                binding,
                selection,
                CompatibilityPoint("frozen-route-evidence", CompatibilityStatus.UNKNOWN),
                EgressOperatorState(
                    EgressPolicyState.OFFLINE
                    if actual_receipt is None
                    else EgressPolicyState.UNVERIFIED
                ),
                actual_receipt,
                fallback_state,
            )
        except (TypeError, ValueError):
            continue
        projected.append(projection.canonical_payload())
    return tuple(
        sorted(
            projected,
            key=lambda item: (
                str(item["employee_id"]),
                str(item["task_id"]),
                str(item["route_id"]),
            ),
        )
    )


def _read_only_continuation_candidate(inspection: object) -> Mapping[str, object]:
    """Project an *eligible-to-recheck* prefix, never a resume authority.

    This deliberately mirrors only facts retained by the content-free audit.
    The eventual controller action must still reopen the user-local Work
    Order, prove the frozen request and ActionPolicy, and atomically claim the
    local/remote receipt before it can dispatch anything.
    """

    terminal_runtime_states = {"SUCCEEDED", "FAILED", "CANCELLED", "BUDGET_EXHAUSTED"}
    runtime_states = tuple(
        str(getattr(item, "status", ""))
        for item in tuple(getattr(inspection, "runtime_runs", ()))
    )
    successful_tasks = sum(
        1
        for item in tuple(getattr(inspection, "reconstructed_tasks", ()))
        if isinstance(item, Mapping) and item.get("status") == "SUCCEEDED"
    )
    candidate = (
        str(getattr(getattr(inspection, "audit_status", None), "value", ""))
        == "INTERRUPTED"
        and bool(getattr(inspection, "replay_matches", False))
        and str(getattr(inspection, "requested_effect", "")) == "READ"
        and int(getattr(inspection, "mutation_count", 0) or 0) == 0
        and int(getattr(inspection, "graph_patch_count", 0) or 0) == 0
        and not tuple(getattr(inspection, "graph_proposal_decisions", ()))
        and successful_tasks > 0
        and all(state in terminal_runtime_states for state in runtime_states)
    )
    return {
        "candidate": candidate,
        "successful_task_count": successful_tasks,
        "requires": (
            "user_local_work_order",
            "exact_frozen_request",
            "structural_read_only_policy",
            "one_shot_receipt_claim",
        ),
    }


def _graph_change_summary(
    inspection: object,
    *,
    initial_digest: str,
    revisions: tuple[Mapping[str, object], ...],
) -> Mapping[str, object]:
    """Return an honest structural summary without copying graph/task content.

    This is intentionally not a causal quality claim or a reconstructed
    topology editor. It tells an operator which accepted mutation categories
    occurred and what terminal task-state shape the immutable audit retained.
    """

    operations: dict[str, int] = {}
    total_cost_delta = 0.0
    final_digest = initial_digest
    for revision in revisions:
        operation = str(revision.get("operation", "unknown"))[:64]
        operations[operation] = operations.get(operation, 0) + 1
        value = revision.get("budget_delta", 0.0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total_cost_delta += max(0.0, float(value))
        next_digest = str(revision.get("next_digest", ""))
        if next_digest:
            final_digest = next_digest
    task_statuses: dict[str, int] = {}
    final_task_count = 0
    for task in tuple(getattr(inspection, "reconstructed_tasks", ())):
        if not isinstance(task, Mapping):
            continue
        status = str(task.get("status", "unknown"))[:64] or "unknown"
        task_statuses[status] = task_statuses.get(status, 0) + 1
        final_task_count += 1
    return {
        "initial_graph_version": 1,
        "final_graph_version": int(getattr(inspection, "final_graph_version", 0) or 0),
        "initial_digest": initial_digest,
        "final_digest": final_digest,
        "accepted_revision_count": len(revisions),
        "accepted_operations": dict(sorted(operations.items())),
        "total_reserved_cost_delta": total_cost_delta,
        "final_task_count": final_task_count,
        "final_task_status_counts": dict(sorted(task_statuses.items())),
        "execution_replica_group_count": len(
            tuple(getattr(inspection, "execution_replica_groups", ()))
        ),
        "meaning": "STRUCTURAL_CHANGE_SUMMARY_NOT_CAUSAL_ATTRIBUTION",
    }


def _frozen_budget_envelope(inspection: object) -> Mapping[str, object]:
    """Project only numeric limits frozen at Job admission.

    A retained Job audit must let an operator distinguish a future-Job
    preference from the actual immutable execution envelope.  The snapshot is
    not trusted as arbitrary UI data, so this function whitelists bounded
    scalar fields rather than returning its whole persisted mapping.
    """

    raw = getattr(inspection, "job_limits", {})
    if not isinstance(raw, Mapping):
        return {}
    keys = (
        "max_tasks",
        "max_concurrency",
        "max_graph_patches",
        "max_task_mutations",
        "max_temporary_roles",
        "max_total_model_calls",
        "max_total_tool_calls",
        "max_total_cost_usd",
        "max_wall_time_ms",
    )
    envelope: dict[str, object] = {}
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            envelope[key] = value
        elif isinstance(value, float) and math.isfinite(value) and value >= 0:
            envelope[key] = value
    return envelope


def _observed_execution(inspection: object) -> Mapping[str, object]:
    """Expose recorded execution facts without attributing a Graph revision.

    This is deliberately parallel to the structural graph summary. A terminal
    Job or completed local tool lifecycle cannot prove user-facing quality,
    cost benefit, or an external outcome caused by a topology change.
    """

    task_statuses: dict[str, int] = {}
    for task in tuple(getattr(inspection, "reconstructed_tasks", ())):
        if not isinstance(task, Mapping):
            continue
        status = str(task.get("status", "UNKNOWN"))[:64] or "UNKNOWN"
        task_statuses[status] = task_statuses.get(status, 0) + 1
    validation_statuses: dict[str, int] = {}
    for receipt in tuple(getattr(inspection, "validation_receipts", ())):
        if not isinstance(receipt, Mapping):
            continue
        status = str(receipt.get("status", "UNKNOWN"))[:64] or "UNKNOWN"
        validation_statuses[status] = validation_statuses.get(status, 0) + 1
    effectful = tuple(
        item
        for item in tuple(getattr(inspection, "tool_receipts", ()))
        if isinstance(item, Mapping)
        and item.get("effect") in {"WRITE", "EXECUTE", "EXTERNAL_COMMUNICATION"}
    )
    if not effectful:
        effect_status = "NOT_RUN"
    elif any(item.get("status") in {"INTENT_RECORDED", "STARTED", "INDETERMINATE"} for item in effectful):
        effect_status = "UNKNOWN"
    else:
        effect_status = "PARTIAL"
    return {
        "terminal_status": str(getattr(inspection, "job_status", "") or "NOT_RECORDED"),
        "task_status_counts": dict(sorted(task_statuses.items())),
        "coding_validation_status_counts": dict(sorted(validation_statuses.items())),
        "effect_receipt_status": effect_status,
        "work_outcome_status": "NOT_VERIFIED",
        "meaning": "OBSERVED_EXECUTION_NOT_CAUSAL_GRAPH_IMPACT",
    }


def job_audit_catalog(state_path: Path, *, limit: int = 20) -> Mapping[str, object]:
    """List bounded content-free retained Job identities for any product surface."""

    if not 1 <= limit <= 100:
        raise ValueError("Job audit catalog limit must be between 1 and 100")
    store = RunStore(state_path)
    try:
        summaries = ActiveJobInspector(store).list(limit)
    finally:
        store.close()
    return {
        "schema": "noruct.job-audit-catalog.v1",
        "jobs": tuple(
            {
                "job_id": item.job_id,
                "audit_status": item.audit_status.value,
                "job_status": item.job_status or "not terminal",
                "company_work_mode": item.company_work_mode,
                "coordination_policy": item.coordination_policy,
                "requested_effect": item.requested_effect,
                "created_at": item.created_at,
                "final_graph_version": item.final_graph_version,
                "attempt_count": item.attempt_count,
                "mutation_count": item.mutation_count,
            }
            for item in summaries
        ),
    }


def job_audit_snapshot(
    state_path: Path,
    job_id: str | None = None,
) -> Mapping[str, object]:
    """Return one strict content-free ACTIVE JOB inspection for any UI.

    The runtime ledger remains the authority.  This projection intentionally
    excludes Work Order text, task objectives, dependency artifacts, tool
    arguments, transcript/output content, approval previews, error traces,
    credentials, and every lifecycle mutation control.  A checkpoint is
    explanatory evidence, never a resume token.
    """

    store = RunStore(state_path)
    try:
        inspector = ActiveJobInspector(store)
        selected_job_id = (job_id or "").strip()
        if selected_job_id and not _JOB_IDENTIFIER.fullmatch(selected_job_id):
            return {
                "schema": "noruct.job-audit-surface.v1",
                "job": None,
                "graph": {},
                "checkpoints": (),
                "route_admissions": (),
                "model_invocations": (),
                "route_operator_projections": (),
                "requested_job_id": selected_job_id[:192],
                "error": "Job identifier is invalid.",
            }
        if not selected_job_id:
            summaries = inspector.list(1)
            selected_job_id = summaries[0].job_id if summaries else ""
        if not selected_job_id:
            return {
                "schema": "noruct.job-audit-surface.v1",
                "job": None,
                "graph": {},
                "checkpoints": (),
                "route_admissions": (),
                "model_invocations": (),
                "route_operator_projections": (),
            }
        try:
            inspection = inspector.inspect(selected_job_id)
        except (KeyError, ValueError):
            return {
                "schema": "noruct.job-audit-surface.v1",
                "job": None,
                "graph": {},
                "checkpoints": (),
                "route_admissions": (),
                "model_invocations": (),
                "route_operator_projections": (),
                "requested_job_id": selected_job_id,
                "error": "No retained ACTIVE JOB matches this identifier.",
            }
        try:
            record = graph_run_record_from_active_job(inspection)
        except ValueError:
            # Invalid/unreplayable audit evidence may still be inspected,
            # but must never be represented as authoritative graph lineage.
            record = None
        history = inspector.checkpoints(inspection.job_id)
        recovery = inspector.recovery_advice(inspection.job_id)
        route_admissions = _frozen_route_admission_projection(store, inspection)
        model_invocations = _model_invocation_receipt_projection(store, inspection)
        route_operator_projections = _route_operator_projection(store, inspection)
    finally:
        store.close()

    blueprint = "unbound"
    revisions: tuple[Mapping[str, object], ...] = ()
    initial_digest = inspection.initial_graph_digest
    if record is not None:
        initial_digest = record.initial_graph_digest
        if record.blueprint_ref is not None:
            blueprint = f"{record.blueprint_ref.blueprint_id}@{record.blueprint_ref.version}"
        revisions = tuple(
            {
                "sequence": item.sequence,
                "operation": item.operation,
                "previous_digest": item.previous_graph_digest,
                "next_digest": item.next_graph_digest,
                "budget_delta": item.budget_delta,
                "approval_policy": item.approval_policy.value,
                "expected_impact": item.expected_impact.value,
                "validation_receipt": item.validation_receipt.value,
                # This is an association with the terminal Job outcome,
                # never an attribution claim that one revision caused it.
                "observed_terminal_outcome": (
                    item.observed_terminal_outcome.value
                ),
            }
            for item in record.revisions[:32]
        )
    checkpoints = tuple(
        {
            "ledger_sequence": item.ledger_sequence,
            "event_type": item.event_type,
            "graph_version": item.graph_version,
            "parent_checkpoint_id": item.parent_checkpoint_id or "",
            "changed_task_ids": tuple(item.changed_task_ids[:32]),
            "task_states": tuple(
                {
                    "task_id": str(state.get("task_id", ""))[:128],
                    "status": str(state.get("status", ""))[:64],
                }
                for state in item.task_states[:64]
                if isinstance(state, Mapping)
            ),
        }
        for item in history.checkpoints[:48]
    )
    return {
        "schema": "noruct.job-audit-surface.v1",
        "job": {
            "job_id": inspection.job_id,
            "audit_status": inspection.audit_status.value,
            "job_status": inspection.job_status or "not terminal",
            "company_work_mode": inspection.company_work_mode,
            "coordination_policy": inspection.coordination_policy,
            "requested_effect": inspection.requested_effect,
            "replay_matches": inspection.replay_matches,
            "final_graph_version": inspection.final_graph_version,
            "attempt_count": inspection.attempt_count,
            "mutation_count": inspection.mutation_count,
        },
        "graph": {
            "blueprint": blueprint,
            "initial_digest": initial_digest,
            "revisions": revisions,
            "change_summary": _graph_change_summary(
                inspection,
                initial_digest=initial_digest,
                revisions=revisions,
            ),
            # Resolved PROPOSE decisions are terminal audit evidence, not
            # GraphRunRecord revisions.  Keep this parallel projection
            # content-free so the TUI and a future GUI cannot mistake a
            # rejected/unavailable candidate for executable topology.
            "proposals": tuple(
                {
                    # The durable proposal id is an opaque receipt identifier,
                    # not graph content.  Showing it lets an operator take the
                    # explicit CLI decision path without treating this
                    # read-only audit surface as an execution authority.
                    "proposal_id": str(item.get("proposal_id", ""))[:128],
                    "sequence": int(item.get("sequence", 0) or 0),
                    "ledger_sequence": int(item.get("ledger_sequence", 0) or 0),
                    "status": str(item.get("status", "unknown")),
                    "operation": str(item.get("operation", "unknown")),
                    "base_graph_version": int(
                        item.get("base_graph_version", 0) or 0
                    ),
                    "proposed_lease": dict(item.get("proposed_lease", {}))
                    if isinstance(item.get("proposed_lease"), Mapping)
                    else {},
                }
                for item in inspection.graph_proposal_decisions[:32]
            ),
        },
        "frozen_budget_envelope": _frozen_budget_envelope(inspection),
        "observed_execution": _observed_execution(inspection),
        # This is intentionally only a display hint. A screen cannot turn an
        # audit row into permission to resume the Job.
        "read_only_continuation": _read_only_continuation_candidate(inspection),
        "route_admissions": route_admissions,
        "model_invocations": model_invocations,
        "route_operator_projections": route_operator_projections,
        "recovery": {
            "state": recovery.recovery_state,
            "requires_new_kernel_attempt": recovery.requires_new_kernel_attempt,
            "provider_cancellation_receipt_count": (
                0
                if recovery.interruption_evidence is None
                else recovery.interruption_evidence.provider_cancellation_receipt_count
            ),
            "incomplete_cancellation_event_count": (
                0
                if recovery.interruption_evidence is None
                else recovery.interruption_evidence.malformed_provider_cancellation_event_count
            ),
            "timeout_terminal_run_count": (
                0
                if recovery.interruption_evidence is None
                else recovery.interruption_evidence.timeout_terminal_run_count
            ),
            "effect_recovery_disposition": (
                ""
                if recovery.effect_recovery is None
                else recovery.effect_recovery.disposition
            ),
            "remote_effect_resource_claims": tuple(
                {
                    "action_id": str(item.get("action_id", ""))[:128],
                    "case_status": str(item.get("case_status", ""))[:64],
                    "effect": str(item.get("effect", ""))[:64],
                    "next_action": str(item.get("next_action", ""))[:96],
                }
                for item in recovery.remote_effect_resource_claims[:64]
            ),
        },
        "checkpoints": checkpoints,
    }

def execute_job_audit_command(argument: str) -> ModernTerminalCommandResult:
    """Return the read-only retained-Job modal intent for the active surface."""

    selected_job_id = argument.strip()
    if selected_job_id and not _JOB_IDENTIFIER.fullmatch(selected_job_id):
        return ModernTerminalCommandResult(
            messages=("Usage: /job [retained-job-id]",),
        )
    return ModernTerminalCommandResult(
        open_job_audit=True,
        job_audit_job_id=selected_job_id,
        messages=(
            "ACTIVE JOB audit · select a retained Job to inspect immutable graph lineage and "
            "read-only checkpoints. Checkpoint execution resume is disabled.",
        ),
    )
