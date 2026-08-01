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
    _SLOTS,
    WORKFLOW_PATCH_COHORT_FAILURE_SCHEMA,
    WORKFLOW_PATCH_COHORT_LEDGER_SCHEMA,
    WORKFLOW_PATCH_COHORT_MANIFEST_SCHEMA,
    WORKFLOW_PATCH_COHORT_PREFLIGHT_SCHEMA,
    WorkflowPatchExpectedRun,
    WorkflowPatchCohortManifest,
    WorkflowPatchCohortPreflight,
    WorkflowPatchCohortStore,
)

def workflow_patch_cohort_expected_runs() -> tuple[tuple[str, str], ...]:
    return _SLOTS


def _create_preflight(
    *,
    source_revision: str,
    distribution_sha256: str,
    model: str,
    provider_free_control_hash: str,
    checks: tuple[InformationBoundaryCheck, ...],
) -> WorkflowPatchCohortPreflight:
    base = WorkflowPatchCohortPreflight(
        schema_version=WORKFLOW_PATCH_COHORT_PREFLIGHT_SCHEMA,
        preflight_id="pending",
        content_hash="pending",
        recorded_at=utc_now().isoformat(),
        noruct_version=__version__,
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        provider_kind="openai-codex-user-managed",
        model_id=model,
        provider_free_control_hash=provider_free_control_hash,
        external_model_calls=0,
        quota_consumed=False,
        ready=all(check.passed for check in checks),
        checks=checks,
    )
    digest = content_digest(base.content_payload())
    return WorkflowPatchCohortPreflight(
        **{
            **to_primitive(base),
            "preflight_id": f"workflow-patch-preflight-{digest[:24]}",
            "content_hash": digest,
            "checks": checks,
        }
    )


def _create_manifest(
    preflight: WorkflowPatchCohortPreflight,
    *,
    max_model_calls_per_run: int,
    max_model_calls_cohort: int,
    max_wall_time_ms_per_run: int,
    lifetime_hours: int,
) -> WorkflowPatchCohortManifest:
    if not 1 <= max_model_calls_per_run <= 8:
        raise ValueError("Workflow Patch cohort allows one to eight calls per run")
    if (
        max_model_calls_cohort != max_model_calls_per_run * _MAX_RECORDS
        or max_model_calls_cohort > 32
    ):
        raise ValueError(
            "Workflow Patch cohort call budget must equal four bounded runs"
        )
    if (
        not 1_000 <= max_wall_time_ms_per_run <= 600_000
        or not 1 <= lifetime_hours <= 336
    ):
        raise ValueError("Workflow Patch cohort time bounds are invalid")
    company_revision = 1
    roster_revision = 1
    base_playbook_revision = 1
    applied_playbook_revision = 2
    created = utc_now().astimezone(timezone.utc)
    expires = created + timedelta(hours=lifetime_hours)
    matched_hash = workflow_patch_matched_context_hash(
        model_profile=preflight.model_id,
        company_revision=company_revision,
        roster_revision=roster_revision,
        max_total_model_calls=max_model_calls_per_run,
        max_wall_time_ms=max_wall_time_ms_per_run,
    )
    expected = tuple(
        WorkflowPatchExpectedRun(
            slot=slot,
            strategy=strategy,
            playbook_revision=(
                applied_playbook_revision
                if strategy == "applied-workflow-patch"
                else base_playbook_revision
            ),
            workload_hash=identity.workload_hash,
            run_id=identity.run_id,
        )
        for slot, strategy in _SLOTS
        for identity in (
            workflow_patch_live_identity(
                strategy=strategy,
                model_profile=preflight.model_id,
                company_revision=company_revision,
                roster_revision=roster_revision,
                playbook_revision=(
                    applied_playbook_revision
                    if strategy == "applied-workflow-patch"
                    else base_playbook_revision
                ),
                max_total_model_calls=max_model_calls_per_run,
                max_wall_time_ms=max_wall_time_ms_per_run,
            ),
        )
    )
    base = WorkflowPatchCohortManifest(
        schema_version=WORKFLOW_PATCH_COHORT_MANIFEST_SCHEMA,
        campaign_id="pending",
        content_hash="pending",
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        noruct_version=__version__,
        source_revision=preflight.source_revision,
        distribution_sha256=preflight.distribution_sha256,
        provider_kind=preflight.provider_kind,
        model_id=preflight.model_id,
        authority_profile="read-only-network-deny-no-tools",
        company_revision=company_revision,
        roster_revision=roster_revision,
        base_playbook_revision=base_playbook_revision,
        applied_playbook_revision=applied_playbook_revision,
        memory_revision=workflow_patch_memory_revision(),
        fixture_revision=workflow_patch_fixture_revision(),
        benchmark_revision=workflow_patch_benchmark_revision(),
        matched_context_hash=matched_hash,
        candidate_pattern_id=workflow_patch_pattern_id(),
        max_records=_MAX_RECORDS,
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_cohort=max_model_calls_cohort,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
        quality_gain_threshold=WORKFLOW_PATCH_QUALITY_GAIN_THRESHOLD,
        expected_runs=expected,
    )
    digest = content_digest(base.content_payload())
    return WorkflowPatchCohortManifest(
        **{
            **to_primitive(base),
            "campaign_id": f"workflow-patch-cohort-{digest[:24]}",
            "content_hash": digest,
            "expected_runs": expected,
        }
    )


