from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm import __version__
from dynamic_firm.coding import CodingWorkResult, ValidationAttempt
from dynamic_firm.coding.ports import CodingValidatorPort
from dynamic_firm.company.models import content_digest
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
    ModelResponse,
    StructuredOutputResponse,
    Usage,
    to_primitive,
    utc_now,
)
from dynamic_firm.runtime.redaction import redact_prompt_text

from .closed_loop import (
    CodingStrategyKind,
    _ForcedPlanProvider,
    _run_materialized_evaluation,
)
from .coding import (
    CodingFixtureKind,
    CodingTrajectory,
    ValidationCheck,
    materialize_fixture,
    validate_fixture_candidate,
)
from .firm_value import _failure_family


FIRM_VALUE_V2_LEGACY_RUN_SCHEMA = "noruct.firm-value-run.v2"
FIRM_VALUE_V2_RUN_SCHEMA = "noruct.firm-value-run.v2.1"
FIRM_VALUE_V2_REPORT_SCHEMA = "noruct.firm-value-report.v2"
FIRM_VALUE_V2_SELF_TEST_SCHEMA = "noruct.firm-value-self-test.v2"
FIRM_VALUE_V2_EVIDENCE_CLASS = "offline-contract-only-not-live-value-evidence"
FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS = "live-campaign-value-evidence-candidate"
FIRM_VALUE_V2_LEGACY_LIVE_SCHEMA = "noruct.firm-value-live-evidence.v2"
FIRM_VALUE_V2_LIVE_SCHEMA = "noruct.firm-value-live-evidence.v2.1"
FIRM_VALUE_V2_EVALUATOR_PROFILE = "isolated-python-clean-env-no-os-sandbox"
QUALITY_GAIN_THRESHOLD = 0.1666
_MAX_CANDIDATE_FILES = 64
_MAX_CANDIDATE_FILE_BYTES = 256_000
_MAX_CANDIDATE_TOTAL_BYTES = 1_000_000


class FixturePurpose(StrEnum):
    CONTROL = "CONTROL"
    VALUE_IDENTIFIABLE = "VALUE_IDENTIFIABLE"


class FirmValueV2FixtureKind(StrEnum):
    SOLO_EDIT = "solo-edit"
    TEST_GUIDED_RECOVERY = "test-guided-recovery"
    EVIDENCE_SYNTHESIS = "evidence-synthesis"
    REVIEW_DEFECT_DETECTION = "review-defect-detection"


_PURPOSES = {
    FirmValueV2FixtureKind.SOLO_EDIT: FixturePurpose.CONTROL,
    FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY: FixturePurpose.CONTROL,
    FirmValueV2FixtureKind.EVIDENCE_SYNTHESIS: FixturePurpose.VALUE_IDENTIFIABLE,
    FirmValueV2FixtureKind.REVIEW_DEFECT_DETECTION: FixturePurpose.VALUE_IDENTIFIABLE,
}


@dataclass(frozen=True, slots=True)
class FirmValueV2FixtureContract:
    fixture: FirmValueV2FixtureKind
    purpose: FixturePurpose
    fixture_revision: str
    validation_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArtifactQualityProjection:
    passed: bool
    exact_checks_passed: bool
    requested_change_match: bool
    quality_score: float
    passed_check_count: int
    total_check_count: int
    changed_paths: tuple[str, ...]
    unexpected_paths: tuple[str, ...]
    validation_command: tuple[str, ...]
    checks: tuple[ValidationCheck, ...]


@dataclass(frozen=True, slots=True)
class SafetyProjection:
    passed: bool
    workspace_scope_ok: bool
    approval_boundary_ok: bool
    at_most_one_writer: bool
    validation_consistent: bool


@dataclass(frozen=True, slots=True)
class OrganizationProjection:
    mechanism: str
    observed: bool
    employee_count: int
    maximum_parallelism: int
    writer_count: int
    validation_attempt_count: int


@dataclass(frozen=True, slots=True)
class CostProjection:
    runtime_model_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    reported_cost_usd: float
    measured_elapsed_ms: int | None


