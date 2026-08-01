"""Persistent Executive Manager identity and bounded assignment contract.

The Manager is a persistent Employee selected from the frozen ROSTER. It can
interpret a WorkOrder and own a user-facing report, while the Firm Kernel keeps
permission, budget, approval, and durable state mutation authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum

from dynamic_firm.kernel.models import EmployeeRecord, PlanProposal
from dynamic_firm.kernel.mutation import content_digest

from .frontdoor import WorkOrder
from .operating import InitialCoordinationPolicy


MANAGER_CAPABILITY = "company_management"
MANAGER_ASSIGNMENT_SCHEMA = "noruct.executive-manager-assignment.v1"
MANAGER_DELEGATION_SCHEMA = "noruct.executive-manager-delegation.v1"


class ManagerAssignmentMode(StrEnum):
    """A Manager proposal, never an authority or effect grant."""

    DIRECT_RESPONSE = "DIRECT_RESPONSE"
    DELEGATE = "DELEGATE"


class ManagerContextLane(StrEnum):
    """The only context projection a delegated Employee may receive.

    This is an auditable instruction to the Kernel, not an Employee-to-
    Employee chat channel.  The actual payload remains bounded by
    ``ContextBundle`` and the dependency projection contract.
    """

    WORK_ORDER_BRIEF = "WORK_ORDER_BRIEF"
    DEPENDENCY_ARTIFACTS = "DEPENDENCY_ARTIFACTS"


@dataclass(frozen=True, slots=True)
class ExecutiveManagerIdentity:
    employee_id: str
    role: str
    roster_revision: int
    model_profile: str
    memory_namespace: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.employee_id.strip() or not self.role.strip():
            raise ValueError("Manager identity requires non-empty employee id and role")
        if self.roster_revision < 1:
            raise ValueError("Manager identity requires a positive ROSTER revision")
        if not self.model_profile.strip():
            raise ValueError("Manager identity requires a model profile")
        object.__setattr__(
            self,
            "memory_namespace",
            f"employee-memory:{self.employee_id}:manager",
        )


@dataclass(frozen=True, slots=True)
class ManagerAssignment:
    """Immutable Manager interpretation bound to one WorkOrder."""

    assignment_id: str
    manager_employee_id: str
    manager_roster_revision: int
    work_order_id: str
    work_order_digest: str
    mode: ManagerAssignmentMode
    reason: str
    session_key: str
    authority_granted: bool = False
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        values = (
            self.assignment_id,
            self.manager_employee_id,
            self.work_order_id,
            self.work_order_digest,
            self.reason,
            self.session_key,
        )
        if any(not value.strip() for value in values):
            raise ValueError("Manager assignment requires non-empty identity fields")
        if self.manager_roster_revision < 1:
            raise ValueError("Manager assignment requires a positive ROSTER revision")
        if self.authority_granted:
            raise ValueError("Manager assignment cannot grant authority")
        object.__setattr__(self, "content_digest", self.computed_digest())

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": MANAGER_ASSIGNMENT_SCHEMA,
            "assignment_id": self.assignment_id,
            "manager_employee_id": self.manager_employee_id,
            "manager_roster_revision": self.manager_roster_revision,
            "work_order_id": self.work_order_id,
            "work_order_digest": self.work_order_digest,
            "mode": self.mode.value,
            "reason": self.reason,
            "session_key": self.session_key,
            "authority_granted": self.authority_granted,
        }

    def computed_digest(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def verify(self) -> None:
        if self.content_digest != self.computed_digest():
            raise ValueError("Manager assignment digest is invalid")


@dataclass(frozen=True, slots=True)
class ManagerDelegatedTask:
    """One typed task adopted by the Manager from an admitted proposal."""

    task_id: str
    objective: str
    depends_on: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    context_lane: ManagerContextLane
    dependency_artifact_ids: tuple[str, ...]
    deliverable_kind: str
    validator_ids: tuple[str, ...]
    final: bool
    replica_group_id: str = ""
    replica_id: str = ""
    replica_strategy: str = ""
    replica_scope: str = ""
    replica_aggregation_task_id: str = ""
    replica_aggregation: str = ""
    replica_value_reason: str = ""

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.objective.strip():
            raise ValueError("Manager delegated task requires an id and objective")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Manager delegated task dependencies must be unique")
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("Manager delegated task capabilities must be unique")
        if self.dependency_artifact_ids != self.depends_on:
            raise ValueError("Manager delegated dependency artifacts must match dependencies")
        if self.context_lane is ManagerContextLane.WORK_ORDER_BRIEF and self.depends_on:
            raise ValueError("Manager root context lane cannot have dependencies")
        if (
            self.context_lane is ManagerContextLane.DEPENDENCY_ARTIFACTS
            and not self.depends_on
        ):
            raise ValueError("Dependency Manager delegation requires dependencies")
        if self.deliverable_kind not in {"SPECIALIST_ARTIFACT", "USER_REPORT"}:
            raise ValueError("Manager delegated deliverable kind is invalid")
        if self.final != (self.deliverable_kind == "USER_REPORT"):
            raise ValueError("Manager delegated final marker does not match deliverable")
        if not self.validator_ids or len(self.validator_ids) != len(set(self.validator_ids)):
            raise ValueError("Manager delegated task validators must be unique and non-empty")
        replica_values = (
            self.replica_group_id,
            self.replica_id,
            self.replica_strategy,
            self.replica_scope,
            self.replica_aggregation_task_id,
            self.replica_aggregation,
            self.replica_value_reason,
        )
        if any(replica_values) and not all(value.strip() for value in replica_values):
            raise ValueError("Manager delegated replica metadata must be complete")

    @classmethod
    def from_job_task(cls, task: object, *, final: bool) -> "ManagerDelegatedTask":
        replica = getattr(task, "execution_replica", None)
        dependencies = tuple(task.depends_on)
        validators = ["structured-completion-v1"]
        if task.acceptance_criteria:
            validators.append("task-acceptance-v1")
        if any(
            capability in {"review", "independent_review", "validation", "verification"}
            or capability.endswith("_review")
            for capability in task.required_capabilities
        ):
            validators.append("independent-review-v1")
        return cls(
            task_id=task.task_id,
            objective=task.objective,
            depends_on=dependencies,
            required_capabilities=task.required_capabilities,
            acceptance_criteria=task.acceptance_criteria,
            context_lane=(
                ManagerContextLane.DEPENDENCY_ARTIFACTS
                if dependencies
                else ManagerContextLane.WORK_ORDER_BRIEF
            ),
            dependency_artifact_ids=dependencies,
            deliverable_kind="USER_REPORT" if final else "SPECIALIST_ARTIFACT",
            validator_ids=tuple(validators),
            final=final,
            replica_group_id="" if replica is None else replica.group_id,
            replica_id="" if replica is None else replica.replica_id,
            replica_strategy="" if replica is None else replica.strategy.value,
            replica_scope="" if replica is None else replica.scope,
            replica_aggregation_task_id=(
                "" if replica is None else replica.aggregation_task_id
            ),
            replica_aggregation="" if replica is None else replica.aggregation.value,
            replica_value_reason=(
                "" if replica is None else replica.marginal_value_reason
            ),
        )


@dataclass(frozen=True, slots=True)
class ManagerDelegation:
    """Immutable, authority-free Manager adoption of one executable proposal.

    The Coordination Compiler may still produce the proposal during the M2
    cutover.  This record makes the Manager's ownership explicit while giving
    the Firm Kernel an exact payload to verify against the accepted graph.
    """

    assignment_digest: str
    manager_employee_id: str
    work_order_id: str
    work_order_digest: str
    proposal_id: str
    final_task_id: str
    tasks: tuple[ManagerDelegatedTask, ...]
    authority_granted: bool = False
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        identity = (
            self.assignment_digest,
            self.manager_employee_id,
            self.work_order_id,
            self.work_order_digest,
            self.proposal_id,
            self.final_task_id,
        )
        if any(not value.strip() for value in identity):
            raise ValueError("Manager delegation requires complete identity")
        if self.authority_granted:
            raise ValueError("Manager delegation cannot grant authority")
        task_ids = tuple(item.task_id for item in self.tasks)
        if not task_ids or len(task_ids) != len(set(task_ids)):
            raise ValueError("Manager delegation requires unique tasks")
        if self.final_task_id not in set(task_ids):
            raise ValueError("Manager delegation final task is unavailable")
        if sum(item.final for item in self.tasks) != 1:
            raise ValueError("Manager delegation requires exactly one final task")
        object.__setattr__(self, "content_digest", content_digest(self.canonical_payload()))

    @classmethod
    def from_proposal(
        cls,
        assignment: ManagerAssignment,
        proposal: PlanProposal,
    ) -> "ManagerDelegation":
        assignment.verify()
        if assignment.mode is not ManagerAssignmentMode.DELEGATE:
            raise ValueError("DIRECT Manager assignment cannot adopt a Job proposal")
        return cls(
            assignment_digest=assignment.content_digest,
            manager_employee_id=assignment.manager_employee_id,
            work_order_id=assignment.work_order_id,
            work_order_digest=assignment.work_order_digest,
            proposal_id=proposal.proposal_id,
            final_task_id=proposal.final_task_id,
            tasks=tuple(
                ManagerDelegatedTask.from_job_task(
                    task,
                    final=task.task_id == proposal.final_task_id,
                )
                for task in proposal.tasks
            ),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": MANAGER_DELEGATION_SCHEMA,
            "assignment_digest": self.assignment_digest,
            "manager_employee_id": self.manager_employee_id,
            "work_order_id": self.work_order_id,
            "work_order_digest": self.work_order_digest,
            "proposal_id": self.proposal_id,
            "final_task_id": self.final_task_id,
            "tasks": tuple(
                {
                    "task_id": task.task_id,
                    "objective": task.objective,
                    "depends_on": task.depends_on,
                    "required_capabilities": task.required_capabilities,
                    "acceptance_criteria": task.acceptance_criteria,
                    "context_lane": task.context_lane.value,
                    "dependency_artifact_ids": task.dependency_artifact_ids,
                    "deliverable_kind": task.deliverable_kind,
                    "validator_ids": task.validator_ids,
                    "final": task.final,
                    "execution_replica": (
                        None
                        if not task.replica_group_id
                        else {
                            "group_id": task.replica_group_id,
                            "replica_id": task.replica_id,
                            "strategy": task.replica_strategy,
                            "scope": task.replica_scope,
                            "aggregation_task_id": task.replica_aggregation_task_id,
                            "aggregation": task.replica_aggregation,
                            "marginal_value_reason": task.replica_value_reason,
                        }
                    ),
                }
                for task in self.tasks
            ),
            "authority_granted": self.authority_granted,
        }

    def verify(self, proposal: PlanProposal) -> None:
        if self.content_digest != content_digest(self.canonical_payload()):
            raise ValueError("Manager delegation digest is invalid")
        expected = type(self).from_proposal_payload(
            assignment_digest=self.assignment_digest,
            manager_employee_id=self.manager_employee_id,
            work_order_id=self.work_order_id,
            work_order_digest=self.work_order_digest,
            proposal=proposal,
        )
        if self.canonical_payload() != expected.canonical_payload():
            raise ValueError("Manager delegation does not match the accepted proposal")

    @classmethod
    def from_proposal_payload(
        cls,
        *,
        assignment_digest: str,
        manager_employee_id: str,
        work_order_id: str,
        work_order_digest: str,
        proposal: PlanProposal,
    ) -> "ManagerDelegation":
        return cls(
            assignment_digest=assignment_digest,
            manager_employee_id=manager_employee_id,
            work_order_id=work_order_id,
            work_order_digest=work_order_digest,
            proposal_id=proposal.proposal_id,
            final_task_id=proposal.final_task_id,
            tasks=tuple(
                ManagerDelegatedTask.from_job_task(
                    task,
                    final=task.task_id == proposal.final_task_id,
                )
                for task in proposal.tasks
            ),
        )


class PersistentExecutiveManager:
    """Select and bind one real Manager Employee without policy authority."""

    def __init__(self, identity: ExecutiveManagerIdentity) -> None:
        self.identity = identity

    @classmethod
    def from_roster(
        cls,
        roster: tuple[EmployeeRecord, ...],
        *,
        roster_revision: int,
    ) -> "PersistentExecutiveManager":
        candidates = tuple(
            employee
            for employee in roster
            if employee.active
            and not employee.temporary
            and MANAGER_CAPABILITY in employee.capabilities
        )
        if len(candidates) != 1:
            raise ValueError(
                "Persistent Company ROSTER requires exactly one active "
                f"{MANAGER_CAPABILITY} Employee; found {len(candidates)}"
            )
        employee = candidates[0]
        return cls(
            ExecutiveManagerIdentity(
                employee_id=employee.employee_id,
                role=employee.role,
                roster_revision=roster_revision,
                model_profile=employee.model_profile,
            )
        )

    @classmethod
    def optional_from_roster(
        cls,
        roster: tuple[EmployeeRecord, ...],
        *,
        roster_revision: int,
    ) -> "PersistentExecutiveManager | None":
        """Return no Manager for a pre-M2 ROSTER without mutating it.

        Existing local companies are not silently rewritten to add a powerful
        employee. A later explicit ROSTER migration can activate this path.
        More than one declared Manager is still invalid and must be repaired.
        """

        candidates = tuple(
            employee
            for employee in roster
            if employee.active
            and not employee.temporary
            and MANAGER_CAPABILITY in employee.capabilities
        )
        if not candidates:
            return None
        return cls.from_roster(roster, roster_revision=roster_revision)

    def initial_assignment(
        self,
        work_order: WorkOrder,
        *,
        session_key: str,
    ) -> ManagerAssignment:
        work_order.verify()
        normalized_session = session_key.strip() or "company-default"
        direct = (
            work_order.operating_decision.coordination_policy
            is InitialCoordinationPolicy.DIRECT
        )
        replica_opportunity = (
            work_order.operating_decision.suggested_execution_replica_strategy
            is not None
        )
        return ManagerAssignment(
            assignment_id=(
                f"manager-assignment-{work_order.work_order_id}-"
                f"{self.identity.employee_id}"
            ),
            manager_employee_id=self.identity.employee_id,
            manager_roster_revision=self.identity.roster_revision,
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            mode=(
                ManagerAssignmentMode.DIRECT_RESPONSE
                if direct
                else ManagerAssignmentMode.DELEGATE
            ),
            reason=(
                "MANAGER_DIRECT_SUFFICIENT"
                if direct
                else "MANAGER_PERFORMANCE_REPLICA_PROPOSAL"
                if replica_opportunity
                else "MANAGER_SPECIALIST_DELEGATION_REQUIRED"
            ),
            session_key=f"manager:{self.identity.employee_id}:{normalized_session}",
        )

    def validate_assignment(
        self,
        assignment: ManagerAssignment,
        work_order: WorkOrder,
    ) -> None:
        assignment.verify()
        work_order.verify()
        if assignment.manager_employee_id != self.identity.employee_id:
            raise ValueError("Manager assignment belongs to a different Employee")
        if assignment.manager_roster_revision != self.identity.roster_revision:
            raise ValueError("Manager assignment belongs to a different ROSTER revision")
        if (
            assignment.work_order_id != work_order.work_order_id
            or assignment.work_order_digest != work_order.content_digest
        ):
            raise ValueError("Manager assignment is not bound to this WorkOrder")
