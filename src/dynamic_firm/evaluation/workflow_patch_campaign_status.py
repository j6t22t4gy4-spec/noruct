from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm import __version__
from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    EvidenceSource,
    OrganizationEpisode,
    WorkflowPatchCandidate,
    WorkflowPatchStatus,
)
from dynamic_firm.company.models import content_digest
from dynamic_firm.compiler import CompilerExecutionProfile
from dynamic_firm.providers.codex_exec import CodexExecProvider, CodexLoginStatus
from dynamic_firm.runtime.models import to_primitive, utc_now
from dynamic_firm.runtime.ports import ModelProviderError, OperationCancelled

from .causal_workflow import run_causal_workflow_evaluation
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
from .workflow_patch_live import (
    WORKFLOW_PATCH_CONTEXT,
    WORKFLOW_PATCH_FAMILY,
    WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS,
    WORKFLOW_PATCH_QUALITY_GAIN_THRESHOLD,
    WORKFLOW_PATCH_STRATEGIES,
    LiveWorkflowPatchConfig,
    LiveWorkflowPatchRecord,
    live_workflow_patch_record_to_json,
    load_live_workflow_patch_record,
    run_live_workflow_patch_evaluation,
    workflow_patch_benchmark_revision,
    workflow_patch_candidate_prior,
    workflow_patch_fixture_revision,
    workflow_patch_live_identity,
    workflow_patch_matched_context_hash,
    workflow_patch_memory_revision,
    workflow_patch_pattern_id,
    workflow_patch_template,
)


WORKFLOW_PATCH_COHORT_MANIFEST_SCHEMA = (
    "noruct.workflow-patch-live-cohort-manifest.v1"
)
WORKFLOW_PATCH_COHORT_PREFLIGHT_SCHEMA = (
    "noruct.workflow-patch-live-cohort-preflight.v1"
)
WORKFLOW_PATCH_COHORT_STATUS_SCHEMA = (
    "noruct.workflow-patch-live-cohort-status.v1"
)
WORKFLOW_PATCH_COHORT_LEDGER_SCHEMA = (
    "noruct.workflow-patch-live-cohort-ledger.v1"
)
WORKFLOW_PATCH_COHORT_FAILURE_SCHEMA = (
    "noruct.workflow-patch-live-cohort-failure.v1"
)
WORKFLOW_PATCH_COHORT_COMPARISON_SCHEMA = (
    "noruct.workflow-patch-live-cohort-comparison.v1"
)
_COHORT_DB = "workflow-patch-cohort.db"
_COMPANY_DB = "isolated-company.db"
_MAX_RECORDS = 4
_SLOTS = (
    ("baseline", "generic-post-gap"),
    ("observation-1", "candidate-prior-observation-1"),
    ("observation-2", "candidate-prior-observation-2"),
    ("patched", "applied-workflow-patch"),
)



from .workflow_patch_campaign_contracts import (
    _MAX_RECORDS,
    WORKFLOW_PATCH_COHORT_STATUS_SCHEMA,
    WorkflowPatchCohortState,
    WorkflowPatchCohortStatus,
    WorkflowPatchCohortStore,
)
from .workflow_patch_campaign_primitives import (
    _campaign_artifacts,
    _company_store,
    _manifest_fresh,
    _sealed_path,
    _validate_failure,
    _validate_record,
)

