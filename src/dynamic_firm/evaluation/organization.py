from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    GraphPatch,
    GraphPatchOperation,
    JobLimits,
    JobStatus,
    JobTask,
    PatchOperationKind,
    PlanProposal,
    SemanticOperation,
)
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import (
    ScriptedEmployeeExecutionPort,
    ScriptedOutcome,
    StaticReplanner,
)
from dynamic_firm.runtime.models import RunLimits, RunSignal, SignalCode, Usage, to_primitive


class FixtureKind(StrEnum):
    SOLO = "solo"
    PARALLEL = "parallel"
    REPLAN = "replan"


class StrategyKind(StrEnum):
    SOLO = "solo"
    FIXED = "fixed"
    DYNAMIC = "dynamic"


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    fixture: FixtureKind
    strategy: StrategyKind
    status: JobStatus
    evidence_hits: int
    evidence_required: int
    quality_score: float
    employee_count: int
    temporary_role_count: int
    unnecessary_role_count: int
    model_calls: int
    tool_calls: int
    cost_usd: float
    maximum_parallelism: int
    graph_mutations: int
    final_graph_version: int


@dataclass(frozen=True, slots=True)
class _Scenario:
    request: CompanyRunRequest
    runner: ScriptedEmployeeExecutionPort
    replanner: StaticReplanner | None
    required_evidence: tuple[str, ...]
    minimum_employees: int


