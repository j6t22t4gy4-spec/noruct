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


from .workflow_patch_efficiency_contracts import (
    _MAX_RECORDS,
    _ParentEfficiencySeed,
    _SLOTS,
    _SLOTS_V2,
    _SLOTS_V3,
    WORKFLOW_PATCH_EFFICIENCY_DIAGNOSTIC_SCHEMA,
    WORKFLOW_PATCH_EFFICIENCY_LEDGER_SCHEMA,
    WORKFLOW_PATCH_EFFICIENCY_MANIFEST_SCHEMA,
    WORKFLOW_PATCH_EFFICIENCY_PREFLIGHT_SCHEMA,
    WORKFLOW_PATCH_NATURAL_GOAL,
    WorkflowPatchEfficiencyDiagnostic,
    WorkflowPatchEfficiencyExpectedRun,
    WorkflowPatchEfficiencyManifest,
    WorkflowPatchEfficiencyPreflight,
    WorkflowPatchEfficiencyStore,
)

def create_workflow_patch_exact_context_binding(
    preflight_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> ExactContextEvidenceBinding:
    """Bind one verified natural preflight without exposing its file path."""

    binding = create_exact_context_evidence_binding(
        preflight_path,
        execution_profile=CompilerExecutionProfile.READ_ONLY.value,
    )
    if output_path is not None:
        _write_private(
            Path(output_path).expanduser().resolve(),
            exact_context_binding_to_json(binding),
        )
    return binding


def prepare_workflow_patch_exact_context_evaluation(
    parent_directory: str | Path,
    binding_path: str | Path,
    *,
    source_root: str | Path,
    goal: str = WORKFLOW_PATCH_NATURAL_GOAL,
    output_path: str | Path | None = None,
) -> ExactContextBoundPreparation:
    """Create a provider-free, non-applicable lineage for a future live pair."""

    if not goal.strip():
        raise ValueError("Exact-context evaluation goal must be non-empty")
    binding = load_exact_context_evidence_binding(binding_path)
    parent = _parent_seed(parent_directory)
    source_revision = source_snapshot_revision(
        Path(source_root).expanduser().resolve()
    )
    bound_prior = workflow_patch_candidate_prior(
        context_fingerprint=binding.production_context_fingerprint
    )
    preparation = create_exact_context_bound_preparation(
        binding,
        noruct_version=__version__,
        source_revision=source_revision,
        goal_digest=content_digest(goal.strip()),
        execution_profile=CompilerExecutionProfile.READ_ONLY.value,
        parent_extension_id=parent.extension_id,
        parent_pattern_id=parent.pattern_id,
        parent_semantic_anchor=parent.semantic_anchor,
        bound_pattern_id=bound_prior.pattern_id,
    )
    if output_path is not None:
        _write_private(
            Path(output_path).expanduser().resolve(),
            exact_context_binding_to_json(preparation),
        )
    return preparation


def _slots_for_contract_revision(
    completion_contract_revision: str,
) -> tuple[tuple[str, str], ...]:
    if (
        completion_contract_revision
        == WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION
    ):
        return _SLOTS
    if (
        completion_contract_revision
        == WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION
    ):
        return _SLOTS_V2
    if (
        completion_contract_revision
        == WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION
    ):
        return _SLOTS_V3
    raise ValueError("Completion efficiency contract revision is invalid")


def workflow_patch_efficiency_expected_runs(
    completion_contract_revision: str = (
        WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION
    ),
) -> tuple[tuple[str, str], ...]:
    return _slots_for_contract_revision(completion_contract_revision)


def _manifest_fresh(manifest: WorkflowPatchEfficiencyManifest) -> bool:
    try:
        expires = datetime.fromisoformat(manifest.expires_at).astimezone(
            timezone.utc
        )
    except ValueError:
        return False
    return utc_now().astimezone(timezone.utc) <= expires


def _parent_seed(directory: str | Path) -> _ParentEfficiencySeed:
    root = Path(directory).expanduser().resolve()
    status = workflow_patch_extension_status(root)
    if (
        status.state != WorkflowPatchExtensionState.KEEP
        or not status.parent_immutable
        or not status.viable
        or status.patch_status != WorkflowPatchStatus.APPLIED.value
        or status.post_apply_observations != 3
        or status.assessment_decision != "KEEP"
    ):
        raise ValueError(
            "Parent Workflow Patch extension is not a complete KEEP seed"
        )
    with WorkflowPatchExtensionStore(root) as store:
        metadata, manifest, _, _ = _extension_artifacts(store)
        events = store.events()
    with _extension_company_store(root, metadata) as company:
        patch = company.get_patch(manifest.patch_id)
        playbook = company.playbook()
        company_projection = {
            "company": to_primitive(company.company()),
            "roster": to_primitive(company.roster()),
            "playbook": to_primitive(playbook),
            "patch": to_primitive(patch),
            "observations": to_primitive(
                company.list_observations(manifest.patch_id)
            ),
            "assessments": to_primitive(
                company.list_assessments(manifest.patch_id)
            ),
        }
    if (
        patch.status != WorkflowPatchStatus.APPLIED
        or patch.pattern.pattern_id != workflow_patch_pattern_id()
        or playbook.revision != manifest.applied_playbook_revision
        or len(playbook.patterns) != 1
    ):
        raise ValueError("Parent Workflow Patch applied state drifted")
    anchor = content_digest(
        {
            "schema": "noruct.workflow-patch-efficiency-parent-anchor.v1",
            "manifest_content_hash": manifest.content_hash,
            "events": tuple(
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
            "company": company_projection,
        }
    )
    return _ParentEfficiencySeed(
        directory=root,
        extension_id=manifest.extension_id,
        semantic_anchor=anchor,
        model_id=manifest.model_id,
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=manifest.applied_playbook_revision,
        pattern_id=manifest.pattern_id,
    )


def _create_preflight(
    *,
    source_revision: str,
    distribution_sha256: str,
    model: str,
    parent_anchor: str,
    provider_free_control_hash: str,
    checks: tuple[InformationBoundaryCheck, ...],
) -> WorkflowPatchEfficiencyPreflight:
    base = WorkflowPatchEfficiencyPreflight(
        schema_version=WORKFLOW_PATCH_EFFICIENCY_PREFLIGHT_SCHEMA,
        preflight_id="pending",
        content_hash="pending",
        recorded_at=utc_now().isoformat(),
        noruct_version=__version__,
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        model_id=model,
        parent_semantic_anchor=parent_anchor,
        provider_free_control_hash=provider_free_control_hash,
        external_model_calls=0,
        quota_consumed=False,
        ready=all(check.passed for check in checks),
        checks=checks,
    )
    digest = content_digest(base.content_payload())
    return WorkflowPatchEfficiencyPreflight(
        **{
            **to_primitive(base),
            "preflight_id": (
                f"workflow-patch-efficiency-preflight-{digest[:24]}"
            ),
            "content_hash": digest,
            "checks": checks,
        }
    )


def _create_manifest(
    preflight: WorkflowPatchEfficiencyPreflight,
    parent: _ParentEfficiencySeed,
    *,
    max_model_calls_per_run: int,
    max_model_calls_pair: int,
    max_wall_time_ms_per_run: int,
    lifetime_hours: int,
    completion_contract_revision: str,
) -> WorkflowPatchEfficiencyManifest:
    if not 1 <= max_model_calls_per_run <= 8:
        raise ValueError("Completion efficiency allows one to eight calls per run")
    if (
        max_model_calls_pair != max_model_calls_per_run * _MAX_RECORDS
        or max_model_calls_pair > 16
    ):
        raise ValueError(
            "Completion efficiency pair budget must equal two bounded runs"
        )
    if (
        not 1_000 <= max_wall_time_ms_per_run <= 600_000
        or not 1 <= lifetime_hours <= 336
    ):
        raise ValueError("Completion efficiency time bounds are invalid")
    slots = _slots_for_contract_revision(completion_contract_revision)
    expected = tuple(
        WorkflowPatchEfficiencyExpectedRun(
            slot=slot,
            strategy=strategy,
            workload_hash=identity.workload_hash,
            run_id=identity.run_id,
        )
        for slot, strategy in slots
        for identity in (
            workflow_patch_live_identity(
                strategy=strategy,
                model_profile=preflight.model_id,
                company_revision=parent.company_revision,
                roster_revision=parent.roster_revision,
                playbook_revision=parent.playbook_revision,
                max_total_model_calls=max_model_calls_per_run,
                max_wall_time_ms=max_wall_time_ms_per_run,
            ),
        )
    )
    created = utc_now().astimezone(timezone.utc)
    expires = created + timedelta(hours=lifetime_hours)
    base = WorkflowPatchEfficiencyManifest(
        schema_version=WORKFLOW_PATCH_EFFICIENCY_MANIFEST_SCHEMA,
        pair_id="pending",
        content_hash="pending",
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        noruct_version=__version__,
        source_revision=preflight.source_revision,
        distribution_sha256=preflight.distribution_sha256,
        provider_kind="openai-codex-user-managed",
        model_id=preflight.model_id,
        authority_profile=INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
        company_revision=parent.company_revision,
        roster_revision=parent.roster_revision,
        playbook_revision=parent.playbook_revision,
        memory_revision=workflow_patch_memory_revision(),
        fixture_revision=workflow_patch_fixture_revision(),
        benchmark_revision=workflow_patch_efficiency_benchmark_revision(
            completion_contract_revision
        ),
        matched_context_hash=workflow_patch_efficiency_matched_context_hash(
            model_profile=preflight.model_id,
            company_revision=parent.company_revision,
            roster_revision=parent.roster_revision,
            playbook_revision=parent.playbook_revision,
            max_total_model_calls=max_model_calls_per_run,
            max_wall_time_ms=max_wall_time_ms_per_run,
            completion_contract_revision=completion_contract_revision,
        ),
        pattern_id=parent.pattern_id,
        parent_extension_id=parent.extension_id,
        parent_semantic_anchor=parent.semantic_anchor,
        completion_contract_revision=completion_contract_revision,
        completion_validator_revision=(
            WORKFLOW_PATCH_COMPLETION_VALIDATOR_REVISION
        ),
        max_records=_MAX_RECORDS,
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_pair=max_model_calls_pair,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
        expected_runs=expected,
    )
    digest = content_digest(base.content_payload())
    return WorkflowPatchEfficiencyManifest(
        **{
            **to_primitive(base),
            "pair_id": f"workflow-patch-efficiency-{digest[:24]}",
            "content_hash": digest,
            "expected_runs": expected,
        }
    )


def _load_manifest(path: Path) -> WorkflowPatchEfficiencyManifest:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Completion efficiency manifest is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != WORKFLOW_PATCH_EFFICIENCY_MANIFEST_SCHEMA
    ):
        raise ValueError("Completion efficiency manifest schema is incompatible")
    expected = tuple(
        WorkflowPatchEfficiencyExpectedRun(**item)
        for item in value["expected_runs"]
    )
    manifest = WorkflowPatchEfficiencyManifest(
        **{
            **{key: item for key, item in value.items() if key != "expected_runs"},
            "expected_runs": expected,
        }
    )
    if (
        manifest.content_hash != content_digest(manifest.content_payload())
        or manifest.pair_id
        != f"workflow-patch-efficiency-{manifest.content_hash[:24]}"
        or tuple((item.slot, item.strategy) for item in expected)
        != _slots_for_contract_revision(
            manifest.completion_contract_revision
        )
        or len({item.run_id for item in expected}) != _MAX_RECORDS
        or manifest.max_records != _MAX_RECORDS
        or manifest.max_model_calls_pair
        != manifest.max_model_calls_per_run * _MAX_RECORDS
        or manifest.pattern_id != workflow_patch_pattern_id()
        or manifest.benchmark_revision
        != workflow_patch_efficiency_benchmark_revision(
            manifest.completion_contract_revision
        )
    ):
        raise ValueError("Completion efficiency manifest contract is invalid")
    return manifest


def _load_preflight(path: Path) -> WorkflowPatchEfficiencyPreflight:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Completion efficiency preflight is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != WORKFLOW_PATCH_EFFICIENCY_PREFLIGHT_SCHEMA
    ):
        raise ValueError("Completion efficiency preflight schema is incompatible")
    checks = tuple(InformationBoundaryCheck(**item) for item in value["checks"])
    preflight = WorkflowPatchEfficiencyPreflight(
        **{key: item for key, item in value.items() if key != "checks"},
        checks=checks,
    )
    if (
        preflight.content_hash != content_digest(preflight.content_payload())
        or preflight.preflight_id
        != f"workflow-patch-efficiency-preflight-{preflight.content_hash[:24]}"
        or preflight.external_model_calls != 0
        or preflight.quota_consumed
        or preflight.ready != all(check.passed for check in checks)
    ):
        raise ValueError("Completion efficiency preflight contract is invalid")
    return preflight


