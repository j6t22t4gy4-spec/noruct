from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from dynamic_firm.kernel.models import (
    ExecutionReplicaPreference,
    ExecutionReplicaStrategy,
    PlanProposal,
)
from dynamic_firm.runtime.models import Usage


class PlanningMode(StrEnum):
    DIRECT = "DIRECT"
    BLUEPRINT = "BLUEPRINT"
    DYNAMIC = "DYNAMIC"
    SOLO = "SOLO"
    SOLO_FALLBACK = "SOLO_FALLBACK"


class CompilerExecutionProfile(StrEnum):
    READ_ONLY = "READ_ONLY"
    HOST_ACTION = "HOST_ACTION"
    HOST_DIRECT = "HOST_DIRECT"
    SHADOW_CODING = "SHADOW_CODING"

    @property
    def requires_implementation(self) -> bool:
        """Whether the request explicitly asks for a code/workspace change."""

        return self in {
            CompilerExecutionProfile.HOST_DIRECT,
            CompilerExecutionProfile.SHADOW_CODING,
        }

    @property
    def allows_workspace_mutation(self) -> bool:
        """Compatibility alias for the former coding-shape predicate."""

        return self.requires_implementation

    @property
    def permits_host_actions(self) -> bool:
        """Whether approved host tools may be relevant to the execution lane."""

        return self in {
            CompilerExecutionProfile.HOST_ACTION,
            CompilerExecutionProfile.HOST_DIRECT,
        }


class CompilerReason(StrEnum):
    DIRECT_USER_MESSAGE = "DIRECT_USER_MESSAGE"
    BLUEPRINT_REUSED = "BLUEPRINT_REUSED"
    SOLO_FIRST_ATTEMPT = "SOLO_FIRST_ATTEMPT"
    VALID_DYNAMIC = "VALID_DYNAMIC"
    VALID_SOLO = "VALID_SOLO"
    COMPILER_SKIPPED_BUDGET = "COMPILER_SKIPPED_BUDGET"
    COMPILER_UNAVAILABLE = "COMPILER_UNAVAILABLE"
    COMPILER_CONTEXT_UNAVAILABLE = "COMPILER_CONTEXT_UNAVAILABLE"
    COMPILER_PROVIDER_FAILURE = "COMPILER_PROVIDER_FAILURE"
    COMPILER_OUTPUT_INVALID = "COMPILER_OUTPUT_INVALID"
    COMPILER_PROPOSAL_REJECTED = "COMPILER_PROPOSAL_REJECTED"
    COMPILER_REQUIRED_REVIEW_MISSING = "COMPILER_REQUIRED_REVIEW_MISSING"
    COMPILER_BUDGET_EXHAUSTED = "COMPILER_BUDGET_EXHAUSTED"
    COMPILER_WALL_TIME_EXHAUSTED = "COMPILER_WALL_TIME_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class PlanningOwner:
    """Bounded identity for the model that makes a coordination proposal.

    It deliberately carries no authority, credentials, memory body, tool
    grant, or mutable Company state.  The Compiler uses it only to make the
    persistent Manager's semantic planning responsibility auditable while
    retaining the existing structured-output parser and validator.
    """

    employee_id: str
    role: str
    assignment_digest: str
    session_key: str

    def __post_init__(self) -> None:
        values = (self.employee_id, self.role, self.assignment_digest, self.session_key)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("Planning owner requires complete identity")
        if len(self.employee_id) > 192 or len(self.role) > 192:
            raise ValueError("Planning owner identity is too long")
        if (
            len(self.assignment_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.assignment_digest)
        ):
            raise ValueError("Planning owner assignment digest is invalid")
        if len(self.session_key.encode("utf-8")) > 320 or any(
            ord(character) < 32 or ord(character) == 127
            for character in self.session_key
        ):
            raise ValueError("Planning owner session key is invalid")


@dataclass(frozen=True, slots=True)
class ManagerPlanningSkill:
    """One selected, revision-bound Manager procedure summary."""

    skill_key: str
    revision: str
    purpose: str
    content_hash: str

    def __post_init__(self) -> None:
        values = (self.skill_key, self.revision, self.purpose, self.content_hash)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("Manager planning Skill requires complete content")
        if len(self.skill_key) > 128 or len(self.revision) > 64 or len(self.purpose) > 360:
            raise ValueError("Manager planning Skill exceeds its bounded projection")
        if len(self.content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_hash
        ):
            raise ValueError("Manager planning Skill content hash is invalid")


@dataclass(frozen=True, slots=True)
class ManagerOutcomeSummary:
    """Aggregated, context-bound outcome signal for one planning decision."""

    context_fingerprint: str
    observed_count: int
    succeeded_count: int
    safety_passed_count: int
    effect_passed_count: int

    def __post_init__(self) -> None:
        if len(self.context_fingerprint) > 128:
            raise ValueError("Manager outcome context fingerprint is invalid")
        counts = (
            self.observed_count,
            self.succeeded_count,
            self.safety_passed_count,
            self.effect_passed_count,
        )
        if any(type(value) is not int or value < 0 or value > 24 for value in counts):
            raise ValueError("Manager outcome summary count is invalid")
        if any(value > self.observed_count for value in counts[1:]):
            raise ValueError("Manager outcome summary is inconsistent")


