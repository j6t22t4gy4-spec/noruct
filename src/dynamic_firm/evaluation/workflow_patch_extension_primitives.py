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
    WORKFLOW_PATCH_EXTENSION_FAILURE_SCHEMA,
    WORKFLOW_PATCH_EXTENSION_LEDGER_SCHEMA,
    WORKFLOW_PATCH_EXTENSION_MANIFEST_SCHEMA,
    WORKFLOW_PATCH_EXTENSION_PREFLIGHT_SCHEMA,
    _ParentEvidence,
    WorkflowPatchExtensionExpectedRun,
    WorkflowPatchExtensionManifest,
    WorkflowPatchExtensionPreflight,
    WorkflowPatchExtensionStore,
)

def workflow_patch_extension_expected_runs() -> tuple[tuple[str, str], ...]:
    return _SLOTS


def _manifest_fresh(manifest: WorkflowPatchExtensionManifest) -> bool:
    try:
        expires = datetime.fromisoformat(manifest.expires_at).astimezone(timezone.utc)
    except ValueError:
        return False
    return utc_now().astimezone(timezone.utc) <= expires


def _sealed_path(root: Path, relative: object, folder: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Workflow Patch extension sealed path is invalid")
    unresolved = root / relative
    candidate = unresolved.resolve()
    boundary = (root / folder).resolve()
    if (
        Path(relative).is_absolute()
        or candidate.parent != boundary
        or not candidate.is_file()
        or unresolved.is_symlink()
    ):
        raise ValueError("Workflow Patch extension artifact escaped its directory")
    return candidate


def _parent_evidence(directory: str | Path) -> _ParentEvidence:
    root = Path(directory).expanduser().resolve()
    status = workflow_patch_cohort_status(root)
    if (
        status.completed_runs != 4
        or status.failed_runs
        or status.interrupted_runs
        or not status.viable
        or status.patch_status != WorkflowPatchStatus.APPLIED.value
        or len(status.record_paths) != 4
    ):
        raise ValueError("Parent Workflow Patch cohort is not a complete applied cohort")
    with WorkflowPatchCohortStore(root) as store:
        metadata, manifest, _, _ = _campaign_artifacts(store)
        events = store.events()
        recorded = {
            (event.fixture, event.strategy): event
            for event in events
            if event.kind == CampaignEventKind.RUN_RECORDED
        }
        records = tuple(
            _validate_parent_record(
                root
                / str(recorded[(item.slot, item.strategy)].payload["record_path"]),
                manifest,
                strategy=item.strategy,
            )
            for item in manifest.expected_runs
        )
    with _parent_company_store(root, metadata) as company:
        patches = company.list_patches()
        if len(patches) != 1:
            raise ValueError("Parent cohort must contain exactly one Workflow Patch")
        patch = patches[0]
        observations = company.list_observations(patch.patch_id)
        assessments = company.list_assessments(patch.patch_id)
        if (
            patch.status != WorkflowPatchStatus.APPLIED
            or patch.applied_revision != manifest.applied_playbook_revision
            or patch.pattern.pattern_id != manifest.candidate_pattern_id
            or len(observations) != 1
            or assessments
        ):
            raise ValueError("Parent cohort is not the one-observation applied seed")
        company_payload = {
            "company": to_primitive(company.company()),
            "roster": to_primitive(company.roster()),
            "playbook": to_primitive(company.playbook()),
            "summary": to_primitive(company.summary()),
            "episodes": to_primitive(company.list_episodes()),
            "patches": to_primitive(patches),
            "patch_events": to_primitive(company.list_patch_events(patch.patch_id)),
            "observation_contract": to_primitive(
                company.get_observation_contract(patch.patch_id)
            ),
            "observations": to_primitive(observations),
            "assessments": to_primitive(assessments),
        }
    company_seed_hash = content_digest(company_payload)
    semantic_anchor = content_digest(
        {
            "schema": "noruct.workflow-patch-parent-semantic-anchor.v1",
            "manifest_content_hash": manifest.content_hash,
            "ledger": tuple(
                {
                    "sequence": event.sequence,
                    "event_hash": event.event_hash,
                    "kind": event.kind.value,
                    "fixture": event.fixture,
                    "strategy": event.strategy,
                    "payload": event.payload,
                }
                for event in events
            ),
            "records": tuple(
                {
                    "content_hash": record.content_hash,
                    "run_id": record.identity.run_id,
                    "strategy": record.strategy,
                }
                for record in records
            ),
            "company": company_payload,
        }
    )
    return _ParentEvidence(
        directory=root,
        metadata=metadata,
        manifest=manifest,
        baseline=records[0],
        applied=records[-1],
        semantic_anchor=semantic_anchor,
        company_seed_hash=company_seed_hash,
        patch_id=patch.patch_id,
        pattern_id=patch.pattern.pattern_id,
        observation_id=observations[0].observation_id,
        observation_content_hash=observations[0].content_hash,
    )


def _clone_company_database(parent: _ParentEvidence, target: Path) -> None:
    source_name = str(parent.metadata.get("company_db", ""))
    if (
        "/" in source_name
        or "\\" in source_name
        or source_name != "isolated-company.db"
    ):
        raise ValueError("Parent Company database path is invalid")
    source = parent.directory / source_name
    if not source.is_file() or source.is_symlink() or target.exists():
        raise ValueError("Parent Company database cannot be cloned safely")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as origin:
        with sqlite3.connect(target) as destination:
            origin.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError("Cloned Company database failed integrity check")
    os.chmod(target, 0o600)


def _company_seed_payload(company: CompanyStateStore, patch_id: str) -> Mapping[str, object]:
    return {
        "company": to_primitive(company.company()),
        "roster": to_primitive(company.roster()),
        "playbook": to_primitive(company.playbook()),
        "summary": to_primitive(company.summary()),
        "episodes": to_primitive(company.list_episodes()),
        "patches": to_primitive(company.list_patches()),
        "patch_events": to_primitive(company.list_patch_events(patch_id)),
        "observation_contract": to_primitive(
            company.get_observation_contract(patch_id)
        ),
        "observations": to_primitive(company.list_observations(patch_id)),
        "assessments": to_primitive(company.list_assessments(patch_id)),
    }


def _create_preflight(
    *,
    source_revision: str,
    distribution_sha256: str,
    model: str,
    provider_free_control_hash: str,
    cloned_company_seed_hash: str,
    checks: tuple[InformationBoundaryCheck, ...],
) -> WorkflowPatchExtensionPreflight:
    base = WorkflowPatchExtensionPreflight(
        schema_version=WORKFLOW_PATCH_EXTENSION_PREFLIGHT_SCHEMA,
        preflight_id="pending",
        content_hash="pending",
        recorded_at=utc_now().isoformat(),
        noruct_version=__version__,
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        model_id=model,
        provider_free_control_hash=provider_free_control_hash,
        cloned_company_seed_hash=cloned_company_seed_hash,
        external_model_calls=0,
        quota_consumed=False,
        ready=all(check.passed for check in checks),
        checks=checks,
    )
    digest = content_digest(base.content_payload())
    return WorkflowPatchExtensionPreflight(
        **{
            **to_primitive(base),
            "preflight_id": f"workflow-patch-extension-preflight-{digest[:24]}",
            "content_hash": digest,
            "checks": checks,
        }
    )


def _create_manifest(
    preflight: WorkflowPatchExtensionPreflight,
    parent: _ParentEvidence,
    *,
    max_model_calls_per_run: int,
    max_model_calls_extension: int,
    max_wall_time_ms_per_run: int,
    lifetime_hours: int,
) -> WorkflowPatchExtensionManifest:
    if not 1 <= max_model_calls_per_run <= 8:
        raise ValueError("Workflow Patch extension allows one to eight calls per run")
    if (
        max_model_calls_extension != max_model_calls_per_run * _MAX_RECORDS
        or max_model_calls_extension > 16
    ):
        raise ValueError("Workflow Patch extension budget must equal two bounded runs")
    if (
        not 1_000 <= max_wall_time_ms_per_run <= 600_000
        or not 1 <= lifetime_hours <= 336
    ):
        raise ValueError("Workflow Patch extension time bounds are invalid")
    parent_manifest = parent.manifest
    matched_hash = workflow_patch_matched_context_hash(
        model_profile=preflight.model_id,
        company_revision=parent_manifest.company_revision,
        roster_revision=parent_manifest.roster_revision,
        max_total_model_calls=max_model_calls_per_run,
        max_wall_time_ms=max_wall_time_ms_per_run,
    )
    if matched_hash != parent_manifest.matched_context_hash:
        raise ValueError("Workflow Patch extension does not match the parent context")
    expected = tuple(
        WorkflowPatchExtensionExpectedRun(
            slot=slot,
            strategy=strategy,
            playbook_revision=parent_manifest.applied_playbook_revision,
            workload_hash=identity.workload_hash,
            run_id=identity.run_id,
        )
        for slot, strategy in _SLOTS
        for identity in (
            workflow_patch_live_identity(
                strategy=strategy,
                model_profile=preflight.model_id,
                company_revision=parent_manifest.company_revision,
                roster_revision=parent_manifest.roster_revision,
                playbook_revision=parent_manifest.applied_playbook_revision,
                max_total_model_calls=max_model_calls_per_run,
                max_wall_time_ms=max_wall_time_ms_per_run,
            ),
        )
    )
    created = utc_now().astimezone(timezone.utc)
    expires = created + timedelta(hours=lifetime_hours)
    base = WorkflowPatchExtensionManifest(
        schema_version=WORKFLOW_PATCH_EXTENSION_MANIFEST_SCHEMA,
        extension_id="pending",
        content_hash="pending",
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        noruct_version=__version__,
        source_revision=preflight.source_revision,
        distribution_sha256=preflight.distribution_sha256,
        provider_kind="openai-codex-user-managed",
        model_id=preflight.model_id,
        authority_profile=parent_manifest.authority_profile,
        company_revision=parent_manifest.company_revision,
        roster_revision=parent_manifest.roster_revision,
        applied_playbook_revision=parent_manifest.applied_playbook_revision,
        memory_revision=parent_manifest.memory_revision,
        fixture_revision=parent_manifest.fixture_revision,
        benchmark_revision=parent_manifest.benchmark_revision,
        matched_context_hash=parent_manifest.matched_context_hash,
        patch_id=parent.patch_id,
        pattern_id=parent.pattern_id,
        parent_campaign_id=parent_manifest.campaign_id,
        parent_manifest_content_hash=parent_manifest.content_hash,
        parent_semantic_anchor=parent.semantic_anchor,
        parent_baseline_content_hash=parent.baseline.content_hash,
        parent_applied_content_hash=parent.applied.content_hash,
        parent_observation_id=parent.observation_id,
        parent_observation_content_hash=parent.observation_content_hash,
        max_records=_MAX_RECORDS,
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_extension=max_model_calls_extension,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
        expected_runs=expected,
    )
    digest = content_digest(base.content_payload())
    return WorkflowPatchExtensionManifest(
        **{
            **to_primitive(base),
            "extension_id": f"workflow-patch-extension-{digest[:24]}",
            "content_hash": digest,
            "expected_runs": expected,
        }
    )


def _load_manifest(path: Path) -> WorkflowPatchExtensionManifest:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Workflow Patch extension manifest is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKFLOW_PATCH_EXTENSION_MANIFEST_SCHEMA
    ):
        raise ValueError("Workflow Patch extension manifest schema is incompatible")
    expected = tuple(
        WorkflowPatchExtensionExpectedRun(**item) for item in value["expected_runs"]
    )
    manifest = WorkflowPatchExtensionManifest(
        **{
            **{key: item for key, item in value.items() if key != "expected_runs"},
            "expected_runs": expected,
        }
    )
    if (
        manifest.content_hash != content_digest(manifest.content_payload())
        or manifest.extension_id
        != f"workflow-patch-extension-{manifest.content_hash[:24]}"
        or tuple((item.slot, item.strategy) for item in expected) != _SLOTS
        or len({item.run_id for item in expected}) != _MAX_RECORDS
        or manifest.max_records != _MAX_RECORDS
        or manifest.max_model_calls_extension
        != manifest.max_model_calls_per_run * _MAX_RECORDS
        or manifest.max_model_calls_extension > 16
        or manifest.pattern_id != workflow_patch_pattern_id()
    ):
        raise ValueError("Workflow Patch extension manifest contract is invalid")
    return manifest


