from __future__ import annotations

import unittest

from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.kernel.mutation import frozen_snapshot_digest
from dynamic_firm.runtime.job_ledger import ActiveJobAuditStatus, ActiveJobInspector, SQLiteActiveJobLedger
from dynamic_firm.runtime.store import RunStore
from tests.kernel.helpers import company_request, task


class ActiveJobArtifactPinTests(unittest.TestCase):
    @staticmethod
    def _request():
        return company_request(
            (task("inspect"),),
            final_task_id="inspect",
            roster=(EmployeeRecord("analyst", "Analyst", ("analysis",)),),
        )

    def test_inspection_projects_only_valid_content_free_artifact_pins(self) -> None:
        request = self._request()
        graph = graph_from_proposal(request.plan_proposal, max_tasks=16)
        pin = {
            "kind": "SKILL_PACKAGE",
            "artifact_id": "repository_skill",
            "version": "1.0.0",
            "manifest_digest": "a" * 64,
            "scope_key": "company_default",
        }
        store = RunStore()
        SQLiteActiveJobLedger(
            store,
            evolution_artifact_pins=(pin,),
        ).start_job(request, graph, frozen_snapshot_digest(request))

        inspection = ActiveJobInspector(store).inspect(request.job_id)

        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.INTERRUPTED)
        self.assertEqual(inspection.evolution_artifact_pins, (pin,))
        self.assertTrue(inspection.replay_matches)
        store.close()

    def test_malformed_artifact_pin_invalidates_the_audit_instead_of_disappearing(self) -> None:
        request = self._request()
        graph = graph_from_proposal(request.plan_proposal, max_tasks=16)
        store = RunStore()
        SQLiteActiveJobLedger(
            store,
            evolution_artifact_pins=(
                {
                    "kind": "SKILL_PACKAGE",
                    "artifact_id": "repository_skill",
                    "version": "1.0.0",
                    "manifest_digest": "not-a-digest",
                    "scope_key": "company_default",
                },
            ),
        ).start_job(request, graph, frozen_snapshot_digest(request))

        inspection = ActiveJobInspector(store).inspect(request.job_id)

        self.assertEqual(inspection.audit_status, ActiveJobAuditStatus.INVALID)
        self.assertFalse(inspection.replay_matches)
        self.assertIn("snapshot Evolution Artifact pin invalid", inspection.errors)
        store.close()


if __name__ == "__main__":
    unittest.main()
