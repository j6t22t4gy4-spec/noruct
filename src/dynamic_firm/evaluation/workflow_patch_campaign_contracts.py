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


class WorkflowPatchCohortState(StrEnum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_APPLY = "AWAITING_APPLY"
    COMPLETE = "COMPLETE"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True, slots=True)
class WorkflowPatchExpectedRun:
    slot: str
    strategy: str
    playbook_revision: int
    workload_hash: str
    run_id: str


@dataclass(frozen=True, slots=True)
class WorkflowPatchCohortManifest:
    schema_version: str
    campaign_id: str
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
    base_playbook_revision: int
    applied_playbook_revision: int
    memory_revision: str
    fixture_revision: str
    benchmark_revision: str
    matched_context_hash: str
    candidate_pattern_id: str
    max_records: int
    max_model_calls_per_run: int
    max_model_calls_cohort: int
    max_wall_time_ms_per_run: int
    quality_gain_threshold: float
    expected_runs: tuple[WorkflowPatchExpectedRun, ...]

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("campaign_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPatchCohortPreflight:
    schema_version: str
    preflight_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    source_revision: str
    distribution_sha256: str
    provider_kind: str
    model_id: str
    provider_free_control_hash: str
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
class WorkflowPatchCohortStatus:
    schema_version: str
    campaign_id: str
    state: WorkflowPatchCohortState
    manifest_content_hash: str
    manifest_fresh: bool
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
    patch_id: str | None
    patch_status: str | None
    operator_action: str
    event_count: int
    ledger_verified: bool
    record_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowPatchCohortPreparation:
    preflight: WorkflowPatchCohortPreflight
    status: WorkflowPatchCohortStatus


@dataclass(frozen=True, slots=True)
class WorkflowPatchCohortRunResult:
    event: FirmValueCampaignEvent
    status: WorkflowPatchCohortStatus
    record_path: str | None
    task_success: bool


@dataclass(frozen=True, slots=True)
class WorkflowPatchCohortComparison:
    schema_version: str
    campaign_id: str
    manifest_content_hash: str
    completed_runs: int
    expected_runs: int
    baseline_quality: float
    patched_quality: float
    artifact_quality_gain: float
    baseline_model_calls: int
    patched_model_calls: int
    model_call_reduction: float
    safety_gate_passed: bool
    attribution_gate_passed: bool
    effect_gate_passed: bool
    budget_gate_passed: bool
    cohort_gate_passed: bool
    patch_id: str
    patch_status: str
    post_apply_observations: int
    outcome: str
    recommended_direction: str
    checks: tuple[InformationBoundaryCheck, ...]
    aggregator_provider_calls: int
    aggregator_quota_consumed: bool


class WorkflowPatchCohortStore(FirmValueCampaignStore):
    def __init__(self, directory: str | Path, *, create: bool = False) -> None:
        super().__init__(
            directory,
            create=create,
            db_name=_COHORT_DB,
            ledger_schema=WORKFLOW_PATCH_COHORT_LEDGER_SCHEMA,
            event_id_prefix="workflow-patch-cohort-event",
        )



