"""Provider-free value qualification for same-Employee execution replicas.

This module does not run a Job and never promotes a Blueprint.  It compares
already observed SINGLE and REPLICA trials under an exact shared identity and
hard budget, then emits bounded evidence for a later Workflow/Blueprint
decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from dynamic_firm.kernel.models import ExecutionReplicaStrategy


EXECUTION_REPLICA_TRIAL_SCHEMA = "noruct.execution-replica-trial.v1"
EXECUTION_REPLICA_ASSESSMENT_SCHEMA = "noruct.execution-replica-assessment.v1"
MINIMUM_QUALIFICATION_PAIRS = 3
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return value


def _identifier(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value)
    ):
        raise ValueError(f"{label} must be a lowercase identifier")
    return value


def _score(value: float, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"{label} must be between zero and one")
    return float(value)


class ExecutionTrialMode(StrEnum):
    SINGLE = "SINGLE"
    REPLICA = "REPLICA"


class ExecutionReplicaPairDecision(StrEnum):
    VALUE_SIGNAL = "VALUE_SIGNAL"
    NO_PROVEN_VALUE = "NO_PROVEN_VALUE"
    ROLLBACK_CANDIDATE = "ROLLBACK_CANDIDATE"


class ExecutionReplicaQualificationDecision(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    KEEP = "KEEP"
    DISABLE = "DISABLE"
    ROLLBACK_CANDIDATE = "ROLLBACK_CANDIDATE"


@dataclass(frozen=True, slots=True)
class ExecutionReplicaBudgetEnvelope:
    max_model_calls: int
    max_tool_calls: int
    max_cost_usd: float
    max_wall_time_ms: int

    def __post_init__(self) -> None:
        for label, value in (
            ("max_model_calls", self.max_model_calls),
            ("max_tool_calls", self.max_tool_calls),
            ("max_wall_time_ms", self.max_wall_time_ms),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if (
            not isinstance(self.max_cost_usd, (int, float))
            or isinstance(self.max_cost_usd, bool)
            or not math.isfinite(float(self.max_cost_usd))
            or self.max_cost_usd < 0
        ):
            raise ValueError("max_cost_usd must be finite and non-negative")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "max_model_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_cost_usd": float(self.max_cost_usd),
            "max_wall_time_ms": self.max_wall_time_ms,
        }

    @property
    def content_digest(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ExecutionReplicaTrial:
    trial_id: str
    workload_digest: str
    environment_digest: str
    employee_capability_digest: str
    budget: ExecutionReplicaBudgetEnvelope
    mode: ExecutionTrialMode
    task_success: bool
    validation_passed: bool
    complete_failure: bool
    quality_score: float
    coverage_score: float
    model_calls: int
    tool_calls: int
    cost_usd: float
    wall_time_ms: int
    safety_violations: tuple[str, ...] = ()
    replica_group_id: str = ""
    replica_strategy: ExecutionReplicaStrategy | None = None
    aggregation_model_calls: int = 0
    aggregation_tool_calls: int = 0
    aggregation_cost_usd: float = 0.0
    aggregation_wall_time_ms: int = 0
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.trial_id, "trial_id")
        _sha256(self.workload_digest, "workload_digest")
        _sha256(self.environment_digest, "environment_digest")
        _sha256(self.employee_capability_digest, "employee_capability_digest")
        if not isinstance(self.budget, ExecutionReplicaBudgetEnvelope):
            raise TypeError("budget must be an ExecutionReplicaBudgetEnvelope")
        if not isinstance(self.mode, ExecutionTrialMode):
            raise TypeError("mode must be typed")
        if any(type(value) is not bool for value in (
            self.task_success,
            self.validation_passed,
            self.complete_failure,
        )):
            raise TypeError("trial outcome flags must be booleans")
        if self.complete_failure and self.task_success:
            raise ValueError("A complete failure cannot be task-successful")
        _score(self.quality_score, "quality_score")
        _score(self.coverage_score, "coverage_score")
        for label, value, maximum in (
            ("model_calls", self.model_calls, self.budget.max_model_calls),
            ("tool_calls", self.tool_calls, self.budget.max_tool_calls),
            ("wall_time_ms", self.wall_time_ms, self.budget.max_wall_time_ms),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > maximum
            ):
                raise ValueError(f"{label} exceeds the shared hard budget")
        if (
            not isinstance(self.cost_usd, (int, float))
            or isinstance(self.cost_usd, bool)
            or not math.isfinite(float(self.cost_usd))
            or self.cost_usd < 0
            or self.cost_usd > self.budget.max_cost_usd
        ):
            raise ValueError("cost_usd exceeds the shared hard budget")
        for label, value, total in (
            ("aggregation_model_calls", self.aggregation_model_calls, self.model_calls),
            ("aggregation_tool_calls", self.aggregation_tool_calls, self.tool_calls),
            ("aggregation_wall_time_ms", self.aggregation_wall_time_ms, self.wall_time_ms),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > total
            ):
                raise ValueError(f"{label} must be contained in total trial usage")
        if (
            not isinstance(self.aggregation_cost_usd, (int, float))
            or isinstance(self.aggregation_cost_usd, bool)
            or self.aggregation_cost_usd < 0
            or self.aggregation_cost_usd > self.cost_usd
        ):
            raise ValueError("aggregation_cost_usd must be contained in total trial cost")
        for violation in self.safety_violations:
            if not isinstance(violation, str) or not violation.strip():
                raise ValueError("safety_violations must contain non-empty strings")
        if self.mode is ExecutionTrialMode.SINGLE:
            if self.replica_group_id or self.replica_strategy is not None:
                raise ValueError("A SINGLE trial cannot declare replica metadata")
            if any(
                (
                    self.aggregation_model_calls,
                    self.aggregation_tool_calls,
                    self.aggregation_cost_usd,
                    self.aggregation_wall_time_ms,
                )
            ):
                raise ValueError("A SINGLE trial cannot declare aggregation overhead")
        else:
            _identifier(self.replica_group_id, "replica_group_id")
            if not isinstance(self.replica_strategy, ExecutionReplicaStrategy):
                raise TypeError("A REPLICA trial requires a typed strategy")
        object.__setattr__(self, "content_digest", _digest(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": EXECUTION_REPLICA_TRIAL_SCHEMA,
            "trial_id": self.trial_id,
            "workload_digest": self.workload_digest,
            "environment_digest": self.environment_digest,
            "employee_capability_digest": self.employee_capability_digest,
            "budget": self.budget.canonical_payload(),
            "mode": self.mode.value,
            "task_success": self.task_success,
            "validation_passed": self.validation_passed,
            "complete_failure": self.complete_failure,
            "quality_score": float(self.quality_score),
            "coverage_score": float(self.coverage_score),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "cost_usd": float(self.cost_usd),
            "wall_time_ms": self.wall_time_ms,
            "safety_violations": list(self.safety_violations),
            "replica_group_id": self.replica_group_id,
            "replica_strategy": (
                None if self.replica_strategy is None else self.replica_strategy.value
            ),
            "aggregation_model_calls": self.aggregation_model_calls,
            "aggregation_tool_calls": self.aggregation_tool_calls,
            "aggregation_cost_usd": float(self.aggregation_cost_usd),
            "aggregation_wall_time_ms": self.aggregation_wall_time_ms,
        }


@dataclass(frozen=True, slots=True)
class ExecutionReplicaValuePair:
    baseline_trial_id: str
    candidate_trial_id: str
    workload_digest: str
    replica_group_id: str
    replica_strategy: ExecutionReplicaStrategy
    quality_delta: float
    coverage_delta: float
    cost_delta_usd: float
    wall_time_delta_ms: int
    decision: ExecutionReplicaPairDecision
    reasons: tuple[str, ...]
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content_digest",
            _digest(
                {
                    "baseline_trial_id": self.baseline_trial_id,
                    "candidate_trial_id": self.candidate_trial_id,
                    "workload_digest": self.workload_digest,
                    "replica_group_id": self.replica_group_id,
                    "replica_strategy": self.replica_strategy.value,
                    "quality_delta": self.quality_delta,
                    "coverage_delta": self.coverage_delta,
                    "cost_delta_usd": self.cost_delta_usd,
                    "wall_time_delta_ms": self.wall_time_delta_ms,
                    "decision": self.decision.value,
                    "reasons": self.reasons,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionReplicaValueAssessment:
    schema_version: str
    replica_group_id: str
    replica_strategy: ExecutionReplicaStrategy
    pair_count: int
    value_signal_count: int
    average_quality_delta: float
    average_coverage_delta: float
    decision: ExecutionReplicaQualificationDecision
    reasons: tuple[str, ...]
    pairs: tuple[ExecutionReplicaValuePair, ...]
    automatic_blueprint_change: bool = False


def compare_execution_replica_trials(
    baseline: ExecutionReplicaTrial,
    candidate: ExecutionReplicaTrial,
) -> ExecutionReplicaValuePair:
    """Compare one exact SINGLE/REPLICA pair under one shared hard cap."""

    if baseline.mode is not ExecutionTrialMode.SINGLE:
        raise ValueError("Replica value baseline must use SINGLE mode")
    if candidate.mode is not ExecutionTrialMode.REPLICA:
        raise ValueError("Replica value candidate must use REPLICA mode")
    for label, left, right in (
        ("workload", baseline.workload_digest, candidate.workload_digest),
        ("environment", baseline.environment_digest, candidate.environment_digest),
        (
            "employee capability",
            baseline.employee_capability_digest,
            candidate.employee_capability_digest,
        ),
        ("hard budget", baseline.budget.content_digest, candidate.budget.content_digest),
    ):
        if left != right:
            raise ValueError(f"Replica value pair has a different {label} identity")

    quality_delta = float(candidate.quality_score) - float(baseline.quality_score)
    coverage_delta = float(candidate.coverage_score) - float(baseline.coverage_score)
    reasons: list[str] = []
    new_safety = set(candidate.safety_violations) - set(baseline.safety_violations)
    rollback = False
    if new_safety:
        rollback = True
        reasons.append("candidate introduced a safety violation")
    if candidate.complete_failure and not baseline.complete_failure:
        rollback = True
        reasons.append("candidate introduced a complete failure")
    if baseline.task_success and not candidate.task_success:
        rollback = True
        reasons.append("candidate regressed task success")
    if baseline.validation_passed and not candidate.validation_passed:
        rollback = True
        reasons.append("candidate regressed validation")
    if quality_delta < -0.05 or coverage_delta < -0.05:
        rollback = True
        reasons.append("candidate materially regressed quality or coverage")

    value_signal = False
    if not rollback:
        if not baseline.task_success and candidate.task_success:
            value_signal = True
            reasons.append("candidate recovered task success")
        if quality_delta >= 0.05:
            value_signal = True
            reasons.append("candidate materially improved quality")
        if coverage_delta >= 0.10:
            value_signal = True
            reasons.append("candidate materially improved coverage")
        if (
            baseline.wall_time_ms > 0
            and candidate.wall_time_ms <= int(baseline.wall_time_ms * 0.80)
            and quality_delta >= 0
            and coverage_delta >= 0
            and candidate.task_success == baseline.task_success
        ):
            value_signal = True
            reasons.append("candidate materially reduced wall time without outcome regression")
    if not reasons:
        reasons.append("same-budget replica produced no material outcome gain")

    decision = (
        ExecutionReplicaPairDecision.ROLLBACK_CANDIDATE
        if rollback
        else (
            ExecutionReplicaPairDecision.VALUE_SIGNAL
            if value_signal
            else ExecutionReplicaPairDecision.NO_PROVEN_VALUE
        )
    )
    return ExecutionReplicaValuePair(
        baseline_trial_id=baseline.trial_id,
        candidate_trial_id=candidate.trial_id,
        workload_digest=baseline.workload_digest,
        replica_group_id=candidate.replica_group_id,
        replica_strategy=candidate.replica_strategy,
        quality_delta=round(quality_delta, 6),
        coverage_delta=round(coverage_delta, 6),
        cost_delta_usd=round(candidate.cost_usd - baseline.cost_usd, 6),
        wall_time_delta_ms=candidate.wall_time_ms - baseline.wall_time_ms,
        decision=decision,
        reasons=tuple(reasons),
    )


def assess_execution_replica_value(
    pairs: tuple[ExecutionReplicaValuePair, ...],
) -> ExecutionReplicaValueAssessment:
    """Require a small repeated cohort before recommending durable reuse."""

    if not pairs:
        raise ValueError("Replica value assessment requires at least one pair")
    group_ids = {pair.replica_group_id for pair in pairs}
    strategies = {pair.replica_strategy for pair in pairs}
    workload_ids = {pair.workload_digest for pair in pairs}
    if len(group_ids) != 1 or len(strategies) != 1:
        raise ValueError("Replica value assessment requires one exact group and strategy")
    if len(workload_ids) != len(pairs):
        raise ValueError("Replica value assessment requires distinct workload pairs")
    rollback_count = sum(
        pair.decision is ExecutionReplicaPairDecision.ROLLBACK_CANDIDATE
        for pair in pairs
    )
    signals = sum(
        pair.decision is ExecutionReplicaPairDecision.VALUE_SIGNAL for pair in pairs
    )
    average_quality = sum(pair.quality_delta for pair in pairs) / len(pairs)
    average_coverage = sum(pair.coverage_delta for pair in pairs) / len(pairs)
    reasons: list[str] = []
    if rollback_count:
        decision = ExecutionReplicaQualificationDecision.ROLLBACK_CANDIDATE
        reasons.append("at least one pair contains a safety, validation, success, or material outcome regression")
    elif len(pairs) < MINIMUM_QUALIFICATION_PAIRS:
        decision = ExecutionReplicaQualificationDecision.INSUFFICIENT_EVIDENCE
        reasons.append(
            f"{MINIMUM_QUALIFICATION_PAIRS} distinct comparable pairs are required before durable reuse"
        )
    elif (
        signals >= math.ceil(len(pairs) * 2 / 3)
        and average_quality >= 0
        and average_coverage >= 0
    ):
        decision = ExecutionReplicaQualificationDecision.KEEP
        reasons.append("same-budget value was reproduced in at least two thirds of the cohort")
    else:
        decision = ExecutionReplicaQualificationDecision.DISABLE
        reasons.append("the complete cohort did not reproduce material same-budget value")
    return ExecutionReplicaValueAssessment(
        schema_version=EXECUTION_REPLICA_ASSESSMENT_SCHEMA,
        replica_group_id=next(iter(group_ids)),
        replica_strategy=next(iter(strategies)),
        pair_count=len(pairs),
        value_signal_count=signals,
        average_quality_delta=round(average_quality, 6),
        average_coverage_delta=round(average_coverage, 6),
        decision=decision,
        reasons=tuple(reasons),
        pairs=pairs,
        automatic_blueprint_change=False,
    )


def execution_replica_trial_from_payload(payload: object) -> ExecutionReplicaTrial:
    if not isinstance(payload, Mapping):
        raise ValueError("Execution replica trial payload must be an object")
    raw_budget = payload.get("budget")
    if not isinstance(raw_budget, Mapping):
        raise ValueError("Execution replica trial budget must be an object")
    raw_strategy = payload.get("replica_strategy")
    return ExecutionReplicaTrial(
        trial_id=str(payload.get("trial_id", "")),
        workload_digest=str(payload.get("workload_digest", "")),
        environment_digest=str(payload.get("environment_digest", "")),
        employee_capability_digest=str(
            payload.get("employee_capability_digest", "")
        ),
        budget=ExecutionReplicaBudgetEnvelope(
            max_model_calls=int(raw_budget.get("max_model_calls", 0)),
            max_tool_calls=int(raw_budget.get("max_tool_calls", 0)),
            max_cost_usd=float(raw_budget.get("max_cost_usd", 0.0)),
            max_wall_time_ms=int(raw_budget.get("max_wall_time_ms", 0)),
        ),
        mode=ExecutionTrialMode(str(payload.get("mode", ""))),
        task_success=payload.get("task_success"),
        validation_passed=payload.get("validation_passed"),
        complete_failure=payload.get("complete_failure"),
        quality_score=float(payload.get("quality_score", -1)),
        coverage_score=float(payload.get("coverage_score", -1)),
        model_calls=int(payload.get("model_calls", -1)),
        tool_calls=int(payload.get("tool_calls", -1)),
        cost_usd=float(payload.get("cost_usd", -1)),
        wall_time_ms=int(payload.get("wall_time_ms", -1)),
        safety_violations=tuple(str(item) for item in payload.get("safety_violations", ())),
        replica_group_id=str(payload.get("replica_group_id", "")),
        replica_strategy=(
            None if raw_strategy is None else ExecutionReplicaStrategy(str(raw_strategy))
        ),
        aggregation_model_calls=int(payload.get("aggregation_model_calls", 0)),
        aggregation_tool_calls=int(payload.get("aggregation_tool_calls", 0)),
        aggregation_cost_usd=float(payload.get("aggregation_cost_usd", 0.0)),
        aggregation_wall_time_ms=int(payload.get("aggregation_wall_time_ms", 0)),
    )


def execution_replica_trial_from_active_job(
    inspection: object,
    *,
    trial_id: str,
    workload_digest: str,
    environment_digest: str,
    employee_capability_digest: str,
    quality_score: float,
    coverage_score: float,
    validation_passed: bool,
    wall_time_ms: int,
    replica_group_id: str = "",
    safety_violations: tuple[str, ...] = (),
    aggregation_model_calls: int = 0,
    aggregation_tool_calls: int = 0,
    aggregation_cost_usd: float = 0.0,
    aggregation_wall_time_ms: int = 0,
) -> ExecutionReplicaTrial:
    """Build a trial from a replay-verified terminal ACTIVE JOB projection.

    Quality, coverage, validation and elapsed time remain explicit evaluator
    observations because a terminal runtime status is not an oracle for real
    outcome quality.  Runtime-owned usage, hard limits, success state and
    replica structure are taken from the audit rather than retyped by hand.
    """

    if not bool(getattr(inspection, "replay_matches", False)):
        raise ValueError("Replica trial requires a replay-verified ACTIVE JOB")
    terminal = getattr(inspection, "terminal", None)
    if not isinstance(terminal, Mapping):
        raise ValueError("Replica trial requires a terminal ACTIVE JOB")
    raw_limits = getattr(inspection, "job_limits", None)
    if not isinstance(raw_limits, Mapping):
        raise ValueError("Replica trial requires the frozen Job hard limits")
    metrics = terminal.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("Replica trial terminal metrics are missing")
    usage = metrics.get("usage")
    if not isinstance(usage, Mapping):
        raise ValueError("Replica trial terminal usage is missing")
    raw_groups = getattr(inspection, "execution_replica_groups", ())
    if not isinstance(raw_groups, (tuple, list)):
        raise ValueError("Replica trial group projection is invalid")
    groups = tuple(item for item in raw_groups if isinstance(item, Mapping))
    selected_group: Mapping[str, object] | None = None
    if groups:
        if not replica_group_id and len(groups) == 1:
            replica_group_id = str(groups[0].get("group_id", ""))
        selected_group = next(
            (
                item
                for item in groups
                if str(item.get("group_id", "")) == replica_group_id
            ),
            None,
        )
        if selected_group is None:
            raise ValueError("Replica trial must select an exact audited replica group")
    elif replica_group_id:
        raise ValueError("SINGLE ACTIVE JOB does not contain the requested replica group")
    status = str(terminal.get("status", ""))
    reconstructed = tuple(getattr(inspection, "reconstructed_tasks", ()))
    complete_failure = status != "SUCCEEDED" and not any(
        isinstance(task, Mapping) and str(task.get("status", "")) == "SUCCEEDED"
        for task in reconstructed
    )
    strategy = (
        None
        if selected_group is None
        else ExecutionReplicaStrategy(str(selected_group.get("strategy", "")))
    )
    return ExecutionReplicaTrial(
        trial_id=trial_id,
        workload_digest=workload_digest,
        environment_digest=environment_digest,
        employee_capability_digest=employee_capability_digest,
        budget=ExecutionReplicaBudgetEnvelope(
            max_model_calls=int(raw_limits.get("max_total_model_calls", 0)),
            max_tool_calls=int(raw_limits.get("max_total_tool_calls", 0)),
            max_cost_usd=float(raw_limits.get("max_total_cost_usd", 0.0)),
            max_wall_time_ms=int(raw_limits.get("max_wall_time_ms", 0)),
        ),
        mode=(
            ExecutionTrialMode.SINGLE
            if selected_group is None
            else ExecutionTrialMode.REPLICA
        ),
        task_success=status == "SUCCEEDED",
        validation_passed=validation_passed,
        complete_failure=complete_failure,
        quality_score=quality_score,
        coverage_score=coverage_score,
        model_calls=int(usage.get("model_calls", 0)),
        tool_calls=int(usage.get("tool_calls", 0)),
        cost_usd=float(usage.get("cost_usd", 0.0)),
        wall_time_ms=wall_time_ms,
        safety_violations=safety_violations,
        replica_group_id=replica_group_id,
        replica_strategy=strategy,
        aggregation_model_calls=aggregation_model_calls,
        aggregation_tool_calls=aggregation_tool_calls,
        aggregation_cost_usd=aggregation_cost_usd,
        aggregation_wall_time_ms=aggregation_wall_time_ms,
    )
