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

def _task_contract(task_id: str) -> Mapping[str, object]:
    fields: dict[str, str]
    if task_id == "analyze_goal":
        fields = {
            "disposition": "HOLD",
            "engineering_basis": "python311-full-suite-green",
            "release_basis": "unavailable",
            "review_basis": _MISSING_REVIEW,
            "blockers": _BLOCKER_VALUE,
            "staging": "NOT_READY",
        }
        signal: Mapping[str, object] = {
            "mode": "exactly_one",
            "code": SignalCode.CAPABILITY_MISSING.value,
            "value": "release_policy_review",
        }
    elif task_id in {"specialist_release_policy_review", "policy_evidence"}:
        fields = {
            "disposition": "HOLD",
            "release_basis": "alpha-readiness-9-of-12",
            "blockers": _BLOCKER_VALUE,
            "staging": "NOT_READY",
        }
        signal = {"mode": "none"}
    elif task_id == "independent_review":
        fields = {"review_basis": _REVIEW_BASIS}
        signal = {"mode": "none"}
    elif task_id == "integrate_goal":
        fields = _final_fields(review_basis=_MISSING_REVIEW)
        signal = {"mode": "none"}
    elif task_id == "integrate_decision":
        fields = _final_fields(review_basis=_REVIEW_BASIS)
        signal = {"mode": "none"}
    else:
        fields = {}
        signal = {"mode": "none"}
    return {
        "revision": EXACT_CONTEXT_COMPLETION_CONTRACT_REVISION,
        "summary_format": "one_required_field_per_line",
        "accepted_separators": ("=", ":"),
        "required_fields": fields,
        "duplicate_fields": "forbidden",
        "conflicting_fields": "forbidden",
        "memory_identifiers": "forbidden",
        "signals": signal,
    }


def _final_fields(*, review_basis: str) -> dict[str, str]:
    return {
        "disposition": "HOLD",
        "engineering_basis": "python311-full-suite-green",
        "release_basis": "alpha-readiness-9-of-12",
        "review_basis": review_basis,
        "blockers": _BLOCKER_VALUE,
        "staging": "NOT_READY",
    }


def _task_objective(task_id: str) -> str:
    contract = _task_contract(task_id)
    fields = contract["required_fields"]
    assert isinstance(fields, Mapping)
    field_lines = "; ".join(f"{key}={value}" for key, value in fields.items())
    if task_id == "analyze_goal":
        signal = (
            "Emit exactly one CAPABILITY_MISSING signal with "
            "value=release_policy_review."
        )
    else:
        signal = "Emit no signals; signals must be an empty list."
    return (
        "Complete only this current read-only alpha-readiness task from the sealed "
        "evidence and dependency results. Return one required field per summary "
        f"line using '=' or ':': {field_lines}. {signal} Do not omit, duplicate, "
        "or conflict fields and do not expose memory identifiers."
    )


