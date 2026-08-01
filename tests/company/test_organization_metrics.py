from __future__ import annotations

from dataclasses import asdict, dataclass
import unittest
from types import SimpleNamespace

from dynamic_firm.company import OrganizationOutcomeMetrics, organization_metric_report, organization_outcome_metrics


@dataclass(frozen=True)
class _Run:
    created_at: str


@dataclass(frozen=True)
class _Audit:
    replay_matches: bool = True
    created_at: str = "2026-07-28T00:00:00+00:00"
    runtime_runs: tuple[object, ...] = ()
    graph_blueprint_id: str = ""
    graph_patches: tuple[object, ...] = ()
    audit_status: object = type("Status", (), {"value": "TERMINAL"})()
    terminal: object = None
    job_limits: object = None


class OrganizationMetricTests(unittest.TestCase):
    def test_outcome_metrics_are_content_free_and_unknown_when_not_observed(self) -> None:
        metrics = organization_outcome_metrics(_Audit())
        self.assertIsNone(metrics.time_to_first_runnable_ms)
        self.assertEqual(metrics.blueprint_outcome, "NOT_SELECTED")
        self.assertEqual(metrics.initial_final_graph_distance, 0)
        self.assertEqual(metrics.user_override_outcome, "NOT_OBSERVED")
        self.assertEqual(metrics.recovery_outcome, "NOT_REQUIRED")

    def test_outcome_metrics_projects_existing_audit_receipts_only(self) -> None:
        audit = _Audit(
            runtime_runs=(_Run("2026-07-28T00:00:01.250000+00:00"),),
            graph_blueprint_id="blueprint-evidence",
            graph_patches=(
                {
                    "patch": {"operations": ({"kind": "ADD_TASK"}, {"kind": "SET_FINAL_TASK"})},
                    "mutation_lease": {"model_calls": 1},
                },
            ),
        )
        metrics = organization_outcome_metrics(
            audit,
            operator_signals=(
                {"status": "CONSUMED", "reference": "must never leave store"},
            ),
        )
        self.assertEqual(metrics.time_to_first_runnable_ms, 1250)
        self.assertEqual(metrics.blueprint_outcome, "REUSED")
        self.assertEqual(metrics.initial_final_graph_distance, 2)
        self.assertEqual(metrics.reserved_model_call_delta, 1)
        self.assertEqual(metrics.user_override_outcome, "ACCEPTED")
        self.assertEqual(metrics.user_override_reason, "USER_CORRECTION")

    def test_budget_variance_uses_only_terminal_usage_and_frozen_cap(self) -> None:
        audit = _Audit(
            terminal={"metrics": {"usage": {"model_calls": 5}}},
            job_limits={"max_total_model_calls": 6},
        )
        self.assertEqual(organization_outcome_metrics(audit).model_call_budget_variance, -1)

    def test_report_excludes_unknown_latency_from_median(self) -> None:
        first = SimpleNamespace(
            **asdict(OrganizationOutcomeMetrics(time_to_first_runnable_ms=3)),
            graph_proposal_approved_count=1,
            graph_proposal_rejected_count=2,
            graph_proposal_unavailable_count=0,
        )
        second = SimpleNamespace(
            **asdict(OrganizationOutcomeMetrics(time_to_first_runnable_ms=9)),
            graph_proposal_approved_count=0,
            graph_proposal_rejected_count=0,
            graph_proposal_unavailable_count=1,
        )
        report = organization_metric_report((first, second, object()))
        self.assertEqual(report.episode_count, 3)
        self.assertEqual(report.observed_time_to_first_runnable_count, 2)
        self.assertEqual(report.median_time_to_first_runnable_ms, 9)
        self.assertEqual(
            report.graph_proposal_decisions,
            {"APPROVED": 1, "REJECTED": 2, "UNAVAILABLE": 1},
        )