def _load_preflight(path: Path) -> WorkflowPatchExtensionPreflight:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Workflow Patch extension preflight is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKFLOW_PATCH_EXTENSION_PREFLIGHT_SCHEMA
    ):
        raise ValueError("Workflow Patch extension preflight schema is incompatible")
    checks = tuple(InformationBoundaryCheck(**item) for item in value["checks"])
    preflight = WorkflowPatchExtensionPreflight(
        **{key: item for key, item in value.items() if key != "checks"},
        checks=checks,
    )
    if (
        preflight.content_hash != content_digest(preflight.content_payload())
        or preflight.preflight_id
        != f"workflow-patch-extension-preflight-{preflight.content_hash[:24]}"
        or preflight.external_model_calls != 0
        or preflight.quota_consumed
        or preflight.ready != all(check.passed for check in checks)
    ):
        raise ValueError("Workflow Patch extension preflight contract is invalid")
    return preflight


def _extension_artifacts(
    store: WorkflowPatchExtensionStore,
) -> tuple[
    dict[str, object],
    WorkflowPatchExtensionManifest,
    WorkflowPatchExtensionPreflight,
    dict[str, object],
]:
    metadata = store.metadata()
    if metadata.get("schema_version") != WORKFLOW_PATCH_EXTENSION_LEDGER_SCHEMA:
        raise ValueError("Workflow Patch extension ledger schema is invalid")
    manifest_path = store.directory / "manifest-v1.json"
    preflight_path = store.directory / "preflight-v1.json"
    control_path = store.directory / "provider-free-control-v1.json"
    for path, key in (
        (manifest_path, "manifest_file_sha256"),
        (preflight_path, "preflight_file_sha256"),
        (control_path, "control_file_sha256"),
    ):
        if _sha256_file(path) != metadata.get(key):
            raise ValueError("Workflow Patch extension sealed artifact changed")
    manifest = _load_manifest(manifest_path)
    preflight = _load_preflight(preflight_path)
    control = json.loads(control_path.read_text(encoding="utf-8"))
    if (
        not isinstance(control, dict)
        or control.get("schema_version") != "noruct.causal-workflow-evaluation.v1"
        or control.get("passed") is not True
        or control.get("external_model_calls") != 0
        or control.get("quota_consumed") is not False
        or manifest.extension_id != metadata.get("extension_id")
        or manifest.source_revision != preflight.source_revision
        or manifest.distribution_sha256 != preflight.distribution_sha256
        or preflight.provider_free_control_hash != _sha256_file(control_path)
    ):
        raise ValueError("Workflow Patch extension identity does not match its ledger")
    return metadata, manifest, preflight, control


