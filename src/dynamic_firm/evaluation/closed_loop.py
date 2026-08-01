from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from dynamic_firm import __version__
from dynamic_firm.company.models import WorkflowTaskTemplate, content_digest
from dynamic_firm.company.frontdoor import (
    AuthoritySnapshotIdentity,
    WorkOrder,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
)
from dynamic_firm.company.manager import (
    ManagerAssignment,
    ManagerDelegation,
    PersistentExecutiveManager,
)
from dynamic_firm.company.operating import (
    CompanyWorkMode,
    InitialCoordinationPolicy,
    OperatingReason,
    RequestedEffect,
    classify_company_input,
)
from dynamic_firm.coding import (
    APPLY_CHANGE_SET_TOOL,
    ChangeSetCatalog,
    CodingWorkResult,
    RoutedEmployeeExecutionService,
    ShadowCodingEmployeeRuntimeService,
    ShadowWorkspaceService,
    ValidationAttempt,
)
from dynamic_firm.coding.ports import CodingValidatorPort, CodingWorkerPort
from dynamic_firm.compiler import (
    CompilerExecutionProfile,
    CompilerRequest,
    DynamicWorkflowCompiler,
    ManagerOutcomeSummary,
    ManagerPlanningBrief,
    PlanningOwner,
)
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobLimits,
    JobStatus,
)
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.supervision import ManagerSupervisionPort
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ApprovalDecision,
    CompletionEnvelope,
    ContextBundle,
    EventType,
    ModelResponse,
    RunLimits,
    StructuredOutputResponse,
    ToolEffect,
    ToolGrant,
    Usage,
    to_primitive,
    utc_now,
)
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.job_ledger import ActiveJobInspector, SQLiteActiveJobLedger
from dynamic_firm.runtime.redaction import redact_prompt_text
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry, WorkspaceReadTools

from .coding import (
    CodingEvaluationRecord,
    CodingFixtureKind,
    CodingTrajectory,
    coding_fixture_contract,
    materialize_fixture,
    score_candidate,
    validate_fixture_candidate,
)


_WORKSPACE_ID = "noruct-evaluation-workspace"
_LIVE_RUN_KINDS = frozenset({"live", "live-v2"})
_TERMINAL_EVENTS = {
    EventType.RUN_SUCCEEDED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
    EventType.RUN_BUDGET_EXHAUSTED,
}


class CodingStrategyKind(StrEnum):
    SOLO = "solo"
    FIXED = "fixed"
    DYNAMIC = "dynamic"


@dataclass(frozen=True, slots=True)
class ClosedLoopCodingRecord:
    fixture: StrEnum
    strategy: CodingStrategyKind
    status: JobStatus
    planning_mode: str
    planning_reason: str
    planning_owner_id: str
    planning_owner_assignment_digest: str
    manager_planning_brief_digest: str
    failure_reason: str
    employee_failure_codes: tuple[str, ...]
    budget_limit_reasons: tuple[str, ...]
    trajectory_source: str
    ledger_run_count: int
    ledger_event_count: int
    ledger_matches_kernel: bool
    workspace_unchanged_before_approval: bool
    compiler_model_calls: int
    runtime_usage: Usage
    trajectory: CodingTrajectory
    score: object
    plan_template: tuple[WorkflowTaskTemplate, ...]
    compiler_plan_template: tuple[WorkflowTaskTemplate, ...]
    fixture_revision: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    permission_mode: str
    approval_mode: str
    configured_model_call_limit: int
    configured_wall_time_ms: int
    distribution_sha256: str
    active_job_audit_status: str
    # A replica is a Job-local execution instance of one persistent Employee,
    # not a second ROSTER identity.  Preserve this separately from trajectory
    # employee_count so evaluations do not accidentally score clone fan-out as
    # a heterogeneous staffing claim.
    execution_replica_count: int
    replica_group_count: int
    task_attempts: tuple[Mapping[str, object], ...]
    task_mutations: tuple[Mapping[str, object], ...]
    # These counts come from the same append-only Runtime/Job stores as the
    # rest of the trajectory.  They let organization benchmarks compare
    # recovery and intervention costs without retaining prompts, arguments,
    # resource names, or tool output.
    runtime_user_intervention_count: int
    external_effect_error_count: int
    external_effect_unknown_count: int


