from __future__ import annotations

import unittest

from dynamic_firm.company.manager import PersistentExecutiveManager
from dynamic_firm.company.manager_report import manager_operating_report
from dynamic_firm.company.models import EmployeeSkillProcedure, EmployeeSkillVersion
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.product.operator_surface import _manager_projection


def _skill(
    *,
    employee_id: str,
    skill_key: str,
    revision: int,
    active: bool = True,
) -> EmployeeSkillVersion:
    procedure = EmployeeSkillProcedure(
        employee_id=employee_id,
        skill_key=skill_key,
        context_key="company-management",
        purpose="Private procedure purpose must not reach the operator surface.",
        steps=("Private step.",),
        verification_steps=("Private verification.",),
    )
    return EmployeeSkillVersion(
        version_id=f"skill-version-{employee_id}-{skill_key}-{revision}",
        employee_id=employee_id,
        skill_key=skill_key,
        context_key="company-management",
        revision=revision,
        active=active,
        procedure=procedure,
        source_patch_id=f"skill-patch-{skill_key}",
        content_hash=("a" * 64),
        created_at="2026-07-29T00:00:00+00:00",
    )


class ManagerReportTests(unittest.TestCase):
    def test_manager_report_projects_only_its_active_skill_heads(self) -> None:
        manager = PersistentExecutiveManager.from_roster(
            (
                EmployeeRecord(
                    "employee-executive-manager",
                    "Executive Manager",
                    ("company_management",),
                    model_profile="company-default",
                ),
            ),
            roster_revision=1,
        )
        report = manager_operating_report(
            manager,
            (),
            skill_versions=(
                _skill(
                    employee_id="employee-executive-manager",
                    skill_key="staffing",
                    revision=3,
                ),
                _skill(
                    employee_id="employee-specialist",
                    skill_key="implementation",
                    revision=2,
                ),
                _skill(
                    employee_id="employee-executive-manager",
                    skill_key="retired",
                    revision=1,
                    active=False,
                ),
            ),
        )
        assert report is not None
        self.assertEqual(len(report.skill_heads), 1)
        self.assertEqual(report.skill_heads[0].skill_key, "staffing")
        projection = _manager_projection(report)
        self.assertEqual(projection["skill_head_count"], 1)
        self.assertEqual(projection["skill_heads"][0]["revision"], 3)
        self.assertNotIn("Private procedure", repr(projection))
        self.assertNotIn("steps", projection["skill_heads"][0])
