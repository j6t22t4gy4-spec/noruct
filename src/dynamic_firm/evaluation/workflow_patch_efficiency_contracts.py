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


WORKFLOW_PATCH_EFFICIENCY_MANIFEST_SCHEMA = (
    "noruct.workflow-patch-completion-efficiency-manifest.v1"
)
WORKFLOW_PATCH_EFFICIENCY_PREFLIGHT_SCHEMA = (
    "noruct.workflow-patch-completion-efficiency-preflight.v1"
)
WORKFLOW_PATCH_EFFICIENCY_STATUS_SCHEMA = (
    "noruct.workflow-patch-completion-efficiency-status.v1"
)
WORKFLOW_PATCH_EFFICIENCY_LEDGER_SCHEMA = (
    "noruct.workflow-patch-completion-efficiency-ledger.v1"
)
WORKFLOW_PATCH_EFFICIENCY_DIAGNOSTIC_SCHEMA = (
    "noruct.workflow-patch-completion-diagnostic.v1"
)
WORKFLOW_PATCH_EFFICIENCY_FAILURE_SCHEMA = (
    "noruct.workflow-patch-completion-efficiency-failure.v1"
)
WORKFLOW_PATCH_EFFICIENCY_COMPARISON_SCHEMA = (
    "noruct.workflow-patch-completion-efficiency-comparison.v1"
)
WORKFLOW_PATCH_NATURAL_PREFLIGHT_SCHEMA = (
    "noruct.workflow-patch-natural-workload-preflight.v2"
)
WORKFLOW_PATCH_NATURAL_GOAL = (
    "현재 저장소의 alpha release readiness evidence를 통합하고, engineering 증거와 "
    "상업 출시 체크리스트를 독립 검수한 뒤 staging 가능 여부와 남은 blocker를 "
    "결정해라."
)
_PAIR_DB = "workflow-patch-completion-efficiency.db"
_MAX_RECORDS = 2
_SLOTS = (
    ("control", WORKFLOW_PATCH_EFFICIENCY_STRATEGIES[0]),
    ("candidate", WORKFLOW_PATCH_EFFICIENCY_STRATEGIES[1]),
)
_SLOTS_V2 = (
    ("control-v2", WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES[0]),
    ("candidate-v2", WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES[1]),
)
_SLOTS_V3 = (
    ("control-v3", WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES[0]),
    ("candidate-v3", WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES[1]),
)


class WorkflowPatchEfficiencyState(StrEnum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class WorkflowPatchEfficiencyExpectedRun:
    slot: str
    strategy: str
    workload_hash: str
    run_id: str


@dataclass(frozen=True, slots=True)
class WorkflowPatchEfficiencyManifest:
    schema_version: str
    pair_id: str
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
    playbook_revision: int
    memory_revision: str
    fixture_revision: str
    benchmark_revision: str
    matched_context_hash: str
    pattern_id: str
    parent_extension_id: str
    parent_semantic_anchor: str
    completion_contract_revision: str
    completion_validator_revision: str
    max_records: int
    max_model_calls_per_run: int
    max_model_calls_pair: int
    max_wall_time_ms_per_run: int
    expected_runs: tuple[WorkflowPatchEfficiencyExpectedRun, ...]

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("pair_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPatchEfficiencyPreflight:
    schema_version: str
    preflight_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    source_revision: str
    distribution_sha256: str
    model_id: str
    parent_semantic_anchor: str
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
class WorkflowPatchEfficiencyDiagnostic:
    schema_version: str
    diagnostic_id: str
    content_hash: str
    pair_id: str
    strategy: str
    live_record_content_hash: str
    attempt_count: int
    failed_attempt_count: int
    repair_count: int
    attempts: tuple[WorkflowPatchCompletionAttemptProjection, ...]

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("diagnostic_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPatchEfficiencyStatus:
    schema_version: str
    pair_id: str
    state: WorkflowPatchEfficiencyState
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
    event_count: int
    ledger_verified: bool
    record_paths: tuple[str, ...]
    diagnostic_paths: tuple[str, ...]
    operator_action: str


@dataclass(frozen=True, slots=True)
class WorkflowPatchEfficiencyPreparation:
    preflight: WorkflowPatchEfficiencyPreflight
    status: WorkflowPatchEfficiencyStatus


@dataclass(frozen=True, slots=True)
class WorkflowPatchEfficiencyRunResult:
    event: FirmValueCampaignEvent
    status: WorkflowPatchEfficiencyStatus
    record_path: str | None
    diagnostic_path: str | None
    task_success: bool


@dataclass(frozen=True, slots=True)
class WorkflowPatchEfficiencyComparison:
    schema_version: str
    pair_id: str
    manifest_content_hash: str
    completed_runs: int
    expected_runs: int
    control_quality: float
    candidate_quality: float
    control_model_calls: int
    candidate_model_calls: int
    model_call_savings: int
    control_repairs: int
    candidate_repairs: int
    repair_savings: int
    control_total_tokens: int
    candidate_total_tokens: int
    token_savings: int
    quality_gate_passed: bool
    attribution_gate_passed: bool
    safety_gate_passed: bool
    validation_gate_passed: bool
    efficiency_gate_passed: bool
    budget_gate_passed: bool
    pair_gate_passed: bool
    target_call_bound_met: bool
    outcome: str
    recommended_direction: str
    checks: tuple[InformationBoundaryCheck, ...]
    aggregator_provider_calls: int
    aggregator_quota_consumed: bool


@dataclass(frozen=True, slots=True)
class WorkflowPatchNaturalPreflight:
    schema_version: str
    preflight_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    source_revision: str
    parent_extension_id: str
    parent_semantic_anchor: str
    applied_pattern_id: str
    applied_context_fingerprint: str
    goal_digest: str
    route: str
    workspace_manifest_status: str
    workspace_manifest_error: str | None
    workspace_manifest_count: int
    workspace_manifest_limit: int
    workspace_identity_status: str
    workspace_identity_failure_code: str | None
    workspace_projection_revision: str
    workspace_projection_truncated: bool
    workspace_context_fingerprint: str
    selected_prior_ids: tuple[str, ...]
    ready_for_live_observation: bool
    outcome: str
    recommended_direction: str
    checks: tuple[InformationBoundaryCheck, ...]
    external_model_calls: int
    quota_consumed: bool

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("preflight_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class _ParentEfficiencySeed:
    directory: Path
    extension_id: str
    semantic_anchor: str
    model_id: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    pattern_id: str


class WorkflowPatchEfficiencyStore(FirmValueCampaignStore):
    def __init__(self, directory: str | Path, *, create: bool = False) -> None:
        super().__init__(
            directory,
            create=create,
            db_name=_PAIR_DB,
            ledger_schema=WORKFLOW_PATCH_EFFICIENCY_LEDGER_SCHEMA,
            event_id_prefix="workflow-patch-efficiency-event",
        )