@dataclass(frozen=True, slots=True)
class LiveCodingEvaluationConfig:
    command: str
    model: str | None = None
    timeout_seconds: float = 120.0
    source_revision: str = "uncommitted-or-unknown"
    max_total_model_calls: int = 4
    max_wall_time_ms: int = 180_000
    quota_confirmed: bool = False
    company_revision: int = 0
    roster_revision: int = 0
    playbook_revision: int = 0
    distribution_sha256: str = ""


@dataclass(frozen=True, slots=True)
class LiveCodingEvaluationRecord:
    schema_version: str
    evidence_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    source_revision: str
    evaluation_run_id: str
    provider_kind: str
    model_id: str
    planner_source: str
    validation_observation_scope: str
    subscription_cost_usd: float | None
    quota_confirmed: bool
    elapsed_ms: int
    external_model_calls: int
    result: ClosedLoopCodingRecord


@dataclass(frozen=True, slots=True)
class LiveCodingPreflightCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class LiveCodingPreflightRecord:
    schema_version: str
    recorded_at: str
    noruct_version: str
    evidence_class: str
    source_revision: str
    provider_kind: str
    model_id: str
    executable: str | None
    max_total_model_calls: int
    max_wall_time_ms: int
    quota_consumed: bool
    external_model_calls: int
    subscription_cost_usd: float | None
    ready: bool
    checks: tuple[LiveCodingPreflightCheck, ...]
    offline_rehearsal: ClosedLoopCodingRecord


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


def _plan(fixture: CodingFixtureKind, strategy: CodingStrategyKind) -> Mapping[str, object]:
    final_objective = {
        CodingFixtureKind.SOLO_EDIT: "Implement the smallest safe_divide correction.",
        CodingFixtureKind.PARALLEL_EVIDENCE: "Integrate specification and test evidence into one identifier implementation.",
        CodingFixtureKind.TEST_GUIDED_RECOVERY: "Implement inclusive bounds with one bounded validation recovery.",
    }[fixture]
    if strategy == CodingStrategyKind.SOLO:
        return {
            "mode": "SOLO",
            "rationale": "Counterfactual single-employee baseline.",
            "assumptions": [],
            "tasks": [_task("implement_change", "implementation", objective=final_objective)],
            "final_task_id": "implement_change",
        }
    if strategy == CodingStrategyKind.FIXED:
        return {
            "mode": "GRAPH",
            "rationale": "Counterfactual fixed researcher-reviewer-writer workflow.",
            "assumptions": [],
            "tasks": [
                _task("research", "research"),
                _task("review", "review", depends_on=("research",)),
                _task(
                    "implement_change",
                    "implementation",
                    depends_on=("review",),
                    objective=final_objective,
                ),
            ],
            "final_task_id": "implement_change",
        }
    if fixture == CodingFixtureKind.PARALLEL_EVIDENCE:
        return {
            "mode": "GRAPH",
            "rationale": "Independent specification and test evidence can be gathered in parallel.",
            "assumptions": [],
            "tasks": [
                _task("spec_evidence", "analysis"),
                _task("test_evidence", "analysis"),
                _task(
                    "implement_change",
                    "implementation",
                    depends_on=("spec_evidence", "test_evidence"),
                    objective=final_objective,
                ),
            ],
            "final_task_id": "implement_change",
        }
    return {
        "mode": "SOLO",
        "rationale": "One coding employee is sufficient for this bounded change.",
        "assumptions": [],
        "tasks": [_task("implement_change", "implementation", objective=final_objective)],
        "final_task_id": "implement_change",
    }


def _roster(strategy: CodingStrategyKind, fixture: CodingFixtureKind) -> tuple[EmployeeRecord, ...]:
    if strategy == CodingStrategyKind.FIXED:
        return (
            EmployeeRecord("employee-fixed-researcher", "Researcher", ("research",)),
            EmployeeRecord("employee-fixed-reviewer", "Reviewer", ("review",)),
            EmployeeRecord("employee-fixed-writer", "Engineer", ("implementation",)),
        )
    if strategy == CodingStrategyKind.DYNAMIC and fixture == CodingFixtureKind.PARALLEL_EVIDENCE:
        return (
            EmployeeRecord("employee-dynamic-analyst", "Analyst", ("analysis",)),
            EmployeeRecord(
                "employee-dynamic-writer",
                "Engineer",
                ("analysis", "implementation"),
            ),
        )
    prefix = "dynamic" if strategy == CodingStrategyKind.DYNAMIC else "solo"
    return (
        EmployeeRecord(
            f"employee-{prefix}-writer",
            "Engineer",
            ("implementation",),
        ),
    )