def workflow_patch_cohort_status(
    directory: str | Path,
) -> WorkflowPatchCohortStatus:
    with WorkflowPatchCohortStore(directory) as store:
        metadata, manifest, preflight, _ = _campaign_artifacts(store)
        events = store.events()
        root = store.directory
    slots = tuple((item.slot, item.strategy) for item in manifest.expected_runs)
    started: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    recorded: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    failed: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    interrupted_terminal: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    qualified: dict[tuple[str, str], LiveWorkflowPatchRecord] = {}
    for event in events:
        if event.fixture is None or event.strategy is None:
            continue
        key = (event.fixture, event.strategy)
        if key not in slots:
            raise ValueError("Workflow Patch ledger contains an unknown slot")
        if event.kind == CampaignEventKind.RUN_STARTED:
            if key in started:
                raise ValueError("Workflow Patch cohort reuses a run slot")
            started[key] = event
        elif event.kind == CampaignEventKind.RUN_RECORDED:
            if key not in started or key in recorded or key in failed:
                raise ValueError("Workflow Patch record has no unique start")
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
                raise ValueError("Workflow Patch failure has no unique start")
            if event.kind == CampaignEventKind.RUN_INTERRUPTED:
                interrupted_terminal[key] = event
            else:
                failed[key] = event
    for key, event in recorded.items():
        path = _sealed_path(root, event.payload.get("record_path"), "records-v1")
        if _sha256_file(path) != event.payload.get("record_file_sha256"):
            raise ValueError("Workflow Patch sealed record changed")
        record = _validate_record(path, manifest, strategy=key[1])
        qualified[key] = record
        if (
            record.content_hash != event.payload.get("record_content_hash")
            or record.identity.run_id != event.payload.get("evaluation_run_id")
            or record.task_success != event.payload.get("task_success")
            or record.external_model_calls
            != event.payload.get("external_model_calls")
            or record.artifact.quality_score
            != event.payload.get("artifact_quality_score")
            or bool(record.prior_exposed_ids)
            != event.payload.get("prior_exposed")
            or bool(record.prior_aligned_ids)
            != event.payload.get("prior_aligned")
        ):
            raise ValueError("Workflow Patch ledger projection changed")
    for key, event in {**failed, **interrupted_terminal}.items():
        path = _sealed_path(root, event.payload.get("failure_path"), "failures-v1")
        if _sha256_file(path) != event.payload.get("failure_file_sha256"):
            raise ValueError("Workflow Patch sealed failure changed")
        _validate_failure(
            path,
            manifest,
            slot=key[0],
            strategy=key[1],
        )
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
    for key, record in qualified.items():
        if not record.task_success:
            stop_reason = f"{key[0].upper()}_TASK_FAILED"
            break
        if not record.validation.passed:
            stop_reason = f"{key[0].upper()}_VALIDATION_FAILED"
            break
        if not record.safety.passed:
            stop_reason = f"{key[0].upper()}_SAFETY_FAILED"
            break
        if record.cost.tool_calls != 0:
            stop_reason = f"{key[0].upper()}_TOOL_BOUNDARY_FAILED"
            break
    patches: tuple[WorkflowPatchCandidate, ...]
    with _company_store(root, metadata) as company:
        patches = company.list_patches()
        playbook = company.playbook()
    if len(patches) > 1:
        raise ValueError("Workflow Patch cohort created more than one candidate")
    patch = patches[0] if patches else None
    completed = len(recorded)
    if completed >= 3 and patch is None and not failed and not interrupted:
        stop_reason = "REPEATED_EVIDENCE_DID_NOT_CREATE_CANDIDATE"
    if patch is not None:
        if patch.pattern.pattern_id != manifest.candidate_pattern_id:
            raise ValueError("Workflow Patch candidate identity drifted")
        if patch.status == WorkflowPatchStatus.APPLIED:
            if (
                playbook.revision != manifest.applied_playbook_revision
                or len(playbook.patterns) != 1
            ):
                raise ValueError("Workflow Patch applied playbook state drifted")
        elif patch.status == WorkflowPatchStatus.ROLLED_BACK:
            if playbook.patterns:
                raise ValueError("Workflow Patch rollback did not clear the playbook")
    fresh = _manifest_fresh(manifest)
    external_calls = sum(item.external_model_calls for item in qualified.values())
    if external_calls > manifest.max_model_calls_cohort:
        raise ValueError("Workflow Patch cohort call budget changed")
    next_expected = (
        manifest.expected_runs[completed]
        if completed < len(manifest.expected_runs)
        else None
    )
    if not preflight.ready or not fresh:
        state = WorkflowPatchCohortState.BLOCKED
        action = "refresh-preflight"
    elif failed or stop_reason is not None:
        state = WorkflowPatchCohortState.PARTIAL_FAILED
        action = "inspect-failure-and-rollback-if-applied"
    elif interrupted:
        state = WorkflowPatchCohortState.INTERRUPTED
        action = "inspect-interrupted-slot"
    elif running:
        state = WorkflowPatchCohortState.RUNNING
        action = "wait-for-current-slot"
    elif completed == _MAX_RECORDS:
        if patch is not None and patch.status == WorkflowPatchStatus.ROLLED_BACK:
            state = WorkflowPatchCohortState.ROLLED_BACK
            action = "archive-cohort"
        else:
            state = WorkflowPatchCohortState.COMPLETE
            action = "compare"
    elif completed >= 3:
        if patch is None or patch.status == WorkflowPatchStatus.PROPOSED:
            state = WorkflowPatchCohortState.AWAITING_APPROVAL
            action = "patch-preview-then-patch-approve"
        elif patch.status == WorkflowPatchStatus.APPROVED:
            state = WorkflowPatchCohortState.AWAITING_APPLY
            action = "patch-apply"
        elif patch.status == WorkflowPatchStatus.APPLIED:
            state = WorkflowPatchCohortState.READY
            action = "run-next"
        else:
            state = WorkflowPatchCohortState.BLOCKED
            action = "inspect-patch-state"
    else:
        state = WorkflowPatchCohortState.READY
        action = "run-next"
    next_allowed = (
        state == WorkflowPatchCohortState.READY and next_expected is not None
    )
    return WorkflowPatchCohortStatus(
        schema_version=WORKFLOW_PATCH_COHORT_STATUS_SCHEMA,
        campaign_id=manifest.campaign_id,
        state=state,
        manifest_content_hash=manifest.content_hash,
        manifest_fresh=fresh,
        viable=stop_reason is None and not failed and not interrupted,
        stop_reason=stop_reason,
        completed_runs=completed,
        expected_runs=_MAX_RECORDS,
        failed_runs=len(failed),
        interrupted_runs=interrupted,
        next_slot=next_expected.slot if next_allowed else None,
        next_strategy=next_expected.strategy if next_allowed else None,
        max_model_calls_for_next_run=(
            manifest.max_model_calls_per_run if next_allowed else 0
        ),
        max_wall_time_ms_for_next_run=(
            manifest.max_wall_time_ms_per_run if next_allowed else 0
        ),
        explicit_quota_confirmation_required=next_allowed,
        external_model_calls_recorded=external_calls,
        patch_id=patch.patch_id if patch else None,
        patch_status=patch.status.value if patch else None,
        operator_action=action,
        event_count=len(events),
        ledger_verified=True,
        record_paths=tuple(
            str(root / str(recorded[slot].payload["record_path"]))
            for slot in slots
            if slot in recorded
        ),
    )



