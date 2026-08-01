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
    _MAX_RECORDS,
    WORKFLOW_PATCH_EXTENSION_COMPARISON_SCHEMA,
    WORKFLOW_PATCH_EXTENSION_FAILURE_SCHEMA,
    WorkflowPatchExtensionComparison,
    WorkflowPatchExtensionRunResult,
    WorkflowPatchExtensionState,
    WorkflowPatchExtensionStore,
)
from .workflow_patch_extension_primitives import (
    _company_store,
    _expected,
    _extension_artifacts,
    _parent_evidence,
    _validate_record,
    _verify_parent,
    _verify_runtime_inputs,
)
from .workflow_patch_extension_status import workflow_patch_extension_status

async def run_next_workflow_patch_extension_slot(
    directory: str | Path,
    *,
    confirm_live_quota: bool,
    provider_factory=None,
    live_runner: Callable[..., Awaitable[LiveWorkflowPatchRecord]] | None = None,
) -> WorkflowPatchExtensionRunResult:
    status = workflow_patch_extension_status(directory)
    if (
        status.state != WorkflowPatchExtensionState.READY
        or status.next_slot is None
        or status.next_strategy is None
    ):
        raise ValueError(
            "Workflow Patch extension cannot run while "
            f"state={status.state.value}"
        )
    if not confirm_live_quota:
        raise ValueError(
            "Workflow Patch extension requires --confirm-live-quota for one slot"
        )
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchExtensionStore(root) as store:
        metadata, manifest, _, _ = _extension_artifacts(store)
        parent = _verify_parent(manifest, metadata)
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
                "workload_hash": expected.workload_hash,
                "evaluation_run_id": expected.run_id,
            },
        )
    with _company_store(root, metadata) as company:
        priors = CompanyLearningService(company).compiler_priors(
            CompilerExecutionProfile.READ_ONLY,
            context_fingerprint=WORKFLOW_PATCH_CONTEXT,
        )
    if len(priors) != 1 or priors[0].pattern_id != manifest.pattern_id:
        raise ValueError("Workflow Patch extension applied prior is unavailable")
    config = LiveWorkflowPatchConfig(
        command=str(metadata["codex_command"]),
        model=manifest.model_id,
        source_revision=manifest.source_revision,
        distribution_sha256=manifest.distribution_sha256,
        campaign_id=manifest.extension_id,
        matched_context_hash=manifest.matched_context_hash,
        timeout_seconds=float(metadata["request_timeout_seconds"]),
        max_total_model_calls=manifest.max_model_calls_per_run,
        max_wall_time_ms=manifest.max_wall_time_ms_per_run,
        quota_confirmed=True,
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=manifest.applied_playbook_revision,
    )
    runner = live_runner or run_live_workflow_patch_evaluation
    try:
        record = await runner(
            config,
            status.next_strategy,
            workflow_priors=priors,
            prior_source="applied-playbook",
            provider_factory=provider_factory,
        )
        relative = Path("records-v1") / (
            f"{start.sequence:02d}-{status.next_slot}-{status.next_strategy}.json"
        )
        record_path = _write_private(
            root / relative,
            live_workflow_patch_record_to_json(record),
        )
        qualified = _validate_record(
            record_path,
            manifest,
            strategy=status.next_strategy,
        )
        episode = _episode(
            qualified,
            parent.baseline,
            planning_mode="SOLO_THEN_TYPED_GAP_APPLIED_PRIOR_POST_APPLY",
        )
        with _company_store(root, metadata) as company:
            learning = CompanyLearningService(company)
            company.record_episode(episode)
            observation = learning.observe(
                manifest.patch_id,
                episode,
                prior_exposed=bool(qualified.prior_exposed_ids),
                proposal_aligned=bool(qualified.prior_aligned_ids),
            )
        with WorkflowPatchExtensionStore(root) as store:
            event = store.append(
                CampaignEventKind.RUN_RECORDED,
                fixture=status.next_slot,
                strategy=status.next_strategy,
                payload={
                    "record_path": relative.as_posix(),
                    "record_file_sha256": _sha256_file(record_path),
                    "record_content_hash": qualified.content_hash,
                    "evaluation_run_id": qualified.identity.run_id,
                    "workload_hash": qualified.identity.workload_hash,
                    "status": qualified.status,
                    "task_success": qualified.task_success,
                    "artifact_quality_score": qualified.artifact.quality_score,
                    "safety_passed": qualified.safety.passed,
                    "external_model_calls": qualified.external_model_calls,
                    "repair_used": qualified.validation.repair_used,
                    "validation_attempt_count": qualified.validation.attempt_count,
                    "prior_exposed": bool(qualified.prior_exposed_ids),
                    "prior_aligned": bool(qualified.prior_aligned_ids),
                    "no_gap_control_exposed": qualified.no_gap_control_exposed,
                    "observation_id": observation.observation_id,
                    "observation_content_hash": observation.content_hash,
                },
            )
        return WorkflowPatchExtensionRunResult(
            event=event,
            status=workflow_patch_extension_status(root),
            record_path=str(record_path),
            task_success=qualified.task_success,
        )
    except BaseException as exc:
        interrupted = isinstance(
            exc,
            (OperationCancelled, asyncio.CancelledError, KeyboardInterrupt),
        )
        kind = (
            CampaignEventKind.RUN_INTERRUPTED
            if interrupted
            else CampaignEventKind.RUN_FAILED
        )
        relative = Path("failures-v1") / (
            f"{start.sequence:02d}-{status.next_slot}-{status.next_strategy}.json"
        )
        code = exc.code if isinstance(exc, ModelProviderError) else type(exc).__name__
        failure = {
            "schema_version": WORKFLOW_PATCH_EXTENSION_FAILURE_SCHEMA,
            "extension_id": manifest.extension_id,
            "slot": status.next_slot,
            "strategy": status.next_strategy,
            "workload_hash": expected.workload_hash,
            "evaluation_run_id": expected.run_id,
            "recorded_at": utc_now().isoformat(),
            "failure_code": str(code),
            "interrupted": interrupted,
            "quota_confirmed": True,
            "partial_result_promoted": False,
        }
        failure_path = _write_private(
            root / relative,
            json.dumps(failure, ensure_ascii=False, sort_keys=True, indent=2),
        )
        with WorkflowPatchExtensionStore(root) as store:
            event = store.append(
                kind,
                fixture=status.next_slot,
                strategy=status.next_strategy,
                payload={
                    "failure_path": relative.as_posix(),
                    "failure_file_sha256": _sha256_file(failure_path),
                    "failure_code": str(code),
                    "partial_result_promoted": False,
                },
            )
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        return WorkflowPatchExtensionRunResult(
            event=event,
            status=workflow_patch_extension_status(root),
            record_path=None,
            task_success=False,
        )