def _company_store(
    root: Path,
    metadata: Mapping[str, object],
) -> CompanyStateStore:
    company_name = str(metadata.get("company_db", ""))
    if "/" in company_name or "\\" in company_name or company_name != _COMPANY_DB:
        raise ValueError("Workflow Patch extension Company path is invalid")
    return CompanyStateStore(root / company_name)


def _verify_parent(manifest: WorkflowPatchExtensionManifest, metadata: Mapping[str, object]) -> _ParentEvidence:
    parent = _parent_evidence(Path(str(metadata["parent_directory"])))
    if (
        parent.manifest.campaign_id != manifest.parent_campaign_id
        or parent.manifest.content_hash != manifest.parent_manifest_content_hash
        or parent.semantic_anchor != manifest.parent_semantic_anchor
        or parent.baseline.content_hash != manifest.parent_baseline_content_hash
        or parent.applied.content_hash != manifest.parent_applied_content_hash
        or parent.patch_id != manifest.patch_id
        or parent.pattern_id != manifest.pattern_id
        or parent.observation_id != manifest.parent_observation_id
        or parent.observation_content_hash
        != manifest.parent_observation_content_hash
    ):
        raise ValueError("Parent Workflow Patch cohort changed after extension prepare")
    return parent


def _verify_runtime_inputs(
    metadata: Mapping[str, object],
    manifest: WorkflowPatchExtensionManifest,
) -> None:
    if (
        source_snapshot_revision(Path(str(metadata["source_root"])))
        != manifest.source_revision
    ):
        raise ValueError("Workflow Patch extension source snapshot changed")
    if (
        wheel_distribution_sha256(Path(str(metadata["wheel_path"])))
        != manifest.distribution_sha256
    ):
        raise ValueError("Workflow Patch extension wheel changed")
    if (
        workflow_patch_memory_revision() != manifest.memory_revision
        or workflow_patch_fixture_revision() != manifest.fixture_revision
        or workflow_patch_benchmark_revision() != manifest.benchmark_revision
        or workflow_patch_pattern_id() != manifest.pattern_id
    ):
        raise ValueError("Workflow Patch extension evaluation contract changed")


