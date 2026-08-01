from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm import __version__
from dynamic_firm.company.models import content_digest
from dynamic_firm.compiler import (
    CapabilityInsertReplanner,
    CompilerExecutionProfile,
    CompilerRequest,
    WorkflowPrior,
    solo_first_decision,
)
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobLimits,
    JobResult,
    JobStatus,
)
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.providers.codex_exec import (
    CodexExecProvider,
    CodexExecProviderConfig,
    CodexLoginStatus,
)
from dynamic_firm.runtime.models import (
    ActionPolicy,
    CompletionEnvelope,
    CompletionValidation,
    ContextBundle,
    EmployeeRunRequest,
    EventType,
    RunEvent,
    RunLimits,
    RunStatus,
    SignalCode,
    VersionedContent,
    to_primitive,
    utc_now,
)
from dynamic_firm.runtime.ports import ModelProviderError, OperationCancelled
from dynamic_firm.runtime.prompt import PromptBuilder
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry

from .alpha_readiness import AlphaReadinessEvaluation, run_alpha_readiness_evaluation
from .context_binding import (
    ExactContextBoundExpectedRun,
    ExactContextBoundPreparation,
    ExactContextEvidenceBinding,
    load_exact_context_bound_preparation,
    load_exact_context_evidence_binding,
)
from .eval_contracts import EvaluationTrajectoryProjection, project_job_trajectory
from .firm_value import wheel_distribution_sha256
from .firm_value_campaign import (
    CampaignEventKind,
    CampaignState,
    FirmValueCampaignEvent,
    FirmValueCampaignStore,
    _process_is_alive,
    _sha256_file,
    _write_private,
    probe_codex_structured_output,
    source_snapshot_revision,
)
from .information_boundary import (
    INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
    InformationBoundaryAdmissionProjection,
    InformationBoundaryArtifactProjection,
    InformationBoundaryCheck,
    InformationBoundaryCostProjection,
    InformationBoundarySafetyProjection,
    _RecordingEmployeeExecutionPort,
    _provider_request_refs,
)
from .workflow_patch_efficiency import (
    WORKFLOW_PATCH_NATURAL_GOAL,
    _parent_seed,
)
from .workflow_patch_live import workflow_patch_candidate_prior


EXACT_CONTEXT_LIVE_PAIR_MANIFEST_SCHEMA = (
    "noruct.exact-context-source-frozen-live-pair-manifest.v1"
)
EXACT_CONTEXT_LIVE_PAIR_PREFLIGHT_SCHEMA = (
    "noruct.exact-context-source-frozen-live-pair-preflight.v1"
)
EXACT_CONTEXT_LIVE_PAIR_RECORD_SCHEMA = (
    "noruct.exact-context-source-frozen-live-record.v1"
)
EXACT_CONTEXT_LIVE_PAIR_FAILURE_SCHEMA = (
    "noruct.exact-context-source-frozen-live-failure.v1"
)
EXACT_CONTEXT_LIVE_PAIR_LEDGER_SCHEMA = (
    "noruct.exact-context-source-frozen-live-ledger.v1"
)
EXACT_CONTEXT_LIVE_PAIR_COMPARISON_SCHEMA = (
    "noruct.exact-context-source-frozen-live-comparison.v1"
)
EXACT_CONTEXT_NATURAL_EVIDENCE_SCHEMA = (
    "noruct.exact-context-natural-evidence-projection.v1"
)
EXACT_CONTEXT_LIVE_EVIDENCE_CLASS = "LIVE_EVALUATION"
EXACT_CONTEXT_COMPLETION_CONTRACT_REVISION = (
    "exact-context-alpha-readiness-task-objective-v1"
)
EXACT_CONTEXT_COMPLETION_VALIDATOR_REVISION = (
    "exact-context-alpha-readiness-validator-v1"
)
EXACT_CONTEXT_PROJECTION_REVISION = "exact-context-alpha-readiness-projection-v1"
EXACT_CONTEXT_LIVE_STRATEGIES = (
    "exact-context-control",
    "exact-context-candidate",
)
EXACT_CONTEXT_QUALITY_GAIN_THRESHOLD = 0.2
_PAIR_DB = "exact-context-live-pair.db"
_RECORD_MAX_BYTES = 1_000_000
_BLOCKERS = (
    "operator-release-approval",
    "alpha-version-staged",
    "clean-release-worktree",
)
_BLOCKER_VALUE = ",".join(_BLOCKERS)
_REVIEW_BASIS = "source-frozen-gates-consistent"
_MISSING_REVIEW = "unavailable"
_FIELD_LINE = re.compile(
    r"^\s*([a-z][a-z0-9_]*)\s*([=:])\s*([^\r\n]+?)\s*$"
)


