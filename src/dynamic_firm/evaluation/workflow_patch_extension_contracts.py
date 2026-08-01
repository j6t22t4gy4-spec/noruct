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


class WorkflowPatchExtensionState(StrEnum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    AWAITING_ASSESSMENT = "AWAITING_ASSESSMENT"
    KEEP = "KEEP"
    ROLLBACK_CANDIDATE = "ROLLBACK_CANDIDATE"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True, slots=True)
class WorkflowPatchExtensionExpectedRun:
    slot: str
    strategy: str
    playbook_revision: int
    workload_hash: str
    run_id: str


@dataclass(frozen=True, slots=True)
class WorkflowPatchExtensionManifest:
    schema_version: str
    extension_id: str
    content_hash: str
    created_at: str
    expires_at: str
    noruct_version: str
    source_revision: str
    distribution_sha256: str
    provider_kind: str
    model_id: str
    authority_profile: str
    company_revision: int
    roster_revision: int
    applied_playbook_revision: int
    memory_revision: str
    fixture_revision: str
    benchmark_revision: str
    matched_context_hash: str
    patch_id: str
    pattern_id: str
    parent_campaign_id: str
    parent_manifest_content_hash: str
    parent_semantic_anchor: str
    parent_baseline_content_hash: str
    parent_applied_content_hash: str
    parent_observation_id: str
    parent_observation_content_hash: str
    max_records: int
    max_model_calls_per_run: int
    max_model_calls_extension: int
    max_wall_time_ms_per_run: int
    expected_runs: tuple[WorkflowPatchExtensionExpectedRun, ...]

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("extension_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPatchExtensionPreflight:
    schema_version: str
    preflight_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    source_revision: str
    distribution_sha256: str
    model_id: str
    provider_free_control_hash: str
    cloned_company_seed_hash: str
    external_model_calls: int
    quota_consumed: bool
    ready: bool
    checks: tuple[InformationBoundaryCheck, ...]

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("preflight_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPatchExtensionStatus:
    schema_version: str
    extension_id: str
    state: WorkflowPatchExtensionState
    manifest_content_hash: str
    manifest_fresh: bool
    parent_immutable: bool
    viable: bool
    stop_reason: str | None
    completed_runs: int
    expected_runs: int
    failed_runs: int
    interrupted_runs: int
    next_slot: str | None
    next_strategy: str | None
    max_model_calls_for_next_run: int
    max_wall_time_ms_for_next_run: int
    explicit_quota_confirmation_required: bool
    external_model_calls_recorded: int
    patch_id: str
    patch_status: str
    post_apply_observations: int
    assessment_id: str | None
    assessment_decision: str | None
    operator_action: str
    event_count: int
    ledger_verified: bool
    record_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowPatchExtensionPreparation:
    preflight: WorkflowPatchExtensionPreflight
    status: WorkflowPatchExtensionStatus


@dataclass(frozen=True, slots=True)
class WorkflowPatchExtensionRunResult:
    event: FirmValueCampaignEvent
    status: WorkflowPatchExtensionStatus
    record_path: str | None
    task_success: bool


@dataclass(frozen=True, slots=True)
class WorkflowPatchExtensionComparison:
    schema_version: str
    extension_id: str
    manifest_content_hash: str
    parent_campaign_id: str
    completed_runs: int
    expected_runs: int
    post_apply_observations: int
    mean_artifact_quality: float
    minimum_artifact_quality: float
    parent_applied_model_calls: int
    extension_mean_model_calls: float
    model_call_delta_from_parent: float
    repair_used_count: int
    repair_free_count: int
    safety_gate_passed: bool
    attribution_gate_passed: bool
    effect_gate_passed: bool
    budget_gate_passed: bool
    assessment_gate_passed: bool
    extension_gate_passed: bool
    assessment_id: str
    assessment_decision: str
    mean_quality_gain: float | None
    mean_model_call_savings: float | None
    outcome: str
    recommended_direction: str
    checks: tuple[InformationBoundaryCheck, ...]
    aggregator_provider_calls: int
    aggregator_quota_consumed: bool


@dataclass(frozen=True, slots=True)
class _ParentEvidence:
    directory: Path
    metadata: Mapping[str, object]
    manifest: object
    baseline: LiveWorkflowPatchRecord
    applied: LiveWorkflowPatchRecord
    semantic_anchor: str
    company_seed_hash: str
    patch_id: str
    pattern_id: str
    observation_id: str
    observation_content_hash: str


class WorkflowPatchExtensionStore(FirmValueCampaignStore):
    def __init__(self, directory: str | Path, *, create: bool = False) -> None:
        super().__init__(
            directory,
            create=create,
            db_name=_EXTENSION_DB,
            ledger_schema=WORKFLOW_PATCH_EXTENSION_LEDGER_SCHEMA,
            event_id_prefix="workflow-patch-extension-event",
        )