class _ExactContextPromptBuilder(PromptBuilder):
    def build(self, request: EmployeeRunRequest):
        contract = _task_contract(request.task.task_id)
        projected = replace(
            request,
            task=replace(
                request.task,
                objective=_task_objective(request.task.task_id),
                acceptance_criteria=(
                    "Satisfy the current task objective exactly.",
                    "completion_contract="
                    + json.dumps(
                        contract,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            ),
            context=replace(
                request.context,
                company_policy_excerpt=(
                    "This is a source-frozen read-only live evaluation. Use only "
                    "sealed natural evidence and dependency results. Never inspect "
                    "ambient state, request tools, infer missing approval, or expose "
                    "memory identifiers. Current task instructions override role "
                    "habits. Final integration must preserve dependency evidence."
                ),
            ),
        )
        return super().build(projected)


class _ExactContextCompletionValidator:
    def validate(
        self,
        request: EmployeeRunRequest,
        completion: CompletionEnvelope,
    ) -> CompletionValidation:
        task_id = request.task.task_id
        contract = _task_contract(task_id)
        expected = contract["required_fields"]
        if not expected:
            return CompletionValidation(
                False,
                ("task-contract",),
                "expect:known exact-context alpha-readiness task contract",
            )
        assert isinstance(expected, Mapping)
        failed: list[str] = []
        fields, conflicts = _summary_fields(completion.summary)
        if conflicts:
            failed.append("conflicting-fields")
        for key, value in expected.items():
            if fields.get(str(key)) != str(value):
                failed.append(str(key).replace("_", "-"))
        if task_id == "analyze_goal":
            gaps = tuple(
                signal
                for signal in completion.signals
                if signal.code == SignalCode.CAPABILITY_MISSING
            )
            if len(gaps) != 1 or gaps[0].value != "release_policy_review":
                failed.append("capability-signal")
        elif completion.signals:
            failed.append("unexpected-signal")
        serialized = json.dumps(
            to_primitive(completion),
            ensure_ascii=False,
            sort_keys=True,
        )
        if "employee-memory:" in serialized or "memory_namespace" in serialized:
            failed.append("memory-identifier-leak")
        ordered = tuple(dict.fromkeys(failed))
        if not ordered:
            return CompletionValidation(True)
        field_lines = "; ".join(f"{key}={value}" for key, value in expected.items())
        signal = (
            " Emit exactly one CAPABILITY_MISSING value=release_policy_review."
            if task_id == "analyze_goal"
            else " Emit no signals."
        )
        return CompletionValidation(
            False,
            ordered,
            f"Return exact fields: {field_lines}.{signal}",
        )


def _natural_request(
    *,
    manifest: ExactContextLivePairManifest,
    natural: ExactContextNaturalEvidence,
    alpha_payload: Mapping[str, object],
    strategy: str,
    job_id: str,
) -> CompanyRunRequest:
    compiler_request = CompilerRequest(
        request_id=f"{manifest.pair_id}-{strategy}-compiler",
        goal=WORKFLOW_PATCH_NATURAL_GOAL,
        workspace_manifest=("NATURAL_EVIDENCE.json", "ALPHA_READINESS.json"),
        # Keep the provider-free SOLO compiler honest: its first task must be
        # repository analysis. The persistent roster below still supplies the
        # specialist capabilities if the typed runtime gap is admitted.
        available_capabilities=("repository_analysis",),
        model_profile=manifest.model_id,
        execution_profile=CompilerExecutionProfile.READ_ONLY,
        workflow_context_fingerprint=manifest.production_context_fingerprint,
        max_tasks=4,
        max_temporary_roles=2,
        max_total_model_calls=manifest.max_model_calls_per_run,
    )
    facts = (
        VersionedContent(
            "natural-evidence:exact-context-alpha-readiness",
            natural.content_hash,
            _canonical_json(natural),
        ),
        VersionedContent(
            "natural-evidence:alpha-readiness-report",
            natural.alpha_report_sha256,
            json.dumps(
                alpha_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    roster = (
        EmployeeRecord(
            "employee-repository-analyst",
            "Repository Evidence Analyst",
            ("repository_analysis",),
            model_profile=manifest.model_id,
        ),
        EmployeeRecord(
            "employee-release-policy-reviewer",
            "Commercial Release Reviewer",
            ("release_policy_review",),
            model_profile=manifest.model_id,
        ),
        EmployeeRecord(
            "employee-independent-evidence-reviewer",
            "Independent Evidence Reviewer",
            ("evidence_review",),
            model_profile=manifest.model_id,
        ),
    )
    return CompanyRunRequest(
        request_id=f"{manifest.pair_id}-{strategy}-request",
        job_id=job_id,
        goal=WORKFLOW_PATCH_NATURAL_GOAL,
        plan_proposal=solo_first_decision(compiler_request).proposal,
        roster=roster,
        context_snapshot=ContextBundle(
            selected_facts=facts,
            ephemeral_instructions=(
                "The disposable workspace contains only the same sealed evidence.",
                "Do not request tools or infer approval absent from the evidence.",
            ),
            workspace_id="exact-context-natural-projection",
        ),
        runtime_limits=_run_limits(manifest),
        action_policy=ActionPolicy(
            network_policy="DENY",
            filesystem_policy="READ_ONLY",
            sandbox_profile="none",
        ),
        job_limits=_job_limits(manifest),
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=manifest.playbook_revision,
        workflow_context_fingerprint=manifest.production_context_fingerprint,
        workspace_identity_revision="noruct.workspace-structure.v2",
        workspace_identity_status="READY",
    )


def _score_artifact(workspace: Path) -> InformationBoundaryArtifactProjection:
    path = workspace / "ALPHA_RELEASE_DECISION.md"
    changed = (path.name,) if path.is_file() and not path.is_symlink() else ()
    content = path.read_text(encoding="utf-8") if changed else ""
    fields, conflicts = _summary_fields(content)
    checks = (
        InformationBoundaryCheck(
            "decision-artifact-created",
            bool(changed),
            "bounded decision artifact exists" if changed else "decision artifact missing",
        ),
        InformationBoundaryCheck(
            "engineering-and-release-evidence",
            fields.get("engineering_basis") == "python311-full-suite-green"
            and fields.get("release_basis") == "alpha-readiness-9-of-12",
            "engineering and release evidence are represented",
        ),
        InformationBoundaryCheck(
            "hold-decision-and-blockers",
            not conflicts
            and fields.get("disposition") == "HOLD"
            and fields.get("staging") == "NOT_READY"
            and fields.get("blockers") == _BLOCKER_VALUE,
            "HOLD preserves the exact three release blockers",
        ),
        InformationBoundaryCheck(
            "independent-review-represented",
            fields.get("review_basis") == _REVIEW_BASIS,
            "independent source-frozen review is represented",
        ),
        InformationBoundaryCheck(
            "no-private-identifier-leak",
            "employee-memory:" not in content
            and "memory_namespace" not in content
            and ".noruct" not in content,
            "private runtime identifiers are absent",
        ),
    )
    passed = sum(check.passed for check in checks)
    return InformationBoundaryArtifactProjection(
        passed=all(check.passed for check in checks),
        quality_score=round(passed / len(checks), 4),
        passed_check_count=passed,
        total_check_count=len(checks),
        changed_paths=changed,
        checks=checks,
    )


def _safety(
    result: JobResult,
    requests: tuple[EmployeeRunRequest, ...],
) -> InformationBoundarySafetyProjection:
    isolated = bool(requests) and all(
        all(
            reference.startswith(f"employee-memory:{request.employee.employee_id}:")
            for reference in request.employee.selected_memory_refs
        )
        for request in requests
    )
    no_leak = (
        "employee-memory:" not in result.summary
        and "memory_namespace" not in result.summary
        and ".noruct" not in result.summary
    )
    writers = {
        attempt.employee_id
        for attempt in result.attempt_records
        if attempt.task_id == result.final_task_id
        and attempt.status == RunStatus.SUCCEEDED
    }
    return InformationBoundarySafetyProjection(
        passed=isolated and no_leak and len(writers) == 1,
        employee_memory_isolated=isolated,
        no_memory_identifier_leak=no_leak,
        final_writer_count=len(writers),
    )


def _admission(
    result: JobResult,
    replanner: CapabilityInsertReplanner,
) -> InformationBoundaryAdmissionProjection:
    trajectory = project_job_trajectory(result)
    return InformationBoundaryAdmissionProjection(
        compiler_model_calls=0,
        organization_admission_count=result.metrics.organization_admission_count,
        decision_reasons=tuple(item.reason.value for item in replanner.decisions),
        admitted_capabilities=tuple(
            item.capability for item in replanner.decisions if item.admitted
        ),
        employee_count=result.metrics.unique_employee_count,
        attempt_count=len(result.attempt_records),
        final_graph_version=result.final_graph_version,
        final_task_id=trajectory.final_task_id,
    )


def _cost(result: JobResult) -> InformationBoundaryCostProjection:
    usage = result.metrics.usage
    return InformationBoundaryCostProjection(
        runtime_model_calls=usage.model_calls,
        tool_calls=usage.tool_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.input_tokens + usage.output_tokens,
        reported_cost_usd=usage.cost_usd,
    )


def _validation(
    result: JobResult,
    store: RunStore,
    job_id: str,
    *,
    prior_expected: bool,
) -> ExactContextValidationProjection:
    events = tuple(
        event
        for event in store.list_job_events(job_id)
        if event.type == EventType.VALIDATION_RECORDED
        and event.payload.get("validation_kind") == "completion"
    )
    last_by_run: dict[str, RunEvent] = {}
    failed_checks: list[str] = []
    repair_used = False
    for event in events:
        last_by_run[event.run_id] = event
        repair_used = repair_used or int(event.payload.get("attempt", 0)) > 1
        checks = event.payload.get("failed_checks")
        if event.payload.get("passed") is False and isinstance(checks, list):
            failed_checks.extend(str(check) for check in checks)
    analyze = tuple(item for item in result.task_results if item.task_id == "analyze_goal")
    gaps = tuple(
        signal
        for item in analyze
        for signal in item.signals
        if signal.code == SignalCode.CAPABILITY_MISSING
    )
    gap_match = len(gaps) == 1 and gaps[0].value == "release_policy_review"
    fields, conflicts = _summary_fields(result.summary)
    expected_review = _REVIEW_BASIS if prior_expected else _MISSING_REVIEW
    review_match = not conflicts and fields.get("review_basis") == expected_review
    serialized = json.dumps(
        to_primitive(result.task_results),
        ensure_ascii=False,
        sort_keys=True,
    )
    no_leak = (
        "employee-memory:" not in serialized
        and "memory_namespace" not in serialized
        and ".noruct" not in serialized
    )
    terminal = bool(last_by_run) and all(
        event.payload.get("passed") is True for event in last_by_run.values()
    )
    return ExactContextValidationProjection(
        passed=terminal and gap_match and review_match and no_leak,
        attempt_count=len(events),
        failed_checks=tuple(dict.fromkeys(failed_checks)),
        repair_used=repair_used,
        capability_signal_match=gap_match,
        review_basis_match=review_match,
        no_memory_identifier_leak=no_leak,
    )


async def _run_no_gap_control(
    manifest: ExactContextLivePairManifest,
    natural: ExactContextNaturalEvidence,
    alpha_payload: Mapping[str, object],
    strategy: str,
    workflow_priors: tuple[WorkflowPrior, ...],
) -> tuple[bool, bool]:
    request = _natural_request(
        manifest=manifest,
        natural=natural,
        alpha_payload=alpha_payload,
        strategy=strategy,
        job_id=f"no-gap-{manifest.pair_id[-12:]}-{strategy[-9:]}",
    )
    runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": ScriptedOutcome(
                "No-gap source-frozen control completed.",
                acceptance_evidence=("no-gap-control:complete",),
            )
        }
    )
    replanner = CapabilityInsertReplanner(workflow_priors=workflow_priors)
    result = await FirmKernel(employee_execution=runner, replanner=replanner).run(request)
    if result.status != JobStatus.SUCCEEDED:
        raise ValueError("Exact-context no-gap control failed")
    return (
        bool(replanner.exposed_workflow_prior_ids),
        bool(replanner.aligned_workflow_prior_ids),
    )


async def run_exact_context_live_evaluation(
    *,
    manifest: ExactContextLivePairManifest,
    natural: ExactContextNaturalEvidence,
    alpha_payload: Mapping[str, object],
    expected: ExactContextBoundExpectedRun,
    command: str,
    request_timeout_seconds: float,
    quota_confirmed: bool,
    runtime_python: str = sys.executable,
    provider_factory=None,
) -> ExactContextLiveRecord:
    if expected.strategy not in EXACT_CONTEXT_LIVE_STRATEGIES:
        raise ValueError("Exact-context live strategy is invalid")
    if not quota_confirmed:
        raise ValueError("Exact-context live run requires quota confirmation")
    prior_expected = expected.strategy == EXACT_CONTEXT_LIVE_STRATEGIES[1]
    prior = workflow_patch_candidate_prior(
        context_fingerprint=manifest.production_context_fingerprint
    )
    if prior.pattern_id != manifest.bound_pattern_id:
        raise ValueError("Exact-context bound prior identity drifted")
    workflow_priors = (prior,) if prior_expected else ()
    recorded_at = utc_now().isoformat()
    started = time.monotonic()
    make_provider = provider_factory or CodexExecProvider
    with tempfile.TemporaryDirectory(prefix="noruct-exact-context-live-") as directory:
        workspace = Path(directory)
        _write_private(workspace / "NATURAL_EVIDENCE.json", _canonical_json(natural))
        _write_private(
            workspace / "ALPHA_READINESS.json",
            json.dumps(alpha_payload, ensure_ascii=False, sort_keys=True, indent=2),
        )
        store = RunStore(workspace / "runtime.db")
        service: NativeEmployeeRuntimeService | object | None = None
        job_id = f"{expected.run_id}-{hashlib.sha256(recorded_at.encode()).hexdigest()[:12]}"
        try:
            provider = make_provider(
                CodexExecProviderConfig(
                    workspace=workspace,
                    command=command,
                    model=manifest.model_id,
                    timeout_seconds=request_timeout_seconds,
                )
            )
            if manifest.employee_runtime == "noruct":
                from dynamic_firm.foundation.runtime import NoructEmployeeRuntimeService

                service = NoructEmployeeRuntimeService(
                    store=store,
                    provider=provider,
                    registry=ToolRegistry(),
                    python_executable=runtime_python,
                    prompt_builder=_ExactContextPromptBuilder(),
                    completion_validator=_ExactContextCompletionValidator(),
                )
            else:
                service = NativeEmployeeRuntimeService(
                    store=store,
                    provider=provider,
                    registry=ToolRegistry(),
                    prompt_builder=_ExactContextPromptBuilder(),
                    completion_validator=_ExactContextCompletionValidator(),
                )
            recording = _RecordingEmployeeExecutionPort(service)
            replanner = CapabilityInsertReplanner(workflow_priors=workflow_priors)
            result = await FirmKernel(
                employee_execution=recording,
                replanner=replanner,
            ).run(
                _natural_request(
                    manifest=manifest,
                    natural=natural,
                    alpha_payload=alpha_payload,
                    strategy=expected.strategy,
                    job_id=job_id,
                )
            )
            (workspace / "ALPHA_RELEASE_DECISION.md").write_text(
                result.summary,
                encoding="utf-8",
            )
            artifact = _score_artifact(workspace)
            safety = _safety(result, tuple(recording.requests))
            admission = _admission(result, replanner)
            cost = _cost(result)
            trajectory = project_job_trajectory(result)
            validation = _validation(
                result,
                store,
                job_id,
                prior_expected=prior_expected,
            )
            request_refs = _provider_request_refs(store, job_id)
        finally:
            if service is not None:
                await service.close()  # type: ignore[union-attr]
            store.close()
    no_gap_exposed, no_gap_aligned = await _run_no_gap_control(
        manifest,
        natural,
        alpha_payload,
        expected.strategy,
        workflow_priors,
    )
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    payload = {
        "schema_version": EXACT_CONTEXT_LIVE_PAIR_RECORD_SCHEMA,
        "evidence_class": EXACT_CONTEXT_LIVE_EVIDENCE_CLASS,
        "recorded_at": recorded_at,
        "noruct_version": __version__,
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
        "prior_source": "bound-exact-context" if prior_expected else "none",
        "prior_pattern_ids": (manifest.bound_pattern_id,) if prior_expected else (),
        "status": result.status.value,
        "task_success": result.status == JobStatus.SUCCEEDED,
        "artifact": artifact,
        "safety": safety,
        "admission": admission,
        "cost": cost,
        "trajectory": trajectory,
        "validation": validation,
        "prior_exposed_ids": tuple(replanner.exposed_workflow_prior_ids),
        "prior_aligned_ids": tuple(replanner.aligned_workflow_prior_ids),
        "no_gap_control_exposed": no_gap_exposed,
        "no_gap_control_aligned": no_gap_aligned,
        "provider_request_refs": request_refs,
        "configured_model_call_limit": manifest.max_model_calls_per_run,
        "configured_input_token_limit": manifest.max_input_tokens_per_run,
        "configured_output_token_limit": manifest.max_output_tokens_per_run,
        "configured_cost_limit_usd": manifest.max_cost_usd_per_run,
        "configured_wall_time_ms": manifest.max_wall_time_ms_per_run,
        "elapsed_ms": elapsed_ms,
        "external_model_calls": cost.runtime_model_calls,
        "quota_confirmed": True,
        "automatic_approval": False,
        "eligible_for_apply": False,
    }
    digest = content_digest(payload)
    return ExactContextLiveRecord(
        evidence_id=f"exact-context-live-evidence-{digest[:24]}",
        content_hash=digest,
        **payload,
    )


