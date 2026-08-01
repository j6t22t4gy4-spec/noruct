from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.application.modern_terminal_company_settings import (
    propose_settings_roster_revision,
    propose_settings_skill_patch,
)
from dynamic_firm.company import (
    CompanyStateStore,
    EmployeeSkillPatchService,
    decode_active_roster,
)
from dynamic_firm.kernel.models import EmployeeRecord


class ModernTerminalCompanySettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary.name) / "runtime.db"
        with CompanyStateStore(self.state_path) as store:
            store.ensure_roster_baseline(
                (EmployeeRecord("employee-analyst", "Analyst", ("analysis",)),)
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_roster_settings_creates_a_proposal_without_mutating_active_roster(self) -> None:
        result = propose_settings_roster_revision(
            self.state_path,
            {
                "employee_id": "employee-analyst",
                "model_profile": "fixture-model",
                "role": "Evidence Analyst",
                "capabilities": ("analysis", "review"),
                "rationale": "The operator wants a reviewed future profile.",
            },
            manager_only=False,
        )

        self.assertIn("ROSTER Patch proposed", "\n".join(result.messages))
        with CompanyStateStore(self.state_path) as store:
            roster = decode_active_roster(store.roster())
        employee = next(item for item in roster.employees if item.employee_id == "employee-analyst")
        self.assertEqual(employee.role, "Analyst")
        self.assertEqual(employee.capabilities, ("analysis",))

    def test_skill_settings_requires_an_explicit_proposal_not_an_applied_skill(self) -> None:
        result = propose_settings_skill_patch(
            self.state_path,
            {
                "employee_id": "employee-analyst",
                "skill_key": "evidence-review",
                "context_key": "repository",
                "purpose": "Review bounded evidence before reporting.",
                "steps": ("Read the bounded evidence.",),
                "verification_steps": ("Record the review result.",),
                "prohibitions": (),
                "correction_id": "operator-correction-1",
                "rationale": "The operator approved only a proposal.",
            },
        )

        self.assertIn("Skill Patch proposed", "\n".join(result.messages))
        with CompanyStateStore(self.state_path) as store:
            active = EmployeeSkillPatchService(store).runtime_snapshots(
                ("employee-analyst",), context_key="repository"
            )
        self.assertEqual(active["employee-analyst"], ())


if __name__ == "__main__":
    unittest.main()
