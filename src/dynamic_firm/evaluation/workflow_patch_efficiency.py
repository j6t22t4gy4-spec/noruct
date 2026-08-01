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



from .workflow_patch_efficiency_contracts import (
    _MAX_RECORDS,
    WORKFLOW_PATCH_EFFICIENCY_COMPARISON_SCHEMA,
    WORKFLOW_PATCH_EFFICIENCY_FAILURE_SCHEMA,
    WORKFLOW_PATCH_NATURAL_GOAL,
    WorkflowPatchEfficiencyComparison,
    WorkflowPatchEfficiencyDiagnostic,
    WorkflowPatchEfficiencyExpectedRun,
    WorkflowPatchEfficiencyManifest,
    WorkflowPatchEfficiencyRunResult,
    WorkflowPatchEfficiencyState,
    WorkflowPatchEfficiencyStatus,
    WorkflowPatchEfficiencyStore,
)
from .workflow_patch_efficiency_natural import (
    evaluate_workflow_patch_natural_preflight,
)
from .workflow_patch_efficiency_preparation import (
    prepare_workflow_patch_efficiency_pair,
)
from .workflow_patch_efficiency_primitives import (
    _create_diagnostic,
    _expected,
    _load_diagnostic,
    _pair_artifacts,
    _parent_seed,
    _sealed_path,
    _validate_record,
    _verify_parent,
    _verify_runtime_inputs,
    create_workflow_patch_exact_context_binding,
    prepare_workflow_patch_exact_context_evaluation,
)
from .workflow_patch_efficiency_status import workflow_patch_efficiency_status

