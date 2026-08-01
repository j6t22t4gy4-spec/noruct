from __future__ import annotations

import io
import json
import unittest

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.roster_patch import run_roster_patch_evaluation


class RosterPatchEvaluationTests(unittest.TestCase):
    def test_offline_evaluation_proves_revision_snapshot_and_stale_guards(self) -> None:
        record = run_roster_patch_evaluation()

        self.assertTrue(record.passed)
        self.assertEqual(record.lifecycle, ("PROPOSED", "APPROVED", "APPLIED"))
        self.assertEqual(record.running_job_roster_revision, 2)
        self.assertEqual(record.next_job_roster_revision, 3)
        self.assertEqual(record.restarted_roster_revision, 3)
        self.assertEqual(record.next_active_employees, 3)
        self.assertTrue(record.stale_apply_rejected)
        self.assertFalse(record.automatic_proposal)
        self.assertFalse(record.automatic_apply)
        self.assertEqual(record.provider_calls, 0)
        self.assertFalse(record.quota_consumed)

    def test_cli_exposes_stable_roster_patch_evaluation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        exit_code = main(["eval", "roster", "--json"], stdout=output, stderr=error)
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["schema_version"], "noruct.roster-patch-evaluation.v1")
        self.assertEqual(payload["evidence_class"], "offline-governance-fixture")
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["quota_consumed"])
        self.assertTrue(all(check["passed"] for check in payload["checks"]))


if __name__ == "__main__":
    unittest.main()
