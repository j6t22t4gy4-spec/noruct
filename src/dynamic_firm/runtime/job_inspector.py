from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Mapping

from dynamic_firm._vendor.paperclip_runtime.timeline import (
    TimelineWindow,
    normalize_event_limit,
    normalize_timeline_window,
)
from dynamic_firm.runtime.models import Usage

from .job_inspector_checkpoints import ActiveJobCheckpointMixin
from .job_inspector_recovery import ActiveJobRecoveryMixin
from .job_ledger_primitives import (
    ActiveJobAuditStatus,
    ActiveJobInspection,
    ActiveJobRuntimeRun,
    ActiveJobSummary,
    ActiveJobTimeline,
    SNAPSHOT_SCHEMA,
    TERMINAL_SCHEMA,
    _SUPPORTED_SNAPSHOT_SCHEMAS,
    _SUPPORTED_TERMINAL_SCHEMAS,
    _digest,
    _graph_blueprint_identity,
    _operating_identity,
    _operator_timeline_event,
    _payload,
    _planning_identity,
    _record_content_hash_valid,
    _safe_terminal_graph_proposal_decisions,
    _work_order_identity,
)
from .store import RunStore, job_chain_digest
from .job_inspector_receipts import (
    safe_continuation_preflight_receipts,
    safe_tool_receipts,
)
from .job_inspector_delivery import (
    safe_final_task_capabilities,
    validation_receipts,
)


