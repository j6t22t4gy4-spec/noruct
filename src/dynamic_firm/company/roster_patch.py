from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.runtime.models import to_primitive, utc_now

from .models import (
    RosterPatchCandidate,
    RosterPatchOperation,
    RosterPatchStatus,
    RosterVersion,
    content_digest,
)
from .roster import decode_active_roster
from .store import CompanyStateStore


class RosterPatchService:
    """Typed ROSTER proposals and explicit transitions; never approves or applies automatically."""

    def __init__(self, store: CompanyStateStore) -> None:
        self.store = store

    @staticmethod
    def _text(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"Roster Patch {field} must be non-empty")
        return normalized

    @classmethod
    def _capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(cls._text(value, "capability") for value in values)
        if not normalized:
            raise ValueError("Roster Patch requires at least one capability")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Roster Patch capabilities must be unique")
        return normalized

    @staticmethod
    def _payload(employee: EmployeeRecord) -> dict[str, Any]:
        payload = to_primitive(employee)
        assert isinstance(payload, dict)
        return payload

    @staticmethod
    def _current_employee(
        roster: RosterVersion,
        employee_id: str,
    ) -> tuple[int, dict[str, Any]]:
        for index, employee in enumerate(roster.employees):
            if employee.get("employee_id") == employee_id:
                return index, dict(employee)
        raise ValueError(f"ROSTER employee does not exist: {employee_id}")

    @staticmethod
    def _validate_after(
        base: RosterVersion,
        employees: tuple[Mapping[str, Any], ...],
    ) -> None:
        decode_active_roster(
            RosterVersion(
                revision=base.revision + 1,
                parent_revision=base.revision,
                employees=employees,
                created_at=utc_now().isoformat(),
            )
        )

    def _candidate(
        self,
        *,
        operation: RosterPatchOperation,
        base: RosterVersion,
        employee_id: str,
        before_employee: Mapping[str, Any] | None,
        after_employee: Mapping[str, Any],
        after_roster: tuple[Mapping[str, Any], ...],
        rationale: str,
        actor: str,
        evidence_ids: tuple[str, ...] = (),
        assessment_ids: tuple[str, ...] = (),
    ) -> RosterPatchCandidate:
        rationale = self._text(rationale, "rationale")
        actor = self._text(actor, "proposal actor")
        normalized_evidence = tuple(
            sorted(self._text(item, "evidence_id") for item in evidence_ids)
        )
        if len(normalized_evidence) != len(set(normalized_evidence)):
            raise ValueError("Roster Patch evidence ids must be unique")
        normalized_assessments = tuple(
            sorted(self._text(item, "assessment_id") for item in assessment_ids)
        )
        if len(normalized_assessments) != len(set(normalized_assessments)):
            raise ValueError("Roster Patch assessment ids must be unique")
        self._validate_after(base, after_roster)
        immutable = {
            "operation": operation,
            "base_roster_revision": base.revision,
            "employee_id": employee_id,
            "before_employee": before_employee,
            "after_employee": after_employee,
            "rationale": rationale,
            "proposed_by": actor,
        }
        if normalized_evidence:
            immutable["evidence_ids"] = normalized_evidence
        if normalized_assessments:
            immutable["assessment_ids"] = normalized_assessments
        digest = content_digest(immutable)
        now = utc_now().isoformat()
        return RosterPatchCandidate(
            patch_id=f"roster-patch-{digest[:24]}",
            status=RosterPatchStatus.PROPOSED,
            operation=operation,
            base_roster_revision=base.revision,
            employee_id=employee_id,
            before_employee=before_employee,
            after_employee=after_employee,
            rationale=rationale,
            proposed_by=actor,
            content_hash=digest,
            created_at=now,
            updated_at=now,
            evidence_ids=normalized_evidence,
            assessment_ids=normalized_assessments,
        )

    def propose_add_employee(
        self,
        employee: EmployeeRecord,
        *,
        rationale: str,
        actor: str,
        evidence_ids: tuple[str, ...] = (),
    ) -> RosterPatchCandidate:
        if employee.temporary:
            raise ValueError("Roster Patch cannot persist a temporary employee")
        employee_id = self._text(employee.employee_id, "employee_id")
        normalized = replace(
            employee,
            employee_id=employee_id,
            role=self._text(employee.role, "role"),
            capabilities=self._capabilities(employee.capabilities),
            model_profile=self._text(employee.model_profile, "model_profile"),
        )
        base = self.store.roster()
        if base.employees:
            decode_active_roster(base)
        if any(item.get("employee_id") == employee_id for item in base.employees):
            raise ValueError(f"ROSTER employee already exists: {employee_id}")
        after_employee = self._payload(normalized)
        after_roster = tuple(base.employees) + (after_employee,)
        candidate = self._candidate(
            operation=RosterPatchOperation.ADD_EMPLOYEE,
            base=base,
            employee_id=employee_id,
            before_employee=None,
            after_employee=after_employee,
            after_roster=after_roster,
            rationale=rationale,
            actor=actor,
            evidence_ids=evidence_ids,
        )
        return self.store.create_roster_patch(candidate, actor=actor)[0]

    def propose_set_active(
        self,
        employee_id: str,
        active: bool,
        *,
        rationale: str,
        actor: str,
        assessment_ids: tuple[str, ...] = (),
    ) -> RosterPatchCandidate:
        if type(active) is not bool:
            raise ValueError("Roster Patch active value must be boolean")
        employee_id = self._text(employee_id, "employee_id")
        base = self.store.roster()
        decode_active_roster(base)
        index, before = self._current_employee(base, employee_id)
        if before.get("active") is active:
            raise ValueError(f"ROSTER employee active state is already {str(active).lower()}")
        after = {**before, "active": active}
        employees = list(base.employees)
        employees[index] = after
        candidate = self._candidate(
            operation=RosterPatchOperation.SET_ACTIVE,
            base=base,
            employee_id=employee_id,
            before_employee=before,
            after_employee=after,
            after_roster=tuple(employees),
            rationale=rationale,
            actor=actor,
            assessment_ids=assessment_ids,
        )
        return self.store.create_roster_patch(candidate, actor=actor)[0]

    def propose_set_capabilities(
        self,
        employee_id: str,
        capabilities: tuple[str, ...],
        *,
        rationale: str,
        actor: str,
    ) -> RosterPatchCandidate:
        employee_id = self._text(employee_id, "employee_id")
        capabilities = self._capabilities(capabilities)
        base = self.store.roster()
        decode_active_roster(base)
        index, before = self._current_employee(base, employee_id)
        if tuple(before.get("capabilities", ())) == capabilities:
            raise ValueError("ROSTER employee already has the proposed capabilities")
        after = {**before, "capabilities": list(capabilities)}
        employees = list(base.employees)
        employees[index] = after
        candidate = self._candidate(
            operation=RosterPatchOperation.SET_CAPABILITIES,
            base=base,
            employee_id=employee_id,
            before_employee=before,
            after_employee=after,
            after_roster=tuple(employees),
            rationale=rationale,
            actor=actor,
        )
        return self.store.create_roster_patch(candidate, actor=actor)[0]

    def propose_update_employee(
        self,
        employee: EmployeeRecord,
        *,
        rationale: str,
        actor: str,
    ) -> RosterPatchCandidate:
        """Propose one exact Employee revision through the ordinary ROSTER lifecycle.

        This is deliberately a same-identity revision.  It lets an operator change a
        Manager's bounded runtime profile (or roll it back) without silently
        replacing a running Job's frozen Employee snapshot.
        """

        employee_id = self._text(employee.employee_id, "employee_id")
        normalized = replace(
            employee,
            employee_id=employee_id,
            role=self._text(employee.role, "role"),
            capabilities=self._capabilities(employee.capabilities),
            model_profile=self._text(employee.model_profile, "model_profile"),
        )
        base = self.store.roster()
        decode_active_roster(base)
        index, before = self._current_employee(base, employee_id)
        after = self._payload(normalized)
        if before == after:
            raise ValueError("ROSTER employee already matches the proposed revision")
        employees = list(base.employees)
        employees[index] = after
        candidate = self._candidate(
            operation=RosterPatchOperation.UPDATE_EMPLOYEE,
            base=base,
            employee_id=employee_id,
            before_employee=before,
            after_employee=after,
            after_roster=tuple(employees),
            rationale=rationale,
            actor=actor,
        )
        return self.store.create_roster_patch(candidate, actor=actor)[0]

    def preview(self, patch_id: str) -> RosterPatchCandidate:
        return self.store.get_roster_patch(patch_id)

    def list(self) -> tuple[RosterPatchCandidate, ...]:
        return self.store.list_roster_patches()

    def approve(self, patch_id: str, *, actor: str) -> RosterPatchCandidate:
        return self.store.approve_roster_patch(patch_id, actor)

    def apply(self, patch_id: str, *, actor: str) -> RosterPatchCandidate:
        return self.store.apply_roster_patch(patch_id, actor)

    def reject(
        self,
        patch_id: str,
        *,
        actor: str,
        reason: str,
    ) -> RosterPatchCandidate:
        return self.store.reject_roster_patch(patch_id, actor, reason)