def _pair_artifacts(
    store: WorkflowPatchEfficiencyStore,
) -> tuple[
    dict[str, object],
    WorkflowPatchEfficiencyManifest,
    WorkflowPatchEfficiencyPreflight,
    Mapping[str, object],
]:
    metadata = store.metadata()
    if metadata.get("schema_version") != WORKFLOW_PATCH_EFFICIENCY_LEDGER_SCHEMA:
        raise ValueError("Completion efficiency ledger schema is invalid")
    manifest_path = store.directory / "manifest-v1.json"
    preflight_path = store.directory / "preflight-v1.json"
    control_path = store.directory / "provider-free-control-v1.json"
    for path, key in (
        (manifest_path, "manifest_file_sha256"),
        (preflight_path, "preflight_file_sha256"),
        (control_path, "control_file_sha256"),
    ):
        if _sha256_file(path) != metadata.get(key):
            raise ValueError("Completion efficiency sealed artifact changed")
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
        or manifest.pair_id != metadata.get("pair_id")
        or manifest.source_revision != preflight.source_revision
        or manifest.distribution_sha256 != preflight.distribution_sha256
        or preflight.provider_free_control_hash != _sha256_file(control_path)
    ):
        raise ValueError("Completion efficiency identity does not match ledger")
    return metadata, manifest, preflight, control


