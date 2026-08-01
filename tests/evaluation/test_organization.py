from __future__ import annotations

import unittest
import json
from pathlib import Path

from dynamic_firm.evaluation.organization import (
    FixtureKind,
    StrategyKind,
    records_to_json,
    run_evaluation,
    run_matrix,
)
from dynamic_firm.kernel.models import JobStatus


class OrganizationEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_captures_minimal_staffing_parallelism_and_replan_tradeoffs(self) -> None:
        records = await run_matrix()
        indexed = {(item.fixture, item.strategy): item for item in records}

        dynamic_solo = indexed[(FixtureKind.SOLO, StrategyKind.DYNAMIC)]
        self.assertEqual(dynamic_solo.status, JobStatus.SUCCEEDED)
        self.assertEqual(dynamic_solo.employee_count, 1)
        self.assertEqual(dynamic_solo.graph_mutations, 0)

        dynamic_parallel = indexed[(FixtureKind.PARALLEL, StrategyKind.DYNAMIC)]
        self.assertEqual(dynamic_parallel.quality_score, 1.0)
        self.assertEqual(dynamic_parallel.maximum_parallelism, 2)
        self.assertEqual(dynamic_parallel.employee_count, 2)

        dynamic_replan = indexed[(FixtureKind.REPLAN, StrategyKind.DYNAMIC)]
        self.assertEqual(dynamic_replan.quality_score, 1.0)
        self.assertEqual(dynamic_replan.temporary_role_count, 1)
        self.assertEqual(dynamic_replan.graph_mutations, 1)
        self.assertEqual(dynamic_replan.final_graph_version, 2)

        solo_replan = indexed[(FixtureKind.REPLAN, StrategyKind.SOLO)]
        fixed_replan = indexed[(FixtureKind.REPLAN, StrategyKind.FIXED)]
        self.assertLess(solo_replan.quality_score, dynamic_replan.quality_score)
        self.assertEqual(fixed_replan.quality_score, dynamic_replan.quality_score)
        self.assertGreater(fixed_replan.employee_count, dynamic_replan.employee_count)
        self.assertEqual(fixed_replan.unnecessary_role_count, 1)

    async def test_matrix_json_is_deterministic(self) -> None:
        first = records_to_json(await run_matrix())
        second = records_to_json(await run_matrix())
        self.assertEqual(first, second)
        self.assertIn('"fixture": "parallel"', first)
        self.assertIn('"strategy": "dynamic"', first)
        snapshot_path = (
            Path(__file__).parents[2]
            / "tests"
            / "fixtures"
            / "public_evaluation"
            / "organization-matrix.json"
        )
        self.assertEqual(json.loads(first), json.loads(snapshot_path.read_text(encoding="utf-8")))

    async def test_single_evaluation_accepts_cli_string_values(self) -> None:
        record = await run_evaluation("solo", "dynamic")
        self.assertEqual(record.fixture, FixtureKind.SOLO)
        self.assertEqual(record.strategy, StrategyKind.DYNAMIC)


if __name__ == "__main__":
    unittest.main()
