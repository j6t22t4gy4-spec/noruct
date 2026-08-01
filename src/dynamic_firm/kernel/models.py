from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from dynamic_firm.runtime.models import (
    ActionPolicy,
    ContextBundle,
    EmployeeRunResult,
    RunStatus,
    RunLimits,
    RunSignal,
    TaskEvidencePack,
    Usage,
    VersionedContent,
)


class JobStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STALLED = "STALLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }


class SemanticOperation(StrEnum):
    SPLIT = "SPLIT"
    JOIN = "JOIN"
    MERGE = "MERGE"
    INSERT = "INSERT"
    CANCEL = "CANCEL"


class GraphPatchExpectedImpact(StrEnum):
    """Typed, content-free expectation recorded for an accepted rewrite."""

    CAPABILITY_COVERAGE = "CAPABILITY_COVERAGE"
    WORK_PARTITIONING = "WORK_PARTITIONING"
    RESULT_INTEGRATION = "RESULT_INTEGRATION"
    TOPOLOGY_CONSOLIDATION = "TOPOLOGY_CONSOLIDATION"
    WORK_REMOVAL = "WORK_REMOVAL"


class GraphPatchValidationReceipt(StrEnum):
    """The only validator receipt a Kernel patch may claim today."""

    KERNEL_GRAPH_AND_LEASE_VALIDATED = "KERNEL_GRAPH_AND_LEASE_VALIDATED"


class GraphPatchObservedOutcome(StrEnum):
    """Terminal Job association, explicitly not per-revision causal impact."""

    NOT_OBSERVED = "NOT_OBSERVED"
    JOB_SUCCEEDED = "JOB_SUCCEEDED"
    JOB_FAILED = "JOB_FAILED"
    JOB_STALLED = "JOB_STALLED"
    JOB_BUDGET_EXHAUSTED = "JOB_BUDGET_EXHAUSTED"


class TaskMutationType(StrEnum):
    RETRY = "RETRY"
    REROUTE = "REROUTE"


class ExecutionReplicaStrategy(StrEnum):
    """Why one frozen Employee profile is worth more than one execution."""

    PARTITION = "PARTITION"
    CANDIDATE = "CANDIDATE"
    DIAGNOSTIC = "DIAGNOSTIC"


class ExecutionReplicaPreference(StrEnum):
    """How strongly planning should seek bounded same-Employee fan-out.

    This is a Manager/Compiler preference carried through first-party
    contracts, never an execution grant.  Kernel graph, action-policy, and
    hard-budget validation remain authoritative for every admitted replica.
    """

    DISABLED = "DISABLED"
    BALANCED = "BALANCED"
    PERFORMANCE_FIRST = "PERFORMANCE_FIRST"


class ExecutionReplicaAggregation(StrEnum):
    """How bounded replica outputs become one downstream artifact."""

    JOIN = "JOIN"
    VALIDATOR_SELECT = "VALIDATOR_SELECT"
    MANAGER_SYNTHESIS = "MANAGER_SYNTHESIS"


class AttemptFailureKind(StrEnum):
    NONE = "NONE"
    RECOVERABLE_MODEL = "RECOVERABLE_MODEL"
    RECOVERABLE_TOOL = "RECOVERABLE_TOOL"
    RECOVERABLE_TIMEOUT = "RECOVERABLE_TIMEOUT"
    RECOVERABLE_LIVENESS = "RECOVERABLE_LIVENESS"
    ASSIGNEE_MISMATCH = "ASSIGNEE_MISMATCH"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    SAFETY_VIOLATION = "SAFETY_VIOLATION"
    POLICY_DENIED = "POLICY_DENIED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CANCELLED = "CANCELLED"
    INPUT_INVALID = "INPUT_INVALID"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NON_RETRYABLE = "NON_RETRYABLE"
    UNKNOWN = "UNKNOWN"


