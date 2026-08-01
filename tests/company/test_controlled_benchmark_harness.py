from __future__ import annotations

import asyncio
import math
import time
import unittest
from dataclasses import replace

from dynamic_firm.company.controlled_benchmark_harness import (
    BenchmarkStrategy,
    ComparisonMatrix,
    ControlledBenchmarkHarness,
    ControlledScenarioEnvelope,
    DataEgressClass,
    ObservationAvailability,
    SyntheticStrategyResult,
)
from dynamic_firm.company.manager import ManagerDelegation
from dynamic_firm.kernel.models import (
    EmployeeRecord,
    ExecutionReplicaAggregation,
    ExecutionReplicaSpec,
    ExecutionReplicaStrategy,
    JobLimits,
)
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from tests.kernel.helpers import company_request, task


def digest(character: str) -> str:
    return character * 64


def envelope(**changes: str) -> ControlledScenarioEnvelope:
    values = {
        "scenario_id": "synthetic-local-scenario",
        "task_digest": digest("a"),
        "tool_envelope_digest": digest("b"),
        "context_envelope_digest": digest("c"),
        "resource_envelope_digest": digest("d"),
    }
    values.update(changes)
    return ControlledScenarioEnvelope(**values)


def result(strategy: BenchmarkStrategy, *, scenario: ControlledScenarioEnvelope | None = None, **changes: object) -> SyntheticStrategyResult:
    values: dict[str, object] = {
        "strategy": strategy,
        "envelope": scenario or envelope(),
        "quality": 0.75,
        "complete_failure": False,
        "cost_availability": ObservationAvailability.AVAILABLE,
        "cost_usd": 0.0,
        "latency_availability": ObservationAvailability.AVAILABLE,
        "latency_ms": 0.0,
        "error_correlation": -0.25,
        "data_egress_class": DataEgressClass.INTERNAL,
        "human_review_minutes": 0.0,
    }
    values.update(changes)
    return SyntheticStrategyResult(**values)


def all_results(scenario: ControlledScenarioEnvelope | None = None) -> tuple[SyntheticStrategyResult, ...]:
    return tuple(result(strategy, scenario=scenario) for strategy in reversed(tuple(BenchmarkStrategy)))