def _expected(
    manifest: WorkflowPatchExtensionManifest,
    strategy: str,
) -> WorkflowPatchExtensionExpectedRun:
    return next(item for item in manifest.expected_runs if item.strategy == strategy)


def _validate_record(
    path: Path,
    manifest: WorkflowPatchExtensionManifest,
    *,
    strategy: str,
) -> LiveWorkflowPatchRecord:
    record = load_live_workflow_patch_record(path)
    expected = _expected(manifest, strategy)
    if (
        record.evidence_class != WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS
        or record.campaign_id != manifest.extension_id
        or record.matched_context_hash != manifest.matched_context_hash
        or record.source_revision != manifest.source_revision
        or record.distribution_sha256 != manifest.distribution_sha256
        or record.model_id != manifest.model_id
        or record.company_revision != manifest.company_revision
        or record.roster_revision != manifest.roster_revision
        or record.playbook_revision != manifest.applied_playbook_revision
        or record.memory_revision != manifest.memory_revision
        or record.fixture_revision != manifest.fixture_revision
        or record.benchmark_revision != manifest.benchmark_revision
        or record.strategy != strategy
        or record.prior_source != "applied-playbook"
        or record.prior_pattern_ids != (manifest.pattern_id,)
        or record.identity.workload_hash != expected.workload_hash
        or record.identity.run_id != expected.run_id
        or record.configured_model_call_limit != manifest.max_model_calls_per_run
        or record.configured_wall_time_ms != manifest.max_wall_time_ms_per_run
        or record.external_model_calls > manifest.max_model_calls_per_run
        or (record.task_success and not record.validation.passed)
    ):
        raise ValueError("Workflow Patch extension live record violates the manifest")
    return record


def _validate_failure(
    path: Path,
    manifest: WorkflowPatchExtensionManifest,
    *,
    slot: str,
    strategy: str,
) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = _expected(manifest, strategy)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKFLOW_PATCH_EXTENSION_FAILURE_SCHEMA
        or value.get("extension_id") != manifest.extension_id
        or value.get("slot") != slot
        or value.get("strategy") != strategy
        or value.get("evaluation_run_id") != expected.run_id
        or value.get("workload_hash") != expected.workload_hash
        or value.get("quota_confirmed") is not True
        or value.get("partial_result_promoted") is not False
        or not str(value.get("failure_code", "")).strip()
    ):
        raise ValueError("Workflow Patch extension failure contract is invalid")


