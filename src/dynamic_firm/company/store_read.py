"""Immutable COMPANY, ROSTER and PLAYBOOK projections.

This mixin keeps read models separate from the Company store's event and
mutation operations while retaining the same SQLite connection, lock and
revision pointers. It is deliberately not a cache or a second state authority.
"""

from __future__ import annotations

import json
from typing import Any

from .models import (
    CompanyStateSummary,
    CompanyVersion,
    EmployeeSkillPatchStatus,
    PlaybookVersion,
    RosterPatchStatus,
    RosterVersion,
    WorkflowPatchStatus,
    workflow_pattern_from_dict,
)


def _loads(raw: str) -> Any:
    return json.loads(raw)


class CompanyReadProjectionMixin:
    """Read-only revision and aggregate projections composed into the Store."""

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM company_state_meta WHERE key = 'schema_version'"
            ).fetchone()
        if row is None:
            raise RuntimeError("Missing company state schema version")
        return int(row["value"])

    def company(self) -> CompanyVersion:
        with self._lock:
            revision = self._active_revision("active_company_revision")
            row = self._conn.execute(
                "SELECT * FROM company_versions WHERE revision = ?", (revision,)
            ).fetchone()
        assert row is not None
        return CompanyVersion(
            revision=revision,
            parent_revision=row["parent_revision"],
            purpose=str(row["purpose"]),
            policies=_loads(row["policies_json"]),
            created_at=str(row["created_at"]),
        )

    def roster(self) -> RosterVersion:
        with self._lock:
            revision = self._active_revision("active_roster_revision")
            row = self._conn.execute(
                "SELECT * FROM roster_versions WHERE revision = ?", (revision,)
            ).fetchone()
        assert row is not None
        return RosterVersion(
            revision=revision,
            parent_revision=row["parent_revision"],
            employees=tuple(_loads(row["employees_json"])),
            created_at=str(row["created_at"]),
        )

    def roster_at_revision(self, revision: int) -> RosterVersion:
        """Read one immutable ROSTER revision without changing active state."""

        if type(revision) is not int or revision < 1:
            raise ValueError("ROSTER revision must be a positive integer")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM roster_versions WHERE revision = ?", (revision,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown ROSTER revision: {revision}")
        return RosterVersion(
            revision=revision,
            parent_revision=row["parent_revision"],
            employees=tuple(_loads(row["employees_json"])),
            created_at=str(row["created_at"]),
        )

    def playbook(self, revision: int | None = None) -> PlaybookVersion:
        with self._lock:
            selected = revision or self._active_revision("active_playbook_revision")
            row = self._conn.execute(
                "SELECT * FROM playbook_versions WHERE revision = ?", (selected,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown playbook revision: {selected}")
        return PlaybookVersion(
            revision=selected,
            parent_revision=row["parent_revision"],
            patterns=tuple(workflow_pattern_from_dict(item) for item in _loads(row["patterns_json"])),
            source_patch_id=row["source_patch_id"],
            rolled_back_from_revision=row["rolled_back_from_revision"],
            created_at=str(row["created_at"]),
        )

    def summary(self) -> CompanyStateSummary:
        company = self.company()
        roster = self.roster()
        playbook = self.playbook()
        with self._lock:
            count = lambda table: int(self._conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
            episode_count = count("organization_episodes")
            staffing_demand_count = count("staffing_demand_evidence")
            hire_observation_contract_count = count("hire_observation_contracts")
            hire_observation_count = count("hire_observations")
            hire_assessment_count = count("hire_assessments")
            retention_review_count = count("roster_retention_reviews")
            company_policy_event_count = count("company_policy_events")
            employee_skill_count = int(self._conn.execute(
                """
                SELECT COUNT(*) AS count FROM employee_skill_heads AS heads
                JOIN employee_skill_versions AS versions
                  ON versions.version_id = heads.current_version_id
                WHERE versions.active = 1
                """
            ).fetchone()["count"])
            employee_skill_observation_count = count("employee_skill_observations")
            employee_skill_assessment_count = count("employee_skill_assessments")
            verified_live_pair_count = count("verified_live_evidence_pairs")
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM workflow_patch_candidates GROUP BY status"
            ).fetchall()
            roster_rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM roster_patch_candidates GROUP BY status"
            ).fetchall()
            employee_skill_patch_rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM employee_skill_patch_candidates GROUP BY status"
            ).fetchall()
        counts = {status.value: 0 for status in WorkflowPatchStatus}
        counts.update({str(row["status"]): int(row["count"]) for row in rows})
        roster_counts = {status.value: 0 for status in RosterPatchStatus}
        roster_counts.update({str(row["status"]): int(row["count"]) for row in roster_rows})
        employee_skill_patch_counts = {status.value: 0 for status in EmployeeSkillPatchStatus}
        employee_skill_patch_counts.update(
            {str(row["status"]): int(row["count"]) for row in employee_skill_patch_rows}
        )
        return CompanyStateSummary(
            company_revision=company.revision,
            roster_revision=roster.revision,
            playbook_revision=playbook.revision,
            employee_count=len(roster.employees),
            active_employee_count=sum(1 for employee in roster.employees if employee.get("active", True) is True),
            workflow_pattern_count=len(playbook.patterns),
            episode_count=episode_count,
            staffing_demand_count=staffing_demand_count,
            hire_observation_contract_count=hire_observation_contract_count,
            hire_observation_count=hire_observation_count,
            hire_assessment_count=hire_assessment_count,
            retention_review_mode=self.retention_review_mode(),
            retention_review_count=retention_review_count,
            company_policy_event_count=company_policy_event_count,
            employee_skill_count=employee_skill_count,
            employee_skill_patch_counts=employee_skill_patch_counts,
            employee_skill_observation_count=employee_skill_observation_count,
            employee_skill_assessment_count=employee_skill_assessment_count,
            verified_live_pair_count=verified_live_pair_count,
            patch_counts=counts,
            roster_patch_counts=roster_counts,
        )
