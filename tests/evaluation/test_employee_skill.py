from __future__ import annotations

import unittest

from dynamic_firm.evaluation.employee_skill import run_employee_skill_evaluation


class EmployeeSkillEvaluationTests(unittest.TestCase):
    def test_skill_governance_contract_passes_without_provider(self) -> None:
        record = run_employee_skill_evaluation()

        self.assertTrue(record.passed)
        self.assertEqual(
            record.lifecycle,
            ("PROPOSED", "APPROVED", "APPLIED", "ROLLED_BACK"),
        )
        self.assertEqual(record.first_assessment, "INSUFFICIENT_OBSERVATION")
        self.assertEqual(record.keep_assessment, "KEEP")
        self.assertEqual(record.safety_assessment, "ROLLBACK_CANDIDATE")
        self.assertTrue(record.stale_apply_rejected)
        self.assertTrue(record.unsafe_content_rejected)
        self.assertEqual(record.provider_calls, 0)
        self.assertFalse(record.quota_consumed)


if __name__ == "__main__":
    unittest.main()
