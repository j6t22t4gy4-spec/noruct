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
    _MAX_RECORDS,
    WORKFLOW_PATCH_COHORT_COMPARISON_SCHEMA,
    WORKFLOW_PATCH_COHORT_FAILURE_SCHEMA,
    WorkflowPatchCohortComparison,
    WorkflowPatchCohortRunResult,
    WorkflowPatchCohortState,
    WorkflowPatchCohortStore,
)
from .workflow_patch_campaign_primitives import (
    _campaign_artifacts,
    _company_store,
    _episode,
    _expected,
    _validate_record,
    _verify_runtime_inputs,
)
from .workflow_patch_campaign_status import workflow_patch_cohort_status

def preview_workflow_patch_cohort(
    directory: str | Path,
) -> WorkflowPatchCandidate:
    status = workflow_patch_cohort_status(directory)
    if status.patch_id is None:
        raise ValueError("Workflow Patch cohort has no candidate to preview")
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchCohortStore(root) as campaign:
        metadata, _, _, _ = _campaign_artifacts(campaign)
    with _company_store(root, metadata) as company:
        return CompanyLearningService(company).preview(status.patch_id)


def approve_workflow_patch_cohort(
    directory: str | Path,
    *,
    confirm: bool,
    actor: str,
) -> WorkflowPatchCandidate:
    status = workflow_patch_cohort_status(directory)
    if status.state != WorkflowPatchCohortState.AWAITING_APPROVAL:
        raise ValueError(
            f"Workflow Patch cannot be approved while state={status.state.value}"
        )
    if not confirm:
        raise ValueError("Workflow Patch approval requires --confirm")
    assert status.patch_id is not None
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchCohortStore(root) as campaign:
        metadata, _, _, _ = _campaign_artifacts(campaign)
    with _company_store(root, metadata) as company:
        learning = CompanyLearningService(company)
        if not learning.replay(status.patch_id):
            raise ValueError("Workflow Patch candidate does not replay from evidence")
        return learning.approve(status.patch_id, actor=actor)


def apply_workflow_patch_cohort(
    directory: str | Path,
    *,
    confirm: bool,
    actor: str,
) -> WorkflowPatchCandidate:
    status = workflow_patch_cohort_status(directory)
    if status.state != WorkflowPatchCohortState.AWAITING_APPLY:
        raise ValueError(
            f"Workflow Patch cannot be applied while state={status.state.value}"
        )
    if not confirm:
        raise ValueError("Workflow Patch apply requires --confirm")
    assert status.patch_id is not None
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchCohortStore(root) as campaign:
        metadata, manifest, _, _ = _campaign_artifacts(campaign)
    with _company_store(root, metadata) as company:
        applied = CompanyLearningService(company).apply(status.patch_id, actor=actor)
        if (
            applied.applied_revision != manifest.applied_playbook_revision
            or company.playbook().revision != manifest.applied_playbook_revision
        ):
            raise ValueError("Workflow Patch applied revision is not the sealed treatment")
        return applied


def rollback_workflow_patch_cohort(
    directory: str | Path,
    *,
    confirm: bool,
    actor: str,
) -> WorkflowPatchCandidate:
    status = workflow_patch_cohort_status(directory)
    if status.patch_id is None or status.patch_status != WorkflowPatchStatus.APPLIED.value:
        raise ValueError("Workflow Patch cohort has no applied patch to roll back")
    if not confirm:
        raise ValueError("Workflow Patch rollback requires --confirm")
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchCohortStore(root) as campaign:
        metadata, _, _, _ = _campaign_artifacts(campaign)
    with _company_store(root, metadata) as company:
        learning = CompanyLearningService(company)
        rolled_back = learning.rollback(status.patch_id, actor=actor)
        remaining = learning.compiler_priors(
            CompilerExecutionProfile.READ_ONLY,
            context_fingerprint=WORKFLOW_PATCH_CONTEXT,
        )
        if remaining or company.playbook().patterns:
            raise ValueError("Workflow Patch rollback left an active prior")
        return rolled_back


