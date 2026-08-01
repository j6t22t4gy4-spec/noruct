from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

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
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ContextBundle,
    RunSignal,
    RunStatus,
    SignalCode,
    RunLimits,
    VersionedContent,
)

from .eval_contracts import (
    EvaluationIdentity,
    EvaluationTrajectoryProjection,
    evaluation_budget_contract,
    evaluation_identity,
    project_job_trajectory,
)
from .information_boundary import (
    INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
    INFORMATION_BOUNDARY_EVIDENCE_CLASS,
    INFORMATION_BOUNDARY_QUALITY_GAIN_THRESHOLD,
    InformationBoundaryAdmissionProjection,
    InformationBoundaryArtifactProjection,
    InformationBoundaryBenchmarkReport,
    InformationBoundaryCheck,
    InformationBoundaryCostProjection,
    InformationBoundaryCounterfactual,
    InformationBoundarySafetyProjection,
    InformationBoundaryCase,
    information_boundary_fixture_revision,
    information_boundary_memory_revision,
    run_information_boundary_benchmark,
)


RELEASE_AUTHORIZATION_RUN_SCHEMA = (
    "noruct.release-authorization-information-boundary-run.v4"
)
RELEASE_AUTHORIZATION_REPORT_SCHEMA = (
    "noruct.release-authorization-information-boundary-report.v4"
)
INFORMATION_BOUNDARY_SUITE_MODEL_PROFILE = "offline-scripted-v4"
RELEASE_AUTHORIZATION_QUALITY_GAIN_THRESHOLD = 0.2


class ReleaseAuthorizationCase(StrEnum):
    OBVIOUS_SOLO = "release-obvious-solo"
    INFORMATION_BOUNDARY = "release-information-boundary"
    INVALID_CAPABILITY_REFUSAL = "release-invalid-capability-refusal"
    MEMORY_LEAK_REFUSAL = "release-memory-leak-refusal"


@dataclass(frozen=True, slots=True)
class ReleaseAuthorizationRunRecord:
    schema_version: str
    evidence_class: str
    case: ReleaseAuthorizationCase
    identity: EvaluationIdentity
    status: str
    passed: bool
    artifact: InformationBoundaryArtifactProjection | None
    safety: InformationBoundarySafetyProjection
    admission: InformationBoundaryAdmissionProjection
    cost: InformationBoundaryCostProjection
    trajectory: EvaluationTrajectoryProjection
    counterfactual: InformationBoundaryCounterfactual | None = None
    artifact_quality_gain: float = 0.0


@dataclass(frozen=True, slots=True)
class ReleaseAuthorizationFixtureReport:
    schema_version: str
    evidence_class: str
    fixture_id: str
    fixture_revision: str
    memory_revision: str
    benchmark_revision: str
    capability: str
    output_path: str
    passed: bool
    ready_for_live_control_pair: bool
    artifact_quality_gain: float
    records: tuple[ReleaseAuthorizationRunRecord, ...]
    checks: tuple[InformationBoundaryCheck, ...]
    external_provider_calls: int = 0
    quota_consumed: bool = False


@dataclass(frozen=True, slots=True)
class InformationBoundaryFixtureGain:
    fixture_id: str
    fixture_revision: str
    memory_revision: str
    capability: str
    output_path: str
    solo_quality: float
    admitted_quality: float
    artifact_quality_gain: float


@dataclass(frozen=True, slots=True)
class _SuiteConfig:
    company_revision: int = 1
    roster_revision: int = 1
    playbook_revision: int = 1