from .exact_context_live_pair_contracts import (
    ExactContextLivePairComparison,
    ExactContextLivePairManifest,
    ExactContextLivePairPreparation,
    ExactContextLivePairPreflight,
    ExactContextLivePairRunResult,
    ExactContextLivePairState,
    ExactContextLivePairStatus,
    ExactContextLiveRecord,
    ExactContextNaturalEvidence,
    ExactContextRegressionProbe,
    ExactContextValidationProjection,
)

from .exact_context_live_pair_primitives import (
    ExactContextLivePairStore,
    _canonical_json,
    _create_manifest,
    _create_natural_evidence,
    _create_preflight,
    _job_limits,
    _load_bounded_json,
    _load_manifest,
    _load_natural_evidence,
    _load_preflight,
    _manifest_fresh,
    _pair_artifacts,
    _run_limits,
    _summary_fields,
    run_python311_regression_probe,
)

from .exact_context_live_pair_preparation import (
    exact_context_live_pair_status,
    prepare_exact_context_live_pair,
)

from .exact_context_live_pair_execution import (
    _ExactContextCompletionValidator,
    _ExactContextPromptBuilder,
    _admission,
    _cost,
    _final_fields,
    _natural_request,
    _run_no_gap_control,
    _safety,
    _score_artifact,
    _task_contract,
    _task_objective,
    _validation,
    run_exact_context_live_evaluation,
)

def exact_context_live_record_to_json(record: ExactContextLiveRecord) -> str:
    return _canonical_json(record)


def _load_record_payload(path: Path) -> dict[str, object]:
    value = _load_bounded_json(path)
    if (
        value.get("schema_version") != EXACT_CONTEXT_LIVE_PAIR_RECORD_SCHEMA
        or value.get("evidence_class") != EXACT_CONTEXT_LIVE_EVIDENCE_CLASS
    ):
        raise ValueError("Exact-context live record schema is incompatible")
    content_hash = str(value.get("content_hash", ""))
    evidence_id = str(value.get("evidence_id", ""))
    payload = {
        key: item
        for key, item in value.items()
        if key not in {"content_hash", "evidence_id"}
    }
    if (
        content_hash != content_digest(payload)
        or evidence_id != f"exact-context-live-evidence-{content_hash[:24]}"
        or value.get("automatic_approval") is not False
        or value.get("eligible_for_apply") is not False
    ):
        raise ValueError("Exact-context live record content hash is invalid")
    return value


