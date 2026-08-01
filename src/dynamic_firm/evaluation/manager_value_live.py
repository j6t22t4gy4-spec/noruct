"""In-process runtime executor for the sealed four-way Manager campaign.

The campaign ledger intentionally owns slot reservation and evidence sealing.
This module owns only the comparable runtime materialization: all four arms
enter the same Firm Kernel, shadow-coding approval boundary and append-only
ledger.  The difference is limited to the admitted roster, graph shape and
Manager binding; it is never inferred from a label after the run.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dynamic_firm import __version__
from dynamic_firm.coding import CodingWorkResult
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.kernel.supervision import (
    ManagerSupervisionAction,
    ManagerSupervisionContext,
    ManagerSupervisionDecision,
)
from dynamic_firm.runtime.models import Usage, utc_now

from .closed_loop import CodingStrategyKind, _ForcedPlanProvider, _run_materialized_evaluation
from .firm_value_v2 import (
    FIRM_VALUE_V2_EVALUATOR_PROFILE,
    FirmValueV2FixtureKind,
    _V2Provider,
    _V2Validator,
    _V2Worker,
    materialize_firm_value_v2_fixture,
    score_firm_value_v2_candidate,
)
from .manager_value_campaign import MANAGER_CAMPAIGN_RECORD_SCHEMA, ManagerValueLiveRecord
from .manager_value_contract import ManagerValueArm, manager_value_qualification_contract


@dataclass(frozen=True, slots=True)
class ManagerValueLiveConfig:
    """Frozen provider and authority envelope for exactly one campaign slot."""

    command: str
    model: str
    source_revision: str
    distribution_sha256: str
    timeout_seconds: float = 120.0
    max_total_model_calls: int = 6
    max_wall_time_ms: int = 180_000
    quota_confirmed: bool = False
    evaluator_risk_confirmed: bool = False
    company_revision: int = 0
    roster_revision: int = 0
    playbook_revision: int = 0


@dataclass(frozen=True, slots=True)
class ManagerValueRuntimeOutcome:
    """Provider-free inspection outcome used by the evaluator tests."""

    record: ManagerValueLiveRecord
    plan_task_ids: tuple[str, ...]
    roster_employee_ids: tuple[str, ...]
    manager_assignment_bound: bool
    manager_supervision_count: int
    manager_planning_owner_id: str = ""
    manager_planning_brief_digest: str = ""


@dataclass(frozen=True, slots=True)
class ManagerValueOfflineRehearsal:
    """Provider-free execution proof for the complete four-way matrix.

    This is intentionally separate from a campaign ledger: it proves that the
    frozen counterfactual fixtures, Kernel path, provenance checks and report
    inputs remain executable without claiming a live Manager staffing outcome.
    """

    schema_version: str
    outcomes: tuple[ManagerValueRuntimeOutcome, ...]
    passed: bool
    external_model_calls: int
    quota_consumed: bool


def _task(
    task_id: str,
    capability: str,
    *,
    depends_on: tuple[str, ...] = (),
    objective: str,
    execution_replica: Mapping[str, object] | None = None,
) -> dict[str, object]:
    task: dict[str, object] = {
        "task_id": task_id,
        "objective": objective,
        "depends_on": list(depends_on),
        "required_capabilities": [capability],
        "acceptance_criteria": [f"Return bounded evidence for {task_id}."],
        "risk_level": "LOW",
    }
    if execution_replica is not None:
        task["execution_replica"] = dict(execution_replica)
    return task


def _partition_replica(*, replica_id: str, scope: str) -> Mapping[str, object]:
    """One explicit, read-only clone instance for the homogeneous arm."""

    return {
        "group_id": "implementation_evidence_partition",
        "replica_id": replica_id,
        "strategy": "PARTITION",
        "scope": scope,
        "aggregation_task_id": "implement_change",
        "aggregation": "JOIN",
        "marginal_value_reason": (
            "Two disjoint evidence checks can reduce integration latency before "
            "one final writer changes the disposable workspace."
        ),
    }


def _final_objective(fixture: FirmValueV2FixtureKind) -> str:
    return {
        FirmValueV2FixtureKind.SOLO_EDIT: "Implement the smallest safe_divide correction.",
        FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY: (
            "Implement inclusive bounds with bounded recovery in window.py; "
            "preserve public tests and change only window.py."
        ),
        FirmValueV2FixtureKind.EVIDENCE_SYNTHESIS: (
            "Integrate channel and priority evidence into delivery.py."
        ),
        FirmValueV2FixtureKind.REVIEW_DEFECT_DETECTION: (
            "Correct retry_policy.py after policy review."
        ),
    }[fixture]


def _arm_plan(
    fixture: FirmValueV2FixtureKind,
    arm: ManagerValueArm,
) -> Mapping[str, object]:
    """Return a frozen counterfactual graph, not a post-hoc arm label."""

    final = _final_objective(fixture)
    if arm is ManagerValueArm.SINGLE_EMPLOYEE:
        return {
            "mode": "SOLO",
            "rationale": "Single Employee counterfactual baseline.",
            "assumptions": [],
            "tasks": [_task("implement_change", "implementation", objective=final)],
            "final_task_id": "implement_change",
        }
    if arm is ManagerValueArm.HOMOGENEOUS_GRAPH:
        # All clone instances expose one *material* capability profile, while
        # the coding compiler still requires `implementation` to remain the
        # final writer capability.  The read-only evidence tasks therefore
        # use the same profile's `analysis` capability rather than pretending
        # they are additional writers.
        evidence_capability = "analysis"
        return {
            "mode": "GRAPH",
            "rationale": "Clone graph counterfactual with one capability profile.",
            "assumptions": [],
            "tasks": [
                _task(
                    "replica_evidence_a",
                    evidence_capability,
                    objective="Inspect one bounded implementation concern without changing files.",
                    execution_replica=_partition_replica(
                        replica_id="implementation_concern_a",
                        scope="primary behavior evidence for the requested change",
                    ),
                ),
                _task(
                    "replica_evidence_b",
                    evidence_capability,
                    objective="Inspect an independent implementation concern without changing files.",
                    execution_replica=_partition_replica(
                        replica_id="implementation_concern_b",
                        scope="boundary and regression evidence for the requested change",
                    ),
                ),
                _task(
                    "implement_change",
                    "implementation",
                    depends_on=("replica_evidence_a", "replica_evidence_b"),
                    objective=final,
                ),
            ],
            "final_task_id": "implement_change",
        }
    if fixture is FirmValueV2FixtureKind.REVIEW_DEFECT_DETECTION:
        evidence = ("review_policy", "review")
    else:
        # Control fixtures remain a real heterogenous graph. This deliberately
        # exposes graph overhead rather than silently collapsing the arm.
        evidence = ("analyze_change", "analysis")
    return {
        "mode": "GRAPH",
        "rationale": (
            "Manager-owned specialist delegation."
            if arm is ManagerValueArm.MANAGER_LED_FIRM
            else "Capability-bound heterogeneous graph counterfactual."
        ),
        "assumptions": [],
        "tasks": [
            _task(evidence[0], evidence[1], objective="Produce bounded read-only evidence for the final change."),
            _task("implement_change", "implementation", depends_on=(evidence[0],), objective=final),
        ],
        "final_task_id": "implement_change",
    }


def _arm_roster(arm: ManagerValueArm) -> tuple[EmployeeRecord, ...]:
    if arm is ManagerValueArm.SINGLE_EMPLOYEE:
        return (EmployeeRecord("manager-value-solo", "Engineer", ("implementation",)),)
    if arm is ManagerValueArm.HOMOGENEOUS_GRAPH:
        profile = ("analysis", "implementation")
        return (
            EmployeeRecord("manager-value-clone-a", "Engineer", profile),
            EmployeeRecord("manager-value-clone-b", "Engineer", profile),
            EmployeeRecord("manager-value-clone-writer", "Engineer", profile),
        )
    if arm is ManagerValueArm.HETEROGENEOUS_GRAPH:
        return (
            EmployeeRecord("manager-value-analyst", "Analyst", ("analysis",)),
            EmployeeRecord("manager-value-reviewer", "Reviewer", ("review",)),
            EmployeeRecord("manager-value-writer", "Engineer", ("implementation",)),
        )
    return (
        EmployeeRecord("manager-value-analyst", "Analyst", ("analysis",)),
        EmployeeRecord("manager-value-reviewer", "Reviewer", ("review",)),
        EmployeeRecord("manager-value-writer", "Engineer", ("implementation",)),
    )


def _manager_employee() -> EmployeeRecord:
    return EmployeeRecord(
        "manager-value-executive",
        "Manager",
        ("company_management",),
        model_profile="manager-evaluation",
    )


class _ReadOnlyEvidenceWorker:
    """Prevents graph evidence tasks from becoming competing file writers."""

    def __init__(self, worker) -> None:
        self.worker = worker

    async def execute(self, request, cancellation):
        if "implementation" not in request.required_capabilities:
            cancellation.raise_if_cancelled()
            return CodingWorkResult(
                summary="Read-only graph evidence collected.",
                acceptance_evidence=(f"manager-value:{request.task_id}:read-only",),
                usage=Usage(),
            )
        return await self.worker.execute(request, cancellation)


class _EvaluationManagerSupervisor:
    """A bounded Manager observation lane with no graph or authority power."""

    def __init__(self) -> None:
        self.assessment_count = 0

    async def assess(self, context: ManagerSupervisionContext) -> ManagerSupervisionDecision:
        self.assessment_count += 1
        return ManagerSupervisionDecision(
            action=ManagerSupervisionAction.CONTINUE,
            rationale="Observed bounded task evidence; no intervention is required.",
        )


def _selected_capability_profile_count(
    plan_template,
    roster: tuple[EmployeeRecord, ...],
    task_attempts: tuple[Mapping[str, object], ...] = (),
) -> int:
    """Count material Employee profiles actually selected for a Job.

    A Manager-bound Compiler may select ``DIRECT``/``SOLO`` for a bounded Job.
    That legitimate choice need not leave a graph template, so execution
    attempts are the primary staffing evidence.  The admitted template is a
    fallback only before the first Employee attempt; neither path counts an
    Employee merely because it was available in the roster.
    """

    selected_ids = {
        str(attempt.get("employee_id", "")).strip()
        for attempt in task_attempts
        if str(attempt.get("employee_id", "")).strip()
    }
    selected = {
        tuple(sorted(employee.capabilities))
        for employee in roster
        if employee.employee_id in selected_ids
    }
    if selected:
        return len(selected)

    selected: set[tuple[str, ...]] = set()
    for task in plan_template:
        required = set(task.required_capabilities)
        candidate = next(
            (
                employee
                for employee in roster
                if required.issubset(set(employee.capabilities))
            ),
            None,
        )
        if candidate is not None:
            selected.add(tuple(sorted(candidate.capabilities)))
    return len(selected)


def _record_from_closed_loop(
    closed_loop,
    *,
    fixture: FirmValueV2FixtureKind,
    arm: ManagerValueArm,
    config: ManagerValueLiveConfig,
    elapsed_ms: int,
    compiler_planning_exercised: bool,
) -> ManagerValueLiveRecord:
    score = closed_loop.score
    payload = {
        "schema_version": MANAGER_CAMPAIGN_RECORD_SCHEMA,
        "recorded_at": utc_now().isoformat(),
        "fixture": fixture.value,
        "fixture_revision": closed_loop.fixture_revision,
        "arm": arm.value,
        "source_revision": config.source_revision,
        "distribution_sha256": config.distribution_sha256,
        "model_id": config.model,
        "company_revision": config.company_revision,
        "roster_revision": config.roster_revision,
        "playbook_revision": config.playbook_revision,
        "configured_model_call_limit": config.max_total_model_calls,
        "configured_wall_time_ms": config.max_wall_time_ms,
        "quota_confirmed": config.quota_confirmed,
        "evaluator_risk_confirmed": config.evaluator_risk_confirmed,
        "task_success": bool(score.task_success),
        "safety_passed": bool(score.safety.passed),
        "quality_score": float(score.artifact.quality_score),
        "external_model_calls": max(0, closed_loop.runtime_usage.model_calls),
        "elapsed_ms": elapsed_ms,
        "employee_count": closed_loop.trajectory.employee_count,
        "capability_profile_count": _selected_capability_profile_count(
            closed_loop.compiler_plan_template,
            _arm_roster(arm),
            closed_loop.task_attempts,
        ),
        "execution_replica_count": closed_loop.execution_replica_count,
        "replica_group_count": closed_loop.replica_group_count,
        "manager_bound": arm is ManagerValueArm.MANAGER_LED_FIRM,
        "manager_planning_owner_id": closed_loop.planning_owner_id,
        "manager_planning_assignment_digest": (
            closed_loop.planning_owner_assignment_digest
        ),
        "manager_planning_brief_digest": closed_loop.manager_planning_brief_digest,
        "compiler_planning_exercised": compiler_planning_exercised,
        "planning_mode": closed_loop.planning_mode,
        "planning_reason": closed_loop.planning_reason,
        "failure_reason_safe": closed_loop.failure_reason[:1_024],
        "employee_failure_codes": tuple(
            code[:128] for code in closed_loop.employee_failure_codes[:16]
        ),
        "task_attempt_count": len(closed_loop.task_attempts),
        "successful_task_attempt_count": sum(
            1
            for attempt in closed_loop.task_attempts
            if attempt.get("status") == "SUCCEEDED"
        ),
        "approvals_requested": closed_loop.trajectory.approvals_requested,
        "approvals_granted": closed_loop.trajectory.approvals_granted,
        "reported_cost_usd": closed_loop.runtime_usage.cost_usd,
        "cost_accounting_mode": (
            "REPORTED_USD"
            if closed_loop.runtime_usage.cost_usd > 0
            else "MODEL_CALL_PROXY"
        ),
        "validation_attempt_count": len(
            closed_loop.trajectory.validation_attempts
        ),
        "validation_recovery_attempt_count": max(
            0, len(closed_loop.trajectory.validation_attempts) - 1
        ),
        "validation_recovery_success_count": sum(
            bool(passed)
            for passed in closed_loop.trajectory.validation_attempts[1:]
        ),
        "runtime_user_intervention_count": (
            closed_loop.runtime_user_intervention_count
        ),
        "external_effect_error_count": closed_loop.external_effect_error_count,
        "external_effect_unknown_count": closed_loop.external_effect_unknown_count,
        "intervention_accounting_mode": "RUNTIME_OPERATOR_SIGNAL_LEDGER",
        "external_effect_accounting_mode": "DURABLE_TOOL_ACTION_STATUS",
    }
    from dynamic_firm.company.models import content_digest

    digest = content_digest(payload)
    return ManagerValueLiveRecord(
        record_id=f"manager-value-live-{digest[:24]}",
        content_hash=digest,
        **payload,
    )


async def _run_arm(
    *,
    config: ManagerValueLiveConfig,
    fixture: FirmValueV2FixtureKind,
    arm: ManagerValueArm,
    provider,
    worker,
    run_kind: str,
    fixed_counterfactual_plan: bool,
) -> tuple[ManagerValueRuntimeOutcome, object]:
    roster = _arm_roster(arm)
    plan = _arm_plan(fixture, arm)
    supervisor = _EvaluationManagerSupervisor() if arm is ManagerValueArm.MANAGER_LED_FIRM else None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="noruct-manager-value-") as directory:
        root = Path(directory)
        workspace = materialize_firm_value_v2_fixture(fixture, root / "workspace")
        closed_loop = await _run_materialized_evaluation(
            fixture=fixture,
            strategy=(CodingStrategyKind.SOLO if arm is ManagerValueArm.SINGLE_EMPLOYEE else CodingStrategyKind.DYNAMIC),
            root=root,
            workspace=workspace,
            provider=(
                _ForcedPlanProvider(provider, plan)
                if fixed_counterfactual_plan
                else provider
            ),
            worker=_ReadOnlyEvidenceWorker(worker),
            model_profile=config.model,
            run_kind=run_kind,
            max_total_model_calls=config.max_total_model_calls,
            max_wall_time_ms=config.max_wall_time_ms,
            company_revision=config.company_revision,
            roster_revision=config.roster_revision,
            playbook_revision=config.playbook_revision,
            distribution_sha256=config.distribution_sha256,
            roster_override=roster,
            validator_override=_V2Validator(fixture),
            score_candidate_override=lambda candidate, trajectory: score_firm_value_v2_candidate(
                fixture,
                CodingStrategyKind.DYNAMIC,
                candidate,
                trajectory,
            ),
            fixture_revision_override=next(
                item.fixture_revision
                for item in manager_value_qualification_contract().fixtures
                if item.fixture == fixture.value
            ),
            manager_employee=(
                _manager_employee() if arm is ManagerValueArm.MANAGER_LED_FIRM else None
            ),
            manager_roster_revision=max(1, config.roster_revision),
            manager_supervisor=supervisor,
            manager_planning_provenance=(
                arm is ManagerValueArm.MANAGER_LED_FIRM
            ),
        )
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    record = _record_from_closed_loop(
        closed_loop,
        fixture=fixture,
        arm=arm,
        config=config,
        elapsed_ms=elapsed_ms,
        compiler_planning_exercised=not fixed_counterfactual_plan,
    )
    return (
        ManagerValueRuntimeOutcome(
            record=record,
            plan_task_ids=(
                tuple(str(item["task_id"]) for item in plan["tasks"])
                if fixed_counterfactual_plan
                else tuple(
                    item.task_key for item in closed_loop.compiler_plan_template
                )
            ),
            roster_employee_ids=tuple(item.employee_id for item in roster),
            manager_assignment_bound=arm is ManagerValueArm.MANAGER_LED_FIRM,
            manager_supervision_count=(0 if supervisor is None else supervisor.assessment_count),
            manager_planning_owner_id=closed_loop.planning_owner_id,
            manager_planning_brief_digest=closed_loop.manager_planning_brief_digest,
        ),
        closed_loop,
    )


async def run_manager_value_offline_case(
    fixture: FirmValueV2FixtureKind | str,
    arm: ManagerValueArm | str,
) -> ManagerValueRuntimeOutcome:
    """Exercise every arm against the real Kernel without quota or network use."""

    fixture = FirmValueV2FixtureKind(fixture)
    arm = ManagerValueArm(arm)
    config = ManagerValueLiveConfig(
        command="offline",
        model="offline-manager-value",
        source_revision="snapshot-sha256:" + "0" * 64,
        distribution_sha256="0" * 64,
        max_total_model_calls=8,
        max_wall_time_ms=10_000,
        quota_confirmed=True,
        evaluator_risk_confirmed=True,
        roster_revision=1,
    )
    outcome, _ = await _run_arm(
        config=config,
        fixture=fixture,
        arm=arm,
        provider=_V2Provider(_arm_plan(fixture, arm), count_compiler=False),
        worker=_V2Worker(fixture, CodingStrategyKind.DYNAMIC),
        run_kind="offline-manager-value",
        fixed_counterfactual_plan=True,
    )
    return outcome


async def run_manager_value_offline_rehearsal() -> ManagerValueOfflineRehearsal:
    """Execute the exact 16-slot contract without contacting a provider."""

    collected: list[ManagerValueRuntimeOutcome] = []
    for fixture in FirmValueV2FixtureKind:
        for arm in ManagerValueArm:
            collected.append(await run_manager_value_offline_case(fixture, arm))
    outcomes = tuple(collected)
    manager_outcomes = tuple(
        outcome
        for outcome in outcomes
        if outcome.record.arm == ManagerValueArm.MANAGER_LED_FIRM.value
    )
    passed = (
        len(outcomes) == len(manager_value_qualification_contract().exact_slots)
        and all(
            outcome.record.task_success and outcome.record.safety_passed
            for outcome in outcomes
        )
        and all(
            outcome.manager_assignment_bound
            and outcome.manager_planning_owner_id == "manager-value-executive"
            and len(outcome.manager_planning_brief_digest) == 64
            and not outcome.record.compiler_planning_exercised
            for outcome in manager_outcomes
        )
    )
    return ManagerValueOfflineRehearsal(
        schema_version="noruct.manager-value-offline-rehearsal.v1",
        outcomes=outcomes,
        passed=passed,
        external_model_calls=0,
        quota_consumed=False,
    )


async def run_live_manager_value_evaluation(
    config: ManagerValueLiveConfig,
    fixture: FirmValueV2FixtureKind | str,
    arm: ManagerValueArm | str,
    *,
    provider_factory=None,
    coding_worker_factory=None,
) -> ManagerValueLiveRecord:
    """Run one confirmed live campaign slot through the in-process Firm runtime."""

    from dynamic_firm.providers.codex_exec import (
        CodexExecCodingWorker,
        CodexExecProvider,
        CodexExecProviderConfig,
    )

    fixture = FirmValueV2FixtureKind(fixture)
    arm = ManagerValueArm(arm)
    if not config.command.strip() or not config.model.strip():
        raise ValueError("Manager-value live evaluation requires command and explicit model")
    if not config.source_revision.startswith("snapshot-sha256:") or len(config.distribution_sha256) != 64:
        raise ValueError("Manager-value live evaluation requires frozen source and wheel")
    if not config.quota_confirmed or not config.evaluator_risk_confirmed:
        raise ValueError("Manager-value live evaluation requires both explicit confirmations")
    if not 4 <= config.max_total_model_calls <= 12 or not 1_000 <= config.max_wall_time_ms <= 600_000:
        raise ValueError("Manager-value live evaluation bounds are invalid")
    if any(value < 0 for value in (config.company_revision, config.roster_revision, config.playbook_revision)):
        raise ValueError("Manager-value live revisions must be non-negative")
    if any(character not in "0123456789abcdef" for character in config.distribution_sha256):
        raise ValueError("Manager-value live wheel SHA-256 is invalid")

    # The provider and coding worker share the same disposable fixture root;
    # no provider sees the operator workspace during this evaluation.
    with tempfile.TemporaryDirectory(prefix="noruct-manager-value-live-root-") as directory:
        placeholder = Path(directory) / "workspace"
        placeholder.mkdir()
        provider_config = CodexExecProviderConfig(
            workspace=placeholder,
            command=config.command,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
        )
        make_provider = provider_factory or CodexExecProvider
        make_worker = coding_worker_factory or CodexExecCodingWorker
        outcome, _ = await _run_arm(
            config=config,
            fixture=fixture,
            arm=arm,
            provider=make_provider(provider_config),
            worker=make_worker(provider_config),
            run_kind="live-manager-value",
            # The three baselines are fixed counterfactuals.  Letting their
            # provider choose topology turns an arm comparison into a test of
            # arbitrary compiler collapse (for example a homogeneous graph
            # silently becoming a one-Employee SOLO plan).  The Manager arm
            # alone must exercise the live Manager-bound compiler.
            fixed_counterfactual_plan=arm is not ManagerValueArm.MANAGER_LED_FIRM,
        )
    if outcome.record.external_model_calls > config.max_total_model_calls:
        raise RuntimeError("Manager-value live runtime exceeded its frozen model-call limit")
    if outcome.record.elapsed_ms > config.max_wall_time_ms:
        raise RuntimeError("Manager-value live runtime exceeded its frozen wall-time limit")
    return outcome.record
