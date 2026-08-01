import unittest
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.manager_fallback_policy import (
    GRAPH_UNCHANGED_EVIDENCE,
    ManagerFallbackDecision,
    ManagerFallbackPolicy,
    ManagerFallbackReason,
    evaluate_manager_fallback_policy,
    project_manager_fallback,
)


class ManagerFallbackPolicyTests(unittest.TestCase):
    def test_negative_transfer_is_terminal_strong_solo(self):
        evidence = project_manager_fallback(negative_transfer=True)

        self.assertEqual(evidence.decision, ManagerFallbackDecision.STRONG_SOLO)
        self.assertEqual(
            evidence.reason,
            ManagerFallbackReason.NEGATIVE_TRANSFER_STRONG_SOLO,
        )
        self.assertTrue(evidence.terminal)
        self.assertFalse(evidence.retry_allowed)
        self.assertFalse(evidence.loop_allowed)
        self.assertFalse(evidence.graph_changed)
        self.assertEqual(evidence.graph_evidence, GRAPH_UNCHANGED_EVIDENCE)

    def test_exhausted_bound_is_terminal_and_names_bound(self):
        policy = ManagerFallbackPolicy(max_review_loops=1)

        evidence = evaluate_manager_fallback_policy(policy, review_loops=1)

        self.assertEqual(evidence.decision, ManagerFallbackDecision.STRONG_SOLO)
        self.assertEqual(
            evidence.reason,
            ManagerFallbackReason.BOUND_EXHAUSTED_STRONG_SOLO,
        )
        self.assertEqual(evidence.exhausted_bound, "review_loops")
        self.assertTrue(evidence.terminal)
        self.assertFalse(evidence.retry_allowed)
        self.assertFalse(evidence.loop_allowed)
        self.assertFalse(evidence.graph_changed)
        self.assertEqual(evidence.graph_evidence, GRAPH_UNCHANGED_EVIDENCE)

    def test_policy_and_terminal_evidence_are_immutable(self):
        policy = ManagerFallbackPolicy()
        evidence = project_manager_fallback(negative_transfer=True)

        with self.assertRaises(AttributeError):
            policy.max_replans = 3
        with self.assertRaises(AttributeError):
            evidence.terminal = False

    def test_unexhausted_evidence_does_not_fallback(self):
        evidence = project_manager_fallback(
            planning_calls=1,
            supervision_calls=1,
            integration_calls=1,
            review_loops=0,
            reassignments=0,
            replans=0,
            wall_time_seconds=299,
        )

        self.assertEqual(evidence.decision, ManagerFallbackDecision.CONTINUE_MANAGER)
        self.assertFalse(evidence.terminal)
        self.assertFalse(evidence.graph_changed)


if __name__ == "__main__":
    unittest.main()
