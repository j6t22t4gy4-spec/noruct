from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm import __version__
from dynamic_firm.company import (
    CompanyStateStore,
    WorkflowPatchStatus,
    WORKSPACE_STRUCTURE_PROJECTION_REVISION,
    WorkspaceProjectionError,
    project_workspace_structure,
    workflow_context_fingerprint_v2,
)
from dynamic_firm.company.models import content_digest
from dynamic_firm.compiler import CompilerExecutionProfile
from dynamic_firm.product import InputRoute, route_interactive_input
from dynamic_firm.providers.codex_exec import CodexExecProvider, CodexLoginStatus
from dynamic_firm.runtime.models import to_primitive, utc_now
from dynamic_firm.runtime.ports import (
    CancellationToken,
    ModelProviderError,
    OperationCancelled,
)
from dynamic_firm.runtime.tools import ToolValidationError, WorkspaceReadTools

from .causal_workflow import run_causal_workflow_evaluation
from .context_binding import (
    ExactContextBoundPreparation,
    ExactContextEvidenceBinding,
    create_exact_context_bound_preparation,
    create_exact_context_evidence_binding,
    exact_context_binding_to_json,
    load_exact_context_evidence_binding,
)
from .firm_value import wheel_distribution_sha256
from .firm_value_campaign import (
    CampaignEventKind,
    FirmValueCampaignEvent,
    FirmValueCampaignStore,
    _process_is_alive,
    _sha256_file,
    _write_private,
    probe_codex_structured_output,
    source_snapshot_revision,
)
from .information_boundary import InformationBoundaryCheck
from .workflow_patch_extension import (
    WorkflowPatchExtensionState,
    WorkflowPatchExtensionStore,
    _company_store as _extension_company_store,
    _extension_artifacts,
    workflow_patch_extension_status,
)
from .workflow_patch_live import (
    INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
    WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION,
    WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION,
    WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION,
    WORKFLOW_PATCH_COMPLETION_VALIDATOR_REVISION,
    WORKFLOW_PATCH_EFFICIENCY_STRATEGIES,
    WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES,
    WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES,
    WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS,
    LiveWorkflowPatchConfig,
    LiveWorkflowPatchRecord,
    WorkflowPatchCompletionAttemptProjection,
    live_workflow_patch_record_to_json,
    load_live_workflow_patch_record,
    run_live_workflow_patch_evaluation,
    workflow_patch_candidate_prior,
    workflow_patch_efficiency_benchmark_revision,
    workflow_patch_efficiency_matched_context_hash,
    workflow_patch_fixture_revision,
    workflow_patch_live_identity,
    workflow_patch_memory_revision,
    workflow_patch_pattern_id,
)


from .workflow_patch_efficiency_contracts import (
    _MAX_RECORDS,
    WORKFLOW_PATCH_EFFICIENCY_STATUS_SCHEMA,
    WorkflowPatchEfficiencyDiagnostic,
    WorkflowPatchEfficiencyState,
    WorkflowPatchEfficiencyStatus,
    WorkflowPatchEfficiencyStore,
)
from .workflow_patch_efficiency_primitives import (
    _load_diagnostic,
    _manifest_fresh,
    _pair_artifacts,
    _sealed_path,
    _validate_record,
    _verify_parent,
    _verify_runtime_inputs,
)