def _validate_record(
    path: Path,
    manifest: ExactContextLivePairManifest,
    expected: ExactContextBoundExpectedRun,
) -> dict[str, object]:
    value = _load_record_payload(path)
    exact = {
        "pair_id": manifest.pair_id,
        "binding_id": manifest.binding_id,
        "binding_content_hash": manifest.binding_content_hash,
        "preparation_id": manifest.preparation_id,
        "preparation_content_hash": manifest.preparation_content_hash,
        "source_revision": manifest.source_revision,
        "distribution_sha256": manifest.distribution_sha256,
        "provider_kind": manifest.provider_kind,
        "model_id": manifest.model_id,
        "authority_profile": manifest.authority_profile,
        "company_revision": manifest.company_revision,
        "roster_revision": manifest.roster_revision,
        "playbook_revision": manifest.playbook_revision,
        "goal_digest": manifest.goal_digest,
        "production_context_fingerprint": manifest.production_context_fingerprint,
        "bound_pattern_id": manifest.bound_pattern_id,
        "natural_evidence_content_hash": manifest.natural_evidence_content_hash,
        "run_id": expected.run_id,
        "workload_hash": expected.workload_hash,
        "strategy": expected.strategy,
        "configured_model_call_limit": manifest.max_model_calls_per_run,
        "configured_input_token_limit": manifest.max_input_tokens_per_run,
        "configured_output_token_limit": manifest.max_output_tokens_per_run,
        "configured_cost_limit_usd": manifest.max_cost_usd_per_run,
        "configured_wall_time_ms": manifest.max_wall_time_ms_per_run,
    }
    if any(value.get(key) != expected_value for key, expected_value in exact.items()):
        raise ValueError("Exact-context live record does not match the manifest")
    if value.get("quota_confirmed") is not True:
        raise ValueError("Exact-context live record lacks quota confirmation")
    return value


def _verify_runtime_inputs(
    metadata: Mapping[str, object],
    manifest: ExactContextLivePairManifest,
    *,
    require_source_snapshot: bool = True,
) -> None:
    source = Path(str(metadata["source_root"]))
    wheel = Path(str(metadata["wheel_path"]))
    parent = _parent_seed(Path(str(metadata["parent_directory"])))
    binding = load_exact_context_evidence_binding(Path(str(metadata["binding_path"])))
    preparation = load_exact_context_bound_preparation(
        Path(str(metadata["preparation_path"]))
    )
    if (
        require_source_snapshot
        and source_snapshot_revision(source) != manifest.source_revision
    ):
        raise ValueError("SOURCE_DRIFT")
    if (
        wheel_distribution_sha256(
            wheel,
            expected_version=manifest.noruct_version,
        )
        != manifest.distribution_sha256
    ):
        raise ValueError("WHEEL_DRIFT")
    if (
        binding.binding_id != manifest.binding_id
        or binding.content_hash != manifest.binding_content_hash
        or preparation.preparation_id != manifest.preparation_id
        or preparation.content_hash != manifest.preparation_content_hash
        or preparation.source_revision != manifest.source_revision
        or preparation.bound_pattern_id != manifest.bound_pattern_id
    ):
        raise ValueError("BINDING_OR_PREPARATION_DRIFT")
    if (
        parent.extension_id != manifest.parent_extension_id
        or parent.pattern_id != manifest.parent_pattern_id
        or parent.semantic_anchor != manifest.parent_semantic_anchor
        or parent.company_revision != manifest.company_revision
        or parent.roster_revision != manifest.roster_revision
        or parent.playbook_revision != manifest.playbook_revision
        or _sha256_file(parent.directory / "isolated-company-extension.db")
        != manifest.parent_company_state_sha256
    ):
        raise ValueError("PARENT_COMPANY_DRIFT")
    prior = workflow_patch_candidate_prior(
        context_fingerprint=manifest.production_context_fingerprint
    )
    if (
        prior.pattern_id != manifest.bound_pattern_id
        or manifest.completion_contract_revision
        != EXACT_CONTEXT_COMPLETION_CONTRACT_REVISION
        or manifest.completion_validator_revision
        != EXACT_CONTEXT_COMPLETION_VALIDATOR_REVISION
    ):
        raise ValueError("LIVE_CONTRACT_DRIFT")


