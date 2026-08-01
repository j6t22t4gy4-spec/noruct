from __future__ import annotations

import unittest
import io
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from dynamic_firm.evaluation.execution_replica_value import (
    ExecutionReplicaBudgetEnvelope,
    ExecutionReplicaPairDecision,
    ExecutionReplicaQualificationDecision,
    ExecutionReplicaTrial,
    ExecutionTrialMode,
    assess_execution_replica_value,
    compare_execution_replica_trials,
    execution_replica_trial_from_payload,
    execution_replica_trial_from_active_job,
)
from dynamic_firm.kernel.models import ExecutionReplicaStrategy
from dynamic_firm.cli import _run_graph_command, build_parser


def baseline(index: int = 1) -> ExecutionReplicaTrial:
    return ExecutionReplicaTrial(
        trial_id=f"single-{index}",
        workload_digest=f"{index:064x}",
        environment_digest="a" * 64,
        employee_capability_digest="b" * 64,
        budget=ExecutionReplicaBudgetEnvelope(
            max_model_calls=8,
            max_tool_calls=8,
            max_cost_usd=2.0,
            max_wall_time_ms=30_000,
        ),
        mode=ExecutionTrialMode.SINGLE,
        task_success=True,
        validation_passed=True,
        complete_failure=False,
        quality_score=0.70,
        coverage_score=0.60,
        model_calls=2,
        tool_calls=2,
        cost_usd=0.40,
        wall_time_ms=10_000,
    )


def candidate(index: int = 1) -> ExecutionReplicaTrial:
    return ExecutionReplicaTrial(
        trial_id=f"replica-{index}",
        workload_digest=f"{index:064x}",
        environment_digest="a" * 64,
        employee_capability_digest="b" * 64,
        budget=baseline(index).budget,
        mode=ExecutionTrialMode.REPLICA,
        task_success=True,
        validation_passed=True,
        complete_failure=False,
        quality_score=0.78,
        coverage_score=0.72,
        model_calls=5,
        tool_calls=4,
        cost_usd=0.90,
        wall_time_ms=7_500,
        replica_group_id="release-surfaces",
        replica_strategy=ExecutionReplicaStrategy.PARTITION,
        aggregation_model_calls=1,
        aggregation_tool_calls=0,
        aggregation_cost_usd=0.10,
        aggregation_wall_time_ms=500,
    )


class ExecutionReplicaValueTests(unittest.TestCase):
    def test_same_budget_pair_records_value_without_automatic_blueprint_change(self) -> None:
        pair = compare_execution_replica_trials(baseline(), candidate())
        assessment = assess_execution_replica_value((pair,))

        self.assertEqual(pair.decision, ExecutionReplicaPairDecision.VALUE_SIGNAL)
        self.assertEqual(
            assessment.decision,
            ExecutionReplicaQualificationDecision.INSUFFICIENT_EVIDENCE,
        )
        self.assertFalse(assessment.automatic_blueprint_change)

    def test_three_distinct_pairs_can_qualify_reproduced_value(self) -> None:
        pairs = tuple(
            compare_execution_replica_trials(baseline(index), candidate(index))
            for index in (1, 2, 3)
        )

        assessment = assess_execution_replica_value(pairs)

        self.assertEqual(assessment.decision, ExecutionReplicaQualificationDecision.KEEP)
        self.assertEqual(assessment.value_signal_count, 3)

    def test_safety_regression_is_fail_fast(self) -> None:
        unsafe = replace(candidate(), safety_violations=("unexpected external effect",))
        pair = compare_execution_replica_trials(baseline(), unsafe)
        assessment = assess_execution_replica_value((pair,))

        self.assertEqual(
            pair.decision,
            ExecutionReplicaPairDecision.ROLLBACK_CANDIDATE,
        )
        self.assertEqual(
            assessment.decision,
            ExecutionReplicaQualificationDecision.ROLLBACK_CANDIDATE,
        )

    def test_pair_rejects_different_budget_identity(self) -> None:
        changed_budget = replace(
            candidate(),
            budget=ExecutionReplicaBudgetEnvelope(
                max_model_calls=9,
                max_tool_calls=8,
                max_cost_usd=2.0,
                max_wall_time_ms=30_000,
            ),
        )
        with self.assertRaisesRegex(ValueError, "different hard budget"):
            compare_execution_replica_trials(baseline(), changed_budget)

    def test_payload_parser_round_trips_canonical_trial(self) -> None:
        original = candidate()
        parsed = execution_replica_trial_from_payload(original.canonical_payload())
        self.assertEqual(parsed.content_digest, original.content_digest)

    def test_cli_evaluates_three_pairs_without_opening_company_state(self) -> None:
        with TemporaryDirectory() as directory:
            arguments = ["graph", "replica-evaluate"]
            for index in (1, 2, 3):
                single_path = Path(directory) / f"single-{index}.json"
                replica_path = Path(directory) / f"replica-{index}.json"
                single_path.write_text(
                    json.dumps(baseline(index).canonical_payload()),
                    encoding="utf-8",
                )
                replica_path.write_text(
                    json.dumps(candidate(index).canonical_payload()),
                    encoding="utf-8",
                )
                arguments.extend(
                    ["--pair", str(single_path), str(replica_path)]
                )
            args = build_parser().parse_args(arguments)
            output = io.StringIO()
            result = _run_graph_command(args, {}, output)

        self.assertEqual(result, 0)
        self.assertIn("Replica value · release-surfaces · PARTITION · KEEP", output.getvalue())
        self.assertIn("no Blueprint revision", output.getvalue())

    def test_active_job_adapter_uses_audited_usage_limits_and_replica_structure(self) -> None:
        inspection = SimpleNamespace(
            replay_matches=True,
            terminal={
                "status": "SUCCEEDED",
                "metrics": {
                    "usage": {
                        "model_calls": 5,
                        "tool_calls": 4,
                        "cost_usd": 0.9,
                    }
                },
            },
            job_limits={
                "max_total_model_calls": 8,
                "max_total_tool_calls": 8,
                "max_total_cost_usd": 2.0,
                "max_wall_time_ms": 30_000,
            },
            execution_replica_groups=(
                {
                    "group_id": "release-surfaces",
                    "strategy": "PARTITION",
                    "aggregation_task_id": "final",
                    "aggregation": "JOIN",
                    "member_task_ids": ("runtime", "delivery"),
                },
            ),
            reconstructed_tasks=(
                {"task_id": "runtime", "status": "SUCCEEDED"},
                {"task_id": "delivery", "status": "SUCCEEDED"},
                {"task_id": "final", "status": "SUCCEEDED"},
            ),
        )

        trial = execution_replica_trial_from_active_job(
            inspection,
            trial_id="replica-audit",
            workload_digest="c" * 64,
            environment_digest="a" * 64,
            employee_capability_digest="b" * 64,
            quality_score=0.78,
            coverage_score=0.72,
            validation_passed=True,
            wall_time_ms=7_500,
            aggregation_model_calls=1,
            aggregation_cost_usd=0.1,
            aggregation_wall_time_ms=500,
        )

        self.assertEqual(trial.mode, ExecutionTrialMode.REPLICA)
        self.assertEqual(trial.replica_group_id, "release-surfaces")
        self.assertEqual(trial.model_calls, 5)
        self.assertEqual(trial.budget.max_model_calls, 8)


if __name__ == "__main__":
    unittest.main()
