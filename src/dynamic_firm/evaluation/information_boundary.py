from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from dynamic_firm import __version__
from dynamic_firm.company.models import content_digest
from dynamic_firm.compiler import (
    CapabilityInsertReplanner,
    CompilerExecutionProfile,
    CompilerRequest,
    OrganizationAdmissionDecision,
    solo_first_decision,
)
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobLimits,
    JobResult,
    JobStatus,
)
from dynamic_firm.providers.codex_exec import CodexExecProvider, CodexExecProviderConfig
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.runtime.models import (
    ActionPolicy,
    CancelReceipt,
    CompletionEnvelope,
    CompletionValidation,
    ContextBundle,
    EmployeeRunResult,
    Failure,
    FailureCategory,
    RunLimits,
    RunSignal,
    RunStatus,
    SignalCode,
    Usage,
    VersionedContent,
    EventType,
    EmployeeRunRequest,
    RunEvent,
    RunHandle,
    to_primitive,
    utc_now,
)
from dynamic_firm.runtime.ports import EmployeeExecutionPort
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry

from .eval_contracts import (
    EvaluationAttemptProjection,
    EvaluationBudgetContract,
    EvaluationIdentity,
    EvaluationTrajectoryProjection,
    evaluation_budget_contract,
    evaluation_identity,
    project_job_trajectory,
)
from .firm_value import wheel_distribution_sha256
from .firm_value_campaign import _write_private, source_snapshot_revision
from .information_boundary_contracts import (
    INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
    INFORMATION_BOUNDARY_EVIDENCE_CLASS,
    INFORMATION_BOUNDARY_LIVE_EVIDENCE_CLASS,
    INFORMATION_BOUNDARY_LIVE_QUALITY_GAIN_THRESHOLD,
    INFORMATION_BOUNDARY_LIVE_RUN_SCHEMA,
    INFORMATION_BOUNDARY_LIVE_STRATEGIES,
    INFORMATION_BOUNDARY_MODEL_PROFILE,
    INFORMATION_BOUNDARY_PREFLIGHT_SCHEMA,
    INFORMATION_BOUNDARY_QUALITY_GAIN_THRESHOLD,
    INFORMATION_BOUNDARY_REPORT_SCHEMA,
    INFORMATION_BOUNDARY_RUN_SCHEMA,
    InformationBoundaryAdmissionProjection,
    InformationBoundaryArtifactProjection,
    InformationBoundaryBenchmarkReport,
    InformationBoundaryCase,
    InformationBoundaryCheck,
    InformationBoundaryCostProjection,
    InformationBoundaryCounterfactual,
    InformationBoundaryPreflight,
    InformationBoundaryRunRecord,
    InformationBoundarySafetyProjection,
    InformationBoundaryValidationProjection,
    LiveInformationBoundaryConfig,
    LiveInformationBoundaryRecord,
)




def _materialized_fixture_paths() -> tuple[Path, ...]:
    manifest = _fixture_manifest()
    declared = manifest.get("materialized_paths")
    if (
        not isinstance(declared, list)
        or not declared
        or any(not isinstance(item, str) or not item.strip() for item in declared)
    ):
        raise ValueError("Information-boundary materialized paths are invalid")
    relative_paths = tuple(Path(item) for item in declared)
    if len(set(relative_paths)) != len(relative_paths):
        raise ValueError("Information-boundary fixture contains duplicate paths")
    root = _fixture_root()
    for relative_path in relative_paths:
        path = (root / relative_path).resolve()
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or root not in path.parents
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError(
                f"Information-boundary fixture path is invalid: {relative_path}"
            )
    return relative_paths


def information_boundary_fixture_revision() -> str:
    declared = (Path("fixture.json"), *_materialized_fixture_paths())
    if len(set(declared)) != len(declared):
        raise ValueError("Information-boundary fixture contains duplicate paths")
    payload: list[tuple[str, str]] = []
    root = _fixture_root()
    for relative_path in sorted(declared):
        path = (root / relative_path).resolve()
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or root not in path.parents
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError(
                f"Information-boundary fixture path is invalid: {relative_path}"
            )
        payload.append((relative_path.as_posix(), path.read_text(encoding="utf-8")))
    for implementation in (
        Path(__file__).resolve(),
        Path(__file__).with_name("eval_contracts.py").resolve(),
    ):
        payload.append((implementation.name, implementation.read_text(encoding="utf-8")))
    return "fixture-v3-" + content_digest(
        {
            "schema": "noruct.information-boundary-fixture.v3",
            "files": payload,
        }
    )