def assess_workflow_patch_extension(
    directory: str | Path,
) -> WorkflowPatchAssessment:
    status = workflow_patch_extension_status(directory)
    if status.state not in {
        WorkflowPatchExtensionState.AWAITING_ASSESSMENT,
        WorkflowPatchExtensionState.PARTIAL_FAILED,
        WorkflowPatchExtensionState.KEEP,
        WorkflowPatchExtensionState.ROLLBACK_CANDIDATE,
    }:
        raise ValueError(
            "Workflow Patch extension cannot assess while "
            f"state={status.state.value}"
        )
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchExtensionStore(root) as store:
        metadata, manifest, _, _ = _extension_artifacts(store)
        existing_event = next(
            (
                event
                for event in store.events()
                if event.kind == CampaignEventKind.ASSESSMENT_RECORDED
            ),
            None,
        )
    with _company_store(root, metadata) as company:
        learning = CompanyLearningService(company)
        if existing_event is not None:
            assessment = company.latest_assessment(manifest.patch_id)
            assert assessment is not None
            return assessment
        if (
            status.completed_runs != _MAX_RECORDS
            or len(company.list_observations(manifest.patch_id)) != 3
        ):
            raise ValueError("Workflow Patch extension requires exactly three observations")
        before_revision = company.playbook().revision
        assessment = learning.assess(manifest.patch_id)
        if (
            company.playbook().revision != before_revision
            or company.get_patch(manifest.patch_id).status
            != WorkflowPatchStatus.APPLIED
        ):
            raise ValueError("Workflow Patch assessment mutated the applied PLAYBOOK")
    with WorkflowPatchExtensionStore(root) as store:
        store.append(
            CampaignEventKind.ASSESSMENT_RECORDED,
            payload={
                "assessment_id": assessment.assessment_id,
                "assessment_content_hash": assessment.content_hash,
                "decision": assessment.decision.value,
                "reasons": assessment.reasons,
                "cohort_observation_ids": assessment.cohort_observation_ids,
                "automatic_rollback": False,
                "aggregator_provider_calls": 0,
                "aggregator_quota_consumed": False,
            },
        )
    workflow_patch_extension_status(root)
    return assessment


