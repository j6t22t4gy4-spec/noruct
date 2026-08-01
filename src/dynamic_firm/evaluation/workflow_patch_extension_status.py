from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm import __version__
from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    WorkflowPatchAssessment,
    WorkflowPatchAssessmentDecision,
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
from .workflow_patch_campaign import (
    WorkflowPatchCohortStore,
    _campaign_artifacts,
    _company_store as _parent_company_store,
    _episode,
    _validate_record as _validate_parent_record,
    workflow_patch_cohort_status,
)
from .workflow_patch_live import (
    WORKFLOW_PATCH_CONTEXT,
    WORKFLOW_PATCH_EXTENSION_STRATEGIES,
    WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS,
    LiveWorkflowPatchConfig,
    LiveWorkflowPatchRecord,
    live_workflow_patch_record_to_json,
    load_live_workflow_patch_record,
    run_live_workflow_patch_evaluation,
    workflow_patch_benchmark_revision,
    workflow_patch_fixture_revision,
    workflow_patch_live_identity,
    workflow_patch_matched_context_hash,
    workflow_patch_memory_revision,
    workflow_patch_pattern_id,
)


WORKFLOW_PATCH_EXTENSION_MANIFEST_SCHEMA = (
    "noruct.workflow-patch-post-apply-extension-manifest.v1"
)
WORKFLOW_PATCH_EXTENSION_PREFLIGHT_SCHEMA = (
    "noruct.workflow-patch-post-apply-extension-preflight.v1"
)
WORKFLOW_PATCH_EXTENSION_STATUS_SCHEMA = (
    "noruct.workflow-patch-post-apply-extension-status.v1"
)
WORKFLOW_PATCH_EXTENSION_LEDGER_SCHEMA = (
    "noruct.workflow-patch-post-apply-extension-ledger.v1"
)
WORKFLOW_PATCH_EXTENSION_FAILURE_SCHEMA = (
    "noruct.workflow-patch-post-apply-extension-failure.v1"
)
WORKFLOW_PATCH_EXTENSION_COMPARISON_SCHEMA = (
    "noruct.workflow-patch-post-apply-extension-comparison.v1"
)
_EXTENSION_DB = "workflow-patch-extension.db"
_COMPANY_DB = "isolated-company-extension.db"
_MAX_RECORDS = 2
_SLOTS = (
    ("post-apply-2", WORKFLOW_PATCH_EXTENSION_STRATEGIES[0]),
    ("post-apply-3", WORKFLOW_PATCH_EXTENSION_STRATEGIES[1]),
)



from .workflow_patch_extension_contracts import (
    _MAX_RECORDS,
    WORKFLOW_PATCH_EXTENSION_STATUS_SCHEMA,
    WorkflowPatchExtensionState,
    WorkflowPatchExtensionStatus,
    WorkflowPatchExtensionStore,
)
from .workflow_patch_extension_primitives import (
    _company_store,
    _extension_artifacts,
    _manifest_fresh,
    _sealed_path,
    _validate_failure,
    _validate_record,
    _verify_parent,
    _verify_runtime_inputs,
)

