"""Content-free outcome metrics for one completed organization episode.

The ACTIVE JOB ledger stays authoritative.  This module is deliberately a
read-only projection: it never retries, edits a graph, changes a lease, or
stores objective/tool/result content.  Unknown evidence remains ``None`` or
``NOT_OBSERVED`` instead of being guessed from a successful terminal status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class OrganizationOutcomeMetrics:
    """Bounded comparable facts, not a causal claim about organization value."""

    time_to_first_runnable_ms: int | None = None
    blueprint_outcome: str = "NOT_SELECTED"
    initial_final_graph_distance: int | None = None
    reserved_model_call_delta: int | None = None
    model_call_budget_variance: int | None = None
    user_override_outcome: str = "NOT_OBSERVED"
    user_override_reason: str = "NOT_OBSERVED"
    recovery_outcome: str = "NOT_OBSERVED"

    def __post_init__(self) -> None:
        if self.time_to_first_runnable_ms is not None and self.time_to_first_runnable_ms < 0:
            raise ValueError("time-to-first-runnable must be non-negative")
        if self.initial_final_graph_distance is not None and self.initial_final_graph_distance < 0:
            raise ValueError("graph distance must be non-negative")
        if self.reserved_model_call_delta is not None and self.reserved_model_call_delta < 0:
            raise ValueError("reserved model-call delta must be non-negative")
        if self.blueprint_outcome not in {"NOT_SELECTED", "BOUND", "REUSED", "REJECTED"}:
            raise ValueError("blueprint outcome is invalid")
        if self.user_override_outcome not in {"NOT_OBSERVED", "ACCEPTED", "PENDING", "REJECTED"}:
            raise ValueError("user override outcome is invalid")
        if self.recovery_outcome not in {"NOT_OBSERVED", "NOT_REQUIRED", "SUCCEEDED", "FAILED"}:
            raise ValueError("recovery outcome is invalid")


@dataclass(frozen=True, slots=True)
class OrganizationMetricReport:
    episode_count: int
    observed_time_to_first_runnable_count: int
    median_time_to_first_runnable_ms: int | None
    blueprint_outcomes: Mapping[str, int]
    observed_graph_distance_count: int
    total_graph_distance: int
    reserved_model_call_delta: int
    observed_model_call_budget_variance_count: int
    total_model_call_budget_variance: int
    user_override_outcomes: Mapping[str, int]
    recovery_outcomes: Mapping[str, int]
    graph_proposal_decisions: Mapping[str, int]


def organization_metric_report(episodes: Iterable[object]) -> OrganizationMetricReport:
    """Aggregate stored episode facts; unknown remains outside every denominator."""

    items = tuple(episodes)
    first = sorted(
        int(value)
        for item in items
        if (value := getattr(item, "time_to_first_runnable_ms", None)) is not None
    )
    distances = [
        int(value)
        for item in items
        if (value := getattr(item, "initial_final_graph_distance", None)) is not None
    ]
    def counts(attribute: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in items:
            key = str(getattr(item, attribute, "NOT_OBSERVED"))
            result[key] = result.get(key, 0) + 1
        return dict(sorted(result.items()))
    return OrganizationMetricReport(
        episode_count=len(items),
        observed_time_to_first_runnable_count=len(first),
        median_time_to_first_runnable_ms=(first[len(first) // 2] if first else None),
        blueprint_outcomes=counts("blueprint_outcome"),
        observed_graph_distance_count=len(distances),
        total_graph_distance=sum(distances),
        reserved_model_call_delta=sum(
            int(getattr(item, "reserved_model_call_delta", 0) or 0) for item in items
        ),
        observed_model_call_budget_variance_count=sum(
            getattr(item, "model_call_budget_variance", None) is not None for item in items
        ),
        total_model_call_budget_variance=sum(
            int(getattr(item, "model_call_budget_variance", 0) or 0) for item in items
        ),
        user_override_outcomes=counts("user_override_outcome"),
        recovery_outcomes=counts("recovery_outcome"),
        graph_proposal_decisions={
            "APPROVED": sum(
                int(getattr(item, "graph_proposal_approved_count", 0) or 0)
                for item in items
            ),
            "REJECTED": sum(
                int(getattr(item, "graph_proposal_rejected_count", 0) or 0)
                for item in items
            ),
            "UNAVAILABLE": sum(
                int(getattr(item, "graph_proposal_unavailable_count", 0) or 0)
                for item in items
            ),
        },
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def organization_outcome_metrics(
    inspection: object | None,
    *,
    operator_signals: Sequence[Mapping[str, object]] = (),
) -> OrganizationOutcomeMetrics:
    """Project one replay-verified audit without exposing its task content."""

    if inspection is None or not bool(getattr(inspection, "replay_matches", False)):
        return OrganizationOutcomeMetrics()
    created_at = _parse_time(getattr(inspection, "created_at", None))
    run_times = tuple(
        value
        for value in (
            _parse_time(getattr(item, "created_at", None))
            for item in tuple(getattr(inspection, "runtime_runs", ()))
        )
        if value is not None
    )
    first_runnable = (
        max(0, int((min(run_times) - created_at).total_seconds() * 1000))
        if created_at is not None and run_times
        else None
    )
    blueprint_id = str(getattr(inspection, "graph_blueprint_id", ""))
    # ACTIVE JOB stores a Blueprint only after a selected immutable registry
    # revision was bound into this Work Order, so this is a true registry reuse
    # receipt rather than a guessed similarity match.
    blueprint_outcome = "REUSED" if blueprint_id else "NOT_SELECTED"
    # The durable patch payload has only accepted operation facts.  Counting
    # added/cancelled/rebound dependency/capability operations is an exact
    # lower-bound structural distance; malformed historical payloads yield
    # unknown rather than an invented number.
    distance = 0
    reserved_calls = 0
    valid_patches = True
    for payload in tuple(getattr(inspection, "graph_patches", ())):
        if not isinstance(payload, Mapping):
            valid_patches = False
            break
        patch = payload.get("patch")
        lease = payload.get("mutation_lease", {})
        if not isinstance(patch, Mapping) or not isinstance(lease, Mapping):
            valid_patches = False
            break
        operations = patch.get("operations", ())
        if not isinstance(operations, (tuple, list)):
            valid_patches = False
            break
        distance += len(operations)
        value = lease.get("model_calls", 0)
        if type(value) is not int or value < 0:
            valid_patches = False
            break
        reserved_calls += value
    override_outcome = "NOT_OBSERVED"
    override_reason = "NOT_OBSERVED"
    if operator_signals:
        statuses = {str(item.get("status", "")) for item in operator_signals}
        override_outcome = (
            "ACCEPTED" if "CONSUMED" in statuses else "PENDING" if "PENDING" in statuses else "REJECTED"
        )
        # References are user content.  Preserve only the typed route/result.
        override_reason = "USER_CORRECTION"
    audit_status = str(getattr(getattr(inspection, "audit_status", None), "value", ""))
    terminal = getattr(inspection, "terminal", None)
    recovery = "NOT_REQUIRED" if audit_status == "TERMINAL" else "NOT_OBSERVED"
    if isinstance(terminal, Mapping) and bool(terminal.get("automatic_resume", False)):
        recovery = "SUCCEEDED" if str(terminal.get("status", "")) == "SUCCEEDED" else "FAILED"
    limits = getattr(inspection, "job_limits", {})
    terminal_metrics = terminal.get("metrics", {}) if isinstance(terminal, Mapping) else {}
    usage = terminal_metrics.get("usage", {}) if isinstance(terminal_metrics, Mapping) else {}
    limit = limits.get("max_total_model_calls") if isinstance(limits, Mapping) else None
    actual = usage.get("model_calls") if isinstance(usage, Mapping) else None
    return OrganizationOutcomeMetrics(
        time_to_first_runnable_ms=first_runnable,
        blueprint_outcome=blueprint_outcome,
        initial_final_graph_distance=distance if valid_patches else None,
        reserved_model_call_delta=reserved_calls if valid_patches else None,
        model_call_budget_variance=(actual - limit if type(actual) is int and type(limit) is int else None),
        user_override_outcome=override_outcome,
        user_override_reason=override_reason,
        recovery_outcome=recovery,
    )