async def run_next_exact_context_live_pair_slot(
    directory: str | Path,
    *,
    confirm_live_quota: bool,
    provider_factory=None,
    live_runner: Callable[..., Awaitable[ExactContextLiveRecord]] | None = None,
) -> ExactContextLivePairRunResult:
    status = exact_context_live_pair_status(directory)
    if (
        status.state != ExactContextLivePairState.READY
        or not status.next_slot
        or not status.next_strategy
    ):
        raise ValueError(
            f"Exact-context live pair cannot run while state={status.state.value}"
        )
    if not confirm_live_quota:
        raise ValueError(
            "Exact-context live pair requires --confirm-live-quota for one slot"
        )
    with ExactContextLivePairStore(directory) as store:
        metadata, manifest, _, natural, alpha_payload = _pair_artifacts(store)
        _verify_runtime_inputs(metadata, manifest)
        expected = next(
            item
            for item in manifest.expected_runs
            if item.slot == status.next_slot and item.strategy == status.next_strategy
        )
        start = store.append(
            CampaignEventKind.RUN_STARTED,
            fixture=expected.slot,
            strategy=expected.strategy,
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
    runner = live_runner or run_exact_context_live_evaluation
    try:
        record = await runner(
            manifest=manifest,
            natural=natural,
            alpha_payload=alpha_payload,
            expected=expected,
            command=str(metadata["codex_command"]),
            request_timeout_seconds=float(metadata["request_timeout_seconds"]),
            quota_confirmed=True,
            runtime_python=str(metadata.get("runtime_python", sys.executable)),
            provider_factory=provider_factory,
        )
        relative = Path("records-v1") / (
            f"{start.sequence:02d}-{expected.slot}-{expected.strategy}.json"
        )
        record_path = _write_private(
            Path(directory).expanduser().resolve() / relative,
            exact_context_live_record_to_json(record),
        )
        qualified = _validate_record(record_path, manifest, expected)
        validation = qualified["validation"]
        safety = qualified["safety"]
        cost = qualified["cost"]
        qualified_for_next = bool(
            qualified.get("task_success") is True
            and isinstance(validation, dict)
            and validation.get("passed") is True
            and isinstance(safety, dict)
            and safety.get("passed") is True
            and isinstance(cost, dict)
            and cost.get("tool_calls") == 0
        )
        with ExactContextLivePairStore(directory) as store:
            event = store.append(
                CampaignEventKind.RUN_RECORDED,
                fixture=expected.slot,
                strategy=expected.strategy,
                payload={
                    "record_path": relative.as_posix(),
                    "record_file_sha256": _sha256_file(record_path),
                    "record_content_hash": qualified["content_hash"],
                    "evaluation_run_id": expected.run_id,
                    "workload_hash": expected.workload_hash,
                    "status": qualified["status"],
                    "task_success": qualified["task_success"],
                    "artifact_quality_score": qualified["artifact"]["quality_score"],
                    "safety_passed": safety["passed"],
                    "completion_validation_passed": validation["passed"],
                    "completion_repair_used": validation["repair_used"],
                    "external_model_calls": qualified["external_model_calls"],
                    "qualified_for_next": qualified_for_next,
                    "automatic_approval": False,
                    "eligible_for_apply": False,
                },
            )
        return ExactContextLivePairRunResult(
            event=event,
            status=exact_context_live_pair_status(directory),
            record_path=str(record_path),
            task_success=bool(qualified["task_success"]),
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
        code = exc.code if isinstance(exc, ModelProviderError) else type(exc).__name__
        relative = Path("failures-v1") / (
            f"{start.sequence:02d}-{expected.slot}-{expected.strategy}.json"
        )
        failure = {
            "schema_version": EXACT_CONTEXT_LIVE_PAIR_FAILURE_SCHEMA,
            "pair_id": manifest.pair_id,
            "binding_content_hash": manifest.binding_content_hash,
            "preparation_content_hash": manifest.preparation_content_hash,
            "source_revision": manifest.source_revision,
            "distribution_sha256": manifest.distribution_sha256,
            "slot": expected.slot,
            "strategy": expected.strategy,
            "workload_hash": expected.workload_hash,
            "evaluation_run_id": expected.run_id,
            "recorded_at": utc_now().isoformat(),
            "failure_code": str(code),
            "interrupted": interrupted,
            "quota_confirmed": True,
            "partial_result_promoted": False,
        }
        failure_path = _write_private(
            Path(directory).expanduser().resolve() / relative,
            json.dumps(failure, ensure_ascii=False, sort_keys=True, indent=2),
        )
        with ExactContextLivePairStore(directory) as store:
            event = store.append(
                kind,
                fixture=expected.slot,
                strategy=expected.strategy,
                payload={
                    "failure_path": relative.as_posix(),
                    "failure_file_sha256": _sha256_file(failure_path),
                    "failure_code": str(code),
                    "partial_result_promoted": False,
                },
            )
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        return ExactContextLivePairRunResult(
            event=event,
            status=exact_context_live_pair_status(directory),
            record_path=None,
            task_success=False,
        )


def compare_exact_context_live_pair(
    directory: str | Path,
    *,
    output_path: str | Path | None = None,
    require_current_source_snapshot: bool = True,
) -> ExactContextLivePairComparison:
    status = exact_context_live_pair_status(directory)
    if status.state != ExactContextLivePairState.COMPLETE:
        raise ValueError(
            "Exact-context live comparison requires two qualified sealed records; "
            f"state={status.state.value},completed={status.completed_runs}/2"
        )
    root = Path(directory).expanduser().resolve()
    with ExactContextLivePairStore(root) as store:
        metadata, manifest, _, _, _ = _pair_artifacts(store)
        _verify_runtime_inputs(
            metadata,
            manifest,
            require_source_snapshot=require_current_source_snapshot,
        )
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
            item,
        )
        for item in manifest.expected_runs
    )
    control, candidate = records
    control_artifact = control["artifact"]
    candidate_artifact = candidate["artifact"]
    control_cost = control["cost"]
    candidate_cost = candidate["cost"]
    control_validation = control["validation"]
    candidate_validation = candidate["validation"]
    control_quality = float(control_artifact["quality_score"])
    candidate_quality = float(candidate_artifact["quality_score"])
    quality_gain = round(candidate_quality - control_quality, 4)
    control_calls = int(control_cost["runtime_model_calls"])
    candidate_calls = int(candidate_cost["runtime_model_calls"])
    control_repairs = int(bool(control_validation["repair_used"]))
    candidate_repairs = int(bool(candidate_validation["repair_used"]))
    control_tokens = int(control_cost["total_tokens"])
    candidate_tokens = int(candidate_cost["total_tokens"])
    safety = all(
        record.get("task_success") is True
        and record["validation"]["passed"] is True
        and record["safety"]["passed"] is True
        and record["safety"]["final_writer_count"] == 1
        and record["cost"]["tool_calls"] == 0
        for record in records
    )
    attribution = (
        control["prior_pattern_ids"] == []
        and control["prior_exposed_ids"] == []
        and control["prior_aligned_ids"] == []
        and candidate["prior_pattern_ids"] == [manifest.bound_pattern_id]
        and candidate["prior_exposed_ids"] == [manifest.bound_pattern_id]
        and candidate["prior_aligned_ids"] == [manifest.bound_pattern_id]
        and candidate["no_gap_control_exposed"] is False
        and candidate["no_gap_control_aligned"] is False
    )
    budget = (
        control_calls <= manifest.max_model_calls_per_run
        and candidate_calls <= manifest.max_model_calls_per_run
        and control_calls + candidate_calls <= manifest.max_model_calls_pair
        and float(control_cost["reported_cost_usd"]) <= manifest.max_cost_usd_per_run
        and float(candidate_cost["reported_cost_usd"]) <= manifest.max_cost_usd_per_run
    )
    effect = (
        candidate_quality >= control_quality
        and (
            quality_gain >= EXACT_CONTEXT_QUALITY_GAIN_THRESHOLD
            or candidate_calls < control_calls
            or candidate_repairs < control_repairs
            or candidate_tokens < control_tokens
        )
    )
    checks = (
        InformationBoundaryCheck(
            "same-source-wheel-model-authority-workload",
            control["workload_hash"] == candidate["workload_hash"]
            and control["source_revision"] == candidate["source_revision"]
            and control["distribution_sha256"] == candidate["distribution_sha256"]
            and control["model_id"] == candidate["model_id"]
            and control["authority_profile"] == candidate["authority_profile"],
            f"workload={control['workload_hash']},model={control['model_id']}",
        ),
        InformationBoundaryCheck(
            "task-validation-safety-single-writer-zero-tools",
            safety,
            "both records passed task, validation, safety, writer, and tool gates",
        ),
        InformationBoundaryCheck(
            "exact-bound-prior-attribution-and-no-gap-isolation",
            attribution,
            f"pattern={manifest.bound_pattern_id}",
        ),
        InformationBoundaryCheck(
            "quality-non-regression-and-measured-effect",
            effect,
            (
                f"quality={control_quality:.4f}->{candidate_quality:.4f},"
                f"calls={control_calls}->{candidate_calls},"
                f"repairs={control_repairs}->{candidate_repairs},"
                f"tokens={control_tokens}->{candidate_tokens}"
            ),
        ),
        InformationBoundaryCheck(
            "bounded-live-budget",
            budget,
            f"calls={control_calls + candidate_calls}/{manifest.max_model_calls_pair}",
        ),
        InformationBoundaryCheck(
            "immutable-parent-and-no-auto-apply",
            _sha256_file(
                Path(str(metadata["parent_directory"]))
                / "isolated-company-extension.db"
            )
            == manifest.parent_company_state_sha256
            and not manifest.automatic_approval
            and not manifest.eligible_for_apply,
            "parent Company unchanged; approval=false; apply=false",
        ),
    )
    pair_gate = all(check.passed for check in checks)
    comparison = ExactContextLivePairComparison(
        schema_version=EXACT_CONTEXT_LIVE_PAIR_COMPARISON_SCHEMA,
        pair_id=manifest.pair_id,
        manifest_content_hash=manifest.content_hash,
        completed_runs=2,
        expected_runs=2,
        control_quality=control_quality,
        candidate_quality=candidate_quality,
        quality_gain=quality_gain,
        control_model_calls=control_calls,
        candidate_model_calls=candidate_calls,
        model_call_delta=candidate_calls - control_calls,
        control_repairs=control_repairs,
        candidate_repairs=candidate_repairs,
        repair_delta=candidate_repairs - control_repairs,
        control_tokens=control_tokens,
        candidate_tokens=candidate_tokens,
        token_delta=candidate_tokens - control_tokens,
        safety_gate_passed=safety,
        attribution_gate_passed=attribution,
        budget_gate_passed=budget,
        effect_gate_passed=effect,
        pair_gate_passed=pair_gate,
        proposal_recommended=pair_gate,
        automatic_approval=False,
        eligible_for_apply=False,
        outcome=(
            "EXACT_CONTEXT_WORKFLOW_PATCH_VALUE_REPRODUCED"
            if pair_gate
            else "EXACT_CONTEXT_WORKFLOW_PATCH_VALUE_NOT_REPRODUCED"
        ),
        recommended_direction=(
            "REVIEW_PRODUCTION_CONTEXT_WORKFLOW_PATCH_PROPOSAL"
            if pair_gate
            else "INSPECT_NATURAL_RUNNER_OR_EFFECT_FAILURE"
        ),
        checks=checks,
        aggregator_provider_calls=0,
        aggregator_quota_consumed=False,
    )
    if output_path is not None:
        _write_private(Path(output_path).expanduser().resolve(), _canonical_json(comparison))
    return comparison