def workflow_patch_extension_status(
    directory: str | Path,
) -> WorkflowPatchExtensionStatus:
    with WorkflowPatchExtensionStore(directory) as store:
        metadata, manifest, preflight, _ = _extension_artifacts(store)
        events = store.events()
        root = store.directory
    parent = _verify_parent(manifest, metadata)
    parent_immutable = parent.semantic_anchor == manifest.parent_semantic_anchor
    slots = tuple((item.slot, item.strategy) for item in manifest.expected_runs)
    started: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    recorded: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    failed: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    interrupted_terminal: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    qualified: dict[tuple[str, str], LiveWorkflowPatchRecord] = {}
    assessment_events = tuple(
        event
        for event in events
        if event.kind == CampaignEventKind.ASSESSMENT_RECORDED
    )
    rollback_events = tuple(
        event for event in events if event.kind == CampaignEventKind.ROLLBACK_RECORDED
    )
    if len(assessment_events) > 1 or len(rollback_events) > 1:
        raise ValueError("Workflow Patch extension repeated a terminal decision event")
    for event in events:
        if event.fixture is None or event.strategy is None:
            continue
        key = (event.fixture, event.strategy)
        if key not in slots:
            raise ValueError("Workflow Patch extension ledger contains an unknown slot")
        if event.kind == CampaignEventKind.RUN_STARTED:
            if key in started:
                raise ValueError("Workflow Patch extension reuses a run slot")
            started[key] = event
        elif event.kind == CampaignEventKind.RUN_RECORDED:
            if key not in started or key in recorded or key in failed:
                raise ValueError("Workflow Patch extension record has no unique start")
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
                raise ValueError("Workflow Patch extension failure has no unique start")
            if event.kind == CampaignEventKind.RUN_INTERRUPTED:
                interrupted_terminal[key] = event
            else:
                failed[key] = event
    for key, event in recorded.items():
        path = _sealed_path(root, event.payload.get("record_path"), "records-v1")
        if _sha256_file(path) != event.payload.get("record_file_sha256"):
            raise ValueError("Workflow Patch extension sealed record changed")
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
            or event.payload.get("observation_id") is None
        ):
            raise ValueError("Workflow Patch extension ledger projection changed")
    for key, event in {**failed, **interrupted_terminal}.items():
        path = _sealed_path(root, event.payload.get("failure_path"), "failures-v1")
        if _sha256_file(path) != event.payload.get("failure_file_sha256"):
            raise ValueError("Workflow Patch extension sealed failure changed")
        _validate_failure(path, manifest, slot=key[0], strategy=key[1])
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
        if record.cost.tool_calls:
            stop_reason = f"{key[0].upper()}_TOOL_BOUNDARY_FAILED"
            break
    with _company_store(root, metadata) as company:
        patch = company.get_patch(manifest.patch_id)
        playbook = company.playbook()
        observations = company.list_observations(manifest.patch_id)
        assessments = company.list_assessments(manifest.patch_id)
    completed = len(recorded)
    if (
        patch.pattern.pattern_id != manifest.pattern_id
        or patch.applied_revision != manifest.applied_playbook_revision
    ):
        raise ValueError("Workflow Patch extension Company identity drifted")
    if patch.status == WorkflowPatchStatus.APPLIED:
        if (
            playbook.revision != manifest.applied_playbook_revision
            or len(playbook.patterns) != 1
        ):
            raise ValueError("Workflow Patch extension applied state drifted")
    elif patch.status == WorkflowPatchStatus.ROLLED_BACK:
        if playbook.patterns:
            raise ValueError("Workflow Patch extension rollback left an active prior")
    else:
        raise ValueError("Workflow Patch extension patch left a terminal state")
    if len(observations) != 1 + completed:
        raise ValueError("Workflow Patch extension observation count drifted")
    expected_observation_ids = {
        manifest.parent_observation_id,
        *(
            str(event.payload["observation_id"])
            for event in recorded.values()
        ),
    }
    if {item.observation_id for item in observations} != expected_observation_ids:
        raise ValueError("Workflow Patch extension observation identity drifted")
    assessment = assessments[-1] if assessments else None
    if bool(assessment) != bool(assessment_events):
        raise ValueError("Workflow Patch extension assessment ledger drifted")
    if assessment and (
        len(assessments) != 1
        or assessment.assessment_id
        != assessment_events[0].payload.get("assessment_id")
        or assessment.content_hash
        != assessment_events[0].payload.get("assessment_content_hash")
        or assessment.decision.value
        != assessment_events[0].payload.get("decision")
    ):
        raise ValueError("Workflow Patch extension assessment projection changed")
    if bool(patch.status == WorkflowPatchStatus.ROLLED_BACK) != bool(rollback_events):
        raise ValueError("Workflow Patch extension rollback ledger drifted")
    fresh = _manifest_fresh(manifest)
    external_calls = sum(item.external_model_calls for item in qualified.values())
    if external_calls > manifest.max_model_calls_extension:
        raise ValueError("Workflow Patch extension call budget changed")
    next_expected = (
        manifest.expected_runs[completed]
        if completed < len(manifest.expected_runs)
        else None
    )
    if patch.status == WorkflowPatchStatus.ROLLED_BACK:
        state = WorkflowPatchExtensionState.ROLLED_BACK
        action = "compare-and-archive"
    elif assessment is not None:
        if assessment.decision == WorkflowPatchAssessmentDecision.KEEP:
            state = WorkflowPatchExtensionState.KEEP
            action = "compare-and-complete-alpha-gates"
        elif (
            assessment.decision
            == WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE
        ):
            state = WorkflowPatchExtensionState.ROLLBACK_CANDIDATE
            action = "inspect-assessment-then-explicit-rollback-or-retain"
        else:
            state = WorkflowPatchExtensionState.BLOCKED
            action = "inspect-unexpected-insufficient-assessment"
    elif failed or stop_reason is not None:
        state = WorkflowPatchExtensionState.PARTIAL_FAILED
        action = "inspect-failure-and-assess-existing-attribution"
    elif interrupted:
        state = WorkflowPatchExtensionState.INTERRUPTED
        action = "inspect-interrupted-slot"
    elif running:
        state = WorkflowPatchExtensionState.RUNNING
        action = "wait-for-current-slot"
    elif completed == _MAX_RECORDS:
        state = WorkflowPatchExtensionState.AWAITING_ASSESSMENT
        action = "assess"
    elif not preflight.ready or not fresh or not parent_immutable:
        state = WorkflowPatchExtensionState.BLOCKED
        action = "refresh-preflight"
    else:
        state = WorkflowPatchExtensionState.READY
        action = "run-next"
    next_allowed = (
        state == WorkflowPatchExtensionState.READY and next_expected is not None
    )
    return WorkflowPatchExtensionStatus(
        schema_version=WORKFLOW_PATCH_EXTENSION_STATUS_SCHEMA,
        extension_id=manifest.extension_id,
        state=state,
        manifest_content_hash=manifest.content_hash,
        manifest_fresh=fresh,
        parent_immutable=parent_immutable,
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
        patch_id=manifest.patch_id,
        patch_status=patch.status.value,
        post_apply_observations=len(observations),
        assessment_id=assessment.assessment_id if assessment else None,
        assessment_decision=assessment.decision.value if assessment else None,
        operator_action=action,
        event_count=len(events),
        ledger_verified=True,
        record_paths=tuple(
            str(root / str(recorded[slot].payload["record_path"]))
            for slot in slots
            if slot in recorded
        ),
    )


