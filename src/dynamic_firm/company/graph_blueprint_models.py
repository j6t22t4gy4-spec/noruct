"""Inert Graph Blueprint and retained Graph Run Record data contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

from dynamic_firm.kernel.models import (
    ExecutionReplicaAggregation,
    ExecutionReplicaStrategy,
    GraphPatchExpectedImpact,
    GraphPatchObservedOutcome,
    GraphPatchValidationReceipt,
    PlanProposal,
)


GRAPH_BLUEPRINT_SCHEMA = "noruct.graph-blueprint.v1"
BLUEPRINT_REVISION_RECEIPT_SCHEMA = "noruct.graph-blueprint-revision-receipt.v1"
GRAPH_RUN_RECORD_SCHEMA = "noruct.graph-run-record.v2"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
MAX_TEMPLATE_BYTES = 16_000


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase identifier")
    return value


def text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError(f"{label} must be non-empty")
    if "\x00" in normalized or len(normalized.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise ValueError(f"{label} is invalid or exceeds its byte bound")
    return normalized


def unique_identifiers(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} must be a tuple")
    normalized = tuple(identifier(item, label) for item in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} must be unique")
    return normalized


class GraphBlueprintOrigin(StrEnum):
    DRAFT = "DRAFT"
    USER_FORK = "USER_FORK"
    USER_REVISION = "USER_REVISION"
    PINNED_EXTERNAL = "PINNED_EXTERNAL"
    VERIFIED_PLAYBOOK = "VERIFIED_PLAYBOOK"
    STAGED_COMMUNITY = "STAGED_COMMUNITY"


class GraphMutationPolicy(StrEnum):
    LOCKED = "LOCKED"
    PROPOSE = "PROPOSE"
    BOUNDED_AUTO = "BOUNDED_AUTO"


class BlueprintResolutionReason(StrEnum):
    SKIPPED_DIRECT = "SKIPPED_DIRECT"
    PINNED_HIT = "PINNED_HIT"
    LOCAL_HIT = "LOCAL_HIT"
    NO_COMPATIBLE_BLUEPRINT = "NO_COMPATIBLE_BLUEPRINT"
    BINDING_REJECTED = "BINDING_REJECTED"


@dataclass(frozen=True, slots=True)
class GraphBlueprintRef:
    blueprint_id: str
    version: int
    content_digest: str

    def __post_init__(self) -> None:
        identifier(self.blueprint_id, "blueprint_id")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("version must be a positive integer")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_digest):
            raise ValueError("content_digest must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class GraphBlueprintExecutionReplica:
    """Inert, parameterized hypothesis for one same-Employee execution.

    A Blueprint may describe the structure, but it cannot claim that the
    structure has proven value.  Qualification remains separate outcome
    evidence and binding still passes the rendered contract through the Firm
    Kernel's ordinary Job Graph validator.
    """

    group_id: str
    replica_id: str
    strategy: ExecutionReplicaStrategy
    scope_template: str
    aggregation_task_id: str
    aggregation: ExecutionReplicaAggregation
    marginal_value_reason_template: str

    def __post_init__(self) -> None:
        identifier(self.group_id, "replica group_id")
        identifier(self.replica_id, "replica replica_id")
        identifier(self.aggregation_task_id, "replica aggregation_task_id")
        if not isinstance(self.strategy, ExecutionReplicaStrategy):
            raise TypeError("Replica strategy must be typed")
        if not isinstance(self.aggregation, ExecutionReplicaAggregation):
            raise TypeError("Replica aggregation must be typed")
        text(self.scope_template, "replica scope_template")
        text(
            self.marginal_value_reason_template,
            "replica marginal_value_reason_template",
        )

    def template_fields(self) -> frozenset[str]:
        return frozenset(
            match.group(1)
            for value in (
                self.scope_template,
                self.marginal_value_reason_template,
            )
            for match in PLACEHOLDER.finditer(value)
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "replica_id": self.replica_id,
            "strategy": self.strategy.value,
            "scope_template": self.scope_template,
            "aggregation_task_id": self.aggregation_task_id,
            "aggregation": self.aggregation.value,
            "marginal_value_reason_template": self.marginal_value_reason_template,
        }


@dataclass(frozen=True, slots=True)
class GraphBlueprintTask:
    task_id: str
    objective_template: str
    depends_on: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    acceptance_templates: tuple[str, ...]
    risk_level: str = "LOW"
    execution_replica: GraphBlueprintExecutionReplica | None = None

    def __post_init__(self) -> None:
        identifier(self.task_id, "task_id")
        text(self.objective_template, "objective_template")
        unique_identifiers(self.depends_on, "depends_on")
        unique_identifiers(self.required_capabilities, "required_capabilities")
        if not self.required_capabilities:
            raise ValueError("required_capabilities must be non-empty")
        if not isinstance(self.acceptance_templates, tuple) or not self.acceptance_templates:
            raise ValueError("acceptance_templates must be a non-empty tuple")
        for item in self.acceptance_templates:
            text(item, "acceptance_template")
        if self.risk_level != "LOW":
            raise ValueError("Graph Blueprint tasks currently support LOW risk only")
        if self.execution_replica is not None and not isinstance(
            self.execution_replica, GraphBlueprintExecutionReplica
        ):
            raise TypeError("execution_replica must be a typed Blueprint replica")

    def template_fields(self) -> frozenset[str]:
        values = (self.objective_template, *self.acceptance_templates)
        replica_fields = (
            frozenset()
            if self.execution_replica is None
            else self.execution_replica.template_fields()
        )
        return frozenset(
            match.group(1) for value in values for match in PLACEHOLDER.finditer(value)
        ) | replica_fields


@dataclass(frozen=True, slots=True)
class GraphBlueprint:
    """A versioned inert structure; never an executable Job Graph."""

    blueprint_id: str
    version: int
    objective_class: str
    execution_profiles: tuple[str, ...]
    parameters: tuple[str, ...]
    tasks: tuple[GraphBlueprintTask, ...]
    final_task_id: str
    origin: GraphBlueprintOrigin = GraphBlueprintOrigin.DRAFT
    parent_ref: GraphBlueprintRef | None = None
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        identifier(self.blueprint_id, "blueprint_id")
        identifier(self.objective_class, "objective_class")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("version must be a positive integer")
        if not unique_identifiers(self.execution_profiles, "execution_profiles"):
            raise ValueError("execution_profiles must be non-empty")
        parameters = unique_identifiers(self.parameters, "parameters")
        if len(self.tasks) < 1 or len(self.tasks) > 64:
            raise ValueError("tasks must contain between one and 64 entries")
        task_ids = tuple(item.task_id for item in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Blueprint task ids must be unique")
        if self.final_task_id not in set(task_ids):
            raise ValueError("final_task_id must reference a Blueprint task")
        known = set(task_ids)
        for task in self.tasks:
            if task.task_id in task.depends_on or not set(task.depends_on).issubset(known):
                raise ValueError("Blueprint dependencies must reference other tasks")
            unknown = task.template_fields() - set(parameters)
            if unknown:
                raise ValueError(
                    "Blueprint template references undeclared parameters: "
                    + ", ".join(sorted(unknown))
                )
        self._validate_execution_replica_templates()
        if self.parent_ref is not None and self.parent_ref.blueprint_id == self.blueprint_id:
            if (
                self.origin is not GraphBlueprintOrigin.USER_REVISION
                or self.version <= self.parent_ref.version
            ):
                raise ValueError(
                    "A same-id Blueprint revision requires USER_REVISION and a later version"
                )
        object.__setattr__(self, "content_digest", digest(self.canonical_payload()))

    def _validate_execution_replica_templates(self) -> None:
        tasks = {task.task_id: task for task in self.tasks}
        groups: dict[str, list[GraphBlueprintTask]] = {}
        for task in self.tasks:
            replica = task.execution_replica
            if replica is None:
                continue
            if task.task_id == self.final_task_id:
                raise ValueError("A Blueprint final task cannot be an execution replica")
            groups.setdefault(replica.group_id, []).append(task)
        for group_id, members in groups.items():
            if not 2 <= len(members) <= 4:
                raise ValueError(
                    f"Blueprint execution replica group {group_id} must contain 2 to 4 tasks"
                )
            replicas = tuple(task.execution_replica for task in members)
            assert all(replica is not None for replica in replicas)
            first = replicas[0]
            assert first is not None
            if len({replica.replica_id for replica in replicas if replica is not None}) != len(members):
                raise ValueError(
                    f"Blueprint execution replica group {group_id} has duplicate replica ids"
                )
            if any(
                replica is None
                or replica.strategy is not first.strategy
                or replica.aggregation_task_id != first.aggregation_task_id
                or replica.aggregation is not first.aggregation
                or replica.marginal_value_reason_template
                != first.marginal_value_reason_template
                for replica in replicas
            ):
                raise ValueError(
                    f"Blueprint execution replica group {group_id} has inconsistent value metadata"
                )
            aggregation_task = tasks.get(first.aggregation_task_id)
            member_ids = {task.task_id for task in members}
            if (
                aggregation_task is None
                or aggregation_task.execution_replica is not None
                or not member_ids.issubset(set(aggregation_task.depends_on))
            ):
                raise ValueError(
                    f"Blueprint execution replica group {group_id} requires a separate downstream aggregation task"
                )
            if len({task.depends_on for task in members}) != 1:
                raise ValueError(
                    f"Blueprint execution replica group {group_id} must share upstream dependencies"
                )
            if len({task.required_capabilities for task in members}) != 1:
                raise ValueError(
                    f"Blueprint execution replica group {group_id} must share required capabilities"
                )

    @property
    def ref(self) -> GraphBlueprintRef:
        return GraphBlueprintRef(self.blueprint_id, self.version, self.content_digest)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": GRAPH_BLUEPRINT_SCHEMA,
            "blueprint_id": self.blueprint_id,
            "version": self.version,
            "objective_class": self.objective_class,
            "execution_profiles": list(self.execution_profiles),
            "parameters": list(self.parameters),
            "tasks": [
                {
                    "task_id": task.task_id,
                    "objective_template": task.objective_template,
                    "depends_on": list(task.depends_on),
                    "required_capabilities": list(task.required_capabilities),
                    "acceptance_templates": list(task.acceptance_templates),
                    "risk_level": task.risk_level,
                    **(
                        {"execution_replica": task.execution_replica.canonical_payload()}
                        if task.execution_replica is not None
                        else {}
                    ),
                }
                for task in self.tasks
            ],
            "final_task_id": self.final_task_id,
            "origin": self.origin.value,
            "parent_ref": (
                {
                    "blueprint_id": self.parent_ref.blueprint_id,
                    "version": self.parent_ref.version,
                    "content_digest": self.parent_ref.content_digest,
                }
                if self.parent_ref is not None
                else None
            ),
        }

    def verify(self) -> None:
        if self.content_digest != digest(self.canonical_payload()):
            raise ValueError("Graph Blueprint content digest is invalid")


@dataclass(frozen=True, slots=True)
class GraphUserConstraints:
    pinned_employee_ids: tuple[str, ...] = ()
    excluded_employee_ids: tuple[str, ...] = ()
    require_independent_review: bool = False
    max_concurrency: int | None = None
    max_cost_usd: float | None = None
    max_wall_time_ms: int | None = None
    mutation_policy: GraphMutationPolicy = GraphMutationPolicy.BOUNDED_AUTO

    def __post_init__(self) -> None:
        pinned = unique_identifiers(self.pinned_employee_ids, "pinned_employee_ids")
        excluded = unique_identifiers(self.excluded_employee_ids, "excluded_employee_ids")
        if set(pinned) & set(excluded):
            raise ValueError("An Employee cannot be both pinned and excluded")
        if self.max_concurrency is not None and (
            type(self.max_concurrency) is not int or self.max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if self.max_cost_usd is not None and (
            isinstance(self.max_cost_usd, bool)
            or not isinstance(self.max_cost_usd, (int, float))
            or not math.isfinite(float(self.max_cost_usd))
            or self.max_cost_usd < 0
        ):
            raise ValueError("max_cost_usd must be a finite non-negative number")
        if self.max_wall_time_ms is not None and (
            type(self.max_wall_time_ms) is not int or self.max_wall_time_ms < 1
        ):
            raise ValueError("max_wall_time_ms must be a positive integer")


class BlueprintRevisionStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class BlueprintRevisionReceipt:
    """One immutable decision about an inert user Blueprint revision.

    This is deliberately separate from ``GraphRevision``.  A Blueprint
    revision changes a reusable template for a *future* Job; it neither
    modifies nor grants authority over an active request-scoped Job Graph.
    """

    source_ref: GraphBlueprintRef
    candidate_ref: GraphBlueprintRef
    status: BlueprintRevisionStatus
    reason: str
    rationale: str
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        text(self.reason, "revision reason")
        text(self.rationale, "revision rationale")
        object.__setattr__(self, "content_digest", digest(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": BLUEPRINT_REVISION_RECEIPT_SCHEMA,
            "source_ref": {
                "blueprint_id": self.source_ref.blueprint_id,
                "version": self.source_ref.version,
                "content_digest": self.source_ref.content_digest,
            },
            "candidate_ref": {
                "blueprint_id": self.candidate_ref.blueprint_id,
                "version": self.candidate_ref.version,
                "content_digest": self.candidate_ref.content_digest,
            },
            "status": self.status.value,
            "reason": self.reason,
            "rationale": self.rationale,
        }

    def verify(self) -> None:
        if self.content_digest != digest(self.canonical_payload()):
            raise ValueError("Blueprint revision receipt content digest is invalid")


@dataclass(frozen=True, slots=True)
class BlueprintBinding:
    blueprint_ref: GraphBlueprintRef
    work_order_id: str
    work_order_digest: str
    parameters: tuple[tuple[str, str], ...]
    constraints: GraphUserConstraints
    proposal: PlanProposal
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if tuple(sorted(self.parameters)) != self.parameters:
            raise ValueError("Blueprint binding parameters must be sorted")
        object.__setattr__(self, "content_digest", digest(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "blueprint_ref": {
                "blueprint_id": self.blueprint_ref.blueprint_id,
                "version": self.blueprint_ref.version,
                "content_digest": self.blueprint_ref.content_digest,
            },
            "work_order_id": self.work_order_id,
            "work_order_digest": self.work_order_digest,
            "parameters": list(self.parameters),
            "constraints": {
                "pinned_employee_ids": list(self.constraints.pinned_employee_ids),
                "excluded_employee_ids": list(self.constraints.excluded_employee_ids),
                "require_independent_review": self.constraints.require_independent_review,
                "max_concurrency": self.constraints.max_concurrency,
                "max_cost_usd": self.constraints.max_cost_usd,
                "max_wall_time_ms": self.constraints.max_wall_time_ms,
                "mutation_policy": self.constraints.mutation_policy.value,
            },
            "proposal_id": self.proposal.proposal_id,
        }


@dataclass(frozen=True, slots=True)
class BlueprintResolution:
    reason: BlueprintResolutionReason
    binding: BlueprintBinding | None = None
    rejected_refs: tuple[GraphBlueprintRef, ...] = ()
    detail: str = ""

    @property
    def hit(self) -> bool:
        return self.binding is not None


@dataclass(frozen=True, slots=True)
class GraphPreviewTask:
    """One display-safe, non-authoritative task projection for a Graph preview."""

    task_id: str
    depends_on: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    proposed_employee_id: str | None
    temporary_role_required: bool
    execution_replica_group_id: str = ""
    execution_replica_id: str = ""
    execution_replica_strategy: str = ""
    execution_replica_scope: str = ""
    execution_replica_aggregation_task_id: str = ""
    execution_replica_aggregation: str = ""
    execution_replica_value_reason: str = ""


@dataclass(frozen=True, slots=True)
class GraphPreview:
    binding_digest: str
    work_order_digest: str
    blueprint_ref: GraphBlueprintRef
    work_mode: str
    final_task_id: str
    task_count: int
    dependency_width: int
    distinct_staffing_profile_count: int
    staffing_difference_dimensions: tuple[str, ...]
    execution_replica_group_ids: tuple[str, ...]
    execution_replica_count: int
    tasks: tuple[GraphPreviewTask, ...]
    proposed_employee_ids: tuple[str, ...]
    uncovered_task_ids: tuple[str, ...]
    admission_status: str
    admission_reason: str
    hard_cap_cost_usd: float
    hard_cap_wall_time_ms: int
    effective_max_cost_usd: float
    effective_max_wall_time_ms: int
    requires_independent_review: bool
    mutation_policy: GraphMutationPolicy
    constraint_warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphRevision:
    sequence: int
    previous_graph_digest: str
    next_graph_digest: str
    operation: str
    proposer: str
    trigger_evidence: tuple[str, ...]
    budget_delta: float
    approval_policy: GraphMutationPolicy
    expected_impact: GraphPatchExpectedImpact = (
        GraphPatchExpectedImpact.CAPABILITY_COVERAGE
    )
    validation_receipt: GraphPatchValidationReceipt = (
        GraphPatchValidationReceipt.KERNEL_GRAPH_AND_LEASE_VALIDATED
    )
    observed_terminal_outcome: GraphPatchObservedOutcome = (
        GraphPatchObservedOutcome.NOT_OBSERVED
    )

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("Graph Revision sequence must be positive")
        for label, value in (
            ("previous_graph_digest", self.previous_graph_digest),
            ("next_graph_digest", self.next_graph_digest),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{label} must be a SHA-256 digest")
        text(self.operation, "operation")
        text(self.proposer, "proposer")
        if self.budget_delta < 0:
            raise ValueError("Graph Revision budget_delta must be non-negative")
        if not isinstance(self.expected_impact, GraphPatchExpectedImpact):
            raise ValueError("Graph Revision expected impact is invalid")
        if not isinstance(self.validation_receipt, GraphPatchValidationReceipt):
            raise ValueError("Graph Revision validation receipt is invalid")
        if not isinstance(self.observed_terminal_outcome, GraphPatchObservedOutcome):
            raise ValueError("Graph Revision observed terminal outcome is invalid")


@dataclass(frozen=True, slots=True)
class GraphRunRecord:
    """Bounded audit evidence; this object never grants future execution."""

    job_id: str
    work_order_digest: str
    initial_graph_digest: str
    blueprint_ref: GraphBlueprintRef | None = None
    revisions: tuple[GraphRevision, ...] = ()
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        identifier(self.job_id, "job_id")
        for label, value in (
            ("work_order_digest", self.work_order_digest),
            ("initial_graph_digest", self.initial_graph_digest),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{label} must be a SHA-256 digest")
        for expected, revision in enumerate(self.revisions, start=1):
            if revision.sequence != expected:
                raise ValueError("Graph Run Record revisions must be contiguous")
        object.__setattr__(self, "content_digest", digest(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": GRAPH_RUN_RECORD_SCHEMA,
            "job_id": self.job_id,
            "work_order_digest": self.work_order_digest,
            "initial_graph_digest": self.initial_graph_digest,
            "blueprint_ref": (
                {
                    "blueprint_id": self.blueprint_ref.blueprint_id,
                    "version": self.blueprint_ref.version,
                    "content_digest": self.blueprint_ref.content_digest,
                }
                if self.blueprint_ref is not None
                else None
            ),
            "revisions": [
                {
                    "sequence": item.sequence,
                    "previous_graph_digest": item.previous_graph_digest,
                    "next_graph_digest": item.next_graph_digest,
                    "operation": item.operation,
                    "proposer": item.proposer,
                    "trigger_evidence": list(item.trigger_evidence),
                    "budget_delta": item.budget_delta,
                    "approval_policy": item.approval_policy.value,
                    "expected_impact": item.expected_impact.value,
                    "validation_receipt": item.validation_receipt.value,
                    "observed_terminal_outcome": item.observed_terminal_outcome.value,
                }
                for item in self.revisions
            ],
        }

    def append(self, revision: GraphRevision) -> "GraphRunRecord":
        expected_before = (
            self.initial_graph_digest if not self.revisions else self.revisions[-1].next_graph_digest
        )
        if revision.previous_graph_digest != expected_before:
            raise ValueError("Graph Revision does not continue the current Run Record")
        return GraphRunRecord(
            job_id=self.job_id,
            work_order_digest=self.work_order_digest,
            initial_graph_digest=self.initial_graph_digest,
            blueprint_ref=self.blueprint_ref,
            revisions=(*self.revisions, revision),
        )