class ControlledBenchmarkHarnessTest(unittest.TestCase):
    def test_same_envelope_four_strategy_run_is_reproducible_without_rank(self) -> None:
        scenario = envelope()
        harness = ControlledBenchmarkHarness(scenario)
        first = harness.evaluate(all_results(scenario))
        second = harness.compare(reversed(all_results(scenario)))

        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual([row.strategy for row in first.rows], sorted(BenchmarkStrategy, key=lambda item: item.value))
        self.assertNotIn("winner", first.canonical_payload())
        self.assertNotIn("rank", first.canonical_json())
        self.assertEqual(first.rows[0].error_correlation, -0.25)

    def test_rejects_different_task_tool_context_or_resource_envelope(self) -> None:
        expected = envelope()
        harness = ControlledBenchmarkHarness(expected)
        for field, value in (
            ("task_digest", digest("e")),
            ("tool_envelope_digest", digest("e")),
            ("context_envelope_digest", digest("e")),
            ("resource_envelope_digest", digest("e")),
        ):
            with self.subTest(field=field):
                mismatched = envelope(**{field: value})
                rows = list(all_results(expected))
                rows[0] = result(rows[0].strategy, scenario=mismatched)
                with self.assertRaisesRegex(ValueError, "identical scenario envelope"):
                    harness.compare(rows)

    def test_zero_and_unavailable_cost_latency_remain_distinct(self) -> None:
        scenario = envelope()
        zero = result(BenchmarkStrategy.STRONG_SOLO, scenario=scenario)
        unknown = result(
            BenchmarkStrategy.SAME_MODEL_BEST_OF_N,
            scenario=scenario,
            cost_availability=ObservationAvailability.UNAVAILABLE,
            cost_usd=None,
            latency_availability=ObservationAvailability.UNAVAILABLE,
            latency_ms=None,
        )

        self.assertEqual(zero.cost_usd, 0.0)
        self.assertEqual(zero.latency_ms, 0.0)
        self.assertIsNone(unknown.cost_usd)
        self.assertIsNone(unknown.latency_ms)
        self.assertEqual(unknown.cost_availability, ObservationAvailability.UNAVAILABLE)
        self.assertEqual(unknown.latency_availability, ObservationAvailability.UNAVAILABLE)

    def test_rejects_nan_inf_and_unknown_availability(self) -> None:
        for field, value in (
            ("quality", math.nan),
            ("cost_usd", math.inf),
            ("latency_ms", -math.inf),
            ("error_correlation", math.nan),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    result(BenchmarkStrategy.STRONG_SOLO, **{field: value})
        with self.assertRaises(ValueError):
            result(BenchmarkStrategy.STRONG_SOLO, cost_availability="UNKNOWN")

    def test_rejects_duplicate_or_incomplete_strategy_results(self) -> None:
        scenario = envelope()
        harness = ControlledBenchmarkHarness(scenario)
        duplicate = list(all_results(scenario))
        duplicate[0] = result(BenchmarkStrategy.STRONG_SOLO, scenario=scenario)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            harness.compare(duplicate)
        with self.assertRaisesRegex(ValueError, "one result"):
            harness.compare(all_results(scenario)[:3])

    def test_canonical_round_trip_and_noncanonical_serialization_rejection(self) -> None:
        matrix = ControlledBenchmarkHarness(envelope()).compare(all_results(envelope()))
        # Equivalent but independently constructed envelopes have the same frozen identity.
        restored = ComparisonMatrix.from_canonical_json(matrix.canonical_json())
        self.assertEqual(restored, matrix)
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            ComparisonMatrix.from_canonical_json(matrix.canonical_json() + " ")

    def test_provider_free_kernel_shapes_fill_all_four_controlled_arms(self) -> None:
        """Exercise actual strategy shapes without a quality/winner claim."""

        async def run(request, outcomes):  # type: ignore[no-untyped-def]
            started = time.monotonic()
            result = await FirmKernel(
                employee_execution=ScriptedEmployeeExecutionPort(outcomes)
            ).run(request)
            return result, max(0.0, (time.monotonic() - started) * 1000)

        async def exercise():  # type: ignore[no-untyped-def]
            solo = company_request(
                (task("final", capabilities=("analysis",)),),
                final_task_id="final",
                roster=(EmployeeRecord("solo", "Solo", ("analysis",), model_profile="same-model"),),
            )
            candidate = lambda task_id, replica_id: replace(
                task(task_id, capabilities=("analysis",)),
                execution_replica=ExecutionReplicaSpec(
                    group_id="best-of-n",
                    replica_id=replica_id,
                    strategy=ExecutionReplicaStrategy.CANDIDATE,
                    scope="one bounded benchmark proposal",
                    aggregation_task_id="select",
                    aggregation=ExecutionReplicaAggregation.VALIDATOR_SELECT,
                    marginal_value_reason="Independent same-model candidates require one validator selection.",
                ),
            )
            best_of_n = company_request(
                (
                    candidate("candidate-a", "a"),
                    candidate("candidate-b", "b"),
                    task("select", depends_on=("candidate-a", "candidate-b"), capabilities=("validation",)),
                ),
                final_task_id="select",
                roster=(EmployeeRecord("same-model", "Same Model", ("analysis", "validation"), model_profile="same-model"),),
                limits=JobLimits(max_concurrency=2, max_wall_time_ms=5_000),
            )
            heterogeneous = company_request(
                (
                    task("research", capabilities=("analysis",)),
                    task("review", capabilities=("review",)),
                    task("final", depends_on=("research", "review"), capabilities=("integration",)),
                ),
                final_task_id="final",
                roster=(
                    EmployeeRecord("analyst", "Analyst", ("analysis",), model_profile="model-a"),
                    EmployeeRecord("reviewer", "Reviewer", ("review",), model_profile="model-b"),
                    EmployeeRecord("integrator", "Integrator", ("integration",), model_profile="model-c"),
                ),
            )
            manager = EmployeeRecord("manager", "Manager", ("company_management",), model_profile="manager-model")
            manager_request = replace(
                company_request(
                    (
                        task("research", capabilities=("analysis",)),
                        task("final", depends_on=("research",), capabilities=("integration",)),
                    ),
                    final_task_id="final",
                    roster=(
                        EmployeeRecord("analyst", "Analyst", ("analysis",)),
                        EmployeeRecord("integrator", "Integrator", ("integration",)),
                    ),
                ),
                company_work_mode="TEAM_JOB",
                work_order_id="benchmark-manager-order",
                work_order_digest="a" * 64,
                manager_employee_id=manager.employee_id,
                manager_assignment_digest="b" * 64,
                manager_session_key="manager:benchmark:session",
                manager_employee=manager,
            )
            delegation = ManagerDelegation.from_proposal_payload(
                assignment_digest=manager_request.manager_assignment_digest,
                manager_employee_id=manager.employee_id,
                work_order_id=manager_request.work_order_id,
                work_order_digest=manager_request.work_order_digest,
                proposal=manager_request.plan_proposal,
            )
            manager_request = replace(
                manager_request,
                manager_delegation_payload=delegation.canonical_payload(),
                manager_delegation_digest=delegation.content_digest,
            )
            return {
                BenchmarkStrategy.STRONG_SOLO: await run(solo, {"final": ScriptedOutcome("solo")}),
                BenchmarkStrategy.SAME_MODEL_BEST_OF_N: await run(
                    best_of_n,
                    {
                        "candidate-a": ScriptedOutcome("candidate-a"),
                        "candidate-b": ScriptedOutcome("candidate-b"),
                        "select": ScriptedOutcome("selected"),
                    },
                ),
                BenchmarkStrategy.HETEROGENEOUS_MULTI_PROVIDER: await run(
                    heterogeneous,
                    {"research": ScriptedOutcome("research"), "review": ScriptedOutcome("review"), "final": ScriptedOutcome("final")},
                ),
                BenchmarkStrategy.MANAGER_LED: await run(
                    manager_request,
                    {"research": ScriptedOutcome("research"), ("final", "manager"): ScriptedOutcome("manager")},
                ),
            }

        outcomes = asyncio.run(exercise())
        self.assertEqual(outcomes[BenchmarkStrategy.STRONG_SOLO][0].metrics.unique_employee_count, 1)
        self.assertEqual(outcomes[BenchmarkStrategy.SAME_MODEL_BEST_OF_N][0].metrics.execution_replica_count, 2)
        self.assertGreaterEqual(outcomes[BenchmarkStrategy.HETEROGENEOUS_MULTI_PROVIDER][0].metrics.unique_employee_count, 2)
        self.assertEqual(outcomes[BenchmarkStrategy.MANAGER_LED][0].manager_employee_id, "manager")

        scenario = envelope(scenario_id="provider-free-kernel-four-arm")
        rows = tuple(
            SyntheticStrategyResult(
                strategy=strategy,
                envelope=scenario,
                # The scripted path proves execution shape, not quality.
                quality=0.0,
                complete_failure=result.status.value != "SUCCEEDED",
                cost_availability=ObservationAvailability.UNAVAILABLE,
                cost_usd=None,
                latency_availability=ObservationAvailability.AVAILABLE,
                latency_ms=elapsed,
                # A one-run-per-arm local fixture cannot estimate a correlation.
                error_correlation=0.0,
                data_egress_class=DataEgressClass.INTERNAL,
                human_review_minutes=0.0,
            )
            for strategy, (result, elapsed) in outcomes.items()
        )
        matrix = ControlledBenchmarkHarness(scenario).compare(rows)
        self.assertEqual({row.strategy for row in matrix.rows}, set(BenchmarkStrategy))
        self.assertTrue(all(row.quality == 0.0 for row in matrix.rows))
        self.assertNotIn("winner", matrix.canonical_payload())


if __name__ == "__main__":
    unittest.main()