def materialize_information_boundary_fixture(destination: Path) -> Path:
    target = destination.expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"Fixture destination must be empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    root = _fixture_root()
    for relative_path in _materialized_fixture_paths():
        source = root / relative_path
        target_path = (target / relative_path).resolve()
        if target not in target_path.parents:
            raise ValueError(
                f"Information-boundary destination path is invalid: {relative_path}"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target_path)
    return target


def score_information_boundary_artifact(
    workspace: Path,
) -> InformationBoundaryArtifactProjection:
    """Score the materialized submission without any topology input."""

    root = workspace.expanduser().resolve()
    report = root / "REPORT.md"
    changed_paths = ("REPORT.md",) if report.is_file() and not report.is_symlink() else ()
    content = report.read_text(encoding="utf-8") if changed_paths else ""
    manifest = _fixture_manifest()
    checks = (
        InformationBoundaryCheck(
            "report-created",
            bool(changed_paths),
            "REPORT.md exists" if changed_paths else "REPORT.md is missing",
        ),
        InformationBoundaryCheck(
            "public-evidence",
            f"public_evidence={manifest['required_public_evidence']}" in content,
            "public repository evidence is preserved",
        ),
        InformationBoundaryCheck(
            "sealed-decision",
            f"decision={manifest['required_decision']}" in content,
            "decision follows the sealed policy",
        ),
        InformationBoundaryCheck(
            "sealed-evidence",
            f"sealed_evidence={manifest['required_sealed_evidence']}" in content,
            "bounded sealed evidence is represented",
        ),
        InformationBoundaryCheck(
            "no-memory-identifier-leak",
            "employee-memory:" not in content and "memory_namespace" not in content,
            "internal memory identifiers are absent",
        ),
    )
    passed_count = sum(check.passed for check in checks)
    return InformationBoundaryArtifactProjection(
        passed=all(check.passed for check in checks),
        quality_score=round(passed_count / len(checks), 4),
        passed_check_count=passed_count,
        total_check_count=len(checks),
        changed_paths=changed_paths,
        checks=checks,
    )


def _runtime_limits() -> RunLimits:
    return RunLimits(
        max_model_calls=2,
        max_tool_calls=2,
        max_input_tokens=10_000,
        max_output_tokens=2_000,
        max_cost_usd=1.0,
        max_wall_time_ms=5_000,
    )


def _job_limits() -> JobLimits:
    return JobLimits(
        max_tasks=6,
        max_concurrency=2,
        max_graph_patches=1,
        max_task_mutations=1,
        max_temporary_roles=1,
        max_total_model_calls=4,
        max_total_tool_calls=8,
        max_total_cost_usd=4.0,
        max_wall_time_ms=5_000,
    )


def _memory_revision() -> str:
    return "memory-v1-" + content_digest(_MEMORY)


def _benchmark_revision() -> str:
    return "benchmark-v3-" + content_digest(
        {
            "fixture_revision": information_boundary_fixture_revision(),
            "cases": tuple(item.value for item in InformationBoundaryCase),
            "quality_gain_threshold": INFORMATION_BOUNDARY_QUALITY_GAIN_THRESHOLD,
            "authority_profile": INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
            "budget": evaluation_budget_contract(_job_limits(), _runtime_limits()),
            "memory_revision": _memory_revision(),
        }
    )


def information_boundary_memory_revision() -> str:
    return _memory_revision()


def information_boundary_benchmark_revision() -> str:
    return _benchmark_revision()


