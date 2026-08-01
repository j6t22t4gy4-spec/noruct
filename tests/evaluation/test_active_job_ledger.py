from __future__ import annotations

import unittest

from dynamic_firm.evaluation.active_job_ledger import (
    run_active_job_ledger_evaluation,
)
from dynamic_firm.runtime.job_ledger import ActiveJobAuditStatus
from dynamic_firm.runtime.store import SCHEMA_VERSION as RUNTIME_SCHEMA_VERSION


class ActiveJobLedgerEvaluationTests(unittest.TestCase):
    def test_offline_ledger_covers_reopen_relation_interruption_tamper_and_privacy(self) -> None:
        record = run_active_job_ledger_evaluation()

        self.assertTrue(record.passed)
        self.assertEqual(record.retry.audit_status, ActiveJobAuditStatus.TERMINAL)
        self.assertEqual(record.reroute.audit_status, ActiveJobAuditStatus.TERMINAL)
        self.assertEqual(
            record.interrupted.audit_status,
            ActiveJobAuditStatus.INTERRUPTED,
        )
        self.assertTrue(record.relation_refused)
        self.assertTrue(record.tamper_detected)
        self.assertTrue(record.privacy_projection_passed)
        self.assertEqual(record.runtime_schema_version, RUNTIME_SCHEMA_VERSION)
        self.assertEqual(record.company_schema_version, 9)
        self.assertEqual(record.provider_calls, 0)
        self.assertFalse(record.quota_consumed)


if __name__ == "__main__":
    unittest.main()