def _load_manifest(path: Path) -> WorkflowPatchCohortManifest:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Workflow Patch cohort manifest is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKFLOW_PATCH_COHORT_MANIFEST_SCHEMA
    ):
        raise ValueError("Workflow Patch cohort manifest schema is incompatible")
    expected = tuple(
        WorkflowPatchExpectedRun(**item) for item in value["expected_runs"]
    )
    manifest = WorkflowPatchCohortManifest(
        **{
            **{key: item for key, item in value.items() if key != "expected_runs"},
            "expected_runs": expected,
        }
    )
    if (
        manifest.content_hash != content_digest(manifest.content_payload())
        or manifest.campaign_id
        != f"workflow-patch-cohort-{manifest.content_hash[:24]}"
        or tuple((item.slot, item.strategy) for item in expected) != _SLOTS
        or len({item.run_id for item in expected}) != _MAX_RECORDS
        or manifest.max_records != _MAX_RECORDS
        or manifest.max_model_calls_cohort
        != manifest.max_model_calls_per_run * _MAX_RECORDS
        or manifest.max_model_calls_cohort > 32
        or manifest.candidate_pattern_id != workflow_patch_pattern_id()
    ):
        raise ValueError("Workflow Patch cohort manifest contract is invalid")
    return manifest


def _load_preflight(path: Path) -> WorkflowPatchCohortPreflight:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Workflow Patch cohort preflight is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKFLOW_PATCH_COHORT_PREFLIGHT_SCHEMA
    ):
        raise ValueError("Workflow Patch cohort preflight schema is incompatible")
    checks = tuple(InformationBoundaryCheck(**item) for item in value["checks"])
    preflight = WorkflowPatchCohortPreflight(
        **{key: item for key, item in value.items() if key != "checks"},
        checks=checks,
    )
    if (
        preflight.content_hash != content_digest(preflight.content_payload())
        or preflight.preflight_id
        != f"workflow-patch-preflight-{preflight.content_hash[:24]}"
        or preflight.external_model_calls != 0
        or preflight.quota_consumed
        or preflight.ready != all(check.passed for check in checks)
    ):
        raise ValueError("Workflow Patch cohort preflight contract is invalid")
    return preflight


def _campaign_artifacts(
    store: WorkflowPatchCohortStore,
) -> tuple[
    dict[str, object],
    WorkflowPatchCohortManifest,
    WorkflowPatchCohortPreflight,
    dict[str, object],
]:
    metadata = store.metadata()
    if metadata.get("schema_version") != WORKFLOW_PATCH_COHORT_LEDGER_SCHEMA:
        raise ValueError("Workflow Patch cohort ledger schema is invalid")
    manifest_path = store.directory / "manifest-v1.json"
    preflight_path = store.directory / "preflight-v1.json"
    control_path = store.directory / "provider-free-control-v1.json"
    for path, key in (
        (manifest_path, "manifest_file_sha256"),
        (preflight_path, "preflight_file_sha256"),
        (control_path, "control_file_sha256"),
    ):
        if _sha256_file(path) != metadata.get(key):
            raise ValueError("Workflow Patch cohort sealed artifact changed")
    manifest = _load_manifest(manifest_path)
    preflight = _load_preflight(preflight_path)
    control = json.loads(control_path.read_text(encoding="utf-8"))
    if (
        not isinstance(control, dict)
        or control.get("schema_version")
        != "noruct.causal-workflow-evaluation.v1"
        or control.get("passed") is not True
        or control.get("external_model_calls") != 0
        or control.get("quota_consumed") is not False
        or manifest.campaign_id != metadata.get("campaign_id")
        or manifest.source_revision != preflight.source_revision
        or manifest.distribution_sha256 != preflight.distribution_sha256
        or preflight.provider_free_control_hash != _sha256_file(control_path)
    ):
        raise ValueError("Workflow Patch cohort identity does not match its ledger")
    return metadata, manifest, preflight, control


def _manifest_fresh(manifest: WorkflowPatchCohortManifest) -> bool:
    try:
        expires = datetime.fromisoformat(manifest.expires_at).astimezone(timezone.utc)
    except ValueError:
        return False
    return utc_now().astimezone(timezone.utc) <= expires