class _OfflineProvider:
    def __init__(self, plan: Mapping[str, object]) -> None:
        self.plan = plan

    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        return StructuredOutputResponse(
            value=self.plan,
            usage=Usage(input_tokens=11, output_tokens=7),
            provider_request_id="offline-compiler",
        )

    async def complete(self, request, cancellation):
        cancellation.raise_if_cancelled()
        await asyncio.sleep(0)
        return ModelResponse(
            completion=CompletionEnvelope(
                summary="Deterministic dependency evidence prepared.",
                acceptance_evidence=("offline:evidence",),
            ),
            usage=Usage(model_calls=1, input_tokens=5, output_tokens=3),
            provider_request_id="offline-employee",
        )


class _ForcedPlanProvider:
    """Validate a counterfactual plan without spending a planner model call."""

    def __init__(self, provider, plan: Mapping[str, object]) -> None:
        self.provider = provider
        self.plan = plan

    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        return StructuredOutputResponse(
            value=self.plan,
            provider_request_id="bounded-counterfactual-plan",
        )

    async def complete(self, request, cancellation):
        return await self.provider.complete(request, cancellation)


_SAFE_FIXTURE_EXPECTATIONS = {
    CodingFixtureKind.TEST_GUIDED_RECOVERY: {
        "reversed-bounds": "expect:raise-ValueError-when-lower-greater-than-upper",
    },
}


def _validation_detail(fixture: CodingFixtureKind, checks) -> str:
    failed = [check.name for check in checks if not check.passed]
    if not failed:
        return "passed"
    expectations = _SAFE_FIXTURE_EXPECTATIONS.get(fixture, {})
    hints = tuple(expectations[name] for name in failed if name in expectations)
    return " ".join(("failed:" + ",".join(failed), *hints))


class _FixtureCodingWorker:
    def __init__(self, fixture: CodingFixtureKind) -> None:
        self.fixture = fixture

    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        if self.fixture == CodingFixtureKind.SOLO_EDIT:
            (request.workspace / "calculator.py").write_text(
                "def safe_divide(numerator: float, denominator: float) -> float | None:\n"
                "    if denominator == 0:\n"
                "        return None\n"
                "    return numerator / denominator\n",
                encoding="utf-8",
            )
        elif self.fixture == CodingFixtureKind.PARALLEL_EVIDENCE:
            (request.workspace / "identifier.py").write_text(
                "import re\n\n"
                "def canonical_identifier(value: str) -> str:\n"
                "    return re.sub(r'[\\s_]+', '-', value.strip().lower())\n",
                encoding="utf-8",
            )
        else:
            target = request.workspace / "window.py"
            if request.validation_feedback:
                target.write_text(
                    "def within_window(value: int, lower: int, upper: int) -> bool:\n"
                    "    if lower > upper:\n"
                    "        raise ValueError('lower must not exceed upper')\n"
                    "    return lower <= value <= upper\n",
                    encoding="utf-8",
                )
            else:
                target.write_text(
                    "def within_window(value: int, lower: int, upper: int) -> bool:\n"
                    "    return lower <= value <= upper\n",
                    encoding="utf-8",
                )
        return CodingWorkResult(
            summary="Prepared and proposed one bounded shadow candidate.",
            acceptance_evidence=("offline:shadow-change",),
            usage=Usage(model_calls=1, input_tokens=13, output_tokens=8),
            provider_request_id="offline-shadow-worker",
        )


class _FixtureValidator(CodingValidatorPort):
    """Run the evaluator-owned check without disclosing its command to a worker."""

    def __init__(self, fixture: CodingFixtureKind) -> None:
        self.fixture = fixture

    async def validate(self, request, cancellation):
        cancellation.raise_if_cancelled()
        passed, checks, _ = validate_fixture_candidate(self.fixture, request.workspace)
        return ValidationAttempt(
            "noruct-fixture-validation",
            passed,
            _validation_detail(self.fixture, checks),
        )


