from __future__ import annotations

import unittest

from dynamic_firm.evaluation.task_mutation import run_task_mutation_evaluation


class TaskMutationEvaluationTests(unittest.TestCase):
    def test_offline_trajectory_covers_retry_reroute_refusal_and_replay(self) -> None:
        record = run_task_mutation_evaluation()

        self.assertTrue(record.passed)
        self.assertEqual(record.retry.mutations, ("RETRY",))
        self.assertEqual(record.reroute.mutations, ("REROUTE",))
        self.assertEqual(record.retry_exhaustion.task_attempts, (1, 2))
        self.assertEqual(record.reroute_cycle.employees, ("analyst-a", "analyst-b"))
        self.assertIn("UNKNOWN", record.refusal_failure_kinds)
        self.assertTrue(record.deterministic_replay)
        self.assertEqual(record.provider_calls, 0)
        self.assertFalse(record.quota_consumed)


if __name__ == "__main__":
    unittest.main()
