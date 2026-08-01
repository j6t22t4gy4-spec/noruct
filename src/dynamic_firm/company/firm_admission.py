"""Deterministic Firm-level admission above one request-scoped Job Graph.

The Firm is not another reasoning agent.  It compares the frozen Work Order
and proposed capability demand with the persistent ROSTER and bounded Job
limits before either DIRECT execution or the managed Kernel starts.  The
Kernel remains the authority for task dispatch and runtime mutation.

This boundary is intentionally first-party: registered employee/runtime and
Paperclip-derived run lifecycle code do not define Noruct's Company, Work
Order, ROSTER, or Firm authority semantics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from dynamic_firm.kernel.models import EmployeeRecord, JobLimits, PlanProposal

from .frontdoor import WorkOrder
from .graph_blueprint_models import GraphUserConstraints


FIRM_ADMISSION_SCHEMA = "noruct.firm-admission.v1"


class FirmAdmissionStatus(StrEnum):
    ADMITTED = "ADMITTED"
    DENIED = "DENIED"


@dataclass(frozen=True, slots=True)
class TaskStaffingPreview:
    task_id: str
    required_capabilities: tuple[str, ...]
    persistent_employee_id: str | None
    temporary_role_required: bool
    # This is deliberately a coarse pre-dispatch proof, not an
    # EmployeeCapabilityProfile. The latter is frozen only when the Kernel
    # has selected task evidence, skills, memory and the task ActionPolicy.
    # A role label must never be presented as a tool/permission/state proof.
    staffing_profile_origin: str
    staffing_model_profile: str
    staffing_capabilities: tuple[str, ...]
    candidate_employee_ids: tuple[str, ...]
    selection_reason: str
    task_relevance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FirmAdmission:
    admission_id: str
    work_order_id: str
    work_order_digest: str
    proposal_id: str
    initial_work_mode: str
    effective_work_mode: str
    task_count: int
    dependency_width: int
    concurrency_ceiling: int
    persistent_employee_count: int
    temporary_role_demand: int
    distinct_staffing_profile_count: int
    staffing_difference_dimensions: tuple[str, ...]
    staffing: tuple[TaskStaffingPreview, ...]
    uncovered_task_ids: tuple[str, ...]
    missing_capability_bundles: tuple[tuple[str, ...], ...]
    status: FirmAdmissionStatus
    reason: str
    content_digest: str

    @property
    def admitted(self) -> bool:
        return self.status is FirmAdmissionStatus.ADMITTED

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": FIRM_ADMISSION_SCHEMA,
            "admission_id": self.admission_id,
            "work_order_id": self.work_order_id,
            "work_order_digest": self.work_order_digest,
            "proposal_id": self.proposal_id,
            "initial_work_mode": self.initial_work_mode,
            "effective_work_mode": self.effective_work_mode,
            "task_count": self.task_count,
            "dependency_width": self.dependency_width,
            "concurrency_ceiling": self.concurrency_ceiling,
            "persistent_employee_count": self.persistent_employee_count,
            "temporary_role_demand": self.temporary_role_demand,
            "distinct_staffing_profile_count": self.distinct_staffing_profile_count,
            "staffing_difference_dimensions": list(self.staffing_difference_dimensions),
            "staffing": [
                {
                    "task_id": item.task_id,
                    "required_capabilities": list(item.required_capabilities),
                    "persistent_employee_id": item.persistent_employee_id,
                    "temporary_role_required": item.temporary_role_required,
                    "staffing_profile_origin": item.staffing_profile_origin,
                    "staffing_model_profile": item.staffing_model_profile,
                    "staffing_capabilities": list(item.staffing_capabilities),
                    "candidate_employee_ids": list(item.candidate_employee_ids),
                    "selection_reason": item.selection_reason,
                    "task_relevance": list(item.task_relevance),
                }
                for item in self.staffing
            ],
            "uncovered_task_ids": list(self.uncovered_task_ids),
            "missing_capability_bundles": [
                list(item) for item in self.missing_capability_bundles
            ],
            "status": self.status.value,
            "reason": self.reason,
        }

    def verify(self) -> None:
        if _digest(self.canonical_payload()) != self.content_digest:
            raise ValueError("Firm admission digest is invalid")
        if self.admitted and self.task_count != len(self.staffing):
            raise ValueError("Firm admission staffing count is inconsistent")
        if not self.admitted and len(self.staffing) > self.task_count:
            raise ValueError("Firm admission staffing count is invalid")
        if self.concurrency_ceiling < 1:
            raise ValueError("Firm admission concurrency ceiling is invalid")
        if self.temporary_role_demand != len(self.missing_capability_bundles):
            raise ValueError("Firm admission temporary-role demand is inconsistent")
        profiles = {
            (item.staffing_model_profile, item.staffing_capabilities)
            for item in self.staffing
            if item.staffing_profile_origin != "UNASSIGNED_DIRECT"
        }
        if self.distinct_staffing_profile_count != len(profiles):
            raise ValueError("Firm admission staffing-profile count is inconsistent")
        for item in self.staffing:
            if item.candidate_employee_ids != tuple(sorted(set(item.candidate_employee_ids))):
                raise ValueError("Firm admission candidate employees are not canonical")
            if item.persistent_employee_id is not None:
                if item.persistent_employee_id not in item.candidate_employee_ids:
                    raise ValueError("Firm admission selected employee is not a capability candidate")
                if item.selection_reason not in {
                    "PINNED_CAPABILITY_MATCH",
                    "MINIMUM_CAPABILITY_SUPERSET",
                }:
                    raise ValueError("Firm admission persistent selection reason is invalid")
                if item.task_relevance != ("REQUIRED_CAPABILITY_COVERAGE",):
                    raise ValueError("Firm admission persistent task relevance is invalid")
            elif item.temporary_role_required:
                if item.candidate_employee_ids or item.selection_reason != "TEMPORARY_CAPABILITY_GAP":
                    raise ValueError("Firm admission temporary staffing evidence is invalid")
                if item.task_relevance != ("REQUIRED_CAPABILITY_GAP",):
                    raise ValueError("Firm admission temporary task relevance is invalid")
            elif item.selection_reason != "DIRECT_UNASSIGNED":
                raise ValueError("Firm admission direct staffing evidence is invalid")
        if self.initial_work_mode == "DIRECT":
            if self.effective_work_mode != "DIRECT":
                raise ValueError("DIRECT admission cannot become managed work")
        elif self.effective_work_mode != (
            "TEAM_JOB" if self.distinct_staffing_profile_count >= 2 else "SOLO_JOB"
        ):
            raise ValueError("Firm admission work mode is inconsistent with staffing proof")


class FirmAdmissionController:
    """Compile Company demand/supply into one bounded, immutable admission."""

    def admit(
        self,
        *,
        work_order: WorkOrder,
        proposal: PlanProposal,
        roster: tuple[EmployeeRecord, ...],
        limits: JobLimits,
        constraints: GraphUserConstraints | None = None,
    ) -> FirmAdmission:
        work_order.verify()
        if not proposal.proposal_id.strip() or not proposal.tasks:
            raise ValueError("Firm admission requires a non-empty Plan Proposal")
        task_ids = tuple(task.task_id for task in proposal.tasks)
        if len(task_ids) != len(set(task_ids)) or any(not item.strip() for item in task_ids):
            raise ValueError("Firm admission requires unique non-empty task ids")
        if proposal.final_task_id not in set(task_ids):
            raise ValueError("Firm admission final task is not present in the proposal")
        if len(proposal.tasks) > limits.max_tasks:
            return self._result(
                work_order=work_order,
                proposal=proposal,
                roster=roster,
                limits=limits,
                dependency_width=1,
                staffing=(),
                missing=(),
                status=FirmAdmissionStatus.DENIED,
                reason="TASK_LIMIT_EXCEEDED",
            )

        constraints = constraints or GraphUserConstraints()
        effective_limits = JobLimits(
            max_tasks=limits.max_tasks,
            max_concurrency=min(
                limits.max_concurrency,
                constraints.max_concurrency
                if constraints.max_concurrency is not None
                else limits.max_concurrency,
            ),
            max_graph_patches=limits.max_graph_patches,
            max_task_mutations=limits.max_task_mutations,
            max_temporary_roles=limits.max_temporary_roles,
            max_total_model_calls=limits.max_total_model_calls,
            max_total_tool_calls=limits.max_total_tool_calls,
            max_total_cost_usd=min(
                limits.max_total_cost_usd,
                constraints.max_cost_usd
                if constraints.max_cost_usd is not None
                else limits.max_total_cost_usd,
            ),
            max_wall_time_ms=min(
                limits.max_wall_time_ms,
                constraints.max_wall_time_ms
                if constraints.max_wall_time_ms is not None
                else limits.max_wall_time_ms,
            ),
        )
        dependency_width = _dependency_width(proposal)
        persistent = tuple(
            sorted(
                (
                    item
                    for item in roster
                    if item.active
                    and not item.temporary
                    and item.employee_id not in constraints.excluded_employee_ids
                ),
                key=lambda item: item.employee_id,
            )
        )
        staffing: list[TaskStaffingPreview] = []
        missing: list[tuple[str, ...]] = []
        for task in sorted(proposal.tasks, key=lambda item: item.task_id):
            required = tuple(sorted(set(task.required_capabilities)))
            capable = tuple(
                employee
                for employee in persistent
                if set(required).issubset(employee.capabilities)
            )
            selected = (
                min(
                    capable,
                    key=lambda item: (
                        0
                        if item.employee_id in constraints.pinned_employee_ids
                        else 1,
                        len(item.capabilities),
                        item.employee_id,
                    ),
                )
                if capable
                else None
            )
            needs_temporary = selected is None and work_order.operating_decision.work_mode.value != "DIRECT"
            if selected is not None:
                staffing_origin = "PERSISTENT"
                staffing_model = selected.model_profile
                staffing_capabilities = tuple(sorted(selected.capabilities))
                candidate_employee_ids = tuple(sorted(item.employee_id for item in capable))
                selection_reason = (
                    "PINNED_CAPABILITY_MATCH"
                    if selected.employee_id in constraints.pinned_employee_ids
                    else "MINIMUM_CAPABILITY_SUPERSET"
                )
                task_relevance = ("REQUIRED_CAPABILITY_COVERAGE",)
            elif needs_temporary:
                # Temporary roles receive the ordinary runtime model selected
                # by the request. At Company admission only the required
                # capability bundle is known; tool/permission/memory proof is
                # deferred to the Kernel's frozen dispatch profile.
                staffing_origin = "TEMPORARY_ROLE"
                staffing_model = "runtime-default"
                staffing_capabilities = required
                candidate_employee_ids = ()
                selection_reason = "TEMPORARY_CAPABILITY_GAP"
                task_relevance = ("REQUIRED_CAPABILITY_GAP",)
            else:
                staffing_origin = "UNASSIGNED_DIRECT"
                staffing_model = ""
                staffing_capabilities = ()
                candidate_employee_ids = ()
                selection_reason = "DIRECT_UNASSIGNED"
                task_relevance = ("DIRECT_REQUEST_NO_STAFFING_PROOF",)
            staffing.append(
                TaskStaffingPreview(
                    task_id=task.task_id,
                    required_capabilities=required,
                    persistent_employee_id=(selected.employee_id if selected else None),
                    temporary_role_required=needs_temporary,
                    staffing_profile_origin=staffing_origin,
                    staffing_model_profile=staffing_model,
                    staffing_capabilities=staffing_capabilities,
                    candidate_employee_ids=candidate_employee_ids,
                    selection_reason=selection_reason,
                    task_relevance=task_relevance,
                )
            )
            if needs_temporary and required not in missing:
                missing.append(required)

        status = FirmAdmissionStatus.ADMITTED
        reason = "CAPABILITY_SUPPLY_CONFIRMED"
        if len(missing) > effective_limits.max_temporary_roles:
            status = FirmAdmissionStatus.DENIED
            reason = "TEMPORARY_ROLE_LIMIT_EXCEEDED"
        return self._result(
            work_order=work_order,
            proposal=proposal,
            roster=roster,
            limits=effective_limits,
            dependency_width=dependency_width,
            staffing=tuple(staffing),
            missing=tuple(missing),
            status=status,
            reason=reason,
        )

    @staticmethod
    def _result(
        *,
        work_order: WorkOrder,
        proposal: PlanProposal,
        roster: tuple[EmployeeRecord, ...],
        limits: JobLimits,
        dependency_width: int,
        staffing: tuple[TaskStaffingPreview, ...],
        missing: tuple[tuple[str, ...], ...],
        status: FirmAdmissionStatus,
        reason: str,
    ) -> FirmAdmission:
        initial_mode = work_order.operating_decision.work_mode.value
        profiles = tuple(
            sorted(
                {
                    (item.staffing_model_profile, item.staffing_capabilities)
                    for item in staffing
                    if item.staffing_profile_origin != "UNASSIGNED_DIRECT"
                }
            )
        )
        dimensions: tuple[str, ...] = ()
        if len(profiles) >= 2:
            dimensions = tuple(
                dimension
                for dimension, index in (
                    ("model_profile", 0),
                    ("capability_ids", 1),
                )
                if len({profile[index] for profile in profiles}) >= 2
            )
        # A multi-task DAG is not automatically a team. Before dispatch we
        # only admit the TEAM label when the frozen ROSTER/temporary-role
        # supply proves at least two non-identity staffing profiles. The
        # Kernel repeats the stronger tool/permission/skill/memory comparison
        # with EmployeeCapabilityProfile before it actually parallelizes work.
        effective_mode = (
            "DIRECT"
            if initial_mode == "DIRECT"
            else "TEAM_JOB"
            if len(profiles) >= 2
            else "SOLO_JOB"
        )
        persistent_count = sum(
            1 for item in roster if item.active and not item.temporary
        )
        payload: dict[str, object] = {
            "schema": FIRM_ADMISSION_SCHEMA,
            "admission_id": f"firm-admission-{work_order.work_order_id}",
            "work_order_id": work_order.work_order_id,
            "work_order_digest": work_order.content_digest,
            "proposal_id": proposal.proposal_id,
            "initial_work_mode": initial_mode,
            "effective_work_mode": effective_mode,
            "task_count": len(proposal.tasks),
            "dependency_width": dependency_width,
            "concurrency_ceiling": min(
                limits.max_concurrency,
                max(1, dependency_width),
            ),
            "persistent_employee_count": persistent_count,
            "temporary_role_demand": len(missing),
            "distinct_staffing_profile_count": len(profiles),
            "staffing_difference_dimensions": list(dimensions),
            "staffing": [
                {
                    "task_id": item.task_id,
                    "required_capabilities": list(item.required_capabilities),
                    "persistent_employee_id": item.persistent_employee_id,
                    "temporary_role_required": item.temporary_role_required,
                    "staffing_profile_origin": item.staffing_profile_origin,
                    "staffing_model_profile": item.staffing_model_profile,
                    "staffing_capabilities": list(item.staffing_capabilities),
                    "candidate_employee_ids": list(item.candidate_employee_ids),
                    "selection_reason": item.selection_reason,
                    "task_relevance": list(item.task_relevance),
                }
                for item in staffing
            ],
            "uncovered_task_ids": [
                item.task_id for item in staffing if item.persistent_employee_id is None
            ],
            "missing_capability_bundles": [list(item) for item in missing],
            "status": status.value,
            "reason": reason,
        }
        admission = FirmAdmission(
            admission_id=str(payload["admission_id"]),
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            proposal_id=proposal.proposal_id,
            initial_work_mode=initial_mode,
            effective_work_mode=effective_mode,
            task_count=len(proposal.tasks),
            dependency_width=dependency_width,
            concurrency_ceiling=int(payload["concurrency_ceiling"]),
            persistent_employee_count=persistent_count,
            temporary_role_demand=len(missing),
            distinct_staffing_profile_count=len(profiles),
            staffing_difference_dimensions=dimensions,
            staffing=staffing,
            uncovered_task_ids=tuple(
                item.task_id for item in staffing if item.persistent_employee_id is None
            ),
            missing_capability_bundles=missing,
            status=status,
            reason=reason,
            content_digest=_digest(payload),
        )
        admission.verify()
        return admission


def _dependency_width(proposal: PlanProposal) -> int:
    """Return maximum concurrently-ready graph width and reject invalid DAGs."""

    dependencies = {
        task.task_id: set(task.depends_on) for task in proposal.tasks
    }
    task_ids = set(dependencies)
    for task_id, required in dependencies.items():
        if task_id in required or not required.issubset(task_ids):
            raise ValueError("Firm admission proposal has invalid dependencies")
    remaining = {task_id: set(values) for task_id, values in dependencies.items()}
    width = 0
    completed: set[str] = set()
    while remaining:
        ready = tuple(
            sorted(
                task_id
                for task_id, required in remaining.items()
                if required.issubset(completed)
            )
        )
        if not ready:
            raise ValueError("Firm admission proposal contains a dependency cycle")
        width = max(width, len(ready))
        completed.update(ready)
        for task_id in ready:
            remaining.pop(task_id)
    return max(1, width)


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