async def run_next_workflow_patch_cohort_slot(
    directory: str | Path,
    *,
    confirm_live_quota: bool,
    provider_factory=None,
    live_runner: Callable[..., Awaitable[LiveWorkflowPatchRecord]] | None = None,
) -> WorkflowPatchCohortRunResult:
    status = workflow_patch_cohort_status(directory)
    if (
        status.state != WorkflowPatchCohortState.READY
        or not status.next_slot
        or not status.next_strategy
    ):
        raise ValueError(
            f"Workflow Patch cohort cannot run while state={status.state.value}"
        )
    if not confirm_live_quota:
        raise ValueError(
            "Workflow Patch cohort requires --confirm-live-quota for one slot"
        )
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchCohortStore(root) as store:
        metadata, manifest, _, _ = _campaign_artifacts(store)
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
    priors = ()
    prior_source = "none"
    if status.next_strategy.startswith("candidate-prior-observation"):
        priors = (workflow_patch_candidate_prior(),)
        prior_source = "candidate-evaluation"
    elif status.next_strategy == "applied-workflow-patch":
        with _company_store(root, metadata) as company:
            priors = CompanyLearningService(company).compiler_priors(
                CompilerExecutionProfile.READ_ONLY,
                context_fingerprint=WORKFLOW_PATCH_CONTEXT,
            )
        if (
            len(priors) != 1
            or priors[0].pattern_id != manifest.candidate_pattern_id
        ):
            raise ValueError("Workflow Patch applied prior is unavailable")
        prior_source = "applied-playbook"
    config = LiveWorkflowPatchConfig(
        command=str(metadata["codex_command"]),
        model=manifest.model_id,
        source_revision=manifest.source_revision,
        distribution_sha256=manifest.distribution_sha256,
        campaign_id=manifest.campaign_id,
        matched_context_hash=manifest.matched_context_hash,
        timeout_seconds=float(metadata["request_timeout_seconds"]),
        max_total_model_calls=manifest.max_model_calls_per_run,
        max_wall_time_ms=manifest.max_wall_time_ms_per_run,
        quota_confirmed=True,
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=expected.playbook_revision,
    )
    runner = live_runner or run_live_workflow_patch_evaluation
    try:
        record = await runner(
            config,
            status.next_strategy,
            workflow_priors=priors,
            prior_source=prior_source,
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
        if status.next_slot != "baseline":
            baseline_path = Path(
                workflow_patch_cohort_status(root).record_paths[0]
            )
            baseline = _validate_record(
                baseline_path,
                manifest,
                strategy="generic-post-gap",
            )
            episode = _episode(
                qualified,
                baseline,
                planning_mode=(
                    "SOLO_THEN_TYPED_GAP_APPLIED_PRIOR"
                    if status.next_slot == "patched"
                    else "SOLO_THEN_TYPED_GAP_CANDIDATE_PRIOR"
                ),
            )
            with _company_store(root, metadata) as company:
                learning = CompanyLearningService(company)
                company.record_episode(episode)
                if status.next_slot.startswith("observation"):
                    learning.curate()
                else:
                    assert status.patch_id is not None
                    learning.observe(
                        status.patch_id,
                        episode,
                        prior_exposed=bool(qualified.prior_exposed_ids),
                        proposal_aligned=bool(qualified.prior_aligned_ids),
                    )
        with WorkflowPatchCohortStore(root) as store:
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
                    "prior_exposed": bool(qualified.prior_exposed_ids),
                    "prior_aligned": bool(qualified.prior_aligned_ids),
                    "no_gap_control_exposed": (
                        qualified.no_gap_control_exposed
                    ),
                },
            )
        return WorkflowPatchCohortRunResult(
            event=event,
            status=workflow_patch_cohort_status(root),
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
            "schema_version": WORKFLOW_PATCH_COHORT_FAILURE_SCHEMA,
            "campaign_id": manifest.campaign_id,
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
        with WorkflowPatchCohortStore(root) as store:
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
        return WorkflowPatchCohortRunResult(
            event=event,
            status=workflow_patch_cohort_status(root),
            record_path=None,
            task_success=False,
        )


def compare_workflow_patch_cohort(
    directory: str | Path,
    *,
    output_path: str | Path | None = None,
) -> WorkflowPatchCohortComparison:
    status = workflow_patch_cohort_status(directory)
    if status.state not in {
        WorkflowPatchCohortState.COMPLETE,
        WorkflowPatchCohortState.ROLLED_BACK,
    }:
        raise ValueError(
            "Workflow Patch comparison requires four sealed records; "
            f"state={status.state.value},completed={status.completed_runs}/4"
        )
    root = Path(directory).expanduser().resolve()
    with WorkflowPatchCohortStore(root) as store:
        metadata, manifest, _, _ = _campaign_artifacts(store)
        events = store.events()
        recorded = {
            (event.fixture, event.strategy): event
            for event in events
            if event.kind == CampaignEventKind.RUN_RECORDED
        }
        records = tuple(
            _validate_record(
                root / str(recorded[(item.slot, item.strategy)].payload["record_path"]),
                manifest,
                strategy=item.strategy,
            )
            for item in manifest.expected_runs
        )
        baseline, observation_one, observation_two, patched = records
        with _company_store(root, metadata) as company:
            patches = company.list_patches()
            if len(patches) != 1:
                raise ValueError("Workflow Patch comparison requires one candidate")
            patch = patches[0]
            learning = CompanyLearningService(company)
            replay_matches = learning.replay(patch.patch_id)
            observations = company.list_observations(patch.patch_id)
            lifecycle = tuple(
                event.event_type.value
                for event in company.list_patch_events(patch.patch_id)
            )
        gain = round(
            patched.artifact.quality_score - baseline.artifact.quality_score,
            4,
        )
        reduction = round(
            (
                baseline.external_model_calls - patched.external_model_calls
            )
            / max(1, baseline.external_model_calls),
            4,
        )
        baseline_tasks = tuple(
            item.task_id for item in baseline.trajectory.attempts
        )
        prior_tasks = tuple(item.task_id for item in patched.trajectory.attempts)
        checks = (
            InformationBoundaryCheck(
                "immutable-four-record-cohort",
                len(records) == _MAX_RECORDS
                and tuple(item.strategy for item in records)
                == WORKFLOW_PATCH_STRATEGIES,
                ",".join(item.strategy for item in records),
            ),
            InformationBoundaryCheck(
                "matched-sibling-context",
                len({item.matched_context_hash for item in records}) == 1
                and records[0].matched_context_hash
                == manifest.matched_context_hash,
                manifest.matched_context_hash,
            ),
            InformationBoundaryCheck(
                "distinct-run-identities",
                len({item.identity.run_id for item in records}) == _MAX_RECORDS,
                ",".join(item.identity.run_id for item in records),
            ),
            InformationBoundaryCheck(
                "generic-post-gap-baseline",
                baseline.artifact.quality_score == 0.8
                and not baseline.prior_exposed_ids
                and not baseline.prior_aligned_ids
                and baseline_tasks
                == (
                    "analyze_goal",
                    "specialist_release_policy_review",
                    "integrate_goal",
                ),
                (
                    f"quality={baseline.artifact.quality_score:.4f},"
                    f"tasks={','.join(baseline_tasks)}"
                ),
            ),
            InformationBoundaryCheck(
                "two-independent-candidate-observations",
                all(
                    item.prior_source == "candidate-evaluation"
                    and item.prior_exposed_ids
                    == (manifest.candidate_pattern_id,)
                    and item.prior_aligned_ids
                    == (manifest.candidate_pattern_id,)
                    and item.artifact.quality_score
                    - baseline.artifact.quality_score
                    >= manifest.quality_gain_threshold - 1e-9
                    for item in (observation_one, observation_two)
                ),
                (
                    f"gains={observation_one.artifact.quality_score - baseline.artifact.quality_score:+.4f},"
                    f"{observation_two.artifact.quality_score - baseline.artifact.quality_score:+.4f}"
                ),
            ),
            InformationBoundaryCheck(
                "explicit-patch-lifecycle",
                replay_matches
                and patch.eligible_for_apply
                and patch.evidence_episode_ids
                and lifecycle[:3] == ("PROPOSED", "APPROVED", "APPLIED")
                and patch.status
                in {WorkflowPatchStatus.APPLIED, WorkflowPatchStatus.ROLLED_BACK},
                f"lifecycle={','.join(lifecycle)},status={patch.status.value}",
            ),
            InformationBoundaryCheck(
                "applied-prior-attribution",
                patched.prior_source == "applied-playbook"
                and patched.prior_exposed_ids == (manifest.candidate_pattern_id,)
                and patched.prior_aligned_ids == (manifest.candidate_pattern_id,)
                and prior_tasks
                == (
                    "analyze_goal",
                    "policy_evidence",
                    "independent_review",
                    "integrate_decision",
                )
                and len(observations) == 1
                and observations[0].attribution_eligible
                and observations[0].cohort_eligible,
                (
                    f"tasks={','.join(prior_tasks)},"
                    f"observations={len(observations)}"
                ),
            ),
            InformationBoundaryCheck(
                "same-context-no-gap-isolation",
                all(
                    not item.no_gap_control_exposed
                    and not item.no_gap_control_aligned
                    for item in (observation_one, observation_two, patched)
                ),
                "candidate and applied priors were not exposed without a typed gap",
            ),
            InformationBoundaryCheck(
                "workflow-patch-causal-effect",
                gain >= manifest.quality_gain_threshold - 1e-9
                or (
                    patched.artifact.quality_score
                    >= baseline.artifact.quality_score
                    and patched.external_model_calls
                    <= baseline.external_model_calls * 0.8
                ),
                (
                    f"quality={baseline.artifact.quality_score:.4f}->"
                    f"{patched.artifact.quality_score:.4f},gain={gain:+.4f},"
                    f"calls={baseline.external_model_calls}->"
                    f"{patched.external_model_calls}"
                ),
            ),
            InformationBoundaryCheck(
                "hard-safety-and-final-writer",
                all(
                    item.task_success
                    and item.validation.passed
                    and item.safety.passed
                    and item.safety.final_writer_count == 1
                    and item.cost.tool_calls == 0
                    for item in records
                ),
                "all records passed validation, memory isolation, one-writer, and zero-tool gates",
            ),
            InformationBoundaryCheck(
                "bounded-cohort-cost",
                all(
                    item.external_model_calls
                    <= manifest.max_model_calls_per_run
                    for item in records
                )
                and sum(item.external_model_calls for item in records)
                <= manifest.max_model_calls_cohort,
                (
                    f"calls={sum(item.external_model_calls for item in records)}/"
                    f"{manifest.max_model_calls_cohort}"
                ),
            ),
            InformationBoundaryCheck(
                "long-term-observation-contract-preserved",
                len(observations) == 1,
                (
                    "one applied observation is sealed; the existing minimum-three "
                    "long-term KEEP contract remains unchanged"
                ),
            ),
        )
        safety_gate = checks[9].passed
        attribution_gate = all(checks[index].passed for index in (4, 5, 6, 7))
        effect_gate = checks[8].passed
        budget_gate = checks[10].passed
        cohort_gate = all(check.passed for check in checks)
        if not safety_gate:
            outcome = "WORKFLOW_PATCH_SAFETY_GATE_FAILED"
            direction = "ROLLBACK_AND_FIX_SAFETY_BOUNDARY"
        elif not attribution_gate:
            outcome = "WORKFLOW_PATCH_ATTRIBUTION_FAILED"
            direction = "ROLLBACK_AND_FIX_PRIOR_ATTRIBUTION"
        elif not effect_gate:
            outcome = "WORKFLOW_PATCH_VALUE_NOT_REPRODUCED"
            direction = "ROLLBACK_WORKFLOW_PATCH"
        elif not budget_gate:
            outcome = "WORKFLOW_PATCH_COST_BOUND_FAILED"
            direction = "ROLLBACK_AND_REDUCE_WORKFLOW_COST"
        elif cohort_gate:
            outcome = "APPLIED_WORKFLOW_PATCH_VALUE_REPRODUCED"
            direction = (
                "ACCUMULATE_TWO_MORE_POST_APPLY_OBSERVATIONS_AND_COMPLETE_ALPHA_GATES"
            )
        else:
            outcome = "WORKFLOW_PATCH_COHORT_CONTRACT_FAILED"
            direction = "FREEZE_AND_INSPECT_COHORT_CONTRACT"
        comparison = WorkflowPatchCohortComparison(
            schema_version=WORKFLOW_PATCH_COHORT_COMPARISON_SCHEMA,
            campaign_id=manifest.campaign_id,
            manifest_content_hash=manifest.content_hash,
            completed_runs=_MAX_RECORDS,
            expected_runs=_MAX_RECORDS,
            baseline_quality=baseline.artifact.quality_score,
            patched_quality=patched.artifact.quality_score,
            artifact_quality_gain=gain,
            baseline_model_calls=baseline.external_model_calls,
            patched_model_calls=patched.external_model_calls,
            model_call_reduction=reduction,
            safety_gate_passed=safety_gate,
            attribution_gate_passed=attribution_gate,
            effect_gate_passed=effect_gate,
            budget_gate_passed=budget_gate,
            cohort_gate_passed=cohort_gate,
            patch_id=patch.patch_id,
            patch_status=patch.status.value,
            post_apply_observations=len(observations),
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

