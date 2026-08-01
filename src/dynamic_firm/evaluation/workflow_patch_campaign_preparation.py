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
    _COMPANY_DB,
    _MAX_RECORDS,
    WORKFLOW_PATCH_COHORT_LEDGER_SCHEMA,
    WorkflowPatchCohortPreparation,
    WorkflowPatchCohortStore,
)
from .workflow_patch_campaign_primitives import (
    _create_manifest,
    _create_preflight,
)
from .workflow_patch_campaign_status import workflow_patch_cohort_status

async def prepare_workflow_patch_cohort(
    directory: str | Path,
    *,
    wheel: str | Path,
    source_root: str | Path,
    model: str,
    command: str,
    max_model_calls_per_run: int = 8,
    max_model_calls_cohort: int = 32,
    max_wall_time_ms_per_run: int = 180_000,
    lifetime_hours: int = 168,
    request_timeout_seconds: float = 120.0,
    login_status_factory: Callable[[str], CodexLoginStatus] | None = None,
    capability_probe: Callable[[str], tuple[str | None, bool, str]] | None = None,
) -> WorkflowPatchCohortPreparation:
    target = Path(directory).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"Workflow Patch cohort directory must be empty: {target}")
    if not model.strip():
        raise ValueError("Workflow Patch cohort requires an explicit model")
    source = Path(source_root).expanduser().resolve()
    wheel_path = Path(wheel).expanduser().resolve()
    source_revision = source_snapshot_revision(source)
    distribution_sha256 = wheel_distribution_sha256(wheel_path)
    control = await run_causal_workflow_evaluation()
    control_payload = json.dumps(
        to_primitive(control),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    login = (login_status_factory or CodexExecProvider.login_status)(command)
    executable, structured, capability_evidence = (
        capability_probe or probe_codex_structured_output
    )(command)
    checks = (
        InformationBoundaryCheck(
            "provider-free-causal-control",
            control.passed
            and control.external_model_calls == 0
            and not control.quota_consumed,
            f"records={control.cohort_job_count},provider-calls=0",
        ),
        InformationBoundaryCheck(
            "exact-four-record-schedule",
            len(_SLOTS) == _MAX_RECORDS
            and tuple(strategy for _, strategy in _SLOTS)
            == WORKFLOW_PATCH_STRATEGIES,
            "baseline -> observation-1 -> observation-2 -> patched",
        ),
        InformationBoundaryCheck(
            "candidate-pattern-identity",
            workflow_patch_candidate_prior().pattern_id
            == workflow_patch_pattern_id(),
            workflow_patch_pattern_id(),
        ),
        InformationBoundaryCheck(
            "source-snapshot-frozen",
            source_revision.startswith("snapshot-sha256:"),
            source_revision,
        ),
        InformationBoundaryCheck(
            "wheel-hash-frozen",
            len(distribution_sha256) == 64,
            distribution_sha256,
        ),
        InformationBoundaryCheck(
            "codex-executable-installed",
            bool(login.installed and login.executable and executable),
            executable or login.executable or command,
        ),
        InformationBoundaryCheck(
            "codex-authenticated",
            bool(login.authenticated),
            "official login status passed"
            if login.authenticated
            else "authentication not confirmed",
        ),
        InformationBoundaryCheck(
            "structured-output-cli-contract",
            structured,
            capability_evidence,
        ),
        InformationBoundaryCheck(
            "bounded-live-quota",
            request_timeout_seconds > 0
            and 1 <= max_model_calls_per_run <= 8
            and max_model_calls_cohort
            == max_model_calls_per_run * _MAX_RECORDS
            and max_model_calls_cohort <= 32,
            (
                f"per-run<={max_model_calls_per_run},"
                f"cohort<={max_model_calls_cohort}"
            ),
        ),
    )
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    control_path = _write_private(
        target / "provider-free-control-v1.json",
        control_payload,
    )
    preflight = _create_preflight(
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        model=model.strip(),
        provider_free_control_hash=_sha256_file(control_path),
        checks=checks,
    )
    manifest = _create_manifest(
        preflight,
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_cohort=max_model_calls_cohort,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
        lifetime_hours=lifetime_hours,
    )
    manifest_path = _write_private(
        target / "manifest-v1.json",
        json.dumps(to_primitive(manifest), ensure_ascii=False, sort_keys=True, indent=2),
    )
    preflight_path = _write_private(
        target / "preflight-v1.json",
        json.dumps(to_primitive(preflight), ensure_ascii=False, sort_keys=True, indent=2),
    )
    with CompanyStateStore(target / _COMPANY_DB) as company:
        if (
            company.playbook().revision != manifest.base_playbook_revision
            or company.playbook().patterns
        ):
            raise ValueError("Workflow Patch isolated Company did not start empty")
    with WorkflowPatchCohortStore(target, create=True) as store:
        store.initialize(
            {
                "schema_version": WORKFLOW_PATCH_COHORT_LEDGER_SCHEMA,
                "campaign_id": manifest.campaign_id,
                "manifest_file_sha256": _sha256_file(manifest_path),
                "preflight_file_sha256": _sha256_file(preflight_path),
                "control_file_sha256": _sha256_file(control_path),
                "source_root": str(source),
                "wheel_path": str(wheel_path),
                "codex_command": command,
                "request_timeout_seconds": request_timeout_seconds,
                "company_db": _COMPANY_DB,
            }
        )
        store.append(
            CampaignEventKind.PREPARED,
            payload={
                "ready": preflight.ready,
                "external_model_calls": 0,
                "quota_consumed": False,
                "expected_runs": _MAX_RECORDS,
                "automatic_patch_apply": False,
            },
        )
    return WorkflowPatchCohortPreparation(
        preflight,
        workflow_patch_cohort_status(target),
    )