def rollback_workflow_patch_extension(
    directory: str | Path,
    *,
    confirm: bool,
    actor: str,
) -> object:
    status = workflow_patch_extension_status(directory)
    if status.state != WorkflowPatchExtensionState.ROLLBACK_CANDIDATE:
        raise ValueError("Workflow Patch extension has no rollback recommendation")
    if not confirm:
        raise ValueError("Workflow Patch extension rollback requires --confirm")
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchExtensionStore(root) as store:
        metadata, manifest, _, _ = _extension_artifacts(store)
    with _company_store(root, metadata) as company:
        learning = CompanyLearningService(company)
        rolled_back = learning.rollback(manifest.patch_id, actor=actor)
        remaining = learning.compiler_priors(
            CompilerExecutionProfile.READ_ONLY,
            context_fingerprint=WORKFLOW_PATCH_CONTEXT,
        )
        if remaining or company.playbook().patterns:
            raise ValueError("Workflow Patch extension rollback left an active prior")
    with WorkflowPatchExtensionStore(root) as store:
        store.append(
            CampaignEventKind.ROLLBACK_RECORDED,
            payload={
                "patch_id": manifest.patch_id,
                "actor": actor,
                "status": rolled_back.status.value,
            },
        )
    workflow_patch_extension_status(root)
    return rolled_back