def _verify_parent(
    manifest: WorkflowPatchEfficiencyManifest,
    metadata: Mapping[str, object],
) -> _ParentEfficiencySeed:
    parent = _parent_seed(Path(str(metadata["parent_directory"])))
    if (
        parent.extension_id != manifest.parent_extension_id
        or parent.semantic_anchor != manifest.parent_semantic_anchor
        or parent.model_id != manifest.model_id
        or parent.company_revision != manifest.company_revision
        or parent.roster_revision != manifest.roster_revision
        or parent.playbook_revision != manifest.playbook_revision
        or parent.pattern_id != manifest.pattern_id
    ):
        raise ValueError("Completion efficiency parent changed after prepare")
    return parent


def _verify_runtime_inputs(
    metadata: Mapping[str, object],
    manifest: WorkflowPatchEfficiencyManifest,
) -> None:
    if (
        source_snapshot_revision(Path(str(metadata["source_root"])))
        != manifest.source_revision
    ):
        raise ValueError("Completion efficiency source snapshot changed")
    if (
        wheel_distribution_sha256(Path(str(metadata["wheel_path"])))
        != manifest.distribution_sha256
    ):
        raise ValueError("Completion efficiency wheel changed")
    if (
        workflow_patch_memory_revision() != manifest.memory_revision
        or workflow_patch_fixture_revision() != manifest.fixture_revision
        or workflow_patch_efficiency_benchmark_revision(
            manifest.completion_contract_revision
        )
        != manifest.benchmark_revision
        or workflow_patch_pattern_id() != manifest.pattern_id
        or manifest.completion_contract_revision
        not in {
            WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION,
            WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION,
            WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION,
        }
        or WORKFLOW_PATCH_COMPLETION_VALIDATOR_REVISION
        != manifest.completion_validator_revision
    ):
        raise ValueError("Completion efficiency contract changed")