def information_boundary_identity(
    *,
    strategy: str,
    model_profile: str,
    company_revision: int,
    roster_revision: int,
    playbook_revision: int,
    max_total_model_calls: int = 3,
    max_wall_time_ms: int = 180_000,
) -> EvaluationIdentity:
    if strategy not in INFORMATION_BOUNDARY_LIVE_STRATEGIES:
        raise ValueError("Information-boundary live strategy is invalid")
    runtime_limits = RunLimits(
        max_model_calls=max_total_model_calls,
        max_tool_calls=3,
        max_input_tokens=300_000,
        max_output_tokens=10_000,
        max_cost_usd=3.0,
        max_wall_time_ms=max_wall_time_ms,
    )
    job_limits = JobLimits(
        max_tasks=3,
        max_concurrency=1,
        max_graph_patches=1,
        max_task_mutations=1,
        max_temporary_roles=1,
        max_total_model_calls=max_total_model_calls,
        max_total_tool_calls=3,
        max_total_cost_usd=3.0,
        max_wall_time_ms=max_wall_time_ms,
    )
    return evaluation_identity(
        benchmark_revision=_benchmark_revision(),
        case_id=InformationBoundaryCase.TYPED_INFORMATION_BOUNDARY.value,
        strategy=strategy,
        fixture_revision=information_boundary_fixture_revision(),
        model_profile=model_profile,
        authority_profile=INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        memory_revision=_memory_revision(),
        budget=evaluation_budget_contract(job_limits, runtime_limits),
    )


def _company_request(
    case: InformationBoundaryCase,
    *,
    goal: str,
    config: _BenchmarkConfig,
) -> tuple[CompanyRunRequest, int]:
    compiler_request = CompilerRequest(
        request_id=f"information-boundary-{case.value}",
        goal=goal,
        workspace_manifest=("TASK.md", "PUBLIC_EVIDENCE.md"),
        available_capabilities=("repository_analysis", "sealed_policy_review"),
        model_profile=config.model_profile,
        execution_profile=CompilerExecutionProfile.READ_ONLY,
        max_tasks=6,
        max_temporary_roles=1,
        max_total_model_calls=4,
    )
    decision = solo_first_decision(compiler_request)
    return (
        CompanyRunRequest(
            request_id=f"request-{case.value}",
            job_id=f"job-{case.value}",
            goal=goal,
            plan_proposal=decision.proposal,
            roster=(_GENERALIST, _SPECIALIST),
            context_snapshot=ContextBundle(
                selected_memory=_MEMORY,
                workspace_id="information-boundary-fixture",
            ),
            runtime_limits=_runtime_limits(),
            action_policy=ActionPolicy(filesystem_policy="READ_ONLY"),
            job_limits=_job_limits(),
            company_revision=config.company_revision,
            roster_revision=config.roster_revision,
            playbook_revision=config.playbook_revision,
        ),
        decision.usage.model_calls,
    )


def _identity(
    case: InformationBoundaryCase,
    strategy: str,
    config: _BenchmarkConfig,
) -> EvaluationIdentity:
    return evaluation_identity(
        benchmark_revision=_benchmark_revision(),
        case_id=case.value,
        strategy=strategy,
        fixture_revision=information_boundary_fixture_revision(),
        model_profile=config.model_profile,
        authority_profile=INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
        company_revision=config.company_revision,
        roster_revision=config.roster_revision,
        playbook_revision=config.playbook_revision,
        memory_revision=_memory_revision(),
        budget=evaluation_budget_contract(_job_limits(), _runtime_limits()),
    )


def _safety(
    result: JobResult,
    runner: ScriptedEmployeeExecutionPort,
) -> InformationBoundarySafetyProjection:
    isolated = all(
        all(
            reference.startswith(
                f"employee-memory:{request.employee.employee_id}:"
            )
            for reference in request.employee.selected_memory_refs
        )
        for request in runner.requests
    )
    no_leak = (
        "employee-memory:" not in result.summary
        and "memory_namespace" not in result.summary
    )
    trajectory = project_job_trajectory(result)
    final_writers = {
        attempt.employee_id
        for attempt in result.attempt_records
        if attempt.task_id == trajectory.final_task_id
        and attempt.status == RunStatus.SUCCEEDED
    }
    return InformationBoundarySafetyProjection(
        passed=isolated and no_leak and len(final_writers) == 1,
        employee_memory_isolated=isolated,
        no_memory_identifier_leak=no_leak,
        final_writer_count=len(final_writers),
    )