def compare_workflow_patch_extension(
    directory: str | Path,
    *,
    output_path: str | Path | None = None,
) -> WorkflowPatchExtensionComparison:
    status = workflow_patch_extension_status(directory)
    if status.state not in {
        WorkflowPatchExtensionState.KEEP,
        WorkflowPatchExtensionState.ROLLBACK_CANDIDATE,
        WorkflowPatchExtensionState.ROLLED_BACK,
    }:
        raise ValueError(
            "Workflow Patch extension comparison requires an assessment; "
            f"state={status.state.value}"
        )
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchExtensionStore(root) as store:
        metadata, manifest, _, _ = _extension_artifacts(store)
        parent = _verify_parent(manifest, metadata)
        events = store.events()
        recorded = {
            (event.fixture, event.strategy): event
            for event in events
            if event.kind == CampaignEventKind.RUN_RECORDED
        }
        records = tuple(
            _validate_record(
                root
                / str(recorded[(item.slot, item.strategy)].payload["record_path"]),
                manifest,
                strategy=item.strategy,
            )
            for item in manifest.expected_runs
        )
        with _company_store(root, metadata) as company:
            patch = company.get_patch(manifest.patch_id)
            observations = company.list_observations(manifest.patch_id)
            assessments = company.list_assessments(manifest.patch_id)
            playbook = company.playbook()
        if len(assessments) != 1:
            raise ValueError("Workflow Patch extension requires one assessment")
        assessment = assessments[0]
        post_apply_records = (parent.applied, *records)
        qualities = tuple(item.artifact.quality_score for item in post_apply_records)
        extension_calls = tuple(item.external_model_calls for item in records)
        mean_quality = round(sum(qualities) / len(qualities), 6)
        minimum_quality = min(qualities)
        mean_extension_calls = round(
            sum(extension_calls) / len(extension_calls),
            6,
        )
        call_delta = round(
            mean_extension_calls - parent.applied.external_model_calls,
            6,
        )
        repair_used_count = sum(item.validation.repair_used for item in post_apply_records)
        checks = (
            InformationBoundaryCheck(
                "immutable-parent-cohort",
                parent.semantic_anchor == manifest.parent_semantic_anchor
                and parent.applied.content_hash
                == manifest.parent_applied_content_hash,
                parent.semantic_anchor,
            ),
            InformationBoundaryCheck(
                "exact-three-post-apply-observations",
                len(observations) == 3
                and len(assessment.cohort_observation_ids) == 3,
                (
                    f"stored={len(observations)},"
                    f"cohort={len(assessment.cohort_observation_ids)}"
                ),
            ),
            InformationBoundaryCheck(
                "distinct-applied-run-identities",
                len({item.identity.run_id for item in post_apply_records}) == 3,
                ",".join(item.identity.run_id for item in post_apply_records),
            ),
            InformationBoundaryCheck(
                "matched-parent-context",
                all(
                    item.matched_context_hash == manifest.matched_context_hash
                    for item in post_apply_records
                ),
                manifest.matched_context_hash,
            ),
            InformationBoundaryCheck(
                "applied-prior-attribution",
                all(
                    item.prior_source == "applied-playbook"
                    and item.prior_exposed_ids == (manifest.pattern_id,)
                    and item.prior_aligned_ids == (manifest.pattern_id,)
                    for item in post_apply_records
                )
                and all(
                    item.attribution_eligible and item.cohort_eligible
                    for item in observations
                ),
                f"pattern={manifest.pattern_id}",
            ),
            InformationBoundaryCheck(
                "quality-effect-reproduced",
                mean_quality >= 1.0 - 1e-9
                and minimum_quality >= 0.8 - 1e-9,
                f"mean={mean_quality:.4f},minimum={minimum_quality:.4f}",
            ),
            InformationBoundaryCheck(
                "hard-safety-final-writer-and-zero-tools",
                all(
                    item.task_success
                    and item.validation.passed
                    and item.safety.passed
                    and item.safety.final_writer_count == 1
                    and item.cost.tool_calls == 0
                    for item in post_apply_records
                ),
                "three applied records passed task, validation, safety, writer, and tool gates",
            ),
            InformationBoundaryCheck(
                "same-context-no-gap-isolation",
                all(
                    not item.no_gap_control_exposed
                    and not item.no_gap_control_aligned
                    for item in post_apply_records
                ),
                "applied prior was not exposed without a typed capability gap",
            ),
            InformationBoundaryCheck(
                "bounded-extension-cost",
                all(
                    item.external_model_calls
                    <= manifest.max_model_calls_per_run
                    and item.elapsed_ms <= manifest.max_wall_time_ms_per_run
                    for item in records
                )
                and sum(extension_calls) <= manifest.max_model_calls_extension,
                (
                    f"calls={sum(extension_calls)}/"
                    f"{manifest.max_model_calls_extension}"
                ),
            ),
            InformationBoundaryCheck(
                "call-efficiency-accounted",
                len(extension_calls) == 2
                and all(item >= 1 for item in extension_calls),
                (
                    f"parent={parent.applied.external_model_calls},"
                    f"extension-mean={mean_extension_calls:.4f},"
                    f"delta={call_delta:+.4f},"
                    f"repairs={repair_used_count}/3"
                ),
            ),
            InformationBoundaryCheck(
                "append-only-assessment-no-auto-rollback",
                len(assessments) == 1
                and (
                    patch.status == WorkflowPatchStatus.ROLLED_BACK
                    or (
                        patch.status == WorkflowPatchStatus.APPLIED
                        and playbook.revision
                        == manifest.applied_playbook_revision
                    )
                ),
                (
                    f"assessment={assessment.assessment_id},"
                    f"patch={patch.status.value},playbook={playbook.revision}"
                ),
            ),
            InformationBoundaryCheck(
                "existing-long-term-contract-decision",
                assessment.decision
                in {
                    WorkflowPatchAssessmentDecision.KEEP,
                    WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE,
                },
                (
                    f"decision={assessment.decision.value},"
                    f"quality-gain={assessment.mean_quality_gain},"
                    f"call-savings={assessment.mean_model_call_savings}"
                ),
            ),
        )
        safety_gate = all(checks[index].passed for index in (6, 7))
        attribution_gate = all(checks[index].passed for index in (0, 1, 2, 3, 4))
        effect_gate = checks[5].passed
        budget_gate = all(checks[index].passed for index in (8, 9))
        assessment_gate = (
            checks[10].passed
            and checks[11].passed
            and assessment.decision == WorkflowPatchAssessmentDecision.KEEP
        )
        extension_gate = (
            all(check.passed for check in checks) and assessment_gate
        )
        if not safety_gate:
            outcome = "WORKFLOW_PATCH_LONG_TERM_SAFETY_FAILED"
            direction = "RETAIN_EVIDENCE_AND_EXPLICITLY_ROLL_BACK"
        elif not attribution_gate:
            outcome = "WORKFLOW_PATCH_LONG_TERM_ATTRIBUTION_FAILED"
            direction = "FREEZE_AND_INSPECT_ATTRIBUTION"
        elif assessment.decision == WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE:
            outcome = "WORKFLOW_PATCH_LONG_TERM_ROLLBACK_CANDIDATE"
            direction = "OPERATOR_REVIEW_THEN_EXPLICIT_ROLLBACK_OR_RETAIN"
        elif not effect_gate:
            outcome = "WORKFLOW_PATCH_LONG_TERM_EFFECT_NOT_REPRODUCED"
            direction = "ASSESS_ROLLBACK_AND_INSPECT_SCORER"
        elif not budget_gate:
            outcome = "WORKFLOW_PATCH_LONG_TERM_COST_BOUND_FAILED"
            direction = "KEEP_EVIDENCE_AND_REDUCE_COMPLETION_REPAIR"
        elif extension_gate:
            outcome = "WORKFLOW_PATCH_LONG_TERM_KEEP_REPRODUCED"
            direction = "COMPLETE_OPERATOR_ALPHA_GATES_AND_OPTIMIZE_CALL_EFFICIENCY"
        else:
            outcome = "WORKFLOW_PATCH_EXTENSION_CONTRACT_FAILED"
            direction = "FREEZE_AND_INSPECT_EXTENSION_CONTRACT"
        comparison = WorkflowPatchExtensionComparison(
            schema_version=WORKFLOW_PATCH_EXTENSION_COMPARISON_SCHEMA,
            extension_id=manifest.extension_id,
            manifest_content_hash=manifest.content_hash,
            parent_campaign_id=manifest.parent_campaign_id,
            completed_runs=_MAX_RECORDS,
            expected_runs=_MAX_RECORDS,
            post_apply_observations=len(observations),
            mean_artifact_quality=mean_quality,
            minimum_artifact_quality=minimum_quality,
            parent_applied_model_calls=parent.applied.external_model_calls,
            extension_mean_model_calls=mean_extension_calls,
            model_call_delta_from_parent=call_delta,
            repair_used_count=repair_used_count,
            repair_free_count=len(post_apply_records) - repair_used_count,
            safety_gate_passed=safety_gate,
            attribution_gate_passed=attribution_gate,
            effect_gate_passed=effect_gate,
            budget_gate_passed=budget_gate,
            assessment_gate_passed=assessment_gate,
            extension_gate_passed=extension_gate,
            assessment_id=assessment.assessment_id,
            assessment_decision=assessment.decision.value,
            mean_quality_gain=assessment.mean_quality_gain,
            mean_model_call_savings=assessment.mean_model_call_savings,
            outcome=outcome,
            recommended_direction=direction,
            checks=checks,
            aggregator_provider_calls=0,
            aggregator_quota_consumed=False,
        )
        target = (
            Path(output_path).expanduser().resolve()
            if output_path
            else root / "report-v1.json"
        )
        _write_private(
            target,
            json.dumps(
                to_primitive(comparison),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
        )
        if not any(
            event.kind == CampaignEventKind.REPORT_CREATED for event in events
        ):
            store.append(
                CampaignEventKind.REPORT_CREATED,
                payload={
                    "report_path": str(target),
                    "report_file_sha256": _sha256_file(target),
                    "classification": outcome,
                    "aggregator_provider_calls": 0,
                    "aggregator_quota_consumed": False,
                },
            )
    return comparison