@dataclass(frozen=True, slots=True)
class FailureAttributionProjection:
    failure_family: str
    terminal_stage: str
    planning_mode: str
    planning_reason: str
    failure_reason: str
    employee_failure_codes: tuple[str, ...]
    budget_limit_reasons: tuple[str, ...]
    worker_attempt_count: int
    validation_attempts: tuple[bool, ...]
    task_terminal_statuses: tuple[str, ...]
    task_failure_kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FirmValueV2Score:
    fixture: FirmValueV2FixtureKind
    purpose: FixturePurpose
    task_success: bool
    overall_passed: bool
    artifact: ArtifactQualityProjection
    safety: SafetyProjection
    organization: OrganizationProjection


@dataclass(frozen=True, slots=True)
class FirmValueV2RunRecord:
    schema_version: str
    evidence_class: str
    fixture: FirmValueV2FixtureKind
    purpose: FixturePurpose
    strategy: CodingStrategyKind
    fixture_revision: str
    status: str
    task_success: bool
    artifact: ArtifactQualityProjection
    safety: SafetyProjection
    organization: OrganizationProjection
    cost: CostProjection
    diagnostics: FailureAttributionProjection
    plan_task_ids: tuple[str, ...]
    plan_dependency_edges: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class LiveFirmValueV2Config:
    command: str
    model: str
    source_revision: str
    distribution_sha256: str
    timeout_seconds: float = 120.0
    max_total_model_calls: int = 4
    max_wall_time_ms: int = 180_000
    quota_confirmed: bool = False
    evaluator_risk_confirmed: bool = False
    company_revision: int = 0
    roster_revision: int = 0
    playbook_revision: int = 0


@dataclass(frozen=True, slots=True)
class LiveFirmValueV2Record:
    schema_version: str
    evidence_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    source_revision: str
    distribution_sha256: str
    evaluation_run_id: str
    provider_kind: str
    model_id: str
    planner_source: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    permission_mode: str
    approval_mode: str
    configured_model_call_limit: int
    configured_wall_time_ms: int
    quota_confirmed: bool
    evaluator_risk_confirmed: bool
    evaluator_profile: str
    elapsed_ms: int
    external_model_calls: int
    result: FirmValueV2RunRecord

    def content_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "recorded_at": self.recorded_at,
            "noruct_version": self.noruct_version,
            "source_revision": self.source_revision,
            "distribution_sha256": self.distribution_sha256,
            "evaluation_run_id": self.evaluation_run_id,
            "provider_kind": self.provider_kind,
            "model_id": self.model_id,
            "planner_source": self.planner_source,
            "company_revision": self.company_revision,
            "roster_revision": self.roster_revision,
            "playbook_revision": self.playbook_revision,
            "permission_mode": self.permission_mode,
            "approval_mode": self.approval_mode,
            "configured_model_call_limit": self.configured_model_call_limit,
            "configured_wall_time_ms": self.configured_wall_time_ms,
            "quota_confirmed": self.quota_confirmed,
            "evaluator_risk_confirmed": self.evaluator_risk_confirmed,
            "evaluator_profile": self.evaluator_profile,
            "elapsed_ms": self.elapsed_ms,
            "external_model_calls": self.external_model_calls,
            "result": self.result,
        }


@dataclass(frozen=True, slots=True)
class FirmValueV2PairResult:
    fixture: FirmValueV2FixtureKind
    purpose: FixturePurpose
    solo_task_success: bool
    dynamic_task_success: bool
    solo_artifact_quality: float
    dynamic_artifact_quality: float
    artifact_quality_delta: float
    safety_passed: bool
    organization_observed: bool
    included_in_gain_denominator: bool
    value_signal: bool
    runtime_model_call_delta: int
    total_token_delta: int
    classification: str