def _workspace_digest(workspace: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(path.relative_to(workspace).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class _InvariantApproval:
    def __init__(self, workspace: Path, baseline_digest: str) -> None:
        self.workspace = workspace
        self.baseline_digest = baseline_digest
        self.workspace_unchanged = True

    async def request(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.workspace_unchanged = (
            self.workspace_unchanged
            and _workspace_digest(self.workspace) == self.baseline_digest
        )
        return (
            ApprovalDecision.ALLOW_ONCE
            if self.workspace_unchanged
            else ApprovalDecision.DENY
        )


def trajectory_from_ledger(store: RunStore, job_id: str) -> CodingTrajectory:
    runs = store.list_job_runs(job_id)
    events = store.list_job_events(job_id)
    active: set[str] = set()
    maximum_parallelism = 0
    writer_ids: list[str] = []
    approvals_requested = 0
    approvals_granted = 0
    approved_actions: set[str] = set()
    preapproval_mutations = 0
    validation_attempts: list[bool] = []
    for event in events:
        if event.type == EventType.RUN_STARTED:
            active.add(event.run_id)
            maximum_parallelism = max(maximum_parallelism, len(active))
        elif event.type in _TERMINAL_EVENTS:
            active.discard(event.run_id)
        elif event.type == EventType.APPROVAL_REQUIRED:
            approvals_requested += 1
        elif event.type == EventType.APPROVAL_RESOLVED:
            if event.payload.get("decision") in {
                ApprovalDecision.ALLOW_ONCE.value,
                ApprovalDecision.ALLOW_SESSION.value,
            }:
                approvals_granted += 1
                approved_actions.add(str(event.payload.get("action_id", "")))
        elif (
            event.type == EventType.TOOL_STARTED
            and event.payload.get("tool_name") == APPLY_CHANGE_SET_TOOL
        ):
            writer_ids.append(event.employee_id)
            if str(event.payload.get("action_id", "")) not in approved_actions:
                preapproval_mutations += 1
        elif event.type == EventType.VALIDATION_RECORDED:
            passed = event.payload.get("passed")
            if type(passed) is bool:
                validation_attempts.append(passed)
    return CodingTrajectory(
        employee_count=len({str(run["employee_id"]) for run in runs}),
        maximum_parallelism=maximum_parallelism,
        writer_employee_ids=tuple(dict.fromkeys(writer_ids)),
        approvals_requested=approvals_requested,
        approvals_granted=approvals_granted,
        preapproval_workspace_mutations=preapproval_mutations,
        validation_attempts=tuple(validation_attempts),
    )


def _stable_score(record: CodingEvaluationRecord) -> CodingEvaluationRecord:
    return replace(
        record,
        validation_command=coding_fixture_contract(record.fixture).validation_command,
    )


def _failure_reason_with_validation(
    base_reason: str,
    status: JobStatus,
    events,
) -> str:
    if status == JobStatus.SUCCEEDED:
        return base_reason
    observations: list[str] = []
    for event in events:
        if (
            event.type != EventType.VALIDATION_RECORDED
            or event.payload.get("passed") is not False
        ):
            continue
        attempt = event.payload.get("attempt")
        name = " ".join(
            redact_prompt_text(str(event.payload.get("name", "validation"))).split()
        )[:128]
        detail = " ".join(
            redact_prompt_text(str(event.payload.get("detail", ""))).split()
        )[:256]
        raw_paths = event.payload.get("candidate_changed_paths", ())
        paths = (
            tuple(str(path) for path in raw_paths[:8])
            if isinstance(raw_paths, list)
            else ()
        )
        changes = ",".join(paths) if paths else "none"
        observations.append(
            f"{attempt if type(attempt) is int else len(observations) + 1}:"
            f"{name}={detail or 'failed'};changes={changes}"
        )
    if not observations:
        return base_reason
    combined = (
        f"{base_reason.rstrip()} Validation observations: "
        + " -> ".join(observations[-2:])
    ).strip()
    return " ".join(redact_prompt_text(combined).split())[:512]


def _prepare_manager_evaluation_context(
    *,
    manager_employee: EmployeeRecord,
    goal: str,
    job_id: str,
    fixture: StrEnum,
    company_revision: int,
    roster_revision: int,
    playbook_revision: int,
    manager_roster_revision: int | None,
    max_total_model_calls: int,
    max_wall_time_ms: int,
) -> tuple[ManagerAssignment, WorkOrder, ManagerPlanningBrief]:
    """Create a bounded Manager assignment without granting runtime authority."""

    effective_manager_revision = (
        manager_roster_revision
        if manager_roster_revision is not None
        else max(1, roster_revision)
    )
    manager = PersistentExecutiveManager.from_roster(
        (manager_employee,),
        roster_revision=effective_manager_revision,
    )
    authority = AuthoritySnapshotIdentity(
        company_id="evaluation-company",
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        action_policy_digest=content_digest(
            {
                "kind": "manager-value-shadow-approved",
                "workspace": _WORKSPACE_ID,
            }
        ),
    )
    work_order = normalize_work_order(
        goal,
        work_order_id=f"work-order-{job_id}",
        requested_outcome="Produce one validated fixture result.",
        constraints=(
            "Use only the disposable evaluation workspace and its approved change set.",
        ),
        acceptance_criteria=("Return one validated final result.",),
        workspace_ref=f"workspace:{_WORKSPACE_ID}",
        authority_snapshot=authority,
        budget_snapshot=WorkOrderBudgetSnapshot(
            max_model_calls=max_total_model_calls,
            max_tool_calls=8,
            max_cost_usd=2.0,
            max_wall_time_ms=max_wall_time_ms,
        ),
        requested_at=utc_now(),
        operating_decision=classify_company_input(goal),
    )
    assignment = manager.initial_assignment(
        work_order,
        session_key=f"evaluation:{job_id}",
    )
    brief = ManagerPlanningBrief(
        company_revision=max(1, company_revision),
        company_purpose="Evaluate a bounded staffing decision in a disposable fixture.",
        work_order_constraints=work_order.constraints,
        skills=(),
        outcome_summary=ManagerOutcomeSummary(
            context_fingerprint=f"evaluation:{fixture.value}"[:128],
            observed_count=0,
            succeeded_count=0,
            safety_passed_count=0,
            effect_passed_count=0,
        ),
    )
    return assignment, work_order, brief




async def run_closed_loop_evaluation(
    fixture: CodingFixtureKind | str,
    strategy: CodingStrategyKind | str,
) -> ClosedLoopCodingRecord:
    fixture = CodingFixtureKind(fixture)
    strategy = CodingStrategyKind(strategy)
    with tempfile.TemporaryDirectory(prefix="noruct-closed-loop-") as directory:
        root = Path(directory)
        workspace = materialize_fixture(fixture, root / "workspace")
        return await _run_materialized_evaluation(
            fixture=fixture,
            strategy=strategy,
            root=root,
            workspace=workspace,
            provider=_OfflineProvider(_plan(fixture, strategy)),
            worker=_FixtureCodingWorker(fixture),
            model_profile="offline-scripted",
            run_kind="offline",
            max_total_model_calls=8,
            max_wall_time_ms=10_000,
        )


async def run_live_coding_evaluation(
    config: LiveCodingEvaluationConfig,
    fixture: CodingFixtureKind | str,
    strategy: CodingStrategyKind | str,
    *,
    provider_factory=None,
    coding_worker_factory=None,
) -> LiveCodingEvaluationRecord:
    from dynamic_firm.providers.codex_exec import (
        CodexExecCodingWorker,
        CodexExecProvider,
        CodexExecProviderConfig,
    )

    fixture = CodingFixtureKind(fixture)
    strategy = CodingStrategyKind(strategy)
    if not config.command.strip():
        raise ValueError("Live evaluation requires a Codex executable command")
    if not config.source_revision.strip():
        raise ValueError("Live evaluation requires a source revision record")
    if config.model is None or not config.model.strip():
        raise ValueError("Live evaluation requires an explicit model id")
    if not 4 <= config.max_total_model_calls <= 8:
        raise ValueError("Live evaluation model-call limit must be between 4 and 8")
    if config.timeout_seconds <= 0 or config.max_wall_time_ms <= 0:
        raise ValueError("Live evaluation time limits must be positive")
    revisions = (
        config.company_revision,
        config.roster_revision,
        config.playbook_revision,
    )
    if any(type(value) is not int or value < 0 for value in revisions):
        raise ValueError("Live evaluation revision values must be non-negative integers")
    if config.distribution_sha256 and (
        len(config.distribution_sha256) != 64
        or any(character not in "0123456789abcdef" for character in config.distribution_sha256)
    ):
        raise ValueError("Live evaluation distribution SHA-256 is invalid")
    model_id = config.model.strip()
    make_provider = provider_factory or CodexExecProvider
    make_worker = coding_worker_factory or CodexExecCodingWorker
    recorded_at = utc_now().isoformat()
    evaluation_run_id = f"live-run-{uuid.uuid4().hex}"
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="noruct-live-evaluation-") as directory:
        root = Path(directory)
        workspace = materialize_fixture(fixture, root / "workspace")
        provider_config = CodexExecProviderConfig(
            workspace=workspace,
            command=config.command,
            model=model_id,
            timeout_seconds=config.timeout_seconds,
        )
        live_provider = make_provider(provider_config)
        external_worker = make_worker(provider_config)
        compiler_provider = (
            live_provider
            if strategy == CodingStrategyKind.DYNAMIC
            else _ForcedPlanProvider(live_provider, _plan(fixture, strategy))
        )
        result = await _run_materialized_evaluation(
            fixture=fixture,
            strategy=strategy,
            root=root,
            workspace=workspace,
            provider=compiler_provider,
            worker=external_worker,
            model_profile=model_id,
            run_kind="live",
            max_total_model_calls=config.max_total_model_calls,
            max_wall_time_ms=config.max_wall_time_ms,
            company_revision=config.company_revision,
            roster_revision=config.roster_revision,
            playbook_revision=config.playbook_revision,
            distribution_sha256=config.distribution_sha256,
        )
    counterfactual_planner_calls = (
        0 if strategy == CodingStrategyKind.DYNAMIC else 1
    )
    identity_payload = {
        "schema_version": "noruct.live-coding-evaluation.v3",
        "recorded_at": recorded_at,
        "noruct_version": __version__,
        "source_revision": config.source_revision,
        "evaluation_run_id": evaluation_run_id,
        "provider_kind": "openai-codex-user-managed",
        "model_id": model_id,
        "planner_source": (
            "live-dynamic-workflow-compiler"
            if strategy == CodingStrategyKind.DYNAMIC
            else "bounded-counterfactual-plan"
        ),
        "validation_observation_scope": "noruct-bounded-recovery-handshake",
        "subscription_cost_usd": None,
        "quota_confirmed": config.quota_confirmed,
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        "external_model_calls": max(
            0,
            result.runtime_usage.model_calls - counterfactual_planner_calls,
        ),
        "result": result,
    }
    digest = content_digest(identity_payload)
    return LiveCodingEvaluationRecord(
        schema_version="noruct.live-coding-evaluation.v3",
        evidence_id=f"live-evidence-{digest[:24]}",
        content_hash=digest,
        recorded_at=recorded_at,
        noruct_version=__version__,
        source_revision=config.source_revision,
        evaluation_run_id=evaluation_run_id,
        provider_kind="openai-codex-user-managed",
        model_id=model_id,
        planner_source=(
            "live-dynamic-workflow-compiler"
            if strategy == CodingStrategyKind.DYNAMIC
            else "bounded-counterfactual-plan"
        ),
        validation_observation_scope="noruct-bounded-recovery-handshake",
        subscription_cost_usd=None,
        quota_confirmed=config.quota_confirmed,
        elapsed_ms=identity_payload["elapsed_ms"],
        external_model_calls=max(
            0,
            result.runtime_usage.model_calls - counterfactual_planner_calls,
        ),
        result=result,
    )


async def run_live_coding_preflight(
    config: LiveCodingEvaluationConfig,
    fixture: CodingFixtureKind | str,
    strategy: CodingStrategyKind | str,
    *,
    login_status_factory=None,
) -> LiveCodingPreflightRecord:
    """Check the one allowed parallel live case without invoking a model."""

    from dynamic_firm.providers.codex_exec import CodexExecProvider

    fixture = CodingFixtureKind(fixture)
    strategy = CodingStrategyKind(strategy)
    if fixture != CodingFixtureKind.PARALLEL_EVIDENCE or strategy != CodingStrategyKind.DYNAMIC:
        raise ValueError("Live preflight is limited to parallel-evidence / dynamic")
    if not config.command.strip():
        raise ValueError("Live preflight requires a Codex executable command")
    if not 4 <= config.max_total_model_calls <= 8:
        raise ValueError("Live preflight model-call limit must be between 4 and 8")
    if config.timeout_seconds <= 0 or config.max_wall_time_ms <= 0:
        raise ValueError("Live preflight time limits must be positive")

    status_reader = login_status_factory or CodexExecProvider.login_status
    login = status_reader(config.command)
    rehearsal = await run_closed_loop_evaluation(fixture, strategy)
    trajectory = rehearsal.trajectory
    score = rehearsal.score
    checks = (
        LiveCodingPreflightCheck(
            "source-revision-recorded",
            bool(config.source_revision.strip())
            and config.source_revision.strip() != "uncommitted-or-unknown",
            config.source_revision.strip() or "missing",
        ),
        LiveCodingPreflightCheck(
            "model-id-explicit",
            bool(config.model and config.model.strip()),
            config.model.strip() if config.model and config.model.strip() else "missing",
        ),
        LiveCodingPreflightCheck(
            "codex-executable-installed",
            bool(login.installed and login.executable),
            login.executable or config.command,
        ),
        LiveCodingPreflightCheck(
            "codex-authenticated",
            bool(login.authenticated),
            "official `codex login status` returned success"
            if login.authenticated
            else "official `codex login status` did not confirm authentication",
        ),
        LiveCodingPreflightCheck(
            "offline-task-success",
            rehearsal.status == JobStatus.SUCCEEDED and score.task_success,
            f"status={rehearsal.status.value} task_success={str(score.task_success).lower()}",
        ),
        LiveCodingPreflightCheck(
            "dependency-derived-parallelism",
            trajectory.employee_count == 2 and trajectory.maximum_parallelism == 2,
            f"employees={trajectory.employee_count} parallelism={trajectory.maximum_parallelism}",
        ),
        LiveCodingPreflightCheck(
            "single-final-writer",
            len(trajectory.writer_employee_ids) == 1,
            f"writers={len(trajectory.writer_employee_ids)}",
        ),
        LiveCodingPreflightCheck(
            "approval-boundary",
            trajectory.approvals_requested == 1
            and trajectory.approvals_granted == 1
            and trajectory.preapproval_workspace_mutations == 0,
            (
                f"approval={trajectory.approvals_granted}/{trajectory.approvals_requested} "
                f"preapproval_mutations={trajectory.preapproval_workspace_mutations}"
            ),
        ),
        LiveCodingPreflightCheck(
            "validation-pass",
            bool(trajectory.validation_attempts) and all(trajectory.validation_attempts),
            "attempts=" + "→".join("pass" if item else "fail" for item in trajectory.validation_attempts),
        ),
        LiveCodingPreflightCheck(
            "independent-score-pass",
            score.overall_passed and score.minimal_staffing and score.parallel_correctness,
            f"quality={score.quality_score:.4f} overall_passed={str(score.overall_passed).lower()}",
        ),
    )
    return LiveCodingPreflightRecord(
        schema_version="noruct.live-coding-preflight.v1",
        recorded_at=utc_now().isoformat(),
        noruct_version=__version__,
        evidence_class="readiness-only-not-live-evidence",
        source_revision=config.source_revision,
        provider_kind="openai-codex-user-managed",
        model_id=config.model.strip() if config.model and config.model.strip() else "missing",
        executable=login.executable,
        max_total_model_calls=config.max_total_model_calls,
        max_wall_time_ms=config.max_wall_time_ms,
        quota_consumed=False,
        external_model_calls=0,
        subscription_cost_usd=None,
        ready=all(check.passed for check in checks),
        checks=checks,
        offline_rehearsal=rehearsal,
    )


async def run_closed_loop_matrix() -> tuple[ClosedLoopCodingRecord, ...]:
    return tuple(
        [
            await run_closed_loop_evaluation(fixture, strategy)
            for fixture in CodingFixtureKind
            for strategy in CodingStrategyKind
        ]
    )


def closed_loop_records_to_json(records: tuple[ClosedLoopCodingRecord, ...]) -> str:
    return json.dumps(to_primitive(records), ensure_ascii=False, sort_keys=True, indent=2)


def live_coding_record_to_json(record: LiveCodingEvaluationRecord) -> str:
    return json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True, indent=2)


def live_coding_preflight_to_json(record: LiveCodingPreflightRecord) -> str:
    return json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True, indent=2)


from .closed_loop_materialized import _run_materialized_evaluation  # noqa: E402
