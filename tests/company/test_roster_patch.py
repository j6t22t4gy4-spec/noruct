from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.company import (
    CompanyStateStore,
    RosterPatchOperation,
    RosterPatchService,
    RosterPatchStatus,
    RosterSnapshotError,
    decode_active_roster,
)
from dynamic_firm.kernel.models import EmployeeRecord


def employee(
    employee_id: str,
    role: str,
    capabilities: tuple[str, ...],
) -> EmployeeRecord:
    return EmployeeRecord(
        employee_id=employee_id,
        role=role,
        capabilities=capabilities,
        model_profile="roster-default",
    )


class RosterPatchLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "runtime.db"
        self.store = CompanyStateStore(self.path)
        self.store.ensure_roster_baseline(
            (
                employee("employee-generalist", "Generalist", ("conversation",)),
                employee(
                    "employee-analyst",
                    "Repository Analyst",
                    ("repository_analysis",),
                ),
            )
        )
        self.service = RosterPatchService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_add_preview_approve_apply_preserves_running_snapshot_and_restart(self) -> None:
        running_snapshot = decode_active_roster(self.store.roster())
        patch = self.service.propose_add_employee(
            employee(
                "employee-security-reviewer",
                "Security Reviewer",
                ("security_review", "evidence_synthesis"),
            ),
            rationale="Repeated goals need independent security review.",
            actor="user:test",
        )

        self.assertEqual(patch.status, RosterPatchStatus.PROPOSED)
        self.assertEqual(patch.operation, RosterPatchOperation.ADD_EMPLOYEE)
        self.assertIsNone(patch.before_employee)
        self.assertEqual(self.store.roster().revision, 2)
        with self.assertRaisesRegex(ValueError, "approved before apply"):
            self.service.apply(patch.patch_id, actor="user:test")

        approved = self.service.approve(patch.patch_id, actor="user:test")
        applied = self.service.apply(patch.patch_id, actor="user:test")
        next_snapshot = decode_active_roster(self.store.roster())

        self.assertEqual(approved.status, RosterPatchStatus.APPROVED)
        self.assertEqual(applied.status, RosterPatchStatus.APPLIED)
        self.assertEqual(applied.applied_revision, 3)
        self.assertEqual(running_snapshot.revision, 2)
        self.assertEqual(running_snapshot.active_employee_count, 2)
        self.assertEqual(next_snapshot.revision, 3)
        self.assertEqual(next_snapshot.active_employee_count, 3)
        self.assertIn(
            "employee-security-reviewer",
            {item.employee_id for item in next_snapshot.employees},
        )
        self.assertEqual(
            [
                event.event_type.value
                for event in self.store.list_roster_patch_events(patch.patch_id)
            ],
            ["PROPOSED", "APPROVED", "APPLIED"],
        )

        self.store.close()
        self.store = CompanyStateStore(self.path)
        self.service = RosterPatchService(self.store)
        restarted = decode_active_roster(self.store.roster())
        self.assertEqual(restarted.revision, 3)
        self.assertEqual(restarted.active_employee_count, 3)

    def test_set_active_and_capabilities_create_new_validated_revisions(self) -> None:
        capability_patch = self.service.propose_set_capabilities(
            "employee-analyst",
            ("repository_analysis", "evidence_synthesis"),
            rationale="The analyst now owns evidence synthesis procedures.",
            actor="user:test",
        )
        self.service.approve(capability_patch.patch_id, actor="user:test")
        self.service.apply(capability_patch.patch_id, actor="user:test")

        inactive_patch = self.service.propose_set_active(
            "employee-analyst",
            False,
            rationale="Temporarily remove the analyst from staffing.",
            actor="user:test",
        )
        self.service.approve(inactive_patch.patch_id, actor="user:test")
        self.service.apply(inactive_patch.patch_id, actor="user:test")
        snapshot = decode_active_roster(self.store.roster())

        self.assertEqual(capability_patch.operation, RosterPatchOperation.SET_CAPABILITIES)
        self.assertEqual(inactive_patch.operation, RosterPatchOperation.SET_ACTIVE)
        self.assertEqual(self.store.roster().revision, 4)
        self.assertEqual(snapshot.active_employee_count, 1)
        self.assertEqual(snapshot.total_employee_count, 2)
        with self.assertRaisesRegex(RosterSnapshotError, "at least one active"):
            self.service.propose_set_active(
                "employee-generalist",
                False,
                rationale="This must be rejected before proposal persistence.",
                actor="user:test",
            )

    def test_stale_patch_cannot_overwrite_a_newer_roster_revision(self) -> None:
        first = self.service.propose_add_employee(
            employee("employee-reviewer-a", "Reviewer A", ("review_a",)),
            rationale="First independent proposal.",
            actor="user:test",
        )
        second = self.service.propose_add_employee(
            employee("employee-reviewer-b", "Reviewer B", ("review_b",)),
            rationale="Second independent proposal.",
            actor="user:test",
        )
        for patch in (first, second):
            self.service.approve(patch.patch_id, actor="user:test")

        self.service.apply(first.patch_id, actor="user:test")
        with self.assertRaisesRegex(ValueError, "ROSTER changed since proposal"):
            self.service.apply(second.patch_id, actor="user:test")

        snapshot = decode_active_roster(self.store.roster())
        self.assertEqual(snapshot.revision, 3)
        self.assertIn("employee-reviewer-a", {item.employee_id for item in snapshot.employees})
        self.assertNotIn("employee-reviewer-b", {item.employee_id for item in snapshot.employees})
        self.assertEqual(
            [event.event_type.value for event in self.store.list_roster_patch_events(second.patch_id)],
            ["PROPOSED", "APPROVED"],
        )

    def test_duplicate_proposal_is_idempotent_and_rejection_is_audited(self) -> None:
        kwargs = {
            "rationale": "One explicit bounded proposal.",
            "actor": "user:test",
        }
        first = self.service.propose_add_employee(
            employee("employee-reviewer", "Reviewer", ("review",)),
            **kwargs,
        )
        duplicate = self.service.propose_add_employee(
            employee("employee-reviewer", "Reviewer", ("review",)),
            **kwargs,
        )
        rejected = self.service.reject(
            first.patch_id,
            actor="user:test",
            reason="The role is not justified yet.",
        )

        self.assertEqual(first.patch_id, duplicate.patch_id)
        self.assertEqual(rejected.status, RosterPatchStatus.REJECTED)
        self.assertEqual(self.store.roster().revision, 2)
        events = self.store.list_roster_patch_events(first.patch_id)
        self.assertEqual([event.event_type.value for event in events], ["PROPOSED", "REJECTED"])
        self.assertEqual(events[-1].payload["reason"], "The role is not justified yet.")

    def test_schema_v4_migrates_without_changing_active_company_state(self) -> None:
        before = self.store.summary()
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                DROP TABLE roster_patch_events;
                DROP TABLE roster_patch_candidates;
                UPDATE company_state_meta SET value = '4' WHERE key = 'schema_version';
                """
            )
        self.store = CompanyStateStore(self.path)
        self.service = RosterPatchService(self.store)

        after = self.store.summary()
        self.assertEqual(self.store.schema_version(), 9)
        self.assertEqual(after.company_revision, before.company_revision)
        self.assertEqual(after.roster_revision, before.roster_revision)
        self.assertEqual(after.playbook_revision, before.playbook_revision)
        self.assertEqual(self.store.list_roster_patches(), ())

    def test_content_hash_detects_tampered_candidate_payload(self) -> None:
        patch = self.service.propose_add_employee(
            employee("employee-reviewer", "Reviewer", ("review",)),
            rationale="Tamper-evidence fixture.",
            actor="user:test",
        )
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM roster_patch_candidates WHERE patch_id = ?",
                (patch.patch_id,),
            ).fetchone()
            assert row is not None
            payload = json.loads(row[0])
            payload["after_employee"]["role"] = "Tampered Role"
            connection.execute(
                "UPDATE roster_patch_candidates SET payload_json = ? WHERE patch_id = ?",
                (json.dumps(payload), patch.patch_id),
            )
        self.store = CompanyStateStore(self.path)
        self.service = RosterPatchService(self.store)

        with self.assertRaisesRegex(ValueError, "content hash mismatch"):
            self.store.get_roster_patch(patch.patch_id)


if __name__ == "__main__":
    unittest.main()
