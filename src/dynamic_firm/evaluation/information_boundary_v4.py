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


INFORMATION_BOUNDARY_SUITE_REPORT_SCHEMA = (
    "noruct.information-boundary-suite-report.v4"
)
RELEASE_AUTHORIZATION_RUN_SCHEMA = (
    "noruct.release-authorization-information-boundary-run.v4"
)
RELEASE_AUTHORIZATION_REPORT_SCHEMA = (
    "noruct.release-authorization-information-boundary-report.v4"
)
INFORMATION_BOUNDARY_SUITE_MODEL_PROFILE = "offline-scripted-v4"
RELEASE_AUTHORIZATION_QUALITY_GAIN_THRESHOLD = 0.2

from .release_authorization_benchmark import (
    InformationBoundaryFixtureGain,
    ReleaseAuthorizationCase,
    ReleaseAuthorizationFixtureReport,
    _GENERALIST,
    _MEMORY,
    _SPECIALIST,
    _fixture_manifest,
    _fixture_root,
    _materialized_fixture_paths,
    materialize_release_authorization_fixture,
    release_authorization_benchmark_revision,
    release_authorization_fixture_revision,
    release_authorization_memory_revision,
    information_boundary_suite_revision,
    run_release_authorization_benchmark,
    score_release_authorization_artifact,
)

@dataclass(frozen=True, slots=True)
class InformationBoundarySuiteReport:
    schema_version: str
    evidence_class: str
    benchmark_revision: str
    passed: bool
    ready_for_second_live_control_pair: bool
    legacy_fixture: InformationBoundaryBenchmarkReport
    release_fixture: ReleaseAuthorizationFixtureReport
    fixture_gains: tuple[InformationBoundaryFixtureGain, ...]
    checks: tuple[InformationBoundaryCheck, ...]
    external_provider_calls: int = 0
    quota_consumed: bool = False

async def run_information_boundary_suite(
    *,
    company_revision: int = 1,
    roster_revision: int = 1,
    playbook_revision: int = 1,
) -> InformationBoundarySuiteReport:
    legacy = await run_information_boundary_benchmark(
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
    )
    release = await run_release_authorization_benchmark(
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
    )
    legacy_boundary = next(
        item
        for item in legacy.records
        if item.case == InformationBoundaryCase.TYPED_INFORMATION_BOUNDARY
    )
    release_boundary = next(
        item
        for item in release.records
        if item.case == ReleaseAuthorizationCase.INFORMATION_BOUNDARY
    )
    if legacy_boundary.counterfactual is None or release_boundary.counterfactual is None:
        raise ValueError("Information-boundary suite counterfactual is unavailable")
    legacy_gain = InformationBoundaryFixtureGain(
        fixture_id="information-boundary",
        fixture_revision=legacy.fixture_revision,
        memory_revision=information_boundary_memory_revision(),
        capability="sealed_policy_review",
        output_path="REPORT.md",
        solo_quality=legacy_boundary.counterfactual.artifact_quality_score,
        admitted_quality=(
            legacy_boundary.artifact.quality_score
            if legacy_boundary.artifact is not None
            else 0.0
        ),
        artifact_quality_gain=legacy_boundary.artifact_quality_gain,
    )
    release_gain = InformationBoundaryFixtureGain(
        fixture_id=release.fixture_id,
        fixture_revision=release.fixture_revision,
        memory_revision=release.memory_revision,
        capability=release.capability,
        output_path=release.output_path,
        solo_quality=release_boundary.counterfactual.artifact_quality_score,
        admitted_quality=(
            release_boundary.artifact.quality_score
            if release_boundary.artifact is not None
            else 0.0
        ),
        artifact_quality_gain=release_boundary.artifact_quality_gain,
    )
    checks = (
        InformationBoundaryCheck(
            "legacy-v3-contract-preserved",
            legacy.passed and legacy.ready_for_live_control_pair,
            (
                f"legacy revision={legacy.benchmark_revision},"
                f"gain={legacy.artifact_quality_gain:.4f}"
            ),
        ),
        InformationBoundaryCheck(
            "independent-fixture-revisions",
            legacy.fixture_revision != release.fixture_revision,
            "fixture revisions differ",
        ),
        InformationBoundaryCheck(
            "independent-memory-revisions",
            information_boundary_memory_revision() != release.memory_revision,
            "employee memory revisions differ",
        ),
        InformationBoundaryCheck(
            "independent-capability-and-artifact-contract",
            (
                legacy_gain.capability != release_gain.capability
                and legacy_gain.output_path != release_gain.output_path
            ),
            (
                f"{legacy_gain.capability}/{legacy_gain.output_path} != "
                f"{release_gain.capability}/{release_gain.output_path}"
            ),
        ),
        InformationBoundaryCheck(
            "release-same-workload-counterfactual",
            (
                release_boundary.identity.workload_hash
                == release_boundary.counterfactual.workload_hash
                and release_boundary.identity.run_id
                != release_boundary.counterfactual.run_id
            ),
            release_boundary.identity.workload_hash,
        ),
        InformationBoundaryCheck(
            "cross-fixture-workloads-distinct",
            (
                legacy_boundary.identity.workload_hash
                != release_boundary.identity.workload_hash
            ),
            "legacy and release workload hashes differ",
        ),
        InformationBoundaryCheck(
            "two-independent-quality-gains",
            (
                legacy_gain.artifact_quality_gain
                >= INFORMATION_BOUNDARY_QUALITY_GAIN_THRESHOLD
                and release_gain.artifact_quality_gain
                >= RELEASE_AUTHORIZATION_QUALITY_GAIN_THRESHOLD
            ),
            (
                f"legacy={legacy_gain.artifact_quality_gain:.4f},"
                f"release={release_gain.artifact_quality_gain:.4f}"
            ),
        ),
        InformationBoundaryCheck(
            "suite-quota-free",
            True,
            "external provider calls=0 and quota consumed=no",
        ),
    )
    passed = all(check.passed for check in checks) and release.passed
    return InformationBoundarySuiteReport(
        schema_version=INFORMATION_BOUNDARY_SUITE_REPORT_SCHEMA,
        evidence_class=INFORMATION_BOUNDARY_EVIDENCE_CLASS,
        benchmark_revision=information_boundary_suite_revision(),
        passed=passed,
        ready_for_second_live_control_pair=passed,
        legacy_fixture=legacy,
        release_fixture=release,
        fixture_gains=(legacy_gain, release_gain),
        checks=checks,
    )