def load_exact_context_workflow_patch_promotion_source(
    directory: str | Path,
):
    """Verify frozen Phase 59 evidence without treating current source bytes as history.

    The historical wheel/source identity remains exact. Current product compatibility is
    separately pinned by the bound prior and completion-contract digest.
    """

    from dynamic_firm.company.promotion import (
        WORKFLOW_PATCH_PROMOTION_EVIDENCE_SCHEMA,
        WorkflowPatchPromotionEvidence,
    )

    root = Path(directory).expanduser().resolve()
    with ExactContextLivePairStore(root) as store:
        metadata, manifest, _, _, _ = _pair_artifacts(store)
        events = store.events()
    _verify_runtime_inputs(
        metadata,
        manifest,
        require_source_snapshot=False,
    )
    comparison = compare_exact_context_live_pair(
        root,
        require_current_source_snapshot=False,
    )
    comparison_path = root / "comparison-v1.json"
    persisted = _load_bounded_json(comparison_path)
    expected_comparison = to_primitive(comparison)
    if persisted != expected_comparison:
        raise ValueError("EXACT_CONTEXT_COMPARISON_DRIFT")

    recorded = {
        (event.fixture, event.strategy): event
        for event in events
        if event.kind == CampaignEventKind.RUN_RECORDED
    }
    records = tuple(
        _validate_record(
            root / str(recorded[(item.slot, item.strategy)].payload["record_path"]),
            manifest,
            item,
        )
        for item in manifest.expected_runs
    )
    if (
        not comparison.pair_gate_passed
        or not comparison.proposal_recommended
        or comparison.automatic_approval
        or comparison.eligible_for_apply
        or comparison.aggregator_provider_calls != 0
        or comparison.aggregator_quota_consumed
        or any(record["external_model_calls"] < 1 for record in records)
    ):
        raise ValueError("EXACT_CONTEXT_PROMOTION_GATE_FAILED")

    prior = workflow_patch_candidate_prior(
        context_fingerprint=manifest.production_context_fingerprint
    )
    compatibility = content_digest(
        {
            "schema": "noruct.workflow-patch-promotion-runtime-compatibility.v1",
            "prior": prior,
            "completion_contract_revision": EXACT_CONTEXT_COMPLETION_CONTRACT_REVISION,
            "completion_validator_revision": EXACT_CONTEXT_COMPLETION_VALIDATOR_REVISION,
        }
    )
    comparison_hash = content_digest(expected_comparison)
    base = WorkflowPatchPromotionEvidence(
        schema_version=WORKFLOW_PATCH_PROMOTION_EVIDENCE_SCHEMA,
        content_hash="pending",
        pair_id=manifest.pair_id,
        manifest_content_hash=manifest.content_hash,
        comparison_content_hash=comparison_hash,
        comparison_file_sha256=_sha256_file(comparison_path),
        binding_id=manifest.binding_id,
        binding_content_hash=manifest.binding_content_hash,
        preparation_id=manifest.preparation_id,
        preparation_content_hash=manifest.preparation_content_hash,
        source_revision=manifest.source_revision,
        distribution_sha256=manifest.distribution_sha256,
        model_id=manifest.model_id,
        authority_profile=manifest.authority_profile,
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=manifest.playbook_revision,
        parent_extension_id=manifest.parent_extension_id,
        parent_pattern_id=manifest.parent_pattern_id,
        parent_semantic_anchor=manifest.parent_semantic_anchor,
        parent_company_state_sha256=manifest.parent_company_state_sha256,
        production_context_fingerprint=manifest.production_context_fingerprint,
        bound_pattern_id=manifest.bound_pattern_id,
        workload_hash=manifest.expected_runs[0].workload_hash,
        run_ids=tuple(str(record["run_id"]) for record in records),
        live_evidence_ids=tuple(str(record["evidence_id"]) for record in records),
        live_evidence_content_hashes=tuple(
            str(record["content_hash"]) for record in records
        ),
        control_quality=comparison.control_quality,
        candidate_quality=comparison.candidate_quality,
        quality_gain=comparison.quality_gain,
        model_call_delta=comparison.model_call_delta,
        repair_delta=comparison.repair_delta,
        token_delta=comparison.token_delta,
        runtime_compatibility_digest=compatibility,
        pair_gate_passed=comparison.pair_gate_passed,
        proposal_recommended=comparison.proposal_recommended,
        automatic_approval=comparison.automatic_approval,
        eligible_for_apply=comparison.eligible_for_apply,
        external_model_calls=0,
        quota_consumed=False,
    )
    evidence = replace(base, content_hash=content_digest(base.content_payload()))
    parent_database = (
        Path(str(metadata["parent_directory"])).expanduser().resolve()
        / "isolated-company-extension.db"
    )
    if (
        not parent_database.is_file()
        or parent_database.is_symlink()
        or _sha256_file(parent_database) != manifest.parent_company_state_sha256
    ):
        raise ValueError("PARENT_COMPANY_DRIFT")
    return evidence, parent_database