def workflow_patch_efficiency_status(
    directory: str | Path,
) -> WorkflowPatchEfficiencyStatus:
    with WorkflowPatchEfficiencyStore(directory) as store:
        metadata, manifest, preflight, _ = _pair_artifacts(store)
        events = store.events()
        root = store.directory
    parent = _verify_parent(manifest, metadata)
    parent_immutable = parent.semantic_anchor == manifest.parent_semantic_anchor
    slots = tuple((item.slot, item.strategy) for item in manifest.expected_runs)
    started: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    recorded: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    failed: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    interrupted_terminal: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    qualified: dict[
        tuple[str, str],
        tuple[LiveWorkflowPatchRecord, WorkflowPatchEfficiencyDiagnostic],
    ] = {}
    for event in events:
        if event.fixture is None or event.strategy is None:
            continue
        key = (event.fixture, event.strategy)
        if key not in slots:
            raise ValueError("Completion efficiency ledger has unknown slot")
        if event.kind == CampaignEventKind.RUN_STARTED:
            if key in started:
                raise ValueError("Completion efficiency reuses a run slot")
            started[key] = event
        elif event.kind == CampaignEventKind.RUN_RECORDED:
            if key not in started or key in recorded or key in failed:
                raise ValueError("Completion efficiency record has no unique start")
            recorded[key] = event
        elif event.kind in {
            CampaignEventKind.RUN_FAILED,
            CampaignEventKind.RUN_INTERRUPTED,
        }:
            if (
                key not in started
                or key in recorded
                or key in failed
                or key in interrupted_terminal
            ):
                raise ValueError(
                    "Completion efficiency failure has no unique start"
                )
            if event.kind == CampaignEventKind.RUN_INTERRUPTED:
                interrupted_terminal[key] = event
            else:
                failed[key] = event
    for key, event in recorded.items():
        record_path = _sealed_path(
            root,
            event.payload.get("record_path"),
            "records-v1",
        )
        diagnostic_path = _sealed_path(
            root,
            event.payload.get("diagnostic_path"),
            "diagnostics-v1",
        )
        if (
            _sha256_file(record_path)
            != event.payload.get("record_file_sha256")
            or _sha256_file(diagnostic_path)
            != event.payload.get("diagnostic_file_sha256")
        ):
            raise ValueError("Completion efficiency sealed record changed")
        record = _validate_record(record_path, manifest, strategy=key[1])
        diagnostic = _load_diagnostic(
            diagnostic_path,
            manifest,
            strategy=key[1],
            live_record_content_hash=record.content_hash,
        )
        qualified[key] = (record, diagnostic)
        if (
            record.content_hash != event.payload.get("record_content_hash")
            or diagnostic.content_hash
            != event.payload.get("diagnostic_content_hash")
            or record.identity.run_id
            != event.payload.get("evaluation_run_id")
            or record.task_success != event.payload.get("task_success")
            or record.external_model_calls
            != event.payload.get("external_model_calls")
            or diagnostic.repair_count != event.payload.get("repair_count")
        ):
            raise ValueError("Completion efficiency ledger projection changed")
    open_slots = {
        key: event
        for key, event in started.items()
        if key not in recorded
        and key not in failed
        and key not in interrupted_terminal
    }
    abandoned = sum(
        not _process_is_alive(event.payload.get("pid"))
        for event in open_slots.values()
    )
    interrupted = len(interrupted_terminal) + abandoned
    running = len(open_slots) - abandoned
    stop_reason: str | None = None
    for key, (record, _) in qualified.items():
        if not record.task_success:
            stop_reason = f"{key[0].upper()}_TASK_FAILED"
            break
        if not record.validation.passed:
            stop_reason = f"{key[0].upper()}_VALIDATION_FAILED"
            break
        if not record.safety.passed:
            stop_reason = f"{key[0].upper()}_SAFETY_FAILED"
            break
        if record.cost.tool_calls:
            stop_reason = f"{key[0].upper()}_TOOL_BOUNDARY_FAILED"
            break
    completed = len(recorded)
    fresh = _manifest_fresh(manifest)
    external_calls = sum(
        record.external_model_calls for record, _ in qualified.values()
    )
    if external_calls > manifest.max_model_calls_pair:
        raise ValueError("Completion efficiency pair call budget changed")
    if not preflight.ready:
        state = WorkflowPatchEfficiencyState.BLOCKED
        action = "fix-preflight"
    elif failed or stop_reason:
        state = WorkflowPatchEfficiencyState.PARTIAL_FAILED
        action = "inspect-failure-and-prepare-new-pair"
    elif interrupted:
        state = WorkflowPatchEfficiencyState.INTERRUPTED
        action = "inspect-interruption-and-prepare-new-pair"
    elif running:
        state = WorkflowPatchEfficiencyState.RUNNING
        action = "wait-for-current-slot"
    elif completed == _MAX_RECORDS:
        state = WorkflowPatchEfficiencyState.COMPLETE
        action = "compare-provider-free"
    elif not fresh or not parent_immutable:
        state = WorkflowPatchEfficiencyState.BLOCKED
        action = "prepare-fresh-pair"
    else:
        state = WorkflowPatchEfficiencyState.READY
        action = "confirm-one-live-slot"
    next_expected = (
        manifest.expected_runs[completed]
        if state == WorkflowPatchEfficiencyState.READY
        and completed < _MAX_RECORDS
        else None
    )
    return WorkflowPatchEfficiencyStatus(
        schema_version=WORKFLOW_PATCH_EFFICIENCY_STATUS_SCHEMA,
        pair_id=manifest.pair_id,
        state=state,
        manifest_content_hash=manifest.content_hash,
        manifest_fresh=fresh,
        parent_immutable=parent_immutable,
        viable=preflight.ready and parent_immutable and stop_reason is None,
        stop_reason=stop_reason,
        completed_runs=completed,
        expected_runs=_MAX_RECORDS,
        failed_runs=len(failed),
        interrupted_runs=interrupted,
        next_slot=next_expected.slot if next_expected else None,
        next_strategy=next_expected.strategy if next_expected else None,
        max_model_calls_for_next_run=(
            manifest.max_model_calls_per_run if next_expected else 0
        ),
        max_wall_time_ms_for_next_run=(
            manifest.max_wall_time_ms_per_run if next_expected else 0
        ),
        explicit_quota_confirmation_required=next_expected is not None,
        external_model_calls_recorded=external_calls,
        event_count=len(events),
        ledger_verified=True,
        record_paths=tuple(
            str(
                root
                / str(recorded[slot].payload["record_path"])
            )
            for slot in slots
            if slot in recorded
        ),
        diagnostic_paths=tuple(
            str(
                root
                / str(recorded[slot].payload["diagnostic_path"])
            )
            for slot in slots
            if slot in recorded
        ),
        operator_action=action,
    )

