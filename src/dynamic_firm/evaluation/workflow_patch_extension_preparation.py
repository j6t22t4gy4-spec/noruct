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
    _COMPANY_DB,
    _MAX_RECORDS,
    _SLOTS,
    WORKFLOW_PATCH_EXTENSION_LEDGER_SCHEMA,
    WorkflowPatchExtensionPreparation,
    WorkflowPatchExtensionStore,
)
from .workflow_patch_extension_primitives import (
    _clone_company_database,
    _company_seed_payload,
    _create_manifest,
    _create_preflight,
    _parent_evidence,
)
from .workflow_patch_extension_status import workflow_patch_extension_status

async def prepare_workflow_patch_extension(
    parent_directory: str | Path,
    directory: str | Path,
    *,
    wheel: str | Path,
    source_root: str | Path,
    model: str,
    command: str,
    max_model_calls_per_run: int = 8,
    max_model_calls_extension: int = 16,
    max_wall_time_ms_per_run: int = 180_000,
    lifetime_hours: int = 168,
    request_timeout_seconds: float = 120.0,
    login_status_factory: Callable[[str], CodexLoginStatus] | None = None,
    capability_probe: Callable[[str], tuple[str | None, bool, str]] | None = None,
) -> WorkflowPatchExtensionPreparation:
    target = Path(directory).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"Workflow Patch extension directory must be empty: {target}")
    parent = _parent_evidence(parent_directory)
    if model.strip() != parent.manifest.model_id:
        raise ValueError("Workflow Patch extension model must match the parent cohort")
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
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    clone_path = target / _COMPANY_DB
    _clone_company_database(parent, clone_path)
    with CompanyStateStore(clone_path) as company:
        seed_payload = _company_seed_payload(company, parent.patch_id)
        seed_hash = content_digest(seed_payload)
        patch = company.get_patch(parent.patch_id)
        observations = company.list_observations(parent.patch_id)
        assessments = company.list_assessments(parent.patch_id)
        clone_valid = (
            seed_hash == parent.company_seed_hash
            and
            patch.status == WorkflowPatchStatus.APPLIED
            and patch.applied_revision == parent.manifest.applied_playbook_revision
            and company.playbook().revision
            == parent.manifest.applied_playbook_revision
            and len(observations) == 1
            and observations[0].content_hash == parent.observation_content_hash
            and not assessments
        )
    control_path = _write_private(
        target / "provider-free-control-v1.json",
        control_payload,
    )
    checks = (
        InformationBoundaryCheck(
            "provider-free-causal-control",
            control.passed
            and control.external_model_calls == 0
            and not control.quota_consumed,
            f"records={control.cohort_job_count},provider-calls=0",
        ),
        InformationBoundaryCheck(
            "immutable-parent-cohort",
            len(parent.semantic_anchor) == 64
            and parent.manifest.campaign_id
            and parent.applied.task_success,
            (
                f"campaign={parent.manifest.campaign_id},"
                f"anchor={parent.semantic_anchor}"
            ),
        ),
        InformationBoundaryCheck(
            "one-observation-applied-seed",
            clone_valid,
            (
                f"patch={parent.patch_id},observation={parent.observation_id},"
                f"playbook={parent.manifest.applied_playbook_revision}"
            ),
        ),
        InformationBoundaryCheck(
            "exact-two-record-extension",
            len(_SLOTS) == _MAX_RECORDS
            and tuple(strategy for _, strategy in _SLOTS)
            == WORKFLOW_PATCH_EXTENSION_STRATEGIES,
            "post-apply-2 -> post-apply-3",
        ),
        InformationBoundaryCheck(
            "matched-parent-context",
            workflow_patch_matched_context_hash(
                model_profile=model.strip(),
                company_revision=parent.manifest.company_revision,
                roster_revision=parent.manifest.roster_revision,
                max_total_model_calls=max_model_calls_per_run,
                max_wall_time_ms=max_wall_time_ms_per_run,
            )
            == parent.manifest.matched_context_hash,
            parent.manifest.matched_context_hash,
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
            and max_model_calls_extension
            == max_model_calls_per_run * _MAX_RECORDS
            and max_model_calls_extension <= 16,
            (
                f"per-run<={max_model_calls_per_run},"
                f"extension<={max_model_calls_extension}"
            ),
        ),
    )
    preflight = _create_preflight(
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        model=model.strip(),
        provider_free_control_hash=_sha256_file(control_path),
        cloned_company_seed_hash=seed_hash,
        checks=checks,
    )
    manifest = _create_manifest(
        preflight,
        parent,
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_extension=max_model_calls_extension,
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
    with WorkflowPatchExtensionStore(target, create=True) as store:
        store.initialize(
            {
                "schema_version": WORKFLOW_PATCH_EXTENSION_LEDGER_SCHEMA,
                "extension_id": manifest.extension_id,
                "manifest_file_sha256": _sha256_file(manifest_path),
                "preflight_file_sha256": _sha256_file(preflight_path),
                "control_file_sha256": _sha256_file(control_path),
                "source_root": str(source),
                "wheel_path": str(wheel_path),
                "parent_directory": str(parent.directory),
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
                "parent_semantic_anchor": parent.semantic_anchor,
                "automatic_assessment": False,
                "automatic_rollback": False,
            },
        )
    return WorkflowPatchExtensionPreparation(
        preflight,
        workflow_patch_extension_status(target),
    )