def _sealed_path(root: Path, relative: object, folder: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Workflow Patch cohort sealed path is invalid")
    unresolved = root / relative
    candidate = unresolved.resolve()
    boundary = (root / folder).resolve()
    if (
        Path(relative).is_absolute()
        or candidate.parent != boundary
        or not candidate.is_file()
        or unresolved.is_symlink()
    ):
        raise ValueError("Workflow Patch artifact escaped its sealed directory")
    return candidate


def _expected(
    manifest: WorkflowPatchCohortManifest,
    strategy: str,
) -> WorkflowPatchExpectedRun:
    return next(item for item in manifest.expected_runs if item.strategy == strategy)


def _validate_record(
    path: Path,
    manifest: WorkflowPatchCohortManifest,
    *,
    strategy: str,
) -> LiveWorkflowPatchRecord:
    record = load_live_workflow_patch_record(path)
    expected = _expected(manifest, strategy)
    if (
        record.evidence_class != WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS
        or record.campaign_id != manifest.campaign_id
        or record.matched_context_hash != manifest.matched_context_hash
        or record.source_revision != manifest.source_revision
        or record.distribution_sha256 != manifest.distribution_sha256
        or record.model_id != manifest.model_id
        or record.company_revision != manifest.company_revision
        or record.roster_revision != manifest.roster_revision
        or record.playbook_revision != expected.playbook_revision
        or record.memory_revision != manifest.memory_revision
        or record.fixture_revision != manifest.fixture_revision
        or record.benchmark_revision != manifest.benchmark_revision
        or record.strategy != strategy
        or record.identity.workload_hash != expected.workload_hash
        or record.identity.run_id != expected.run_id
        or record.configured_model_call_limit != manifest.max_model_calls_per_run
        or record.configured_wall_time_ms != manifest.max_wall_time_ms_per_run
        or record.external_model_calls > manifest.max_model_calls_per_run
        or (record.task_success and not record.validation.passed)
    ):
        raise ValueError("Workflow Patch live record violates the manifest")
    return record


def _validate_failure(
    path: Path,
    manifest: WorkflowPatchCohortManifest,
    *,
    slot: str,
    strategy: str,
) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = _expected(manifest, strategy)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKFLOW_PATCH_COHORT_FAILURE_SCHEMA
        or value.get("campaign_id") != manifest.campaign_id
        or value.get("slot") != slot
        or value.get("strategy") != strategy
        or value.get("evaluation_run_id") != expected.run_id
        or value.get("workload_hash") != expected.workload_hash
        or value.get("quota_confirmed") is not True
        or value.get("partial_result_promoted") is not False
        or not str(value.get("failure_code", "")).strip()
    ):
        raise ValueError("Workflow Patch cohort failure contract is invalid")



def _verify_runtime_inputs(
    metadata: Mapping[str, object],
    manifest: WorkflowPatchCohortManifest,
) -> None:
    if (
        source_snapshot_revision(Path(str(metadata["source_root"])))
        != manifest.source_revision
    ):
        raise ValueError("Workflow Patch cohort source snapshot changed")
    if (
        wheel_distribution_sha256(Path(str(metadata["wheel_path"])))
        != manifest.distribution_sha256
    ):
        raise ValueError("Workflow Patch cohort wheel changed")
    if (
        workflow_patch_memory_revision() != manifest.memory_revision
        or workflow_patch_fixture_revision() != manifest.fixture_revision
        or workflow_patch_benchmark_revision() != manifest.benchmark_revision
        or workflow_patch_pattern_id() != manifest.candidate_pattern_id
    ):
        raise ValueError("Workflow Patch evaluation contract changed")


def _company_store(root: Path, metadata: Mapping[str, object]) -> CompanyStateStore:
    company_name = str(metadata.get("company_db", ""))
    if "/" in company_name or "\\" in company_name or company_name != _COMPANY_DB:
        raise ValueError("Workflow Patch Company database path is invalid")
    return CompanyStateStore(root / company_name)


def _episode(
    record: LiveWorkflowPatchRecord,
    baseline: LiveWorkflowPatchRecord,
    *,
    planning_mode: str,
) -> OrganizationEpisode:
    violations: list[str] = []
    if not record.safety.passed:
        violations.append("live_safety_gate_failed")
    if not record.validation.passed:
        violations.append("completion_validation_failed")
    if record.cost.tool_calls:
        violations.append("unexpected_tool_call")
    return OrganizationEpisode.create(
        job_id=record.evidence_id,
        source=EvidenceSource.LIVE_EVALUATION,
        task_family=WORKFLOW_PATCH_FAMILY,
        context_fingerprint=WORKFLOW_PATCH_CONTEXT,
        execution_profile=CompilerExecutionProfile.READ_ONLY.value,
        planning_mode=planning_mode,
        plan_template=workflow_patch_template(),
        success=record.task_success,
        quality_score=record.artifact.quality_score,
        baseline_quality_score=baseline.artifact.quality_score,
        model_calls=record.external_model_calls,
        baseline_model_calls=baseline.external_model_calls,
        employee_count=record.admission.employee_count,
        maximum_parallelism=record.trajectory.maximum_parallelism,
        writer_count=record.safety.final_writer_count,
        approvals_requested=0,
        approvals_granted=0,
        preapproval_mutations=0,
        validation_attempts=(record.validation.passed,),
        safety_violations=tuple(violations),
        ledger_digest=record.content_hash,
        recorded_at=record.recorded_at,
    )