@dataclass(frozen=True, slots=True)
class ManagerKnowledgeCitation:
    """Content-free citation identity visible to a Manager planning call."""

    citation_id: str
    source_id: str
    source_revision: str

    def __post_init__(self) -> None:
        if (
            not self.citation_id.startswith("evidence-")
            or not self.source_id.strip()
            or not self.source_revision.strip()
            or any(len(value.encode("utf-8")) > maximum for value, maximum in (
                (self.citation_id, 128), (self.source_id, 256), (self.source_revision, 256)
            ))
        ):
            raise ValueError("Manager Knowledge citation is invalid")


@dataclass(frozen=True, slots=True)
class ManagerPlanningBrief:
    """Small, secret-free operating context for one Manager planning call.

    It contains selected Company purpose, Work Order constraints, relevant
    Manager Skill revisions, aggregated observed outcomes and an optional
    content-free Knowledge citation index. It cannot carry raw Knowledge,
    evidence excerpts, employee-memory bodies, credentials, tool grants, or
    mutable Company state.
    """

    company_revision: int
    company_purpose: str
    work_order_constraints: tuple[str, ...]
    skills: tuple[ManagerPlanningSkill, ...]
    outcome_summary: ManagerOutcomeSummary
    knowledge_pack_id: str = ""
    knowledge_pack_digest: str = ""
    knowledge_delivery_digest: str = ""
    knowledge_citations: tuple[ManagerKnowledgeCitation, ...] = ()

    def __post_init__(self) -> None:
        if type(self.company_revision) is not int or self.company_revision < 1:
            raise ValueError("Manager planning brief requires a Company revision")
        if not self.company_purpose.strip() or len(self.company_purpose) > 1_000:
            raise ValueError("Manager planning brief purpose is invalid")
        if len(self.work_order_constraints) > 6 or len(self.skills) > 3:
            raise ValueError("Manager planning brief exceeds its bounded projection")
        if any(
            not isinstance(item, str) or not item.strip() or len(item) > 360
            for item in self.work_order_constraints
        ):
            raise ValueError("Manager planning brief constraint is invalid")
        if len({item.skill_key for item in self.skills}) != len(self.skills):
            raise ValueError("Manager planning brief Skill keys must be unique")
        provided = (self.knowledge_pack_id, self.knowledge_pack_digest, self.knowledge_delivery_digest)
        if any(provided) and (not self.knowledge_pack_id.startswith("pack-") or any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in provided[1:]
        )):
            raise ValueError("Manager planning brief Knowledge identity is invalid")
        if self.knowledge_citations and not all(provided):
            raise ValueError("Manager Knowledge citations require a frozen Evidence Pack")
        if len(self.knowledge_citations) > 6:
            raise ValueError("Manager planning brief exceeds its Knowledge citation bound")
        if len({item.citation_id for item in self.knowledge_citations}) != len(self.knowledge_citations):
            raise ValueError("Manager Knowledge citations must be unique")

    @property
    def content_digest(self) -> str:
        encoded = json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkflowPriorTask:
    task_key: str
    required_capabilities: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    final: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowPrior:
    pattern_id: str
    task_family: str
    context_fingerprint: str
    execution_profile: CompilerExecutionProfile
    rationale: str
    tasks: tuple[WorkflowPriorTask, ...]
    evidence_count: int


@dataclass(frozen=True, slots=True)
class CompilerRequest:
    request_id: str
    goal: str
    workspace_manifest: tuple[str, ...]
    available_capabilities: tuple[str, ...]
    model_profile: str
    execution_profile: CompilerExecutionProfile = CompilerExecutionProfile.READ_ONLY
    workflow_context_fingerprint: str = ""
    workflow_priors: tuple[WorkflowPrior, ...] = ()
    max_tasks: int = 6
    max_temporary_roles: int = 2
    max_total_model_calls: int = 8
    # This is the caller-owned planning slice of the Job wall-time ceiling.
    # Callers that own a larger lifecycle must pass the current remaining
    # budget rather than relying on this compatibility default.
    max_wall_time_ms: int = 30_000
    requires_independent_review: bool = False
    required_final_action_capability: str = ""
    execution_replica_preference: ExecutionReplicaPreference = (
        ExecutionReplicaPreference.PERFORMANCE_FIRST
    )
    suggested_execution_replica_strategy: ExecutionReplicaStrategy | None = None
    # Set only for a Manager-capable Company. The structured planning model is
    # then an auditable Manager proposal, while Compiler parsing and Kernel
    # admission remain deterministic and authority-free.
    planning_owner: PlanningOwner | None = None
    manager_planning_brief: ManagerPlanningBrief | None = None


@dataclass(frozen=True, slots=True)
class CompilerDecision:
    proposal: PlanProposal
    mode: PlanningMode
    reason: CompilerReason
    rationale: str
    usage: Usage = field(default_factory=Usage)
    provider_request_id: str | None = None
    exposed_workflow_prior_ids: tuple[str, ...] = ()
    aligned_workflow_prior_ids: tuple[str, ...] = ()
    planning_owner_id: str = ""
    planning_owner_assignment_digest: str = ""
    manager_planning_brief_digest: str = ""
