from __future__ import annotations

import unittest

from dynamic_firm.evaluation.retention_review import (
    run_retention_review_evaluation,
)


class RetentionReviewEvaluationTests(unittest.TestCase):
    def test_three_modes_and_hard_stale_guard_pass(self) -> None:
        record = run_retention_review_evaluation()

        self.assertTrue(record.passed)
        self.assertEqual(record.manual_decision, "PENDING_USER_APPROVAL")
        self.assertEqual(record.auto_review_decision, "AUTO_APPROVED")
        self.assertEqual(
            record.auto_review_safety_decision,
            "REQUIRES_USER_APPROVAL",
        )
        self.assertEqual(record.always_approve_decision, "APPROVAL_BYPASSED")
        self.assertTrue(record.stale_apply_rejected)
        self.assertEqual(record.provider_calls, 0)
        self.assertFalse(record.quota_consumed)