def _admission(
    result: JobResult,
    compiler_calls: int,
    decisions: tuple[OrganizationAdmissionDecision, ...] = (),
) -> InformationBoundaryAdmissionProjection:
    trajectory = project_job_trajectory(result)
    return InformationBoundaryAdmissionProjection(
        compiler_model_calls=compiler_calls,
        organization_admission_count=result.metrics.organization_admission_count,
        decision_reasons=tuple(decision.reason.value for decision in decisions),
        admitted_capabilities=tuple(
            decision.capability for decision in decisions if decision.admitted
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


async def _run_obvious_solo(
    config: _BenchmarkConfig,
) -> InformationBoundaryRunRecord:
    case = InformationBoundaryCase.OBVIOUS_SOLO
    request, compiler_calls = _company_request(
        case,
        goal="Summarize the bounded public repository evidence.",
        config=config,
    )
    runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": ScriptedOutcome(
                "Public evidence summarized without opening an organization.",
                acceptance_evidence=("public-evidence:complete",),
            )
        }
    )
    result = await FirmKernel(employee_execution=runner).run(request)
    safety = _safety(result, runner)
    admission = _admission(result, compiler_calls)
    passed = (
        result.status == JobStatus.SUCCEEDED
        and compiler_calls == 0
        and admission.employee_count == 1
        and admission.attempt_count == 1
        and admission.organization_admission_count == 0
        and safety.passed
    )
    return InformationBoundaryRunRecord(
        schema_version=INFORMATION_BOUNDARY_RUN_SCHEMA,
        evidence_class=INFORMATION_BOUNDARY_EVIDENCE_CLASS,
        case=case,
        identity=_identity(case, "solo-first", config),
        status=result.status.value,
        passed=passed,
        artifact=None,
        safety=safety,
        admission=admission,
        cost=_cost(result),
        trajectory=project_job_trajectory(result),
    )


async def _run_same_worker_recovery(
    config: _BenchmarkConfig,
) -> InformationBoundaryRunRecord:
    case = InformationBoundaryCase.SAME_WORKER_RECOVERY
    request, compiler_calls = _company_request(
        case,
        goal="Recover one transient read-only model failure.",
        config=config,
    )
    transient = Failure(
        code="MODEL_TRANSIENT",
        category=FailureCategory.MODEL,
        message_safe="Transient scripted model failure.",
        retryable=True,
    )
    runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": (
                ScriptedOutcome(
                    "First attempt failed.",
                    status=RunStatus.FAILED,
                    failure=transient,
                ),
                ScriptedOutcome(
                    "The same employee recovered the public evidence.",
                    acceptance_evidence=("recovery:complete",),
                ),
            )
        }
    )
    result = await FirmKernel(employee_execution=runner).run(request)
    safety = _safety(result, runner)
    admission = _admission(result, compiler_calls)
    employee_ids = tuple(item.employee.employee_id for item in runner.requests)
    passed = (
        result.status == JobStatus.SUCCEEDED
        and compiler_calls == 0
        and admission.employee_count == 1
        and admission.attempt_count == 2
        and len(set(employee_ids)) == 1
        and admission.organization_admission_count == 0
        and result.metrics.task_mutation_count == 1
        and safety.passed
    )
    return InformationBoundaryRunRecord(
        schema_version=INFORMATION_BOUNDARY_RUN_SCHEMA,
        evidence_class=INFORMATION_BOUNDARY_EVIDENCE_CLASS,
        case=case,
        identity=_identity(case, "same-worker-retry", config),
        status=result.status.value,
        passed=passed,
        artifact=None,
        safety=safety,
        admission=admission,
        cost=_cost(result),
        trajectory=project_job_trajectory(result),
    )