def _task(
    task_id: str,
    capability: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> JobTask:
    return JobTask(
        task_id=task_id,
        objective=f"Produce the {task_id} fixture result.",
        depends_on=depends_on,
        required_capabilities=(capability,),
        acceptance_criteria=(f"Return deterministic evidence for {task_id}.",),
    )


def _outcome(
    summary: str,
    evidence: tuple[str, ...],
    *,
    delay: float = 0.0,
    model_calls: int = 1,
    signals: tuple[RunSignal, ...] = (),
) -> ScriptedOutcome:
    return ScriptedOutcome(
        summary=summary,
        delay_seconds=delay,
        acceptance_evidence=evidence,
        signals=signals,
        usage=Usage(model_calls=model_calls, cost_usd=round(model_calls * 0.01, 2)),
    )


def _request(
    fixture: FixtureKind,
    strategy: StrategyKind,
    tasks: tuple[JobTask, ...],
    final_task_id: str,
    roster: tuple[EmployeeRecord, ...],
) -> CompanyRunRequest:
    identity = f"{fixture.value}-{strategy.value}"
    return CompanyRunRequest(
        request_id=f"evaluation-request-{identity}",
        job_id=f"evaluation-job-{identity}",
        goal=f"Evaluate the {fixture.value} fixture with the {strategy.value} strategy.",
        plan_proposal=PlanProposal(
            proposal_id=f"evaluation-proposal-{identity}",
            goal=f"Evaluate {fixture.value}",
            tasks=tasks,
            final_task_id=final_task_id,
        ),
        roster=roster,
        runtime_limits=RunLimits(max_model_calls=4, max_tool_calls=4, max_cost_usd=2.0),
        job_limits=JobLimits(
            max_tasks=8,
            max_concurrency=3,
            max_graph_patches=2,
            max_temporary_roles=2,
            max_total_model_calls=24,
            max_total_tool_calls=24,
            max_total_cost_usd=2.0,
            max_wall_time_ms=5_000,
        ),
    )


def _solo_strategy(fixture: FixtureKind) -> _Scenario:
    required = {
        FixtureKind.SOLO: ("goal:complete",),
        FixtureKind.PARALLEL: ("analysis:a", "analysis:b", "integration:complete"),
        FixtureKind.REPLAN: ("discovery:gap", "compliance:checked", "integration:complete"),
    }[fixture]
    evidence = required if fixture != FixtureKind.REPLAN else (
        "discovery:gap",
        "integration:complete",
    )
    model_calls = {FixtureKind.SOLO: 1, FixtureKind.PARALLEL: 3, FixtureKind.REPLAN: 2}[fixture]
    tasks = (_task("final", "general_execution"),)
    runner = ScriptedEmployeeExecutionPort(
        {"final": _outcome("Solo baseline result", evidence, model_calls=model_calls)}
    )
    request = _request(
        fixture,
        StrategyKind.SOLO,
        tasks,
        "final",
        (EmployeeRecord("solo-generalist", "Generalist", ("general_execution",)),),
    )
    minimum = {FixtureKind.SOLO: 1, FixtureKind.PARALLEL: 2, FixtureKind.REPLAN: 2}[fixture]
    return _Scenario(request, runner, None, required, minimum)


def _fixed_strategy(fixture: FixtureKind) -> _Scenario:
    if fixture == FixtureKind.SOLO:
        tasks = (
            _task("research", "research"),
            _task("review", "review", depends_on=("research",)),
            _task("final", "integration", depends_on=("review",)),
        )
        roster = (
            EmployeeRecord("fixed-researcher", "Researcher", ("research",)),
            EmployeeRecord("fixed-reviewer", "Reviewer", ("review",)),
            EmployeeRecord("fixed-integrator", "Integrator", ("integration",)),
        )
        outcomes = {
            "research": _outcome("Researched", ("goal:complete",)),
            "review": _outcome("Reviewed", ()),
            "final": _outcome("Fixed result", ()),
        }
        required = ("goal:complete",)
        minimum = 1
    elif fixture == FixtureKind.PARALLEL:
        tasks = (
            _task("analysis-a", "analysis_a"),
            _task("analysis-b", "analysis_b"),
            _task(
                "final",
                "integration",
                depends_on=("analysis-a", "analysis-b"),
            ),
        )
        roster = (
            EmployeeRecord("fixed-analyst-a", "Analyst A", ("analysis_a",)),
            EmployeeRecord("fixed-analyst-b", "Analyst B", ("analysis_b",)),
            EmployeeRecord("fixed-integrator", "Integrator", ("integration",)),
        )
        outcomes = {
            "analysis-a": _outcome("A", ("analysis:a",), delay=0.01),
            "analysis-b": _outcome("B", ("analysis:b",), delay=0.01),
            "final": _outcome("Fixed parallel result", ("integration:complete",)),
        }
        required = ("analysis:a", "analysis:b", "integration:complete")
        minimum = 2
    else:
        tasks = (
            _task("discovery", "discovery"),
            _task("compliance", "compliance_review", depends_on=("discovery",)),
            _task("final", "integration", depends_on=("compliance",)),
        )
        roster = (
            EmployeeRecord("fixed-scout", "Scout", ("discovery",)),
            EmployeeRecord("fixed-compliance", "Compliance Reviewer", ("compliance_review",)),
            EmployeeRecord("fixed-integrator", "Integrator", ("integration",)),
        )
        outcomes = {
            "discovery": _outcome("Gap found", ("discovery:gap",)),
            "compliance": _outcome("Checked", ("compliance:checked",)),
            "final": _outcome("Fixed replan result", ("integration:complete",)),
        }
        required = ("discovery:gap", "compliance:checked", "integration:complete")
        minimum = 2
    runner = ScriptedEmployeeExecutionPort(outcomes)
    request = _request(fixture, StrategyKind.FIXED, tasks, "final", roster)
    return _Scenario(request, runner, None, required, minimum)


def _dynamic_strategy(fixture: FixtureKind) -> _Scenario:
    if fixture == FixtureKind.SOLO:
        tasks = (_task("final", "analysis"),)
        roster = (EmployeeRecord("dynamic-analyst", "Analyst", ("analysis",)),)
        outcomes = {"final": _outcome("Dynamic solo result", ("goal:complete",))}
        required = ("goal:complete",)
        minimum = 1
        replanner = None
    elif fixture == FixtureKind.PARALLEL:
        tasks = (
            _task("analysis-a", "analysis"),
            _task("analysis-b", "analysis"),
            _task(
                "final",
                "integration",
                depends_on=("analysis-a", "analysis-b"),
            ),
        )
        roster = (
            EmployeeRecord("dynamic-senior", "Senior Analyst", ("analysis", "integration")),
            EmployeeRecord("dynamic-analyst", "Analyst", ("analysis",)),
        )
        outcomes = {
            "analysis-a": _outcome("A", ("analysis:a",), delay=0.01),
            "analysis-b": _outcome("B", ("analysis:b",), delay=0.01),
            "final": _outcome("Dynamic parallel result", ("integration:complete",)),
        }
        required = ("analysis:a", "analysis:b", "integration:complete")
        minimum = 2
        replanner = None
    else:
        capability_signal = RunSignal(
            SignalCode.CAPABILITY_MISSING,
            "compliance_review",
            ("discovery:gap",),
        )
        tasks = (
            _task("discovery", "discovery"),
            _task("final", "integration", depends_on=("discovery",)),
        )
        roster = (
            EmployeeRecord(
                "dynamic-generalist",
                "Generalist",
                ("discovery", "integration"),
            ),
        )
        outcomes = {
            "discovery": _outcome(
                "Gap found",
                ("discovery:gap",),
                signals=(capability_signal,),
            ),
            "compliance": _outcome("Checked", ("compliance:checked",)),
            "final": _outcome("Dynamic replan result", ("integration:complete",)),
        }
        patch = GraphPatch(
            patch_id="evaluation-insert-compliance",
            base_graph_version=1,
            trigger_task_id="discovery",
            semantic_operation=SemanticOperation.INSERT,
            rationale="The employee emitted a typed compliance capability gap.",
            expected_gain="Add compliance evidence before integration.",
            operations=(
                GraphPatchOperation(
                    PatchOperationKind.ADD_TASK,
                    task=_task("compliance", "compliance_review", depends_on=("discovery",)),
                ),
                GraphPatchOperation(
                    PatchOperationKind.ADD_DEPENDENCY,
                    task_id="final",
                    dependency_id="compliance",
                ),
            ),
        )
        required = ("discovery:gap", "compliance:checked", "integration:complete")
        minimum = 2
        replanner = StaticReplanner({"discovery": patch})
    runner = ScriptedEmployeeExecutionPort(outcomes)
    request = _request(fixture, StrategyKind.DYNAMIC, tasks, "final", roster)
    return _Scenario(request, runner, replanner, required, minimum)


def _scenario(fixture: FixtureKind, strategy: StrategyKind) -> _Scenario:
    if strategy == StrategyKind.SOLO:
        return _solo_strategy(fixture)
    if strategy == StrategyKind.FIXED:
        return _fixed_strategy(fixture)
    return _dynamic_strategy(fixture)


async def run_evaluation(
    fixture: FixtureKind | str,
    strategy: StrategyKind | str,
) -> EvaluationRecord:
    fixture = FixtureKind(fixture)
    strategy = StrategyKind(strategy)
    scenario = _scenario(fixture, strategy)
    result = await FirmKernel(
        employee_execution=scenario.runner,
        replanner=scenario.replanner,
    ).run(scenario.request)
    evidence = set(result.acceptance_evidence)
    hits = sum(item in evidence for item in scenario.required_evidence)
    required = len(scenario.required_evidence)
    return EvaluationRecord(
        fixture=fixture,
        strategy=strategy,
        status=result.status,
        evidence_hits=hits,
        evidence_required=required,
        quality_score=round(hits / required, 4),
        employee_count=result.metrics.unique_employee_count,
        temporary_role_count=result.metrics.temporary_role_count,
        unnecessary_role_count=max(
            0,
            result.metrics.unique_employee_count - scenario.minimum_employees,
        ),
        model_calls=result.metrics.usage.model_calls,
        tool_calls=result.metrics.usage.tool_calls,
        cost_usd=result.metrics.usage.cost_usd,
        maximum_parallelism=result.metrics.maximum_parallelism,
        graph_mutations=result.metrics.graph_patch_count,
        final_graph_version=result.final_graph_version,
    )


async def run_matrix() -> tuple[EvaluationRecord, ...]:
    return tuple(
        [
            await run_evaluation(fixture, strategy)
            for fixture in FixtureKind
            for strategy in StrategyKind
        ]
    )


def records_to_json(records: tuple[EvaluationRecord, ...]) -> str:
    return json.dumps(to_primitive(records), ensure_ascii=False, sort_keys=True, indent=2)
