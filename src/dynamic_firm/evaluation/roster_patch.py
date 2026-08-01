from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from dynamic_firm.company import (
    CompanyStateStore,
    RosterPatchService,
    decode_active_roster,
)
from dynamic_firm.kernel.models import EmployeeRecord


@dataclass(frozen=True, slots=True)
class RosterPatchEvaluationCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class RosterPatchEvaluationRecord:
    schema_version: str
    evidence_class: str
    operation: str
    initial_roster_revision: int
    running_job_roster_revision: int
    applied_roster_revision: int
    next_job_roster_revision: int
    restarted_roster_revision: int
    initial_active_employees: int
    next_active_employees: int
    lifecycle: tuple[str, ...]
    stale_apply_rejected: bool
    automatic_proposal: bool
    automatic_apply: bool
    provider_calls: int
    quota_consumed: bool
    checks: tuple[RosterPatchEvaluationCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def _employee(
    employee_id: str,
    role: str,
    capabilities: tuple[str, ...],
) -> EmployeeRecord:
    return EmployeeRecord(
        employee_id=employee_id,
        role=role,
        capabilities=capabilities,
        model_profile="company-default",
    )


def run_roster_patch_evaluation() -> RosterPatchEvaluationRecord:
    """Exercise explicit ROSTER revision authority without a model or provider."""

    with tempfile.TemporaryDirectory(prefix="noruct-roster-patch-") as directory:
        path = Path(directory) / "runtime.db"
        with CompanyStateStore(path) as store:
            store.ensure_roster_baseline(
                (
                    _employee("employee-generalist", "Generalist", ("conversation",)),
                    _employee(
                        "employee-analyst",
                        "Repository Analyst",
                        ("repository_analysis",),
                    ),
                )
            )
            service = RosterPatchService(store)
            running = decode_active_roster(store.roster())
            selected = service.propose_add_employee(
                _employee(
                    "employee-security-reviewer",
                    "Security Reviewer",
                    ("security_review", "evidence_synthesis"),
                ),
                rationale="Offline governance fixture for one explicit hiring decision.",
                actor="user:evaluation",
            )
            stale = service.propose_add_employee(
                _employee(
                    "employee-stale-reviewer",
                    "Stale Reviewer",
                    ("stale_review",),
                ),
                rationale="Independent same-base proposal used to verify stale refusal.",
                actor="user:evaluation",
            )
            service.approve(selected.patch_id, actor="user:evaluation")
            service.approve(stale.patch_id, actor="user:evaluation")
            applied = service.apply(selected.patch_id, actor="user:evaluation")
            stale_rejected = False
            try:
                service.apply(stale.patch_id, actor="user:evaluation")
            except ValueError as exc:
                stale_rejected = "ROSTER changed since proposal" in str(exc)
            next_job = decode_active_roster(store.roster())
            lifecycle = tuple(
                event.event_type.value
                for event in store.list_roster_patch_events(selected.patch_id)
            )
            active_revision = store.roster().revision

        with CompanyStateStore(path) as restarted_store:
            restarted = decode_active_roster(restarted_store.roster())

        checks = (
            RosterPatchEvaluationCheck(
                "proposal_does_not_change_active_roster",
                running.revision == selected.base_roster_revision == 2,
                f"running=r{running.revision},base=r{selected.base_roster_revision}",
            ),
            RosterPatchEvaluationCheck(
                "explicit_lifecycle_is_complete",
                lifecycle == ("PROPOSED", "APPROVED", "APPLIED"),
                "→".join(lifecycle),
            ),
            RosterPatchEvaluationCheck(
                "running_job_snapshot_is_frozen",
                running.revision == 2 and running.active_employee_count == 2,
                f"r{running.revision}:{running.active_employee_count}",
            ),
            RosterPatchEvaluationCheck(
                "next_job_uses_new_roster",
                next_job.revision == 3
                and next_job.active_employee_count == 3
                and any(
                    item.employee_id == "employee-security-reviewer"
                    for item in next_job.employees
                ),
                f"r{next_job.revision}:{next_job.active_employee_count}",
            ),
            RosterPatchEvaluationCheck(
                "stale_same_base_apply_is_rejected",
                stale_rejected and active_revision == 3,
                f"rejected={stale_rejected},active=r{active_revision}",
            ),
            RosterPatchEvaluationCheck(
                "restart_restores_active_roster",
                restarted.revision == 3 and restarted.active_employee_count == 3,
                f"r{restarted.revision}:{restarted.active_employee_count}",
            ),
        )
        return RosterPatchEvaluationRecord(
            schema_version="noruct.roster-patch-evaluation.v1",
            evidence_class="offline-governance-fixture",
            operation=selected.operation.value,
            initial_roster_revision=2,
            running_job_roster_revision=running.revision,
            applied_roster_revision=int(applied.applied_revision or 0),
            next_job_roster_revision=next_job.revision,
            restarted_roster_revision=restarted.revision,
            initial_active_employees=running.active_employee_count,
            next_active_employees=next_job.active_employee_count,
            lifecycle=lifecycle,
            stale_apply_rejected=stale_rejected,
            automatic_proposal=False,
            automatic_apply=False,
            provider_calls=0,
            quota_consumed=False,
            checks=checks,
        )