def _expected(
    manifest: WorkflowPatchEfficiencyManifest,
    strategy: str,
) -> WorkflowPatchEfficiencyExpectedRun:
    return next(item for item in manifest.expected_runs if item.strategy == strategy)


def _sealed_path(root: Path, relative: object, folder: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Completion efficiency sealed path is invalid")
    unresolved = root / relative
    candidate = unresolved.resolve()
    boundary = (root / folder).resolve()
    if (
        Path(relative).is_absolute()
        or candidate.parent != boundary
        or not candidate.is_file()
        or unresolved.is_symlink()
    ):
        raise ValueError("Completion efficiency artifact escaped its directory")
    return candidate


def _validate_record(
    path: Path,
    manifest: WorkflowPatchEfficiencyManifest,
    *,
    strategy: str,
) -> LiveWorkflowPatchRecord:
    record = load_live_workflow_patch_record(path)
    expected = _expected(manifest, strategy)
    if (
        record.evidence_class != WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS
        or record.campaign_id != manifest.pair_id
        or record.matched_context_hash != manifest.matched_context_hash
        or record.source_revision != manifest.source_revision
        or record.distribution_sha256 != manifest.distribution_sha256
        or record.model_id != manifest.model_id
        or record.company_revision != manifest.company_revision
        or record.roster_revision != manifest.roster_revision
        or record.playbook_revision != manifest.playbook_revision
        or record.memory_revision != manifest.memory_revision
        or record.fixture_revision != manifest.fixture_revision
        or record.benchmark_revision != manifest.benchmark_revision
        or record.strategy != strategy
        or record.prior_source != "applied-playbook"
        or record.prior_pattern_ids != (manifest.pattern_id,)
        or record.identity.workload_hash != expected.workload_hash
        or record.identity.run_id != expected.run_id
        or record.configured_model_call_limit
        != manifest.max_model_calls_per_run
        or record.configured_wall_time_ms
        != manifest.max_wall_time_ms_per_run
        or record.external_model_calls > manifest.max_model_calls_per_run
        or (record.task_success and not record.validation.passed)
    ):
        raise ValueError("Completion efficiency live record violates manifest")
    return record


def _create_diagnostic(
    manifest: WorkflowPatchEfficiencyManifest,
    strategy: str,
    record: LiveWorkflowPatchRecord,
    attempts: tuple[WorkflowPatchCompletionAttemptProjection, ...],
) -> WorkflowPatchEfficiencyDiagnostic:
    failed_count = sum(not item.passed for item in attempts)
    base = WorkflowPatchEfficiencyDiagnostic(
        schema_version=WORKFLOW_PATCH_EFFICIENCY_DIAGNOSTIC_SCHEMA,
        diagnostic_id="pending",
        content_hash="pending",
        pair_id=manifest.pair_id,
        strategy=strategy,
        live_record_content_hash=record.content_hash,
        attempt_count=len(attempts),
        failed_attempt_count=failed_count,
        repair_count=failed_count,
        attempts=attempts,
    )
    digest = content_digest(base.content_payload())
    return WorkflowPatchEfficiencyDiagnostic(
        **{
            **to_primitive(base),
            "diagnostic_id": (
                f"workflow-patch-completion-diagnostic-{digest[:24]}"
            ),
            "content_hash": digest,
            "attempts": attempts,
        }
    )


def _load_diagnostic(
    path: Path,
    manifest: WorkflowPatchEfficiencyManifest,
    *,
    strategy: str,
    live_record_content_hash: str,
) -> WorkflowPatchEfficiencyDiagnostic:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != WORKFLOW_PATCH_EFFICIENCY_DIAGNOSTIC_SCHEMA
    ):
        raise ValueError("Completion diagnostic schema is incompatible")
    attempts = tuple(
        WorkflowPatchCompletionAttemptProjection(
            **{
                **item,
                "failed_checks": tuple(item["failed_checks"]),
                "separators": tuple(item["separators"]),
                "duplicate_fields": tuple(item["duplicate_fields"]),
                "conflicting_fields": tuple(item["conflicting_fields"]),
                "signal_codes": tuple(item["signal_codes"]),
            }
        )
        for item in value["attempts"]
    )
    diagnostic = WorkflowPatchEfficiencyDiagnostic(
        **{
            **{key: item for key, item in value.items() if key != "attempts"},
            "attempts": attempts,
        }
    )
    if (
        diagnostic.content_hash != content_digest(diagnostic.content_payload())
        or diagnostic.diagnostic_id
        != (
            "workflow-patch-completion-diagnostic-"
            f"{diagnostic.content_hash[:24]}"
        )
        or diagnostic.pair_id != manifest.pair_id
        or diagnostic.strategy != strategy
        or diagnostic.live_record_content_hash != live_record_content_hash
        or diagnostic.attempt_count != len(attempts)
        or diagnostic.failed_attempt_count
        != sum(not item.passed for item in attempts)
        or diagnostic.repair_count != diagnostic.failed_attempt_count
        or any(item.model_call_index < 1 for item in attempts)
    ):
        raise ValueError("Completion diagnostic contract is invalid")
    return diagnostic