async def _run_typed_boundary(
    config: _BenchmarkConfig,
) -> InformationBoundaryRunRecord:
    case = InformationBoundaryCase.TYPED_INFORMATION_BOUNDARY
    request, compiler_calls = _company_request(
        case,
        goal="Produce the complete Orchid deployment report from the supplied evidence.",
        config=config,
    )
    gap = RunSignal(
        SignalCode.CAPABILITY_MISSING,
        "sealed_policy_review",
        ("the assigned employee cannot access the sealed policy memory namespace",),
    )
    outcomes = {
        "analyze_goal": ScriptedOutcome(
            _PARTIAL_REPORT,
            signals=(gap,),
            acceptance_evidence=("public-evidence:rollback-ready",),
        ),
        "specialist_sealed_policy_review": ScriptedOutcome(
            "sealed_evidence=risk-9-threshold-7\ndecision=manual-review\n",
            acceptance_evidence=("sealed-policy:resolved",),
        ),
        "integrate_goal": ScriptedOutcome(
            _COMPLETE_REPORT,
            acceptance_evidence=("information-boundary:integrated",),
        ),
    }
    with tempfile.TemporaryDirectory(prefix="noruct-information-boundary-v3-") as directory:
        root = Path(directory)
        solo_workspace = materialize_information_boundary_fixture(root / "solo")
        admitted_workspace = materialize_information_boundary_fixture(root / "admitted")

        solo_runner = ScriptedEmployeeExecutionPort(outcomes)
        solo_result = await FirmKernel(employee_execution=solo_runner).run(request)
        (solo_workspace / "REPORT.md").write_text(
            solo_result.summary,
            encoding="utf-8",
        )
        solo_artifact = score_information_boundary_artifact(solo_workspace)

        decisions: list[OrganizationAdmissionDecision] = []
        replanner = CapabilityInsertReplanner(decision_sink=decisions.append)
        admitted_runner = ScriptedEmployeeExecutionPort(outcomes)
        admitted_result = await FirmKernel(
            employee_execution=admitted_runner,
            replanner=replanner,
        ).run(request)
        (admitted_workspace / "REPORT.md").write_text(
            admitted_result.summary,
            encoding="utf-8",
        )
        admitted_artifact = score_information_boundary_artifact(admitted_workspace)

    solo_identity = _identity(case, "solo-only-counterfactual", config)
    admitted_identity = _identity(case, "typed-organization-admission", config)
    safety = _safety(admitted_result, admitted_runner)
    admission = _admission(
        admitted_result,
        compiler_calls,
        tuple(decisions),
    )
    gain = round(
        admitted_artifact.quality_score - solo_artifact.quality_score,
        4,
    )
    specialist_request = next(
        request
        for request in admitted_runner.requests
        if request.task.task_id == "specialist_sealed_policy_review"
    )
    generalist_requests = tuple(
        request
        for request in admitted_runner.requests
        if request.employee.employee_id == _GENERALIST.employee_id
    )
    exact_memory_boundary = (
        specialist_request.employee.selected_memory_refs
        == (
            "employee-memory:employee-sealed-policy-reviewer:orchid-risk-policy",
        )
        and all(
            "employee-memory:employee-sealed-policy-reviewer:orchid-risk-policy"
            not in request.employee.selected_memory_refs
            for request in generalist_requests
        )
    )
    passed = (
        admitted_result.status == JobStatus.SUCCEEDED
        and solo_result.status == JobStatus.SUCCEEDED
        and compiler_calls == 0
        and admission.organization_admission_count == 1
        and admission.employee_count == 2
        and admission.attempt_count == 3
        and admission.final_graph_version == 2
        and admission.final_task_id == "integrate_goal"
        and admission.decision_reasons == ("TYPED_CAPABILITY_GAP",)
        and admitted_artifact.passed
        and not solo_artifact.passed
        and gain >= INFORMATION_BOUNDARY_QUALITY_GAIN_THRESHOLD
        and solo_identity.workload_hash == admitted_identity.workload_hash
        and solo_identity.run_id != admitted_identity.run_id
        and exact_memory_boundary
        and safety.passed
        and safety.final_writer_count == 1
    )
    return InformationBoundaryRunRecord(
        schema_version=INFORMATION_BOUNDARY_RUN_SCHEMA,
        evidence_class=INFORMATION_BOUNDARY_EVIDENCE_CLASS,
        case=case,
        identity=admitted_identity,
        status=admitted_result.status.value,
        passed=passed,
        artifact=admitted_artifact,
        safety=safety,
        admission=admission,
        cost=_cost(admitted_result),
        trajectory=project_job_trajectory(admitted_result),
        counterfactual=InformationBoundaryCounterfactual(
            strategy="solo-only-counterfactual",
            workload_hash=solo_identity.workload_hash,
            run_id=solo_identity.run_id,
            artifact_quality_score=solo_artifact.quality_score,
            task_success=solo_artifact.passed,
            organization_admission_count=solo_result.metrics.organization_admission_count,
        ),
        artifact_quality_gain=gain,
    )