async def run_next_workflow_patch_efficiency_slot(
    directory: str | Path,
    *,
    confirm_live_quota: bool,
    provider_factory=None,
    live_runner: Callable[..., Awaitable[LiveWorkflowPatchRecord]] | None = None,
) -> WorkflowPatchEfficiencyRunResult:
    status = workflow_patch_efficiency_status(directory)
    if (
        status.state != WorkflowPatchEfficiencyState.READY
        or not status.next_slot
        or not status.next_strategy
    ):
        raise ValueError(
            "Completion efficiency pair cannot start while "
            f"state={status.state.value}"
        )
    if not confirm_live_quota:
        raise ValueError(
            "Completion efficiency pair requires --confirm-live-quota for "
            f"exactly one slot: {status.next_slot}/{status.next_strategy}"
        )
    with WorkflowPatchEfficiencyStore(directory) as store:
        metadata, manifest, _, _ = _pair_artifacts(store)
        _verify_parent(manifest, metadata)
        _verify_runtime_inputs(metadata, manifest)
        expected = _expected(manifest, status.next_strategy)
        start = store.append(
            CampaignEventKind.RUN_STARTED,
            fixture=status.next_slot,
            strategy=status.next_strategy,
            payload={
                "attempt": 1,
                "pid": os.getpid(),
                "quota_confirmed": True,
                "max_model_calls": manifest.max_model_calls_per_run,
                "max_wall_time_ms": manifest.max_wall_time_ms_per_run,
                "evaluation_run_id": expected.run_id,
                "workload_hash": expected.workload_hash,
            },
        )
    config = LiveWorkflowPatchConfig(
        command=str(metadata["codex_command"]),
        model=manifest.model_id,
        source_revision=manifest.source_revision,
        distribution_sha256=manifest.distribution_sha256,
        campaign_id=manifest.pair_id,
        matched_context_hash=manifest.matched_context_hash,
        timeout_seconds=float(metadata["request_timeout_seconds"]),
        max_total_model_calls=manifest.max_model_calls_per_run,
        max_wall_time_ms=manifest.max_wall_time_ms_per_run,
        quota_confirmed=True,
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=manifest.playbook_revision,
    )
    runner = live_runner or run_live_workflow_patch_evaluation
    projected: list[WorkflowPatchCompletionAttemptProjection] = []
    try:
        record = await runner(
            config,
            status.next_strategy,
            workflow_priors=(workflow_patch_candidate_prior(),),
            prior_source="applied-playbook",
            provider_factory=provider_factory,
            diagnostic_sink=projected.extend,
        )
        if record.identity.run_id != expected.run_id:
            raise ValueError("Completion efficiency run identity changed")
        diagnostic = _create_diagnostic(
            manifest,
            status.next_strategy,
            record,
            tuple(projected),
        )
        root = Path(directory).expanduser().resolve()
        sequence = start.sequence + 1
        stem = f"{sequence:02d}-{status.next_slot}-{status.next_strategy}"
        record_path = _write_private(
            root / "records-v1" / f"{stem}.json",
            live_workflow_patch_record_to_json(record),
        )
        diagnostic_path = _write_private(
            root / "diagnostics-v1" / f"{stem}.json",
            json.dumps(
                to_primitive(diagnostic),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
        )
        with WorkflowPatchEfficiencyStore(root) as store:
            event = store.append(
                CampaignEventKind.RUN_RECORDED,
                fixture=status.next_slot,
                strategy=status.next_strategy,
                payload={
                    "record_path": record_path.relative_to(root).as_posix(),
                    "record_file_sha256": _sha256_file(record_path),
                    "record_content_hash": record.content_hash,
                    "diagnostic_path": (
                        diagnostic_path.relative_to(root).as_posix()
                    ),
                    "diagnostic_file_sha256": _sha256_file(diagnostic_path),
                    "diagnostic_content_hash": diagnostic.content_hash,
                    "evaluation_run_id": record.identity.run_id,
                    "workload_hash": record.identity.workload_hash,
                    "task_success": record.task_success,
                    "artifact_quality_score": record.artifact.quality_score,
                    "external_model_calls": record.external_model_calls,
                    "repair_count": diagnostic.repair_count,
                    "total_tokens": record.cost.total_tokens,
                    "quota_confirmed": True,
                },
            )
        current = workflow_patch_efficiency_status(root)
        return WorkflowPatchEfficiencyRunResult(
            event,
            current,
            str(record_path),
            str(diagnostic_path),
            record.task_success,
        )
    except (OperationCancelled, asyncio.CancelledError) as exc:
        return _record_failure(
            directory,
            status.next_slot,
            status.next_strategy,
            expected,
            start,
            kind=CampaignEventKind.RUN_INTERRUPTED,
            code="RUN_INTERRUPTED",
            message=type(exc).__name__,
        )
    except ModelProviderError as exc:
        return _record_failure(
            directory,
            status.next_slot,
            status.next_strategy,
            expected,
            start,
            kind=CampaignEventKind.RUN_FAILED,
            code=exc.code,
            message=exc.message_safe,
        )
    except Exception as exc:
        return _record_failure(
            directory,
            status.next_slot,
            status.next_strategy,
            expected,
            start,
            kind=CampaignEventKind.RUN_FAILED,
            code=type(exc).__name__,
            message="Completion efficiency live slot failed.",
        )


def _record_failure(
    directory: str | Path,
    slot: str,
    strategy: str,
    expected: WorkflowPatchEfficiencyExpectedRun,
    start: FirmValueCampaignEvent,
    *,
    kind: CampaignEventKind,
    code: str,
    message: str,
) -> WorkflowPatchEfficiencyRunResult:
    root = Path(directory).expanduser().resolve()
    payload = {
        "schema_version": WORKFLOW_PATCH_EFFICIENCY_FAILURE_SCHEMA,
        "pair_id": workflow_patch_efficiency_status(root).pair_id,
        "slot": slot,
        "strategy": strategy,
        "evaluation_run_id": expected.run_id,
        "workload_hash": expected.workload_hash,
        "failure_code": code,
        "message_safe": message[:256],
        "quota_confirmed": True,
        "partial_result_promoted": False,
        "start_event_id": start.event_id,
    }
    path = _write_private(
        root
        / "failures-v1"
        / f"{start.sequence + 1:02d}-{slot}-{strategy}.json",
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
    )
    with WorkflowPatchEfficiencyStore(root) as store:
        event = store.append(
            kind,
            fixture=slot,
            strategy=strategy,
            payload={
                "failure_path": path.relative_to(root).as_posix(),
                "failure_file_sha256": _sha256_file(path),
                "failure_code": code,
                "evaluation_run_id": expected.run_id,
                "external_model_calls": 0,
                "quota_confirmed": True,
            },
        )
    return WorkflowPatchEfficiencyRunResult(
        event,
        workflow_patch_efficiency_status(root),
        None,
        None,
        False,
    )


def compare_workflow_patch_efficiency_pair(
    directory: str | Path,
    *,
    output_path: str | Path | None = None,
) -> WorkflowPatchEfficiencyComparison:
    status = workflow_patch_efficiency_status(directory)
    if (
        status.state != WorkflowPatchEfficiencyState.COMPLETE
        or status.completed_runs != _MAX_RECORDS
    ):
        raise ValueError("Completion efficiency pair is not complete")
    with WorkflowPatchEfficiencyStore(directory) as store:
        metadata, manifest, _, _ = _pair_artifacts(store)
        events = store.events()
        root = store.directory
    _verify_parent(manifest, metadata)
    recorded = {
        (event.fixture, event.strategy): event
        for event in events
        if event.kind == CampaignEventKind.RUN_RECORDED
    }
    pairs: list[
        tuple[LiveWorkflowPatchRecord, WorkflowPatchEfficiencyDiagnostic]
    ] = []
    for item in manifest.expected_runs:
        event = recorded[(item.slot, item.strategy)]
        record_path = _sealed_path(
            root,
            event.payload["record_path"],
            "records-v1",
        )
        diagnostic_path = _sealed_path(
            root,
            event.payload["diagnostic_path"],
            "diagnostics-v1",
        )
        record = _validate_record(
            record_path,
            manifest,
            strategy=item.strategy,
        )
        diagnostic = _load_diagnostic(
            diagnostic_path,
            manifest,
            strategy=item.strategy,
            live_record_content_hash=record.content_hash,
        )
        pairs.append((record, diagnostic))
    (control, control_diagnostic), (
        candidate,
        candidate_diagnostic,
    ) = pairs
    quality_gate = (
        control.artifact.quality_score == 1.0
        and candidate.artifact.quality_score == 1.0
        and control.task_success
        and candidate.task_success
    )
    attribution_gate = all(
        record.prior_exposed_ids == (manifest.pattern_id,)
        and record.prior_aligned_ids == (manifest.pattern_id,)
        and not record.no_gap_control_exposed
        and not record.no_gap_control_aligned
        for record in (control, candidate)
    )
    safety_gate = all(
        record.safety.passed
        and record.safety.final_writer_count == 1
        and record.cost.tool_calls == 0
        for record in (control, candidate)
    )
    validation_gate = all(
        record.validation.passed
        and diagnostic.attempt_count >= 4
        and all(
            item.passed
            for task_id in {
                attempt.task_id for attempt in diagnostic.attempts
            }
            for item in (
                tuple(
                    attempt
                    for attempt in diagnostic.attempts
                    if attempt.task_id == task_id
                )[-1],
            )
        )
        for record, diagnostic in pairs
    )
    call_savings = (
        control.external_model_calls - candidate.external_model_calls
    )
    repair_savings = (
        control_diagnostic.repair_count - candidate_diagnostic.repair_count
    )
    token_savings = control.cost.total_tokens - candidate.cost.total_tokens
    efficiency_gate = (
        call_savings > 0
        and repair_savings > 0
        and candidate.external_model_calls <= 5
        and token_savings > 0
    )
    budget_gate = all(
        record.external_model_calls <= manifest.max_model_calls_per_run
        and record.elapsed_ms <= manifest.max_wall_time_ms_per_run
        for record in (control, candidate)
    )
    target_met = candidate.external_model_calls <= 4
    pair_gate = all(
        (
            quality_gate,
            attribution_gate,
            safety_gate,
            validation_gate,
            efficiency_gate,
            budget_gate,
        )
    )
    checks = (
        InformationBoundaryCheck(
            "quality-preserved",
            quality_gate,
            (
                f"{control.artifact.quality_score:.1f}->"
                f"{candidate.artifact.quality_score:.1f}"
            ),
        ),
        InformationBoundaryCheck(
            "prior-attribution-and-no-gap-isolation",
            attribution_gate,
            (
                f"pattern={manifest.pattern_id},"
                "no-gap-exposed=0,no-gap-aligned=0"
            ),
        ),
        InformationBoundaryCheck(
            "safety-writer-tool-boundary",
            safety_gate,
            "safety=pass,final-writer=1,tool-calls=0",
        ),
        InformationBoundaryCheck(
            "validator-not-weakened",
            validation_gate,
            manifest.completion_validator_revision,
        ),
        InformationBoundaryCheck(
            "model-call-and-repair-reduction",
            efficiency_gate,
            (
                f"calls={control.external_model_calls}->"
                f"{candidate.external_model_calls},"
                f"repairs={control_diagnostic.repair_count}->"
                f"{candidate_diagnostic.repair_count},"
                f"tokens={control.cost.total_tokens}->"
                f"{candidate.cost.total_tokens}"
            ),
        ),
        InformationBoundaryCheck(
            "declared-budget",
            budget_gate,
            (
                f"per-run-calls<={manifest.max_model_calls_per_run},"
                f"wall<={manifest.max_wall_time_ms_per_run}"
            ),
        ),
        InformationBoundaryCheck(
            "four-call-target",
            target_met,
            f"candidate-calls={candidate.external_model_calls}",
        ),
    )
    if pair_gate and target_met:
        outcome = "COMPLETION_EFFICIENCY_TARGET_OBSERVED"
        direction = "run-natural-workload-observation"
    elif pair_gate:
        outcome = "COMPLETION_EFFICIENCY_IMPROVEMENT_OBSERVED"
        direction = "run-natural-workload-observation"
    else:
        outcome = "COMPLETION_EFFICIENCY_NOT_OBSERVED"
        direction = "preserve-control-and-test-one-contract-variant"
    report = WorkflowPatchEfficiencyComparison(
        schema_version=WORKFLOW_PATCH_EFFICIENCY_COMPARISON_SCHEMA,
        pair_id=manifest.pair_id,
        manifest_content_hash=manifest.content_hash,
        completed_runs=_MAX_RECORDS,
        expected_runs=_MAX_RECORDS,
        control_quality=control.artifact.quality_score,
        candidate_quality=candidate.artifact.quality_score,
        control_model_calls=control.external_model_calls,
        candidate_model_calls=candidate.external_model_calls,
        model_call_savings=call_savings,
        control_repairs=control_diagnostic.repair_count,
        candidate_repairs=candidate_diagnostic.repair_count,
        repair_savings=repair_savings,
        control_total_tokens=control.cost.total_tokens,
        candidate_total_tokens=candidate.cost.total_tokens,
        token_savings=token_savings,
        quality_gate_passed=quality_gate,
        attribution_gate_passed=attribution_gate,
        safety_gate_passed=safety_gate,
        validation_gate_passed=validation_gate,
        efficiency_gate_passed=efficiency_gate,
        budget_gate_passed=budget_gate,
        pair_gate_passed=pair_gate,
        target_call_bound_met=target_met,
        outcome=outcome,
        recommended_direction=direction,
        checks=checks,
        aggregator_provider_calls=0,
        aggregator_quota_consumed=False,
    )
    destination = (
        Path(output_path).expanduser().resolve()
        if output_path is not None
        else root / "report-v1.json"
    )
    report_path = _write_private(
        destination,
        json.dumps(
            to_primitive(report),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
    )
    if report_path.parent == root:
        with WorkflowPatchEfficiencyStore(root) as store:
            store.append(
                CampaignEventKind.REPORT_CREATED,
                payload={
                    "report_path": report_path.relative_to(root).as_posix(),
                    "report_file_sha256": _sha256_file(report_path),
                    "outcome": report.outcome,
                    "pair_gate_passed": report.pair_gate_passed,
                    "aggregator_provider_calls": 0,
                    "quota_consumed": False,
                },
            )
    return report