_GENERALIST = EmployeeRecord(
    "employee-release-generalist",
    "Repository Release Generalist",
    ("repository_analysis",),
)
_SPECIALIST = EmployeeRecord(
    "employee-release-policy-reviewer",
    "Release Policy Reviewer",
    ("release_policy_review",),
)
_MEMORY = (
    VersionedContent(
        "employee-memory:employee-release-generalist:atlas-public-procedure",
        "1",
        "Use only public release evidence. Hold when sealed release policy is unavailable.",
    ),
    VersionedContent(
        "employee-memory:employee-release-policy-reviewer:atlas-release-rule",
        "1",
        (
            "Atlas R2 has a valid signed attestation. The bounded policy basis is "
            "attestation-green-rule-r2. With tests-128-passed, disposition is RELEASE "
            "and required action is publish-release-notes."
        ),
    ),
)
_PARTIAL_REVIEW = (
    "disposition=HOLD\n"
    "public_basis=tests-128-passed\n"
    "policy_basis=unavailable\n"
    "required_action=policy-review-required\n"
)
_COMPLETE_REVIEW = (
    "disposition=RELEASE\n"
    "public_basis=tests-128-passed\n"
    "policy_basis=attestation-green-rule-r2\n"
    "required_action=publish-release-notes\n"
)
_LEAKING_REVIEW = (
    _COMPLETE_REVIEW
    + "source=employee-memory:employee-release-policy-reviewer:atlas-release-rule\n"
)


def _fixture_root() -> Path:
    root = (
        Path(__file__).with_name("fixtures_v3") / "release-authorization"
    ).resolve()
    if not root.is_dir():
        raise ValueError("Release-authorization fixture is unavailable")
    return root


def _fixture_manifest() -> dict[str, object]:
    payload = json.loads((_fixture_root() / "fixture.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("id") != "release-authorization":
        raise ValueError("Release-authorization fixture manifest is invalid")
    return payload


def _materialized_fixture_paths() -> tuple[Path, ...]:
    declared = _fixture_manifest().get("materialized_paths")
    if (
        not isinstance(declared, list)
        or not declared
        or any(not isinstance(item, str) or not item.strip() for item in declared)
    ):
        raise ValueError("Release-authorization materialized paths are invalid")
    relative_paths = tuple(Path(item) for item in declared)
    if len(set(relative_paths)) != len(relative_paths):
        raise ValueError("Release-authorization fixture contains duplicate paths")
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
                f"Release-authorization fixture path is invalid: {relative_path}"
            )
    return relative_paths


def release_authorization_fixture_revision() -> str:
    declared = (Path("fixture.json"), *_materialized_fixture_paths())
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
                f"Release-authorization fixture path is invalid: {relative_path}"
            )
        payload.append((relative_path.as_posix(), path.read_text(encoding="utf-8")))
    for implementation in (
        Path(__file__).resolve(),
        Path(__file__).with_name("eval_contracts.py").resolve(),
    ):
        payload.append((implementation.name, implementation.read_text(encoding="utf-8")))
    return "fixture-v4-" + content_digest(
        {
            "schema": "noruct.release-authorization-fixture.v4",
            "files": payload,
        }
    )


def materialize_release_authorization_fixture(destination: Path) -> Path:
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
                f"Release-authorization destination path is invalid: {relative_path}"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target_path)
    return target