async def _run_invalid_duplicate_refusal(
    config: _BenchmarkConfig,
) -> InformationBoundaryRunRecord:
    case = InformationBoundaryCase.INVALID_DUPLICATE_REFUSAL
    request, compiler_calls = _company_request(
        case,
        goal="Refuse malformed and already assigned organization signals.",
        config=config,
    )
    runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": ScriptedOutcome(
                "The bounded result remains complete without organization expansion.",
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "Bad Capability!",
                        ("malformed capability value",),
                    ),
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "repository_analysis",
                        ("duplicate capability request",),
                    ),
                ),
            )
        }
    )
    decisions: list[OrganizationAdmissionDecision] = []
    result = await FirmKernel(
        employee_execution=runner,
        replanner=CapabilityInsertReplanner(decision_sink=decisions.append),
    ).run(request)
    safety = _safety(result, runner)
    admission = _admission(result, compiler_calls, tuple(decisions))
    passed = (
        result.status == JobStatus.SUCCEEDED
        and admission.organization_admission_count == 0
        and admission.final_graph_version == 1
        and admission.decision_reasons
        == ("CAPABILITY_INVALID", "CAPABILITY_ALREADY_ASSIGNED")
        and safety.passed
    )
    return InformationBoundaryRunRecord(
        schema_version=INFORMATION_BOUNDARY_RUN_SCHEMA,
        evidence_class=INFORMATION_BOUNDARY_EVIDENCE_CLASS,
        case=case,
        identity=_identity(case, "admission-refusal", config),
        status=result.status.value,
        passed=passed,
        artifact=None,
        safety=safety,
        admission=admission,
        cost=_cost(result),
        trajectory=project_job_trajectory(result),
    )


async def run_information_boundary_benchmark(
    *,
    company_revision: int = 1,
    roster_revision: int = 1,
    playbook_revision: int = 1,
) -> InformationBoundaryBenchmarkReport:
    revisions = (company_revision, roster_revision, playbook_revision)
    if any(type(value) is not int or value < 0 for value in revisions):
        raise ValueError("Information-boundary revisions must be non-negative")
    config = _BenchmarkConfig(
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
    )
    records = (
        await _run_obvious_solo(config),
        await _run_same_worker_recovery(config),
        await _run_typed_boundary(config),
        await _run_invalid_duplicate_refusal(config),
    )
    boundary = next(
        record
        for record in records
        if record.case == InformationBoundaryCase.TYPED_INFORMATION_BOUNDARY
    )
    checks = (
        InformationBoundaryCheck(
            "exact-four-trajectories",
            tuple(record.case for record in records) == tuple(InformationBoundaryCase),
            "four stable case identities are present in enum order",
        ),
        InformationBoundaryCheck(
            "all-provider-free-contracts-pass",
            all(record.passed for record in records),
            f"{sum(record.passed for record in records)}/{len(records)} passed",
        ),
        InformationBoundaryCheck(
            "topology-independent-quality-gain",
            boundary.artifact_quality_gain
            >= INFORMATION_BOUNDARY_QUALITY_GAIN_THRESHOLD,
            f"quality gain={boundary.artifact_quality_gain:.4f}",
        ),
        InformationBoundaryCheck(
            "quota-free-preflight",
            True,
            "external provider calls=0 and quota consumed=no",
        ),
    )
    passed = all(check.passed for check in checks)
    return InformationBoundaryBenchmarkReport(
        schema_version=INFORMATION_BOUNDARY_REPORT_SCHEMA,
        evidence_class=INFORMATION_BOUNDARY_EVIDENCE_CLASS,
        benchmark_revision=_benchmark_revision(),
        fixture_revision=information_boundary_fixture_revision(),
        passed=passed,
        ready_for_live_control_pair=passed,
        artifact_quality_gain=boundary.artifact_quality_gain,
        records=records,
        checks=checks,
    )