class PatchOperationKind(StrEnum):
    ADD_TASK = "ADD_TASK"
    ADD_DEPENDENCY = "ADD_DEPENDENCY"
    REMOVE_DEPENDENCY = "REMOVE_DEPENDENCY"
    REPLACE_DEPENDENCIES = "REPLACE_DEPENDENCIES"
    CANCEL_TASK = "CANCEL_TASK"
    SET_FINAL_TASK = "SET_FINAL_TASK"


@dataclass(frozen=True, slots=True)
class JobLimits:
    max_tasks: int = 16
    max_concurrency: int = 4
    max_graph_patches: int = 3
    max_task_mutations: int = 2
    max_temporary_roles: int = 2
    max_total_model_calls: int = 64
    max_total_tool_calls: int = 128
    max_total_cost_usd: float = 20.0
    max_wall_time_ms: int = 300_000


@dataclass(frozen=True, slots=True)
class EmployeeRecord:
    employee_id: str
    role: str
    capabilities: tuple[str, ...]
    active: bool = True
    temporary: bool = False
    model_profile: str = "scripted"


@dataclass(frozen=True, slots=True)
class TaskAssignmentEvent:
    """One factual Kernel dispatch, projected without product-layer authority."""

    job_id: str
    task_id: str
    graph_version: int
    employee_id: str
    employee_role: str
    employee_temporary: bool
    required_capabilities: tuple[str, ...]
    depends_on: tuple[str, ...]
    attempt: int
    final_task: bool
    selection_reason: str
    active_task_count: int
    capability_profile_digest: str = ""
    capability_material_digest: str = ""
    task_relevance: tuple[str, ...] = ()
    chosen_over_employee_ids: tuple[str, ...] = ()
    profile_difference: tuple[str, ...] = ()
    execution_instance_id: str = ""
    replica_group_id: str = ""
    replica_id: str = ""
    replica_strategy: str = ""
    replica_scope: str = ""
    replica_value_reason: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionReplicaSpec:
    """Manager proposal for one Job-local run of an existing Employee.

    This is not a ROSTER identity.  The Kernel admits the complete group or
    rejects the graph, assigns every member to one exact Employee profile,
    and forces RUN_ONLY state retention.
    """

    group_id: str
    replica_id: str
    strategy: ExecutionReplicaStrategy
    scope: str
    aggregation_task_id: str
    aggregation: ExecutionReplicaAggregation
    marginal_value_reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, ExecutionReplicaStrategy):
            raise TypeError("Execution replica strategy must be typed")
        if not isinstance(self.aggregation, ExecutionReplicaAggregation):
            raise TypeError("Execution replica aggregation must be typed")
        for label, value, maximum in (
            ("group id", self.group_id, 64),
            ("replica id", self.replica_id, 64),
            ("scope", self.scope, 500),
            ("aggregation task id", self.aggregation_task_id, 64),
            ("marginal value reason", self.marginal_value_reason, 500),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > maximum
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError(f"Execution replica {label} is invalid")


@dataclass(frozen=True, slots=True)
class JobTask:
    task_id: str
    objective: str
    depends_on: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    risk_level: str = "LOW"
    status: TaskStatus = TaskStatus.PENDING
    assignee_id: str | None = None
    attempt: int = 1
    runtime_result: EmployeeRunResult | None = None
    execution_replica: ExecutionReplicaSpec | None = None


@dataclass(frozen=True, slots=True)
class PlanProposal:
    proposal_id: str
    goal: str
    tasks: tuple[JobTask, ...]
    final_task_id: str
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JobGraph:
    """Versioned, request-scoped executable task dependency structure.

    The name denotes scheduling and control semantics.  It never represents a
    diagram, a UI layout, or the user's knowledge relations.
    """

    version: int
    tasks: tuple[JobTask, ...]
    final_task_id: str


@dataclass(frozen=True, slots=True)
class GraphPatchOperation:
    kind: PatchOperationKind
    task: JobTask | None = None
    task_id: str = ""
    dependency_id: str = ""
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphPatch:
    """One proposed atomic rewrite of an executable JobGraph."""

    patch_id: str
    base_graph_version: int
    trigger_task_id: str
    semantic_operation: SemanticOperation
    rationale: str
    expected_gain: str
    operations: tuple[GraphPatchOperation, ...]
    # Opaque references to the assumption/constraint evidence that justified
    # this revision. They are audit metadata, not executable graph input.
    semantic_evidence_refs: tuple[str, ...] = ()


class GraphPatchProposalStatus(StrEnum):
    """Durable operator state for one bounded active-Job graph proposal."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class GraphMutationLease:
    """Budget capacity committed before a topology patch becomes executable.

    It is an intra-Job reservation, not a Company-budget expansion.  The Job
    hard cap is already admitted before dispatch; this receipt prevents a
    newly added pending task from silently competing with later dispatches.
    """

    model_calls: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if type(self.model_calls) is not int or self.model_calls < 0:
            raise ValueError("Graph mutation lease model_calls must be non-negative")
        if type(self.tool_calls) is not int or self.tool_calls < 0:
            raise ValueError("Graph mutation lease tool_calls must be non-negative")
        if (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, (int, float))
            or not math.isfinite(float(self.cost_usd))
            or self.cost_usd < 0
        ):
            raise ValueError("Graph mutation lease cost_usd must be non-negative")


@dataclass(frozen=True, slots=True)
class GraphPatchEvent:
    """Immutable audit record for one validated execution-graph rewrite."""

    event_id: str
    sequence: int
    patch: GraphPatch
    target_graph_version: int
    before_graph_digest: str
    after_graph_digest: str
    added_task_ids: tuple[str, ...]
    cancelled_task_ids: tuple[str, ...]
    content_hash: str
    mutation_lease: GraphMutationLease = field(default_factory=GraphMutationLease)
    expected_impact: GraphPatchExpectedImpact = (
        GraphPatchExpectedImpact.CAPABILITY_COVERAGE
    )
    validation_receipt: GraphPatchValidationReceipt = (
        GraphPatchValidationReceipt.KERNEL_GRAPH_AND_LEASE_VALIDATED
    )


@dataclass(frozen=True, slots=True)
class GraphPatchProposalEvent:
    """One operator-visible decision before a PROPOSE graph rewrite.

    This records a decision about a fully validated, bounded candidate.  It
    has no execution authority by itself: only the separate accepted
    ``GraphPatchEvent`` changes the request-scoped JobGraph.
    """

    # proposal_id binds every later decision to the same candidate structure
    # and lease. event_id remains append-only and state-specific.
    proposal_id: str
    event_id: str
    patch: GraphPatch
    before_graph_digest: str
    after_graph_digest: str
    proposed_lease: GraphMutationLease
    status: GraphPatchProposalStatus
    content_hash: str


@dataclass(frozen=True, slots=True)
class ExecutionOriginBinding:
    binding_id: str
    intent_id: str
    intent_revision: int
    intent_hash: str
    pack_id: str
    pack_revision: int
    pack_digest: str
    delivery_digest: str
    item_count: int
    selected_bytes: int
    access_scope: str
    decision_context_id: str = ""
    decision_context_digest: str = ""
    oracle_contract_id: str = ""
    oracle_contract_digest: str = ""


@dataclass(frozen=True, slots=True)
class CompanyRunRequest:
    request_id: str
    job_id: str
    goal: str
    plan_proposal: PlanProposal
    roster: tuple[EmployeeRecord, ...]
    employee_skill_snapshots: Mapping[str, tuple[VersionedContent, ...]] = field(
        default_factory=dict
    )
    # Read-only external procedures selected for this one Job.  They are the
    # only skill lane a job-local specialist may use: a temporary employee
    # cannot inherit a persistent employee's private procedures or memory.
    # The field is frozen with the Work Order and is never a Skill Patch.
    job_local_skill_snapshots: tuple[VersionedContent, ...] = ()
    context_snapshot: ContextBundle = field(default_factory=ContextBundle)
    execution_origin: ExecutionOriginBinding | None = None
    runtime_limits: RunLimits = field(default_factory=RunLimits)
    action_policy: ActionPolicy = field(default_factory=ActionPolicy)
    job_limits: JobLimits = field(default_factory=JobLimits)
    company_revision: int = 0
    roster_revision: int = 0
    playbook_revision: int = 0
    workflow_context_fingerprint: str = ""
    workspace_identity_revision: str = ""
    workspace_identity_status: str = "NOT_APPLICABLE"
    workspace_identity_failure_code: str = ""
    session_key: str = ""
    # Immutable Manager provenance. These fields never grant an effect or
    # replace Firm Kernel admission; empty values retain historical ABI use.
    manager_employee_id: str = ""
    manager_assignment_digest: str = ""
    manager_session_key: str = ""
    # The Manager is outside the specialist staffing pool.  Its frozen
    # EmployeeRecord and authority-free delegation payload are carried
    # separately so the Kernel can validate and, for safe read-only terminal
    # integration, assign the existing final task to the Manager.
    manager_employee: EmployeeRecord | None = None
    manager_delegation_payload: Mapping[str, object] = field(default_factory=dict)
    manager_delegation_digest: str = ""
    # Planning is part of the immutable Company request, not a CLI-only
    # decoration applied after the Kernel has already settled its budget and
    # terminal ledger.  The provider rationale itself is deliberately absent:
    # only bounded, safe provenance and aggregate usage cross this boundary.
    planning_mode: str = "PRECOMPILED"
    planning_reason: str = "LEGACY_PRECOMPILED"
    compiler_usage: Usage = field(default_factory=Usage)
    compiler_provider_request_id: str | None = None
    work_order_id: str = ""
    work_order_digest: str = ""
    work_order_authority_digest: str = ""
    firm_admission_digest: str = ""
    # Secret-free digests of the exact provider routing, granted
    # ToolDefinition contract, and optional remote coordination authority
    # domain assembled for this Job. They do not grant authority; a same-Job
    # continuation must reproduce all digests before Runtime construction.
    runtime_provider_binding_digest: str = ""
    runtime_tool_contract_digest: str = ""
    runtime_company_coordination_digest: str = ""
    # Company Front Door facts are audit identity, not execution authority.
    # Legacy and non-product callers may leave the compatibility defaults;
    # production entry points freeze the exact operating decision here.
    company_work_mode: str = "UNSPECIFIED"
    coordination_policy: str = "PRECOMPILED"
    requested_effect: str = "UNSPECIFIED"
    operating_reason: str = "LEGACY_PRECOMPILED"
    # A graph Blueprint is immutable planning provenance.  It describes the
    # durable template selected before this request-scoped JobGraph exists;
    # it never replaces the JobGraph as Kernel execution authority.
    graph_blueprint_id: str = ""
    graph_blueprint_version: int = 0
    graph_blueprint_digest: str = ""
    graph_mutation_policy: str = "BOUNDED_AUTO"
    graph_constraints_digest: str = ""
    # These are the normalized, request-scoped projection of a durable Graph
    # Blueprint selection.  The selection itself lives outside the Kernel;
    # once a Job starts, its effective constraints are frozen here so that a
    # later UI/API edit cannot silently change a running graph.
    graph_pinned_employee_ids: tuple[str, ...] = ()
    graph_excluded_employee_ids: tuple[str, ...] = ()
    graph_require_independent_review: bool = False
    graph_max_concurrency: int | None = None
    graph_max_cost_usd: float | None = None
    graph_max_wall_time_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReplanContext:
    request: CompanyRunRequest
    graph: JobGraph
    trigger_task: JobTask
    signal: RunSignal
    roster: tuple[EmployeeRecord, ...]


@dataclass(frozen=True, slots=True)
class AttemptBudgetEvidence:
    model_calls: int
    tool_calls: int
    cost_usd: float
    wall_time_ceiling_ms: int


@dataclass(frozen=True, slots=True)
class TaskAttemptRecord:
    attempt_id: str
    task_id: str
    sequence: int
    employee_id: str
    source_attempt_id: str | None
    graph_version: int
    status: RunStatus
    failure_kind: AttemptFailureKind
    failure_code: str
    failure_detail: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    frozen_snapshot_hash: str
    capability_evidence: tuple[str, ...]
    capability_profile_digest: str
    capability_material_digest: str
    usage: Usage
    content_hash: str
    execution_instance_id: str = ""
    replica_group_id: str = ""


@dataclass(frozen=True, slots=True)
class JobMutationEvent:
    event_id: str
    sequence: int
    mutation_type: TaskMutationType
    task_id: str
    source_attempt_id: str
    source_attempt_content_hash: str
    target_attempt_id: str
    source_attempt_sequence: int
    target_attempt_sequence: int
    from_employee_id: str
    to_employee_id: str
    failure_kind: AttemptFailureKind
    rationale: str
    matched_capabilities: tuple[str, ...]
    downstream_task_ids: tuple[str, ...]
    mutation_budget_before: int
    mutation_budget_after: int
    next_attempt_reservation: AttemptBudgetEvidence
    frozen_snapshot_hash: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class JobMetrics:
    unique_employee_count: int
    temporary_role_count: int
    maximum_parallelism: int
    graph_patch_count: int
    usage: Usage
    task_mutation_count: int = 0
    organization_admission_count: int = 0
    manager_integration_count: int = 0
    execution_replica_count: int = 0
    replica_group_count: int = 0


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: str
    request_id: str
    status: JobStatus
    summary: str
    acceptance_evidence: tuple[str, ...]
    unresolved_issues: tuple[str, ...]
    task_results: tuple[EmployeeRunResult, ...]
    final_graph_version: int
    final_tasks: tuple[JobTask, ...]
    metrics: JobMetrics
    final_task_id: str = ""
    failure_reason: str = ""
    planning_mode: str = "PRECOMPILED"
    planning_reason: str = "LEGACY_PRECOMPILED"
    compiler_usage: Usage = field(default_factory=Usage)
    compiler_provider_request_id: str | None = None
    # The selected persistent Company Manager is a reporting authority, not
    # necessarily the employee that executed the terminal task. Keep the
    # binding with the result so product surfaces never infer it from prose.
    manager_employee_id: str = ""
    work_order_id: str = ""
    work_order_digest: str = ""
    work_order_authority_digest: str = ""
    firm_admission_digest: str = ""
    initial_company_work_mode: str = "UNSPECIFIED"
    company_work_mode: str = "UNSPECIFIED"
    coordination_policy: str = "PRECOMPILED"
    requested_effect: str = "UNSPECIFIED"
    operating_reason: str = "LEGACY_PRECOMPILED"
    attempt_records: tuple[TaskAttemptRecord, ...] = ()
    mutation_events: tuple[JobMutationEvent, ...] = ()
    graph_patch_events: tuple[GraphPatchEvent, ...] = ()
    graph_patch_proposal_events: tuple[GraphPatchProposalEvent, ...] = ()
    graph_blueprint_id: str = ""
    graph_blueprint_version: int = 0
    graph_blueprint_digest: str = ""
    graph_mutation_policy: str = "BOUNDED_AUTO"
    graph_constraints_digest: str = ""
    graph_pinned_employee_ids: tuple[str, ...] = ()
    graph_excluded_employee_ids: tuple[str, ...] = ()
    graph_require_independent_review: bool = False
    graph_max_concurrency: int | None = None
    graph_max_cost_usd: float | None = None
    graph_max_wall_time_ms: int | None = None