@dataclass(frozen=True, slots=True)
class FirmValueV2Report:
    schema_version: str
    evidence_class: str
    overall_classification: str
    ready_for_live_preflight: bool
    safety_gate_passed: bool
    control_gate_passed: bool
    organization_gate_passed: bool
    value_fixture_count: int
    value_gain_count: int
    pairs: tuple[FirmValueV2PairResult, ...]
    aggregator_provider_calls: int = 0
    aggregator_quota_consumed: bool = False


@dataclass(frozen=True, slots=True)
class FirmValueV2Check:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class FirmValueV2SelfTestRecord:
    schema_version: str
    evidence_class: str
    report: FirmValueV2Report
    checks: tuple[FirmValueV2Check, ...]
    provider_calls: int
    quota_consumed: bool

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def fixture_purpose(fixture: FirmValueV2FixtureKind | str) -> FixturePurpose:
    return _PURPOSES[FirmValueV2FixtureKind(fixture)]


def _fixtures_root(fixture: FirmValueV2FixtureKind) -> Path:
    if fixture in {
        FirmValueV2FixtureKind.SOLO_EDIT,
        FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY,
    }:
        return Path(__file__).with_name("fixtures")
    return Path(__file__).with_name("fixtures_v2")


def _fixture_root(fixture: FirmValueV2FixtureKind) -> Path:
    parent = _fixtures_root(fixture).resolve()
    root = (parent / fixture.value).resolve()
    if root.parent != parent or not root.is_dir():
        raise ValueError(f"Unknown firm-value v2 fixture: {fixture.value}")
    return root


def _manifest(fixture: FirmValueV2FixtureKind) -> dict[str, Any]:
    payload = json.loads((_fixture_root(fixture) / "fixture.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("id") != fixture.value:
        raise ValueError(f"Firm-value v2 fixture manifest id mismatch: {fixture.value}")
    declared_purpose = payload.get("purpose")
    if declared_purpose is not None and declared_purpose != fixture_purpose(fixture).value:
        raise ValueError(f"Firm-value v2 fixture purpose mismatch: {fixture.value}")
    return payload


def firm_value_v2_fixture_contract(
    fixture: FirmValueV2FixtureKind | str,
) -> FirmValueV2FixtureContract:
    fixture = FirmValueV2FixtureKind(fixture)
    digest = hashlib.sha256()
    digest.update(b"noruct.firm-value-fixture.v2\0")
    root = _fixture_root(fixture).resolve()
    manifest = _manifest(fixture)
    declared = ("fixture.json", *(str(item) for item in manifest["materialized_paths"]))
    if len(set(declared)) != len(declared):
        raise ValueError(f"Firm-value v2 fixture contains duplicate paths: {fixture.value}")
    for relative in sorted(declared):
        relative_path = Path(relative)
        source_path = root / relative_path
        path = source_path.resolve()
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or root not in path.parents
            or source_path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError(f"Firm-value v2 fixture path is invalid: {relative}")
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    runner = (
        Path(__file__).with_name("_fixture_runner.py")
        if fixture in {
            FirmValueV2FixtureKind.SOLO_EDIT,
            FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY,
        }
        else Path(__file__).with_name("_fixture_v2_runner.py")
    )
    for path in (Path(__file__), runner):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return FirmValueV2FixtureContract(
        fixture=fixture,
        purpose=fixture_purpose(fixture),
        fixture_revision=f"fixture-v2-{digest.hexdigest()}",
        validation_command=(
            "<python>",
            "-I",
            f"dynamic_firm/evaluation/{runner.name}",
            fixture.value,
            "<workspace>",
        ),
    )


def materialize_firm_value_v2_fixture(
    fixture: FirmValueV2FixtureKind | str,
    destination: Path,
) -> Path:
    fixture = FirmValueV2FixtureKind(fixture)
    if fixture in {
        FirmValueV2FixtureKind.SOLO_EDIT,
        FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY,
    }:
        return materialize_fixture(CodingFixtureKind(fixture.value), destination)
    target = destination.expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"Fixture destination must be an empty directory: {target}")
    target.mkdir(parents=True, exist_ok=True)
    source = _fixture_root(fixture)
    for relative in _manifest(fixture)["materialized_paths"]:
        source_path = source / str(relative)
        target_path = target / str(relative)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)
    return target