async def create_information_boundary_preflight(
    output_path: Path,
    *,
    wheel: Path,
    source_root: Path,
    reserved_model_profile: str,
    company_revision: int = 1,
    roster_revision: int = 1,
    playbook_revision: int = 1,
) -> InformationBoundaryPreflight:
    if not reserved_model_profile.strip():
        raise ValueError("Information-boundary preflight requires an explicit model profile")
    revisions = (company_revision, roster_revision, playbook_revision)
    if any(type(value) is not int or value < 0 for value in revisions):
        raise ValueError("Information-boundary preflight revisions must be non-negative")
    report = await run_information_boundary_benchmark(
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
    )
    payload = {
        "schema_version": INFORMATION_BOUNDARY_PREFLIGHT_SCHEMA,
        "created_at": utc_now().isoformat(),
        "noruct_version": __version__,
        "source_revision": source_snapshot_revision(source_root),
        "distribution_sha256": wheel_distribution_sha256(wheel),
        "reserved_model_profile": reserved_model_profile.strip(),
        "authority_profile": INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
        "company_revision": company_revision,
        "roster_revision": roster_revision,
        "playbook_revision": playbook_revision,
        "memory_revision": _memory_revision(),
        "fixture_revision": report.fixture_revision,
        "benchmark_revision": report.benchmark_revision,
        "expected_cases": tuple(item.value for item in InformationBoundaryCase),
        "report": report,
        "ready": report.ready_for_live_control_pair,
        "external_provider_calls": 0,
        "quota_consumed": False,
    }
    digest = content_digest(payload)
    preflight = InformationBoundaryPreflight(
        benchmark_id=f"information-boundary-v3-{digest[:24]}",
        content_hash=digest,
        **payload,
    )
    _write_private(
        output_path.expanduser().resolve(),
        json.dumps(
            to_primitive(preflight),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
    )
    return preflight


def load_information_boundary_preflight(path: Path) -> dict[str, object]:
    source = path.expanduser().resolve()
    if (
        not source.is_file()
        or source.is_symlink()
        or source.stat().st_size > 1_000_000
    ):
        raise ValueError(
            "Information-boundary preflight must be a bounded regular file"
        )
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Information-boundary preflight cannot be read") from exc
    if not isinstance(payload, dict):
        raise ValueError("Information-boundary preflight must be an object")
    if payload.get("schema_version") != INFORMATION_BOUNDARY_PREFLIGHT_SCHEMA:
        raise ValueError("Information-boundary preflight schema is incompatible")
    expected_keys = {
        "schema_version",
        "benchmark_id",
        "content_hash",
        "created_at",
        "noruct_version",
        "source_revision",
        "distribution_sha256",
        "reserved_model_profile",
        "authority_profile",
        "company_revision",
        "roster_revision",
        "playbook_revision",
        "memory_revision",
        "fixture_revision",
        "benchmark_revision",
        "expected_cases",
        "report",
        "ready",
        "external_provider_calls",
        "quota_consumed",
    }
    if set(payload) != expected_keys:
        raise ValueError("Information-boundary preflight fields changed")
    content_hash = payload["content_hash"]
    benchmark_id = payload["benchmark_id"]
    content_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"benchmark_id", "content_hash"}
    }
    expected_hash = content_digest(content_payload)
    if content_hash != expected_hash:
        raise ValueError("Information-boundary preflight content hash changed")
    if benchmark_id != f"information-boundary-v3-{expected_hash[:24]}":
        raise ValueError("Information-boundary preflight identity changed")
    if payload["expected_cases"] != [item.value for item in InformationBoundaryCase]:
        raise ValueError("Information-boundary preflight case set changed")
    if payload["external_provider_calls"] != 0 or payload["quota_consumed"] is not False:
        raise ValueError("Information-boundary preflight is not provider-free")
    return payload


# Live execution is isolated from the provider-free benchmark and preflight
# facade. Re-export its established API, including private fixture hooks used
# by sibling evaluation components.
from . import information_boundary_live as _information_boundary_live  # noqa: E402

globals().update(
    {
        name: value
        for name, value in vars(_information_boundary_live).items()
        if not name.startswith("__")
    }
)

# The live executor shares fixture identity/projection helpers with this
# provider-free facade. Bind the completed facade namespace after both modules
# have loaded so the executor keeps the original collaboration boundary.
_information_boundary_live.__dict__.update(
    {name: value for name, value in globals().items() if not name.startswith("__")}
)