def _safe_evolution_artifact_pins(
    snapshot: Mapping[str, Any],
    *,
    errors: list[str],
) -> tuple[Mapping[str, str], ...]:
    """Validate immutable artifact identities without projecting manifests."""

    raw = snapshot.get("evolution_artifact_pins", ())
    if not isinstance(raw, (tuple, list)) or len(raw) > 64:
        errors.append("snapshot Evolution Artifact pins malformed")
        return ()
    pins: list[Mapping[str, str]] = []
    identities: set[tuple[str, str, str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            errors.append("snapshot Evolution Artifact pin malformed")
            continue
        projected = {
            key: str(item.get(key, ""))
            for key in ("kind", "artifact_id", "version", "manifest_digest", "scope_key")
        }
        if (
            any(not projected[key] or len(projected[key]) > 160 for key in ("kind", "artifact_id", "version", "scope_key"))
            or len(projected["manifest_digest"]) != 64
            or any(character not in "0123456789abcdef" for character in projected["manifest_digest"])
        ):
            errors.append("snapshot Evolution Artifact pin invalid")
            continue
        identity = (
            projected["scope_key"],
            projected["kind"],
            projected["artifact_id"],
            projected["version"],
        )
        if identity in identities:
            errors.append("snapshot Evolution Artifact pin duplicated")
            continue
        identities.add(identity)
        pins.append(projected)
    return tuple(sorted(pins, key=lambda item: (item["scope_key"], item["kind"], item["artifact_id"], item["version"])))


class ActiveJobInspector(ActiveJobRecoveryMixin, ActiveJobCheckpointMixin):
    """Provider-free hash, relation, and terminal-aggregate replay."""

    def __init__(
        self,
        store: RunStore,
        *,
        company_coordination: RemoteCompanyCoordinationClient | None = None,
    ) -> None:
        self.store = store
        self.company_coordination = company_coordination

    def _runtime_run_projection(
        self,
        job_id: str,
    ) -> tuple[ActiveJobRuntimeRun, ...]:
        """Join live run state without copying request, prompt, or tool payloads.

        The ACTIVE JOB hash chain remains the durable Kernel audit authority.
        This projection merely lets an operator see a currently running or
        approval-waiting employee before that attempt reaches its terminal
        ledger record.
        """

        projected = []
        for run in self.store.list_job_runs(job_id):
            run_id = str(run["run_id"])
            projected.append(
                ActiveJobRuntimeRun(
                    run_id=run_id,
                    task_id=str(run["task_id"]),
                    employee_id=str(run["employee_id"]),
                    status=str(run["status"]),
                    created_at=str(run["created_at"]),
                    updated_at=str(run["updated_at"]),
                    pending_approval_count=len(
                        self.store.list_pending_approvals(run_id)
                    ),
                )
            )
        return tuple(projected)

    def timeline(
        self,
        job_id: str,
        *,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        limit: object = None,
        now: datetime | None = None,
    ) -> ActiveJobTimeline:
        """Project bounded lifecycle facts without exposing event payloads.

        The hash-chain inspection remains the durable authority.  The timeline
        only exposes event identity, scalar usage deltas, and the already
        redacted terminal summary used by the operator surface.
        """

        inspection = self.inspect(job_id)
        window: TimelineWindow = normalize_timeline_window(
            from_at=from_at,
            to_at=to_at,
            now=now,
        )
        event_limit = normalize_event_limit(limit)
        events, truncated = self.store.list_job_events_window(
            job_id,
            from_at=window.from_at,
            to_at=window.to_at,
            limit=event_limit,
        )
        runtime_runs = self.store.list_job_runs(job_id)
        usage = Usage()
        for run in runtime_runs:
            usage = usage.plus(self.store.get_usage(str(run["run_id"])))
        return ActiveJobTimeline(
            job_id=job_id,
            audit_status=inspection.audit_status,
            window_from=window.from_at.isoformat(),
            window_to=window.to_at.isoformat(),
            window_capped=window.capped,
            event_limit=event_limit,
            event_count=len(events),
            truncated=truncated,
            runtime_run_count=len(runtime_runs),
            job_usage=usage,
            events=tuple(_operator_timeline_event(event) for event in events),
        )

    def inspect(self, job_id: str) -> ActiveJobInspection:
        rows = self.store.get_job_ledger_rows(job_id)
        if rows is None:
            raise KeyError(f"Unknown ACTIVE JOB: {job_id}")

        snapshot_row = rows["snapshot"]
        errors: list[str] = []
        try:
            snapshot = _payload(snapshot_row)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            snapshot = {}
            errors.append(f"snapshot payload invalid: {type(exc).__name__}")
        snapshot_payload_hash = _digest(snapshot)
        if snapshot_payload_hash != str(snapshot_row["payload_hash"]):
            errors.append("snapshot payload hash mismatch")
        expected_snapshot_chain = job_chain_digest(
            "GENESIS", "SNAPSHOT", snapshot_payload_hash
        )
        if expected_snapshot_chain != str(snapshot_row["chain_hash"]):
            errors.append("snapshot chain hash mismatch")
        snapshot_schema = str(snapshot.get("schema_version", ""))
        if snapshot_schema not in _SUPPORTED_SNAPSHOT_SCHEMAS:
            errors.append("snapshot schema version mismatch")
        snapshot_task_count = sum(
            1
            for task in snapshot.get("tasks", ())
            if isinstance(task, Mapping) and task.get("task_id")
        )
        snapshot_operating = _operating_identity(
            snapshot,
            schema_version=snapshot_schema,
            task_count=snapshot_task_count,
            label="snapshot",
            errors=errors,
        )
        snapshot_planning = _planning_identity(
            snapshot,
            schema_version=snapshot_schema,
            label="snapshot",
            errors=errors,
        )
        snapshot_work_order = _work_order_identity(
            snapshot,
            schema_version=snapshot_schema,
            label="snapshot",
            errors=errors,
        )
        snapshot_blueprint = _graph_blueprint_identity(
            snapshot,
            label="snapshot",
            errors=errors,
        )

        attempts_by_id: dict[str, Mapping[str, Any]] = {}
        mutation_targets: dict[str, Mapping[str, Any]] = {}
        task_states: dict[str, dict[str, Any]] = {
            str(task.get("task_id", "")): {
                "task_id": str(task.get("task_id", "")),
                "status": "PENDING",
                "assignee_id": None,
                "attempt": 1,
            }
            for task in snapshot.get("tasks", ())
            if isinstance(task, Mapping) and task.get("task_id")
        }
        attempt_payloads: list[Mapping[str, Any]] = []
        mutation_payloads: list[Mapping[str, Any]] = []
        graph_patch_payloads: list[Mapping[str, Any]] = []
        graph_proposal_payloads: list[Mapping[str, Any]] = []
        mutation_sequence = 0
        graph_patch_sequence = 0
        reconstructed_graph_version = int(snapshot.get("graph_version", 0) or 0)
        reconstructed_final_task_id = str(snapshot.get("final_task_id", ""))
        expected_ledger_seq = 1
        previous_hash = str(snapshot_row["chain_hash"])

        ordered: list[tuple[int, str, Mapping[str, Any]]] = []
        ordered.extend(
            (int(row["ledger_seq"]), "ATTEMPT", row) for row in rows["attempts"]
        )
        ordered.extend(
            (int(row["ledger_seq"]), "MUTATION", row) for row in rows["mutations"]
        )
        ordered.extend(
            (int(row["ledger_seq"]), "GRAPH_PATCH", row)
            for row in rows["graph_patches"]
        )
        ordered.extend(
            (int(row["ledger_seq"]), "GRAPH_PROPOSAL", row)
            for row in rows.get("graph_proposals", ())
        )
        if rows["terminal"] is not None:
            ordered.append(
                (int(rows["terminal"]["ledger_seq"]), "TERMINAL", rows["terminal"])
            )
        ordered.sort(key=lambda item: item[0])

        terminal_payload: Mapping[str, Any] | None = None
        chain_head = previous_hash
        for ledger_seq, event_type, row in ordered:
            if ledger_seq != expected_ledger_seq:
                errors.append(
                    f"ledger sequence mismatch: expected {expected_ledger_seq}, got {ledger_seq}"
                )
                expected_ledger_seq = ledger_seq
            expected_ledger_seq += 1
            try:
                payload = _payload(row)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                errors.append(
                    f"{event_type.lower()} payload invalid at {ledger_seq}: {type(exc).__name__}"
                )
                payload = {}
            payload_hash = _digest(payload)
            if payload_hash != str(row["payload_hash"]):
                errors.append(f"{event_type.lower()} payload hash mismatch at {ledger_seq}")
            if str(row["previous_chain_hash"]) != previous_hash:
                errors.append(f"{event_type.lower()} previous chain mismatch at {ledger_seq}")
            expected_chain = job_chain_digest(previous_hash, event_type, payload_hash)
            if expected_chain != str(row["chain_hash"]):
                errors.append(f"{event_type.lower()} chain hash mismatch at {ledger_seq}")
            previous_hash = str(row["chain_hash"])
            chain_head = previous_hash

            if event_type == "ATTEMPT":
                if not _record_content_hash_valid(payload):
                    errors.append(f"attempt content hash mismatch at {ledger_seq}")
                attempt_id = str(payload.get("attempt_id", ""))
                task_id = str(payload.get("task_id", ""))
                sequence = int(payload.get("sequence", 0) or 0)
                employee_id = str(payload.get("employee_id", ""))
                source_id = payload.get("source_attempt_id")
                if not attempt_id or attempt_id in attempts_by_id:
                    errors.append(f"attempt identity invalid at {ledger_seq}")
                if str(payload.get("frozen_snapshot_hash", "")) != str(
                    snapshot.get("frozen_snapshot_hash", "")
                ):
                    errors.append(f"attempt snapshot mismatch at {ledger_seq}")
                if source_id is None:
                    if sequence != 1:
                        errors.append(f"initial attempt sequence invalid at {ledger_seq}")
                else:
                    source = attempts_by_id.get(str(source_id))
                    mutation = mutation_targets.get(attempt_id)
                    if source is None or mutation is None:
                        errors.append(f"attempt source relation missing at {ledger_seq}")
                    elif (
                        str(source.get("task_id", "")) != task_id
                        or int(source.get("sequence", 0)) + 1 != sequence
                        or str(mutation.get("source_attempt_id", "")) != str(source_id)
                        or str(mutation.get("to_employee_id", "")) != employee_id
                    ):
                        errors.append(f"attempt source relation mismatch at {ledger_seq}")
                attempts_by_id[attempt_id] = payload
                attempt_payloads.append(payload)
                status = str(payload.get("status", ""))
                task_states[task_id] = {
                    "task_id": task_id,
                    "status": (
                        "SUCCEEDED"
                        if status == "SUCCEEDED"
                        else "CANCELLED"
                        if status == "CANCELLED"
                        else "FAILED"
                    ),
                    "assignee_id": employee_id,
                    "attempt": sequence,
                }
            elif event_type == "MUTATION":
                if not _record_content_hash_valid(payload):
                    errors.append(f"mutation content hash mismatch at {ledger_seq}")
                mutation_sequence += 1
                source_id = str(payload.get("source_attempt_id", ""))
                target_id = str(payload.get("target_attempt_id", ""))
                source = attempts_by_id.get(source_id)
                if int(payload.get("sequence", 0) or 0) != mutation_sequence:
                    errors.append(f"mutation sequence mismatch at {ledger_seq}")
                if source is None:
                    errors.append(f"mutation source missing at {ledger_seq}")
                elif (
                    str(source.get("task_id", "")) != str(payload.get("task_id", ""))
                    or int(source.get("sequence", 0))
                    != int(payload.get("source_attempt_sequence", 0) or 0)
                    or str(source.get("employee_id", ""))
                    != str(payload.get("from_employee_id", ""))
                    or str(source.get("failure_kind", ""))
                    != str(payload.get("failure_kind", ""))
                    or str(source.get("content_hash", ""))
                    != str(payload.get("source_attempt_content_hash", ""))
                ):
                    errors.append(f"mutation source mismatch at {ledger_seq}")
                if target_id in mutation_targets or target_id in attempts_by_id:
                    errors.append(f"mutation target duplicated at {ledger_seq}")
                if int(payload.get("target_attempt_sequence", 0) or 0) != int(
                    payload.get("source_attempt_sequence", 0) or 0
                ) + 1:
                    errors.append(f"mutation target sequence mismatch at {ledger_seq}")
                mutation_type = str(payload.get("mutation_type", ""))
                from_employee = str(payload.get("from_employee_id", ""))
                to_employee = str(payload.get("to_employee_id", ""))
                if mutation_type == "RETRY" and from_employee != to_employee:
                    errors.append(f"retry employee transition invalid at {ledger_seq}")
                elif mutation_type == "REROUTE" and from_employee == to_employee:
                    errors.append(f"reroute employee transition invalid at {ledger_seq}")
                elif mutation_type not in {"RETRY", "REROUTE"}:
                    errors.append(f"mutation type invalid at {ledger_seq}")
                if int(payload.get("mutation_budget_after", -1)) != int(
                    payload.get("mutation_budget_before", -1)
                ) - 1:
                    errors.append(f"mutation budget invalid at {ledger_seq}")
                mutation_targets[target_id] = payload
                mutation_payloads.append(payload)
                task_id = str(payload.get("task_id", ""))
                task_states[task_id] = {
                    "task_id": task_id,
                    "status": "PENDING",
                    "assignee_id": None,
                    "attempt": int(payload.get("target_attempt_sequence", 0) or 0),
                }
            elif event_type == "GRAPH_PATCH":
                if not _record_content_hash_valid(payload):
                    errors.append(f"graph patch content hash mismatch at {ledger_seq}")
                mutation_lease = payload.get("mutation_lease", {})
                if mutation_lease is None:
                    mutation_lease = {}
                if not isinstance(mutation_lease, Mapping):
                    errors.append(f"graph patch mutation lease malformed at {ledger_seq}")
                    mutation_lease = {}
                else:
                    lease_model_calls = mutation_lease.get("model_calls", 0)
                    lease_tool_calls = mutation_lease.get("tool_calls", 0)
                    lease_cost_usd = mutation_lease.get("cost_usd", 0.0)
                    if (
                        type(lease_model_calls) is not int
                        or lease_model_calls < 0
                        or type(lease_tool_calls) is not int
                        or lease_tool_calls < 0
                        or isinstance(lease_cost_usd, bool)
                        or not isinstance(lease_cost_usd, (int, float))
                        or not math.isfinite(float(lease_cost_usd))
                        or float(lease_cost_usd) < 0
                    ):
                        errors.append(f"graph patch mutation lease invalid at {ledger_seq}")
                graph_patch_sequence += 1
                patch = payload.get("patch")
                if not isinstance(patch, Mapping):
                    errors.append(f"graph patch payload is malformed at {ledger_seq}")
                    patch = {}
                if int(payload.get("sequence", 0) or 0) != graph_patch_sequence:
                    errors.append(f"graph patch sequence mismatch at {ledger_seq}")
                base_version = int(patch.get("base_graph_version", 0) or 0)
                target_version = int(payload.get("target_graph_version", 0) or 0)
                if (
                    base_version != reconstructed_graph_version
                    or target_version != base_version + 1
                ):
                    errors.append(f"graph patch version mismatch at {ledger_seq}")
                reconstructed_graph_version = target_version
                operations = patch.get("operations", ())
                if not isinstance(operations, (list, tuple)) or not operations:
                    errors.append(f"graph patch operations missing at {ledger_seq}")
                    operations = ()
                added_ids: list[str] = []
                cancelled_ids: list[str] = []
                for operation in operations:
                    if not isinstance(operation, Mapping):
                        errors.append(f"graph patch operation malformed at {ledger_seq}")
                        continue
                    kind = str(operation.get("kind", ""))
                    if kind == "ADD_TASK":
                        task = operation.get("task")
                        task_id = str(task.get("task_id", "")) if isinstance(task, Mapping) else ""
                        if not task_id or task_id in task_states:
                            errors.append(f"graph patch added task invalid at {ledger_seq}")
                            continue
                        task_states[task_id] = {
                            "task_id": task_id,
                            "status": "PENDING",
                            "assignee_id": None,
                            "attempt": 1,
                        }
                        added_ids.append(task_id)
                    elif kind == "CANCEL_TASK":
                        task_id = str(operation.get("task_id", ""))
                        current = task_states.get(task_id)
                        if current is None or current.get("status") != "PENDING":
                            errors.append(f"graph patch cancelled task invalid at {ledger_seq}")
                            continue
                        task_states[task_id] = {
                            "task_id": task_id,
                            "status": "CANCELLED",
                            "assignee_id": None,
                            "attempt": int(current.get("attempt", 1) or 1),
                        }
                        cancelled_ids.append(task_id)
                    elif kind == "SET_FINAL_TASK":
                        reconstructed_final_task_id = str(operation.get("task_id", ""))
                if tuple(sorted(added_ids)) != tuple(payload.get("added_task_ids", ())):
                    errors.append(f"graph patch added-task aggregate mismatch at {ledger_seq}")
                if tuple(sorted(cancelled_ids)) != tuple(payload.get("cancelled_task_ids", ())):
                    errors.append(f"graph patch cancelled-task aggregate mismatch at {ledger_seq}")
                graph_patch_payloads.append(payload)
            elif event_type == "GRAPH_PROPOSAL":
                if not _record_content_hash_valid(payload):
                    errors.append(f"graph proposal content hash mismatch at {ledger_seq}")
                graph_proposal_payloads.append(
                    {"ledger_sequence": ledger_seq, **payload}
                )
            else:
                terminal_payload = payload

        dangling_targets = set(mutation_targets) - set(attempts_by_id)
        terminal_row = rows["terminal"]
        replay_matches = not errors
        terminal_operating: dict[str, str] | None = None
        terminal_planning: dict[str, Any] | None = None
        terminal_work_order: dict[str, str] | None = None
        terminal_blueprint: dict[str, Any] | None = None
        graph_proposal_decisions: tuple[Mapping[str, object], ...] = ()
        if terminal_payload is not None:
            terminal_schema = str(terminal_payload.get("schema_version", ""))
            if terminal_schema not in _SUPPORTED_TERMINAL_SCHEMAS:
                errors.append("terminal schema version mismatch")
            terminal_operating = _operating_identity(
                terminal_payload,
                schema_version=terminal_schema,
                task_count=len(task_states),
                label="terminal",
                errors=errors,
            )
            terminal_planning = _planning_identity(
                terminal_payload,
                schema_version=terminal_schema,
                label="terminal",
                errors=errors,
            )
            terminal_work_order = _work_order_identity(
                terminal_payload,
                schema_version=terminal_schema,
                label="terminal",
                errors=errors,
            )
            terminal_blueprint = _graph_blueprint_identity(
                terminal_payload,
                label="terminal",
                errors=errors,
            )
            if (
                snapshot_schema == SNAPSHOT_SCHEMA
                and terminal_schema == TERMINAL_SCHEMA
            ):
                for key in (
                    "initial_company_work_mode",
                    "coordination_policy",
                    "requested_effect",
                    "operating_reason",
                ):
                    if terminal_operating[key] != snapshot_operating[key]:
                        errors.append(f"terminal operating {key} mismatch")
                for key in (
                    "planning_mode",
                    "planning_reason",
                    "compiler_usage",
                    "compiler_provider_request_id",
                ):
                    if terminal_planning[key] != snapshot_planning[key]:
                        errors.append(f"terminal planning {key} mismatch")
                for key in (
                    "work_order_id",
                    "work_order_digest",
                    "work_order_authority_digest",
                    "firm_admission_digest",
                ):
                    if terminal_work_order[key] != snapshot_work_order[key]:
                        errors.append(f"terminal work order {key} mismatch")
                for key in (
                    "blueprint_id",
                    "blueprint_version",
                    "blueprint_digest",
                    "mutation_policy",
                    "constraints_digest",
                    "constraints",
                ):
                    if terminal_blueprint[key] != snapshot_blueprint[key]:
                        errors.append(f"terminal graph Blueprint {key} mismatch")
            if int(terminal_payload.get("task_attempt_count", -1)) != len(
                attempt_payloads
            ):
                errors.append("terminal attempt aggregate mismatch")
            if int(terminal_payload.get("task_mutation_count", -1)) != len(
                mutation_payloads
            ):
                errors.append("terminal mutation aggregate mismatch")
            if int(
                terminal_payload.get("graph_patch_count", len(graph_patch_payloads))
            ) != len(
                graph_patch_payloads
            ):
                errors.append("terminal graph patch aggregate mismatch")
            terminal_proposal_decisions = _safe_terminal_graph_proposal_decisions(
                terminal_payload,
                errors=errors,
            )
            if graph_proposal_payloads:
                durable_payload = {
                    "graph_proposal_decision_count": len(graph_proposal_payloads),
                    "graph_proposal_decisions": tuple(
                        {
                            "status": item.get("status"),
                            "semantic_operation": (
                                item.get("patch", {}).get("semantic_operation")
                                if isinstance(item.get("patch"), Mapping)
                                else None
                            ),
                            "base_graph_version": (
                                item.get("patch", {}).get("base_graph_version")
                                if isinstance(item.get("patch"), Mapping)
                                else None
                            ),
                            "proposed_lease": item.get("proposed_lease"),
                        }
                        for item in graph_proposal_payloads
                    ),
                }
                durable_decisions = _safe_terminal_graph_proposal_decisions(
                    durable_payload,
                    errors=errors,
                )
                if terminal_proposal_decisions != durable_decisions:
                    errors.append("terminal graph proposal evidence mismatch")
                graph_proposal_decisions = tuple(
                    {
                        **decision,
                        "ledger_sequence": int(
                            graph_proposal_payloads[index].get("ledger_sequence", 0)
                        ),
                    }
                    for index, decision in enumerate(durable_decisions)
                )
            else:
                graph_proposal_decisions = terminal_proposal_decisions
            if int(
                terminal_payload.get("metrics", {}).get("task_mutation_count", -1)
            ) != len(mutation_payloads):
                errors.append("terminal metrics mutation aggregate mismatch")
            if int(
                terminal_payload.get("metrics", {}).get(
                    "graph_patch_count", len(graph_patch_payloads)
                )
            ) != len(graph_patch_payloads):
                errors.append("terminal metrics graph patch aggregate mismatch")
            if dangling_targets:
                errors.append("terminal job has an unexecuted mutation target")
            terminal_tasks = {
                str(task.get("task_id", "")): dict(task)
                for task in terminal_payload.get("tasks", ())
                if isinstance(task, Mapping) and task.get("task_id")
            }
            if terminal_tasks != task_states:
                errors.append("terminal task aggregate mismatch")
            observed_graph_version = max(
                [int(snapshot.get("graph_version", 0) or 0)]
                + [int(item.get("graph_version", 0) or 0) for item in attempt_payloads]
                + [int(item.get("target_graph_version", 0) or 0) for item in graph_patch_payloads]
            )
            if int(terminal_payload.get("final_graph_version", 0) or 0) != observed_graph_version:
                errors.append("terminal graph aggregate mismatch")
            usage_keys = (
                "model_calls",
                "tool_calls",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "cost_usd",
            )
            reconstructed_usage = {
                key: (
                    float(getattr(snapshot_planning["compiler_usage"], key))
                    if key == "cost_usd"
                    else int(getattr(snapshot_planning["compiler_usage"], key))
                )
                + sum(
                    float(item.get("usage", {}).get(key, 0.0))
                    if key == "cost_usd"
                    else int(item.get("usage", {}).get(key, 0))
                    for item in attempt_payloads
                )
                for key in usage_keys
            }
            terminal_usage = terminal_payload.get("metrics", {}).get("usage", {})
            if any(
                abs(float(terminal_usage.get(key, 0.0)) - float(value)) > 1e-9
                for key, value in reconstructed_usage.items()
            ):
                errors.append("terminal usage aggregate mismatch")
            terminal_final_task_id = str(
                terminal_payload.get("final_task_id") or reconstructed_final_task_id
            )
            if terminal_final_task_id != reconstructed_final_task_id:
                errors.append("terminal final task aggregate mismatch")
            final_task = task_states.get(terminal_final_task_id)
            if terminal_payload.get("status") == "SUCCEEDED" and (
                final_task is None or final_task.get("status") != "SUCCEEDED"
            ):
                errors.append("terminal success does not match the final task")
            replay_matches = not errors

        # A PROPOSE decision is useful to an operator while a Job is still
        # running too.  The durable row already has a chain position; only
        # terminal aggregate agreement must wait for terminalization.
        if graph_proposal_payloads and not graph_proposal_decisions:
            durable_payload = {
                "graph_proposal_decision_count": len(graph_proposal_payloads),
                "graph_proposal_decisions": tuple(
                    {
                        "status": item.get("status"),
                        "semantic_operation": (
                            item.get("patch", {}).get("semantic_operation")
                            if isinstance(item.get("patch"), Mapping)
                            else None
                        ),
                        "base_graph_version": (
                            item.get("patch", {}).get("base_graph_version")
                            if isinstance(item.get("patch"), Mapping)
                            else None
                        ),
                        "proposed_lease": item.get("proposed_lease"),
                    }
                    for item in graph_proposal_payloads
                ),
            }
            durable_decisions = _safe_terminal_graph_proposal_decisions(
                durable_payload,
                errors=errors,
            )
            graph_proposal_decisions = tuple(
                {
                    **decision,
                    "ledger_sequence": int(
                        graph_proposal_payloads[index].get("ledger_sequence", 0)
                    ),
                }
                for index, decision in enumerate(durable_decisions)
            )

        audit_status = (
            ActiveJobAuditStatus.INVALID
            if errors
            else ActiveJobAuditStatus.TERMINAL
            if terminal_row is not None
            else ActiveJobAuditStatus.INTERRUPTED
        )
        final_graph_version = (
            int(terminal_payload.get("final_graph_version", 0))
            if terminal_payload is not None
            else max(
                [int(snapshot.get("graph_version", 0) or 0)]
                + [int(item.get("graph_version", 0) or 0) for item in attempt_payloads]
                + [int(item.get("target_graph_version", 0) or 0) for item in graph_patch_payloads]
            )
        )
        effective_operating = terminal_operating or snapshot_operating
        replica_groups: dict[str, dict[str, Any]] = {}
        for task in snapshot.get("tasks", ()):
            if not isinstance(task, Mapping):
                continue
            replica = task.get("execution_replica")
            if not isinstance(replica, Mapping):
                continue
            group_id = str(replica.get("group_id", ""))
            if not group_id:
                continue
            group = replica_groups.setdefault(
                group_id,
                {
                    "group_id": group_id,
                    "strategy": str(replica.get("strategy", "")),
                    "aggregation_task_id": str(
                        replica.get("aggregation_task_id", "")
                    ),
                    "aggregation": str(replica.get("aggregation", "")),
                    "marginal_value_reason": str(
                        replica.get("marginal_value_reason", "")
                    ),
                    "member_task_ids": [],
                },
            )
            group["member_task_ids"].append(str(task.get("task_id", "")))
        safe_replica_groups = tuple(
            {
                **replica_groups[group_id],
                "member_task_ids": tuple(
                    sorted(replica_groups[group_id]["member_task_ids"])
                ),
            }
            for group_id in sorted(replica_groups)
        )
        evolution_artifact_pins = _safe_evolution_artifact_pins(
            snapshot,
            errors=errors,
        )
        tool_receipts = safe_tool_receipts(
            self.store.list_job_tool_receipts(job_id),
            errors=errors,
        )
        continuation_preflight_receipts = safe_continuation_preflight_receipts(
            self.store.list_job_continuation_preflight_receipts(job_id),
            errors=errors,
        )
        final_task_capabilities = safe_final_task_capabilities(
            snapshot,
            final_task_id=reconstructed_final_task_id,
            errors=errors,
        )
        if errors:
            audit_status = ActiveJobAuditStatus.INVALID
        return ActiveJobInspection(
            job_id=str(snapshot_row["job_id"]),
            request_id=str(snapshot_row["request_id"]),
            initial_company_work_mode=snapshot_operating[
                "initial_company_work_mode"
            ],
            company_work_mode=effective_operating["company_work_mode"],
            coordination_policy=snapshot_operating["coordination_policy"],
            requested_effect=snapshot_operating["requested_effect"],
            operating_reason=snapshot_operating["operating_reason"],
            planning_mode=snapshot_planning["planning_mode"],
            planning_reason=snapshot_planning["planning_reason"],
            compiler_usage=snapshot_planning["compiler_usage"],
            compiler_provider_request_id=snapshot_planning[
                "compiler_provider_request_id"
            ],
            work_order_id=snapshot_work_order["work_order_id"],
            work_order_digest=snapshot_work_order["work_order_digest"],
            work_order_authority_digest=snapshot_work_order[
                "work_order_authority_digest"
            ],
            firm_admission_digest=snapshot_work_order["firm_admission_digest"],
            graph_blueprint_id=snapshot_blueprint["blueprint_id"],
            graph_blueprint_version=snapshot_blueprint["blueprint_version"],
            graph_blueprint_digest=snapshot_blueprint["blueprint_digest"],
            graph_mutation_policy=snapshot_blueprint["mutation_policy"],
            graph_constraints_digest=snapshot_blueprint["constraints_digest"],
            initial_graph_digest=snapshot_blueprint["initial_graph_digest"],
            audit_status=audit_status,
            job_status=(
                None if terminal_payload is None else str(terminal_payload.get("status", ""))
            ),
            created_at=str(snapshot_row["created_at"]),
            frozen_snapshot_hash=str(snapshot_row["frozen_snapshot_hash"]),
            chain_head=chain_head,
            final_graph_version=final_graph_version,
            attempt_count=len(attempt_payloads),
            mutation_count=len(mutation_payloads),
            graph_patch_count=len(graph_patch_payloads),
            replay_matches=replay_matches and audit_status != ActiveJobAuditStatus.INVALID,
            reconstructed_tasks=tuple(task_states[key] for key in sorted(task_states)),
            attempts=tuple(attempt_payloads),
            mutations=tuple(mutation_payloads),
            graph_patches=tuple(graph_patch_payloads),
            graph_proposal_decisions=graph_proposal_decisions,
            terminal=terminal_payload,
            job_limits=(
                dict(snapshot.get("job_limits", {}))
                if isinstance(snapshot.get("job_limits"), Mapping)
                else {}
            ),
            execution_replica_groups=safe_replica_groups,
            errors=tuple(errors),
            evolution_artifact_pins=evolution_artifact_pins,
            runtime_runs=self._runtime_run_projection(job_id),
            tool_receipts=tool_receipts,
            continuation_preflight_receipts=continuation_preflight_receipts,
            final_task_id=reconstructed_final_task_id,
            final_task_capabilities=final_task_capabilities,
            validation_receipts=validation_receipts(self.store, job_id),
        )

    def list(self, limit: int = 20) -> tuple[ActiveJobSummary, ...]:
        summaries = []
        for row in self.store.list_job_snapshot_rows(limit):
            inspection = self.inspect(str(row["job_id"]))
            summaries.append(
                ActiveJobSummary(
                    job_id=inspection.job_id,
                    request_id=inspection.request_id,
                    company_work_mode=inspection.company_work_mode,
                    coordination_policy=inspection.coordination_policy,
                    requested_effect=inspection.requested_effect,
                    planning_mode=inspection.planning_mode,
                    work_order_id=inspection.work_order_id,
                    audit_status=inspection.audit_status,
                    job_status=inspection.job_status,
                    created_at=inspection.created_at,
                    attempt_count=inspection.attempt_count,
                    mutation_count=inspection.mutation_count,
                    graph_patch_count=inspection.graph_patch_count,
                    final_graph_version=inspection.final_graph_version,
                    chain_head=inspection.chain_head,
                )
            )
        return tuple(summaries)