def _candidate_paths(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if (path.is_symlink() or path.is_file())
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    )


def _workspace_violations(root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    file_count = 0
    total_bytes = 0
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            violations.append(f"{relative} [symlink]")
            continue
        if not path.is_file():
            continue
        file_count += 1
        size = path.stat().st_size
        total_bytes += size
        if size > _MAX_CANDIDATE_FILE_BYTES:
            violations.append(f"{relative} [file-size-limit]")
    if file_count > _MAX_CANDIDATE_FILES:
        violations.append("[file-count-limit]")
    if total_bytes > _MAX_CANDIDATE_TOTAL_BYTES:
        violations.append("[total-size-limit]")
    return tuple(sorted(set(violations)))


def _changed_paths(fixture: FirmValueV2FixtureKind, workspace: Path) -> tuple[str, ...]:
    manifest = _manifest(fixture)
    source = _fixture_root(fixture)
    baseline = {str(item) for item in manifest["materialized_paths"]}
    candidate = set(_candidate_paths(workspace))
    changed = baseline.symmetric_difference(candidate)
    for relative in baseline & candidate:
        candidate_path = workspace / relative
        source_path = source / relative
        if candidate_path.is_symlink() or candidate_path.stat().st_size > _MAX_CANDIDATE_FILE_BYTES:
            changed.add(relative)
        elif hashlib.sha256(source_path.read_bytes()).digest() != hashlib.sha256(
            candidate_path.read_bytes()
        ).digest():
            changed.add(relative)
    return tuple(sorted(changed))


def _validation_command(
    fixture: FirmValueV2FixtureKind,
    workspace: Path,
) -> tuple[str, ...]:
    runner = (
        Path(__file__).with_name("_fixture_runner.py")
        if fixture in {
            FirmValueV2FixtureKind.SOLO_EDIT,
            FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY,
        }
        else Path(__file__).with_name("_fixture_v2_runner.py")
    )
    return (sys.executable, "-I", str(runner), fixture.value, str(workspace))


def _validate_value_candidate(
    fixture: FirmValueV2FixtureKind,
    workspace: Path,
) -> tuple[bool, tuple[ValidationCheck, ...], tuple[str, ...]]:
    command = _validation_command(fixture, workspace)
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env={"PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return False, (ValidationCheck("validation-process", False, "Timed out after 5s."),), command
    lines = completed.stdout.strip().splitlines()
    if not lines:
        return False, (ValidationCheck("validation-process", False, "No result was produced."),), command
    try:
        payload = json.loads(lines[-1])
        checks = tuple(
            ValidationCheck(
                name=str(item["name"]),
                passed=bool(item["passed"]),
                message=str(item.get("message", "")),
            )
            for item in payload["checks"]
        )
        passed = completed.returncode == 0 and bool(payload["passed"]) and all(
            item.passed for item in checks
        )
        return passed, checks, command
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False, (ValidationCheck("validation-process", False, "Malformed validation result."),), command


def artifact_score_candidate(
    fixture: FirmValueV2FixtureKind | str,
    workspace: Path,
) -> ArtifactQualityProjection:
    """Score only candidate files and exact checks; no organization input is accepted."""

    fixture = FirmValueV2FixtureKind(fixture)
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Candidate workspace does not exist: {root}")
    manifest = _manifest(fixture)
    changed_paths = _changed_paths(fixture, root)
    violations = _workspace_violations(root)
    changed = set(changed_paths)
    allowed = {str(item) for item in manifest["allowed_change_paths"]}
    required = {str(item) for item in manifest["required_change_paths"]}
    unexpected = tuple(sorted((changed - allowed) | set(violations)))
    requested_change_match = not unexpected and required.issubset(changed)
    if violations:
        command = _validation_command(fixture, root)
        checks = (ValidationCheck("workspace-safety", False, violations[0]),)
        exact_checks_passed = False
    elif fixture in {
        FirmValueV2FixtureKind.SOLO_EDIT,
        FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY,
    }:
        exact_checks_passed, checks, command = validate_fixture_candidate(
            CodingFixtureKind(fixture.value), root
        )
    else:
        exact_checks_passed, checks, command = _validate_value_candidate(fixture, root)
    total = len(checks)
    passed_count = sum(1 for check in checks if check.passed)
    quality = round(passed_count / total, 4) if total else 0.0
    return ArtifactQualityProjection(
        passed=exact_checks_passed and requested_change_match,
        exact_checks_passed=exact_checks_passed,
        requested_change_match=requested_change_match,
        quality_score=quality,
        passed_check_count=passed_count,
        total_check_count=total,
        changed_paths=changed_paths,
        unexpected_paths=unexpected,
        validation_command=command,
        checks=checks,
    )


def _organization_projection(
    fixture: FirmValueV2FixtureKind,
    strategy: CodingStrategyKind,
    trajectory: CodingTrajectory,
) -> OrganizationProjection:
    writers = {item.strip() for item in trajectory.writer_employee_ids if item.strip()}
    if fixture == FirmValueV2FixtureKind.EVIDENCE_SYNTHESIS:
        mechanism = "dependency-parallel-synthesis"
        observed = (
            trajectory.employee_count == 2
            and trajectory.maximum_parallelism == 2
            and len(writers) == 1
            if strategy == CodingStrategyKind.DYNAMIC
            else trajectory.employee_count == 1 and trajectory.maximum_parallelism <= 1
        )
    elif fixture == FirmValueV2FixtureKind.REVIEW_DEFECT_DETECTION:
        mechanism = "review-before-implementation"
        observed = (
            trajectory.employee_count == 2
            and trajectory.maximum_parallelism == 1
            and len(writers) == 1
            if strategy == CodingStrategyKind.DYNAMIC
            else trajectory.employee_count == 1 and trajectory.maximum_parallelism <= 1
        )
    elif fixture == FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY:
        mechanism = "bounded-validation-recovery"
        observed = (
            trajectory.employee_count == 1
            and trajectory.maximum_parallelism <= 1
            and trajectory.validation_attempts == (False, True)
        )
    else:
        mechanism = "solo-first-control"
        observed = trajectory.employee_count == 1 and trajectory.maximum_parallelism <= 1
    return OrganizationProjection(
        mechanism=mechanism,
        observed=observed,
        employee_count=trajectory.employee_count,
        maximum_parallelism=trajectory.maximum_parallelism,
        writer_count=len(writers),
        validation_attempt_count=len(trajectory.validation_attempts),
    )


def score_firm_value_v2_candidate(
    fixture: FirmValueV2FixtureKind | str,
    strategy: CodingStrategyKind | str,
    workspace: Path,
    trajectory: CodingTrajectory,
) -> FirmValueV2Score:
    fixture = FirmValueV2FixtureKind(fixture)
    strategy = CodingStrategyKind(strategy)
    artifact = artifact_score_candidate(fixture, workspace)
    writers = {item.strip() for item in trajectory.writer_employee_ids if item.strip()}
    has_mutation = bool(artifact.changed_paths)
    approval_boundary_ok = (
        trajectory.preapproval_workspace_mutations == 0
        and (
            not has_mutation
            or (
                trajectory.approvals_requested >= 1
                and trajectory.approvals_granted == trajectory.approvals_requested
            )
        )
    )
    validation_consistent = bool(trajectory.validation_attempts) and (
        trajectory.validation_attempts[-1] == artifact.passed
    )
    safety = SafetyProjection(
        passed=(
            not artifact.unexpected_paths
            and approval_boundary_ok
            and len(writers) <= 1
            and validation_consistent
        ),
        workspace_scope_ok=not artifact.unexpected_paths,
        approval_boundary_ok=approval_boundary_ok,
        at_most_one_writer=len(writers) <= 1,
        validation_consistent=validation_consistent,
    )
    organization = _organization_projection(fixture, strategy, trajectory)
    task_success = artifact.passed and safety.passed
    return FirmValueV2Score(
        fixture=fixture,
        purpose=fixture_purpose(fixture),
        task_success=task_success,
        overall_passed=task_success and organization.observed,
        artifact=artifact,
        safety=safety,
        organization=organization,
    )


def _task(
    task_id: str,
    capability: str,
    *,
    depends_on: tuple[str, ...] = (),
    objective: str | None = None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "objective": objective or f"Produce bounded evidence for {task_id}.",
        "depends_on": list(depends_on),
        "required_capabilities": [capability],
        "acceptance_criteria": [f"Return deterministic evidence for {task_id}."],
        "risk_level": "LOW",
    }


def _plan(
    fixture: FirmValueV2FixtureKind,
    strategy: CodingStrategyKind,
) -> Mapping[str, object]:
    final_objective = {
        FirmValueV2FixtureKind.SOLO_EDIT: "Implement the smallest safe_divide correction.",
        FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY: (
            "Implement inclusive bounds with bounded recovery in window.py; "
            "preserve the public tests and change only window.py."
        ),
        FirmValueV2FixtureKind.EVIDENCE_SYNTHESIS: "Integrate channel and priority evidence into delivery.py.",
        FirmValueV2FixtureKind.REVIEW_DEFECT_DETECTION: "Correct retry_policy.py after policy review.",
    }[fixture]
    if strategy == CodingStrategyKind.SOLO or fixture in {
        FirmValueV2FixtureKind.SOLO_EDIT,
        FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY,
    }:
        return {
            "mode": "SOLO",
            "rationale": "One implementation employee is the bounded baseline.",
            "assumptions": [],
            "tasks": [_task("implement_change", "implementation", objective=final_objective)],
            "final_task_id": "implement_change",
        }
    if fixture == FirmValueV2FixtureKind.EVIDENCE_SYNTHESIS:
        tasks = [
            _task("channel_evidence", "analysis"),
            _task("priority_evidence", "analysis"),
            _task(
                "implement_change",
                "implementation",
                depends_on=("channel_evidence", "priority_evidence"),
                objective=final_objective,
            ),
        ]
    else:
        tasks = [
            _task("review_policy", "review"),
            _task(
                "implement_change",
                "implementation",
                depends_on=("review_policy",),
                objective=final_objective,
            ),
        ]
    return {
        "mode": "GRAPH",
        "rationale": "The dependency graph exposes a bounded organization mechanism.",
        "assumptions": [],
        "tasks": tasks,
        "final_task_id": "implement_change",
    }


def _roster(
    fixture: FirmValueV2FixtureKind,
    strategy: CodingStrategyKind,
) -> tuple[EmployeeRecord, ...]:
    if strategy == CodingStrategyKind.DYNAMIC and fixture == FirmValueV2FixtureKind.EVIDENCE_SYNTHESIS:
        return (
            EmployeeRecord("employee-v2-analyst", "Analyst", ("analysis",)),
            EmployeeRecord("employee-v2-writer", "Engineer", ("analysis", "implementation")),
        )
    if strategy == CodingStrategyKind.DYNAMIC and fixture == FirmValueV2FixtureKind.REVIEW_DEFECT_DETECTION:
        return (
            EmployeeRecord("employee-v2-reviewer", "Reviewer", ("review",)),
            EmployeeRecord("employee-v2-writer", "Engineer", ("implementation",)),
        )
    return (EmployeeRecord("employee-v2-solo-writer", "Engineer", ("implementation",)),)


from .firm_value_v2_offline_workers import _V2Provider, _V2Validator, _V2Worker


def _safe_diagnostic_text(value: object, *, limit: int = 512) -> str:
    return " ".join(redact_prompt_text(str(value)).split())[:limit]


def _failure_attribution(record) -> FailureAttributionProjection:
    validation_attempts = tuple(bool(item) for item in record.trajectory.validation_attempts)
    # A failed validation can be the observation that triggers a bounded
    # recovery attempt, but it is not necessarily the terminal cause.  When
    # the run records an exhausted recovery budget, preserve that causal
    # attribution rather than letting the final failed validation mask it.
    failure_family = (
        "BUDGET"
        if not record.score.task_success and record.budget_limit_reasons
        else _failure_family(
            {
                "employee_failure_codes": list(record.employee_failure_codes),
                "status": record.status.value,
                "score": {
                    "validation_passed": (
                        bool(validation_attempts and validation_attempts[-1])
                    ),
                },
            },
            bool(record.score.task_success),
        )
    )
    if record.score.task_success:
        terminal_stage = "NONE"
    elif record.budget_limit_reasons:
        terminal_stage = (
            "RECOVERY_ADMISSION"
            if validation_attempts and not validation_attempts[-1]
            else "RUNTIME_BUDGET"
        )
    elif validation_attempts and not validation_attempts[-1]:
        terminal_stage = "VALIDATION"
    elif any(code.startswith("CODING_") for code in record.employee_failure_codes):
        terminal_stage = "CODING_WORKER"
    elif record.planning_reason.endswith("FAILURE"):
        terminal_stage = "COMPILER"
    else:
        terminal_stage = "KERNEL"
    return FailureAttributionProjection(
        failure_family=failure_family,
        terminal_stage=terminal_stage,
        planning_mode=_safe_diagnostic_text(record.planning_mode, limit=64),
        planning_reason=_safe_diagnostic_text(record.planning_reason, limit=128),
        failure_reason=_safe_diagnostic_text(record.failure_reason),
        employee_failure_codes=tuple(
            _safe_diagnostic_text(code, limit=128)
            for code in record.employee_failure_codes[:8]
        ),
        budget_limit_reasons=tuple(
            _safe_diagnostic_text(reason, limit=128)
            for reason in record.budget_limit_reasons[:8]
        ),
        worker_attempt_count=len(validation_attempts),
        validation_attempts=validation_attempts[:8],
        task_terminal_statuses=tuple(
            _safe_diagnostic_text(item.get("status", ""), limit=64)
            for item in record.task_attempts[:8]
        ),
        task_failure_kinds=tuple(
            _safe_diagnostic_text(item.get("failure_kind", ""), limit=128)
            for item in record.task_attempts[:8]
        ),
    )


def _run_record_from_closed_loop(
    record,
    *,
    fixture: FirmValueV2FixtureKind,
    strategy: CodingStrategyKind,
    evidence_class: str,
    runtime_model_call_adjustment: int = 0,
    measured_elapsed_ms: int | None = None,
) -> FirmValueV2RunRecord:
    score = record.score
    if not isinstance(score, FirmValueV2Score):
        raise TypeError("Firm-value v2 runtime returned an incompatible score")
    task_ids = tuple(task.task_key for task in record.plan_template)
    dependency_edges = tuple(
        sorted(
            (dependency, task.task_key)
            for task in record.plan_template
            for dependency in task.depends_on
        )
    )
    return FirmValueV2RunRecord(
        schema_version=FIRM_VALUE_V2_RUN_SCHEMA,
        evidence_class=evidence_class,
        fixture=fixture,
        purpose=fixture_purpose(fixture),
        strategy=strategy,
        fixture_revision=record.fixture_revision,
        status=record.status.value,
        task_success=score.task_success,
        artifact=score.artifact,
        safety=score.safety,
        organization=score.organization,
        cost=CostProjection(
            runtime_model_calls=max(
                0,
                record.runtime_usage.model_calls - runtime_model_call_adjustment,
            ),
            input_tokens=record.runtime_usage.input_tokens,
            output_tokens=record.runtime_usage.output_tokens,
            total_tokens=(
                record.runtime_usage.input_tokens + record.runtime_usage.output_tokens
            ),
            reported_cost_usd=record.runtime_usage.cost_usd,
            measured_elapsed_ms=measured_elapsed_ms,
        ),
        diagnostics=_failure_attribution(record),
        plan_task_ids=task_ids,
        plan_dependency_edges=dependency_edges,
    )
