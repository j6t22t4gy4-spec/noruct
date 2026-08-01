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
    WORKFLOW_PATCH_EXTENSION_COMPARISON_SCHEMA,
    WORKFLOW_PATCH_EXTENSION_FAILURE_SCHEMA,
    WORKFLOW_PATCH_EXTENSION_LEDGER_SCHEMA,
    WORKFLOW_PATCH_EXTENSION_MANIFEST_SCHEMA,
    WORKFLOW_PATCH_EXTENSION_PREFLIGHT_SCHEMA,
    WORKFLOW_PATCH_EXTENSION_STATUS_SCHEMA,
    WorkflowPatchExtensionComparison,
    WorkflowPatchExtensionExpectedRun,
    WorkflowPatchExtensionManifest,
    WorkflowPatchExtensionPreflight,
    WorkflowPatchExtensionPreparation,
    WorkflowPatchExtensionRunResult,
    WorkflowPatchExtensionState,
    WorkflowPatchExtensionStatus,
    WorkflowPatchExtensionStore,
)
from .workflow_patch_extension_execution import (
    assess_workflow_patch_extension,
    compare_workflow_patch_extension,
    rollback_workflow_patch_extension,
    run_next_workflow_patch_extension_slot,
)
from .workflow_patch_extension_preparation import prepare_workflow_patch_extension
from .workflow_patch_extension_primitives import (
    _company_store,
    _extension_artifacts,
    workflow_patch_extension_expected_runs,
)
from .workflow_patch_extension_status import workflow_patch_extension_status
