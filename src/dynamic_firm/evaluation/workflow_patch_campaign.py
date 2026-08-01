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
    WORKFLOW_PATCH_COHORT_COMPARISON_SCHEMA,
    WORKFLOW_PATCH_COHORT_FAILURE_SCHEMA,
    WORKFLOW_PATCH_COHORT_LEDGER_SCHEMA,
    WORKFLOW_PATCH_COHORT_MANIFEST_SCHEMA,
    WORKFLOW_PATCH_COHORT_PREFLIGHT_SCHEMA,
    WORKFLOW_PATCH_COHORT_STATUS_SCHEMA,
    WorkflowPatchCohortComparison,
    WorkflowPatchExpectedRun,
    WorkflowPatchCohortManifest,
    WorkflowPatchCohortPreflight,
    WorkflowPatchCohortPreparation,
    WorkflowPatchCohortRunResult,
    WorkflowPatchCohortState,
    WorkflowPatchCohortStatus,
    WorkflowPatchCohortStore,
)
from .workflow_patch_campaign_execution import (
    apply_workflow_patch_cohort,
    approve_workflow_patch_cohort,
    compare_workflow_patch_cohort,
    preview_workflow_patch_cohort,
    rollback_workflow_patch_cohort,
    run_next_workflow_patch_cohort_slot,
)
from .workflow_patch_campaign_preparation import prepare_workflow_patch_cohort
from .workflow_patch_campaign_primitives import (
    _campaign_artifacts,
    _company_store,
    _episode,
    _validate_record,
    workflow_patch_cohort_expected_runs,
)
from .workflow_patch_campaign_status import workflow_patch_cohort_status