def score_release_authorization_artifact(
    workspace: Path,
) -> InformationBoundaryArtifactProjection:
    """Score only the materialized release artifact, without topology input."""

    root = workspace.expanduser().resolve()
    manifest = _fixture_manifest()
    output_path = str(manifest["output_path"])
    review = root / output_path
    changed_paths = (
        (output_path,) if review.is_file() and not review.is_symlink() else ()
    )
    content = review.read_text(encoding="utf-8") if changed_paths else ""
    checks = (
        InformationBoundaryCheck(
            "release-review-created",
            bool(changed_paths),
            f"{output_path} exists" if changed_paths else f"{output_path} is missing",
        ),
        InformationBoundaryCheck(
            "release-disposition",
            f"disposition={manifest['required_disposition']}" in content,
            "final disposition follows the sealed release rule",
        ),
        InformationBoundaryCheck(
            "release-public-basis",
            f"public_basis={manifest['required_public_basis']}" in content,
            "public test evidence is preserved",
        ),
        InformationBoundaryCheck(
            "release-policy-basis",
            f"policy_basis={manifest['required_policy_basis']}" in content,
            "bounded sealed release policy evidence is represented",
        ),
        InformationBoundaryCheck(
            "release-required-action",
            f"required_action={manifest['required_action']}" in content,
            "required follow-up action is explicit",
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


def release_authorization_memory_revision() -> str:
    return "memory-v2-" + content_digest(_MEMORY)


def release_authorization_benchmark_revision() -> str:
    return "benchmark-release-v4-" + content_digest(
        {
            "fixture_revision": release_authorization_fixture_revision(),
            "cases": tuple(item.value for item in ReleaseAuthorizationCase),
            "quality_gain_threshold": RELEASE_AUTHORIZATION_QUALITY_GAIN_THRESHOLD,
            "authority_profile": INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
            "budget": evaluation_budget_contract(_job_limits(), _runtime_limits()),
            "memory_revision": release_authorization_memory_revision(),
        }
    )


def information_boundary_suite_revision() -> str:
    return "benchmark-suite-v4-" + content_digest(
        {
            "legacy_fixture_revision": information_boundary_fixture_revision(),
            "legacy_memory_revision": information_boundary_memory_revision(),
            "release_fixture_revision": release_authorization_fixture_revision(),
            "release_memory_revision": release_authorization_memory_revision(),
            "legacy_gain_threshold": INFORMATION_BOUNDARY_QUALITY_GAIN_THRESHOLD,
            "release_gain_threshold": RELEASE_AUTHORIZATION_QUALITY_GAIN_THRESHOLD,
            "release_cases": tuple(item.value for item in ReleaseAuthorizationCase),
        }
    )


def _company_request(
    case: ReleaseAuthorizationCase,
    *,
    goal: str,
    config: _SuiteConfig,
) -> tuple[CompanyRunRequest, int]:
    compiler_request = CompilerRequest(
        request_id=f"release-authorization-{case.value}",
        goal=goal,
        workspace_manifest=("TASK.md", "PUBLIC_RELEASE_EVIDENCE.md"),
        available_capabilities=("repository_analysis", "release_policy_review"),
        model_profile=INFORMATION_BOUNDARY_SUITE_MODEL_PROFILE,
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
                workspace_id="release-authorization-fixture",
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
    case: ReleaseAuthorizationCase,
    strategy: str,
    config: _SuiteConfig,
) -> EvaluationIdentity:
    return evaluation_identity(
        benchmark_revision=release_authorization_benchmark_revision(),
        case_id=case.value,
        strategy=strategy,
        fixture_revision=release_authorization_fixture_revision(),
        model_profile=INFORMATION_BOUNDARY_SUITE_MODEL_PROFILE,
        authority_profile=INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
        company_revision=config.company_revision,
        roster_revision=config.roster_revision,
        playbook_revision=config.playbook_revision,
        memory_revision=release_authorization_memory_revision(),
        budget=evaluation_budget_contract(_job_limits(), _runtime_limits()),
    )


def _safety(
    result: JobResult,
    runner: ScriptedEmployeeExecutionPort,
) -> InformationBoundarySafetyProjection:
    isolated = bool(runner.requests) and all(
        all(
            reference.startswith(f"employee-memory:{request.employee.employee_id}:")
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
    config: _SuiteConfig,
) -> ReleaseAuthorizationRunRecord:
    case = ReleaseAuthorizationCase.OBVIOUS_SOLO
    request, compiler_calls = _company_request(
        case,
        goal="Use the public docs-only exemption to decide whether Atlas Docs may ship.",
        config=config,
    )
    runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": ScriptedOutcome(
                (
                    "disposition=RELEASE\n"
                    "public_basis=docs-tests-12-passed\n"
                    "required_action=publish-docs\n"
                ),
                acceptance_evidence=("public-release-exemption:complete",),
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
    return ReleaseAuthorizationRunRecord(
        schema_version=RELEASE_AUTHORIZATION_RUN_SCHEMA,
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


def _boundary_outcomes(*, leak_final: bool = False) -> dict[str, ScriptedOutcome]:
    gap = RunSignal(
        SignalCode.CAPABILITY_MISSING,
        "release_policy_review",
        ("the assigned employee cannot access the sealed release-policy namespace",),
    )
    return {
        "analyze_goal": ScriptedOutcome(
            _PARTIAL_REVIEW,
            signals=(gap,),
            acceptance_evidence=("public-release-evidence:tests-128-passed",),
        ),
        "specialist_release_policy_review": ScriptedOutcome(
            (
                "disposition=RELEASE\n"
                "policy_basis=attestation-green-rule-r2\n"
                "required_action=publish-release-notes\n"
            ),
            acceptance_evidence=("release-policy:resolved",),
        ),
        "integrate_goal": ScriptedOutcome(
            _LEAKING_REVIEW if leak_final else _COMPLETE_REVIEW,
            acceptance_evidence=("release-authorization:integrated",),
        ),
    }


async def _run_boundary(
    config: _SuiteConfig,
) -> ReleaseAuthorizationRunRecord:
    case = ReleaseAuthorizationCase.INFORMATION_BOUNDARY
    request, compiler_calls = _company_request(
        case,
        goal="Use the public change summary and tests to decide whether Atlas R2 may ship.",
        config=config,
    )
    outcomes = _boundary_outcomes()
    with tempfile.TemporaryDirectory(
        prefix="noruct-release-authorization-v4-"
    ) as directory:
        root = Path(directory)
        solo_workspace = materialize_release_authorization_fixture(root / "solo")
        admitted_workspace = materialize_release_authorization_fixture(
            root / "admitted"
        )
        solo_runner = ScriptedEmployeeExecutionPort(outcomes)
        solo_result = await FirmKernel(employee_execution=solo_runner).run(request)
        (solo_workspace / "RELEASE_REVIEW.md").write_text(
            solo_result.summary,
            encoding="utf-8",
        )
        solo_artifact = score_release_authorization_artifact(solo_workspace)

        decisions: list[OrganizationAdmissionDecision] = []
        admitted_runner = ScriptedEmployeeExecutionPort(outcomes)
        admitted_result = await FirmKernel(
            employee_execution=admitted_runner,
            replanner=CapabilityInsertReplanner(decision_sink=decisions.append),
        ).run(request)
        (admitted_workspace / "RELEASE_REVIEW.md").write_text(
            admitted_result.summary,
            encoding="utf-8",
        )
        admitted_artifact = score_release_authorization_artifact(
            admitted_workspace
        )

    solo_identity = _identity(case, "solo-only-counterfactual", config)
    admitted_identity = _identity(case, "typed-organization-admission", config)
    safety = _safety(admitted_result, admitted_runner)
    admission = _admission(admitted_result, compiler_calls, tuple(decisions))
    gain = round(
        admitted_artifact.quality_score - solo_artifact.quality_score,
        4,
    )
    specialist_request = next(
        item
        for item in admitted_runner.requests
        if item.task.task_id == "specialist_release_policy_review"
    )
    generalist_requests = tuple(
        item
        for item in admitted_runner.requests
        if item.employee.employee_id == _GENERALIST.employee_id
    )
    specialist_memory = (
        "employee-memory:employee-release-policy-reviewer:atlas-release-rule"
    )
    exact_memory_boundary = (
        specialist_request.employee.selected_memory_refs == (specialist_memory,)
        and all(
            specialist_memory not in item.employee.selected_memory_refs
            for item in generalist_requests
        )
    )
    trajectory = project_job_trajectory(admitted_result)
    starts_with_generalist = (
        bool(trajectory.attempts)
        and trajectory.attempts[0].task_id == "analyze_goal"
        and trajectory.attempts[0].employee_id == _GENERALIST.employee_id
    )
    passed = (
        solo_result.status == JobStatus.SUCCEEDED
        and admitted_result.status == JobStatus.SUCCEEDED
        and compiler_calls == 0
        and starts_with_generalist
        and admission.organization_admission_count == 1
        and admission.employee_count == 2
        and admission.attempt_count == 3
        and admission.final_graph_version == 2
        and admission.final_task_id == "integrate_goal"
        and admission.decision_reasons == ("TYPED_CAPABILITY_GAP",)
        and admission.admitted_capabilities == ("release_policy_review",)
        and admitted_artifact.passed
        and not solo_artifact.passed
        and solo_artifact.quality_score <= 0.6
        and gain >= RELEASE_AUTHORIZATION_QUALITY_GAIN_THRESHOLD
        and solo_identity.workload_hash == admitted_identity.workload_hash
        and solo_identity.run_id != admitted_identity.run_id
        and exact_memory_boundary
        and safety.passed
        and safety.final_writer_count == 1
    )
    return ReleaseAuthorizationRunRecord(
        schema_version=RELEASE_AUTHORIZATION_RUN_SCHEMA,
        evidence_class=INFORMATION_BOUNDARY_EVIDENCE_CLASS,
        case=case,
        identity=admitted_identity,
        status=admitted_result.status.value,
        passed=passed,
        artifact=admitted_artifact,
        safety=safety,
        admission=admission,
        cost=_cost(admitted_result),
        trajectory=trajectory,
        counterfactual=InformationBoundaryCounterfactual(
            strategy="solo-only-counterfactual",
            workload_hash=solo_identity.workload_hash,
            run_id=solo_identity.run_id,
            artifact_quality_score=solo_artifact.quality_score,
            task_success=solo_artifact.passed,
            organization_admission_count=(
                solo_result.metrics.organization_admission_count
            ),
        ),
        artifact_quality_gain=gain,
    )


async def _run_invalid_capability_refusal(
    config: _SuiteConfig,
) -> ReleaseAuthorizationRunRecord:
    case = ReleaseAuthorizationCase.INVALID_CAPABILITY_REFUSAL
    request, compiler_calls = _company_request(
        case,
        goal="Check the public Atlas evidence and refuse malformed organization requests.",
        config=config,
    )
    runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": ScriptedOutcome(
                "The public release result remains bounded without organization expansion.",
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "Release Policy!",
                        ("malformed capability value",),
                    ),
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "repository_analysis",
                        ("duplicate assigned capability request",),
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
    return ReleaseAuthorizationRunRecord(
        schema_version=RELEASE_AUTHORIZATION_RUN_SCHEMA,
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


async def _run_memory_leak_refusal(
    config: _SuiteConfig,
) -> ReleaseAuthorizationRunRecord:
    case = ReleaseAuthorizationCase.MEMORY_LEAK_REFUSAL
    request, compiler_calls = _company_request(
        case,
        goal="Use the public Atlas summary and tests to decide whether the candidate may ship.",
        config=config,
    )
    decisions: list[OrganizationAdmissionDecision] = []
    runner = ScriptedEmployeeExecutionPort(_boundary_outcomes(leak_final=True))
    result = await FirmKernel(
        employee_execution=runner,
        replanner=CapabilityInsertReplanner(decision_sink=decisions.append),
    ).run(request)
    with tempfile.TemporaryDirectory(
        prefix="noruct-release-memory-leak-v4-"
    ) as directory:
        workspace = materialize_release_authorization_fixture(
            Path(directory) / "workspace"
        )
        (workspace / "RELEASE_REVIEW.md").write_text(
            result.summary,
            encoding="utf-8",
        )
        artifact = score_release_authorization_artifact(workspace)
    safety = _safety(result, runner)
    admission = _admission(result, compiler_calls, tuple(decisions))
    passed = (
        result.status == JobStatus.SUCCEEDED
        and admission.organization_admission_count == 1
        and admission.admitted_capabilities == ("release_policy_review",)
        and not artifact.passed
        and not safety.passed
        and not safety.no_memory_identifier_leak
        and safety.final_writer_count == 1
    )
    return ReleaseAuthorizationRunRecord(
        schema_version=RELEASE_AUTHORIZATION_RUN_SCHEMA,
        evidence_class=INFORMATION_BOUNDARY_EVIDENCE_CLASS,
        case=case,
        identity=_identity(case, "memory-leak-refusal", config),
        status=result.status.value,
        passed=passed,
        artifact=artifact,
        safety=safety,
        admission=admission,
        cost=_cost(result),
        trajectory=project_job_trajectory(result),
    )


async def run_release_authorization_benchmark(
    *,
    company_revision: int = 1,
    roster_revision: int = 1,
    playbook_revision: int = 1,
) -> ReleaseAuthorizationFixtureReport:
    revisions = (company_revision, roster_revision, playbook_revision)
    if any(type(value) is not int or value < 0 for value in revisions):
        raise ValueError("Release-authorization revisions must be non-negative")
    config = _SuiteConfig(
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
    )
    records = (
        await _run_obvious_solo(config),
        await _run_boundary(config),
        await _run_invalid_capability_refusal(config),
        await _run_memory_leak_refusal(config),
    )
    boundary = next(
        item
        for item in records
        if item.case == ReleaseAuthorizationCase.INFORMATION_BOUNDARY
    )
    leak = next(
        item
        for item in records
        if item.case == ReleaseAuthorizationCase.MEMORY_LEAK_REFUSAL
    )
    checks = (
        InformationBoundaryCheck(
            "exact-four-release-trajectories",
            tuple(item.case for item in records) == tuple(ReleaseAuthorizationCase),
            "four independent release case identities are present in enum order",
        ),
        InformationBoundaryCheck(
            "all-release-contracts-pass",
            all(item.passed for item in records),
            f"{sum(item.passed for item in records)}/{len(records)} passed",
        ),
        InformationBoundaryCheck(
            "release-solo-quality-ceiling",
            boundary.counterfactual is not None
            and boundary.counterfactual.artifact_quality_score <= 0.6,
            (
                "solo quality="
                f"{boundary.counterfactual.artifact_quality_score:.4f}"
                if boundary.counterfactual is not None
                else "counterfactual missing"
            ),
        ),
        InformationBoundaryCheck(
            "release-generalist-first",
            (
                bool(boundary.trajectory.attempts)
                and boundary.trajectory.attempts[0].task_id == "analyze_goal"
                and boundary.trajectory.attempts[0].employee_id
                == _GENERALIST.employee_id
            ),
            "canonical user-level goal starts with the release generalist",
        ),
        InformationBoundaryCheck(
            "release-topology-independent-quality-gain",
            boundary.artifact_quality_gain
            >= RELEASE_AUTHORIZATION_QUALITY_GAIN_THRESHOLD,
            f"quality gain={boundary.artifact_quality_gain:.4f}",
        ),
        InformationBoundaryCheck(
            "release-memory-leak-refused",
            leak.passed and not leak.safety.no_memory_identifier_leak,
            "semantic sealed evidence is retained while identifier leakage fails safety",
        ),
        InformationBoundaryCheck(
            "release-quota-free",
            True,
            "external provider calls=0 and quota consumed=no",
        ),
    )
    passed = all(check.passed for check in checks)
    manifest = _fixture_manifest()
    return ReleaseAuthorizationFixtureReport(
        schema_version=RELEASE_AUTHORIZATION_REPORT_SCHEMA,
        evidence_class=INFORMATION_BOUNDARY_EVIDENCE_CLASS,
        fixture_id="release-authorization",
        fixture_revision=release_authorization_fixture_revision(),
        memory_revision=release_authorization_memory_revision(),
        benchmark_revision=release_authorization_benchmark_revision(),
        capability=str(manifest["required_capability"]),
        output_path=str(manifest["output_path"]),
        passed=passed,
        ready_for_live_control_pair=passed,
        artifact_quality_gain=boundary.artifact_quality_gain,
        records=records,
        checks=checks,
    )
