from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.BUDGET_EXHAUSTED,
        }


class EventType(StrEnum):
    RUN_CREATED = "RUN_CREATED"
    RUN_STARTED = "RUN_STARTED"
    PROMPT_SNAPSHOTTED = "PROMPT_SNAPSHOTTED"
    MODEL_CALL_STARTED = "MODEL_CALL_STARTED"
    MODEL_STREAM_PROGRESS = "MODEL_STREAM_PROGRESS"
    MODEL_TEXT_DELTA = "MODEL_TEXT_DELTA"
    MODEL_CALL_COMPLETED = "MODEL_CALL_COMPLETED"
    MODEL_CALL_CANCELLED = "MODEL_CALL_CANCELLED"
    MODEL_RECOVERY_REQUESTED = "MODEL_RECOVERY_REQUESTED"
    CONTEXT_COMPACTED = "CONTEXT_COMPACTED"
    CONTEXT_ECONOMY_PROJECTED = "CONTEXT_ECONOMY_PROJECTED"
    VALIDATION_RECORDED = "VALIDATION_RECORDED"
    TOOL_INTENT_RECORDED = "TOOL_INTENT_RECORDED"
    TOOL_BATCH_PLANNED = "TOOL_BATCH_PLANNED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"
    APPROVAL_RESUME_CLAIMED = "APPROVAL_RESUME_CLAIMED"
    APPROVAL_RESUME_COMPLETED = "APPROVAL_RESUME_COMPLETED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_SUCCEEDED = "TOOL_SUCCEEDED"
    TOOL_FAILED = "TOOL_FAILED"
    TOOL_EFFECT_OUTCOME_UNKNOWN = "TOOL_EFFECT_OUTCOME_UNKNOWN"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    RUN_SUCCEEDED = "RUN_SUCCEEDED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"
    RUN_BUDGET_EXHAUSTED = "RUN_BUDGET_EXHAUSTED"


class FailureCategory(StrEnum):
    INPUT = "INPUT"
    MODEL = "MODEL"
    TOOL = "TOOL"
    POLICY = "POLICY"
    TIMEOUT = "TIMEOUT"
    VALIDATION = "VALIDATION"
    CANCEL = "CANCEL"
    INTERNAL = "INTERNAL"


class ToolEffect(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    EXECUTE = "EXECUTE"
    NETWORK = "NETWORK"
    EXTERNAL_COMMUNICATION = "EXTERNAL_COMMUNICATION"


class ToolRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    IRREVERSIBLE = "IRREVERSIBLE"


class IdempotencyMode(StrEnum):
    NONE = "NONE"
    CALL_KEY = "CALL_KEY"
    NATURAL_KEY = "NATURAL_KEY"


class PolicyDecision(StrEnum):
    DENY = "DENY"
    ALLOW = "ALLOW"


class CostEfficiencyMode(StrEnum):
    """Model-context policy selected by the operator, not a billing promise."""

    STANDARD = "standard"
    ECONOMY = "economy"


# Employee execution receives normalized first-party capabilities only.  The
# private foundation's native coordination and MCP-discovery surfaces are not
# valid ActionPolicy grants: organization changes belong to the Firm Kernel and
# external reads cross a first-party read_external_* contract below.
EXTERNAL_READ_TOOL_NAME = "read_external_context"
EXTERNAL_READ_TOOL_PREFIX = "read_external_"
RESERVED_EMPLOYEE_TOOL_NAMES = frozenset({"delegate_task"})
RESERVED_EMPLOYEE_TOOL_PREFIXES = ("mcp_",)


def is_reserved_employee_tool_name(name: str) -> bool:
    return name in RESERVED_EMPLOYEE_TOOL_NAMES or any(
        name.startswith(prefix) for prefix in RESERVED_EMPLOYEE_TOOL_PREFIXES
    )


def is_external_read_tool_name(name: str) -> bool:
    """Return whether a normalized first-party external-read name is used."""

    return name == EXTERNAL_READ_TOOL_NAME or name.startswith(EXTERNAL_READ_TOOL_PREFIX)


class ApprovalDecision(StrEnum):
    ALLOW_ONCE = "ALLOW_ONCE"
    ALLOW_SESSION = "ALLOW_SESSION"
    DENY = "DENY"
    # This is a terminal audit outcome, never a user-selectable choice.  It
    # prevents a terminal/UI fault from being misrepresented as a user denial.
    UNAVAILABLE = "UNAVAILABLE"


class ApprovalResumeState(StrEnum):
    WAITING = "WAITING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"


class EmployeeSessionRetention(StrEnum):
    PERSIST = "PERSIST"
    RUN_ONLY = "RUN_ONLY"


class SignalCode(StrEnum):
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    ASSIGNEE_MISMATCH = "ASSIGNEE_MISMATCH"
    ASSUMPTION_INVALIDATED = "ASSUMPTION_INVALIDATED"
    CONSTRAINT_CHANGED = "CONSTRAINT_CHANGED"
    # These codes describe a bounded Job-level exception after the local
    # Employee loop has exhausted its own recovery path. They are proposals
    # to the Firm Kernel, never topology or permission authority.
    VALIDATION_FAILED = "VALIDATION_FAILED"
    GRAPH_STALLED = "GRAPH_STALLED"
    USER_CORRECTION = "USER_CORRECTION"


class SemanticReplanOperation(StrEnum):
    """The small, topology-free vocabulary a runtime may propose.

    This deliberately lives below the Firm Kernel so an Employee Runtime, a
    Manager adapter, or a future GUI can express the same *intent* without
    gaining access to ``GraphPatch`` or Kernel state.  The Kernel still
    reconstructs and validates every concrete graph operation.
    """

    SPLIT = "SPLIT"
    JOIN = "JOIN"
    MERGE = "MERGE"
    CANCEL = "CANCEL"


@dataclass(frozen=True, slots=True)
class SemanticReplanDirective:
    """Typed semantic evidence for one bounded runtime graph proposal.

    ``capability_ids`` is used only by ``SPLIT``. ``task_ids`` identifies
    existing tasks for ``JOIN``, ``MERGE`` and ``CANCEL``.  Evidence refs are
    opaque, bounded identifiers (for example an Intent constraint revision or
    an Evidence Pack citation); they never contain raw prompt, transcript, or
    Knowledge content.  A directive is not a patch and grants no authority.
    """

    operation: SemanticReplanOperation
    task_ids: tuple[str, ...] = ()
    capability_ids: tuple[str, ...] = ()
    assumption_refs: tuple[str, ...] = ()
    constraint_refs: tuple[str, ...] = ()

    @staticmethod
    def _valid_refs(values: tuple[str, ...], *, maximum: int) -> bool:
        return (
            len(values) <= maximum
            and len(values) == len(set(values))
            and all(
                value.strip()
                and len(value.encode("utf-8")) <= 160
                and all(ord(character) >= 32 and ord(character) != 127 for character in value)
                for value in values
            )
        )

    @staticmethod
    def _valid_identifiers(values: tuple[str, ...], *, maximum: int) -> bool:
        return (
            len(values) <= maximum
            and len(values) == len(set(values))
            and all(
                value
                and len(value) <= 64
                and value[0].islower()
                and all(character.islower() or character.isdigit() or character == "_" for character in value)
                for value in values
            )
        )

    @staticmethod
    def _valid_task_identifiers(values: tuple[str, ...], *, maximum: int) -> bool:
        return (
            len(values) <= maximum
            and len(values) == len(set(values))
            and all(
                value
                and len(value) <= 64
                and value[0].islower()
                and all(
                    character.islower()
                    or character.isdigit()
                    or character in {"_", "-"}
                    for character in value
                )
                for value in values
            )
        )

    def verify(self) -> None:
        if not isinstance(self.operation, SemanticReplanOperation):
            raise ValueError("Semantic replan operation must be typed")
        if not self._valid_task_identifiers(self.task_ids, maximum=4):
            raise ValueError("Semantic replan task identifiers are invalid")
        if not self._valid_identifiers(self.capability_ids, maximum=4):
            raise ValueError("Semantic replan capability identifiers are invalid")
        if not self._valid_refs(self.assumption_refs, maximum=4):
            raise ValueError("Semantic replan assumption references are invalid")
        if not self._valid_refs(self.constraint_refs, maximum=4):
            raise ValueError("Semantic replan constraint references are invalid")
        if self.operation is SemanticReplanOperation.SPLIT:
            if not 2 <= len(self.capability_ids) <= 4 or self.task_ids:
                raise ValueError("Semantic split requires two to four capabilities only")
            if not self.assumption_refs:
                raise ValueError("Semantic split requires an assumption reference")
        elif self.operation is SemanticReplanOperation.JOIN:
            if len(self.task_ids) != 1 or self.capability_ids:
                raise ValueError("Semantic join requires one task identifier only")
            if not self.constraint_refs:
                raise ValueError("Semantic join requires a constraint reference")
        elif self.operation is SemanticReplanOperation.MERGE:
            if not 2 <= len(self.task_ids) <= 4 or self.capability_ids:
                raise ValueError("Semantic merge requires two to four task identifiers only")
            if not self.constraint_refs:
                raise ValueError("Semantic merge requires a constraint reference")
        elif self.operation is SemanticReplanOperation.CANCEL:
            if not 1 <= len(self.task_ids) <= 4 or self.capability_ids:
                raise ValueError("Semantic cancel requires one to four task identifiers only")
            if not self.constraint_refs:
                raise ValueError("Semantic cancel requires a constraint reference")


@dataclass(frozen=True, slots=True)
class VersionedContent:
    content_id: str
    revision: str
    content: str
    content_hash: str = ""


@dataclass(frozen=True, slots=True)
class TaskEvidenceItem:
    citation_id: str
    source_id: str
    source_revision: str
    title: str
    content: str
    source_hash: str
    content_hash: str
    location: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskEvidencePack:
    pack_id: str
    revision: int
    pack_digest: str
    delivery_digest: str
    access_scope: str
    items: tuple[TaskEvidenceItem, ...] = ()

    @property
    def selected_bytes(self) -> int:
        return sum(len(item.content.encode("utf-8")) for item in self.items)

    def delivery_payload(self) -> dict[str, object]:
        return {
            "pack_id": self.pack_id,
            "revision": self.revision,
            "pack_digest": self.pack_digest,
            "access_scope": self.access_scope,
            "items": [
                {
                    "citation_id": item.citation_id,
                    "source_id": item.source_id,
                    "source_revision": item.source_revision,
                    "title": item.title,
                    "content": item.content,
                    "source_hash": item.source_hash,
                    "content_hash": item.content_hash,
                    "location": dict(item.location),
                }
                for item in self.items
            ],
        }

    def computed_delivery_digest(self) -> str:
        encoded = json.dumps(
            self.delivery_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def redacted(self) -> "TaskEvidencePack":
        """Return the exact credential-redacted payload that may cross a provider boundary."""

        from .redaction import redact_runtime_value

        if self.delivery_digest:
            self.verify(max_items=20, max_bytes=64_000)
        value = redact_runtime_value(self.delivery_payload())
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise ValueError("Task Evidence Pack could not be safely redacted")
        fixed_identity = {
            "pack_id": self.pack_id,
            "revision": self.revision,
            "pack_digest": self.pack_digest,
            "access_scope": self.access_scope,
        }
        if any(value.get(key) != expected for key, expected in fixed_identity.items()):
            raise ValueError("Task Evidence Pack identity changed during redaction")
        if len(value["items"]) != len(self.items):
            raise ValueError("Task Evidence Pack item count changed during redaction")
        items: list[TaskEvidenceItem] = []
        for original, safe in zip(self.items, value["items"], strict=True):
            if not isinstance(safe, dict):
                raise ValueError("Task Evidence Pack item redaction is malformed")
            immutable = {
                "citation_id": original.citation_id,
                "source_id": original.source_id,
                "source_revision": original.source_revision,
                "source_hash": original.source_hash,
            }
            if any(safe.get(key) != expected for key, expected in immutable.items()):
                raise ValueError("Task Evidence Pack citation identity changed during redaction")
            content = str(safe.get("content") or "")
            location = safe.get("location", {})
            if not isinstance(location, dict):
                raise ValueError("Task Evidence Pack location redaction is malformed")
            items.append(
                TaskEvidenceItem(
                    citation_id=original.citation_id,
                    source_id=original.source_id,
                    source_revision=original.source_revision,
                    title=str(safe.get("title") or ""),
                    content=content,
                    source_hash=original.source_hash,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    location=location,
                )
            )
        provisional = TaskEvidencePack(
            pack_id=self.pack_id,
            revision=self.revision,
            pack_digest=self.pack_digest,
            delivery_digest="",
            access_scope=self.access_scope,
            items=tuple(items),
        )
        result = TaskEvidencePack(
            pack_id=provisional.pack_id,
            revision=provisional.revision,
            pack_digest=provisional.pack_digest,
            delivery_digest=provisional.computed_delivery_digest(),
            access_scope=provisional.access_scope,
            items=provisional.items,
        )
        result.verify(max_items=20, max_bytes=64_000)
        return result

    def verify(self, *, max_items: int = 6, max_bytes: int = 16_000) -> None:
        if max_items < 0 or max_items > 20 or max_bytes < 1 or max_bytes > 64_000:
            raise ValueError("Task Evidence Pack verification bounds are invalid")
        if (
            not self.pack_id.startswith("pack-")
            or self.revision < 1
            or not self.access_scope.strip()
            or len(self.access_scope.encode("utf-8")) > 256
        ):
            raise ValueError("Task Evidence Pack identity is invalid")
        for digest in (self.pack_digest, self.delivery_digest):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("Task Evidence Pack digest is invalid")
        if len(self.items) > max_items or self.selected_bytes > max_bytes:
            raise ValueError("Task Evidence Pack exceeds its delivery bounds")
        identities: set[tuple[str, str]] = set()
        citation_ids: set[str] = set()
        for item in self.items:
            identity = (item.source_id, item.source_revision)
            if identity in identities or item.citation_id in citation_ids:
                raise ValueError("Task Evidence Pack has duplicate citation identity")
            identities.add(identity)
            citation_ids.add(item.citation_id)
            if (
                not item.citation_id.startswith("evidence-")
                or len(item.citation_id.encode("utf-8")) > 128
                or not item.source_id.strip()
                or len(item.source_id.encode("utf-8")) > 256
                or not item.source_revision.strip()
                or len(item.source_revision.encode("utf-8")) > 256
                or len(item.title.encode("utf-8")) > 1024
            ):
                raise ValueError("Task Evidence Pack citation namespace is invalid")
            if len(item.source_hash) != 64 or any(
                character not in "0123456789abcdef" for character in item.source_hash
            ):
                raise ValueError("Task Evidence Pack source hash is invalid")
            if hashlib.sha256(item.content.encode("utf-8")).hexdigest() != item.content_hash:
                raise ValueError("Task Evidence Pack content hash is invalid")
            try:
                location_bytes = len(
                    json.dumps(
                        dict(item.location),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("Task Evidence Pack location metadata is invalid") from exc
            if location_bytes > 8192:
                raise ValueError("Task Evidence Pack location metadata exceeds its bound")
        try:
            payload_bytes = len(
                json.dumps(
                    self.delivery_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Task Evidence Pack payload is not canonical JSON") from exc
        maximum_payload_bytes = max(16_384, min(96_000, max_bytes + 32_000))
        if payload_bytes > maximum_payload_bytes:
            raise ValueError("Task Evidence Pack total serialized payload exceeds its bound")
        if self.computed_delivery_digest() != self.delivery_digest:
            raise ValueError("Task Evidence Pack delivery digest is invalid")


@dataclass(frozen=True, slots=True)
class EmployeeCapabilityProfile:
    """Frozen, content-free proof of one EmployeeRun's real capability.

    ``profile_digest`` binds the exact Employee identity and private-state
    namespace. ``material_digest`` excludes identity-only differences so the
    Firm Kernel can detect role/prompt clones that add no task-relevant
    capability.
    """

    employee_id: str
    roster_revision: int
    model_profile: str
    capability_ids: tuple[str, ...]
    skill_revision_refs: tuple[str, ...]
    tool_names: tuple[str, ...]
    tool_grant_digest: str
    permission_effects: tuple[str, ...]
    permission_digest: str
    knowledge_scopes: tuple[str, ...]
    memory_namespace: str
    memory_revision_refs: tuple[str, ...]
    session_policy: str
    validator_ids: tuple[str, ...]
    evaluation_revision: str
    profile_digest: str
    material_digest: str
    schema_version: str = "noruct.employee-capability-profile.v1"

    @staticmethod
    def _digest(payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def exact_payload(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "employee_id": self.employee_id,
            "roster_revision": self.roster_revision,
            "model_profile": self.model_profile,
            "capability_ids": self.capability_ids,
            "skill_revision_refs": self.skill_revision_refs,
            "tool_names": self.tool_names,
            "tool_grant_digest": self.tool_grant_digest,
            "permission_effects": self.permission_effects,
            "permission_digest": self.permission_digest,
            "knowledge_scopes": self.knowledge_scopes,
            "memory_namespace": self.memory_namespace,
            "memory_revision_refs": self.memory_revision_refs,
            "session_policy": self.session_policy,
            "validator_ids": self.validator_ids,
            "evaluation_revision": self.evaluation_revision,
        }

    def material_payload(self) -> Mapping[str, Any]:
        payload = dict(self.exact_payload())
        payload.pop("employee_id")
        payload.pop("roster_revision")
        # An empty private namespace is not expertise. Only selected, versioned
        # memory makes the state boundary materially different for this Run.
        if not self.memory_revision_refs:
            payload.pop("memory_namespace")
        return payload

    def verify(self) -> None:
        if (
            not self.employee_id.strip()
            or self.roster_revision < 0
            or not self.model_profile.strip()
            or not self.memory_namespace.strip()
            or not self.session_policy.strip()
            or not self.evaluation_revision.strip()
        ):
            raise ValueError("Employee capability profile identity is incomplete")
        for values in (
            self.capability_ids,
            self.skill_revision_refs,
            self.tool_names,
            self.permission_effects,
            self.knowledge_scopes,
            self.memory_revision_refs,
            self.validator_ids,
        ):
            if tuple(sorted(set(values))) != values or any(not item.strip() for item in values):
                raise ValueError("Employee capability profile tuples must be sorted and unique")
        for digest in (
            self.tool_grant_digest,
            self.permission_digest,
            self.profile_digest,
            self.material_digest,
        ):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("Employee capability profile digest is invalid")
        if self._digest(self.exact_payload()) != self.profile_digest:
            raise ValueError("Employee capability profile exact digest mismatch")
        if self._digest(self.material_payload()) != self.material_digest:
            raise ValueError("Employee capability profile material digest mismatch")

    @classmethod
    def create(
        cls,
        *,
        employee_id: str,
        roster_revision: int,
        model_profile: str,
        capability_ids: Sequence[str],
        skill_revision_refs: Sequence[str],
        tool_names: Sequence[str],
        tool_grant_digest: str,
        permission_effects: Sequence[str],
        permission_digest: str,
        knowledge_scopes: Sequence[str],
        memory_namespace: str,
        memory_revision_refs: Sequence[str],
        session_policy: str,
        validator_ids: Sequence[str],
        evaluation_revision: str,
    ) -> "EmployeeCapabilityProfile":
        values = {
            "employee_id": employee_id.strip(),
            "roster_revision": roster_revision,
            "model_profile": model_profile.strip(),
            "capability_ids": tuple(sorted(set(capability_ids))),
            "skill_revision_refs": tuple(sorted(set(skill_revision_refs))),
            "tool_names": tuple(sorted(set(tool_names))),
            "tool_grant_digest": tool_grant_digest,
            "permission_effects": tuple(sorted(set(permission_effects))),
            "permission_digest": permission_digest,
            "knowledge_scopes": tuple(sorted(set(knowledge_scopes))),
            "memory_namespace": memory_namespace.strip(),
            "memory_revision_refs": tuple(sorted(set(memory_revision_refs))),
            "session_policy": session_policy.strip(),
            "validator_ids": tuple(sorted(set(validator_ids))),
            "evaluation_revision": evaluation_revision.strip(),
            "schema_version": "noruct.employee-capability-profile.v1",
        }
        exact = cls._digest(values)
        material = dict(values)
        material.pop("employee_id")
        material.pop("roster_revision")
        if not values["memory_revision_refs"]:
            material.pop("memory_namespace")
        profile = cls(
            **values,
            profile_digest=exact,
            material_digest=cls._digest(material),
        )
        profile.verify()
        return profile


@dataclass(frozen=True, slots=True)
class EmployeeSnapshot:
    employee_id: str
    role: str
    capabilities: tuple[str, ...] = ()
    temporary: bool = False
    prompt_template_id: str = "native-employee-v1"
    prompt_revision: str = "1"
    skills: tuple[VersionedContent, ...] = ()
    memory_namespace: str = ""
    selected_memory_refs: tuple[str, ...] = ()
    model_profile: str = "scripted"
    tool_grant_profile: str = "default-deny"
    authority_revision: str = "1"
    capability_profile: EmployeeCapabilityProfile | None = None


@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    job_id: str
    job_graph_version: int
    task_id: str
    attempt: int
    objective: str
    required_capabilities: tuple[str, ...] = ()
    input_artifact_refs: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    risk_level: str = "LOW"
    expected_output_kind: str = "structured_completion"


@dataclass(frozen=True, slots=True)
class ContextBundle:
    company_policy_excerpt: str = ""
    task_dependencies: tuple[VersionedContent, ...] = ()
    selected_facts: tuple[VersionedContent, ...] = ()
    selected_memory: tuple[VersionedContent, ...] = ()
    ephemeral_instructions: tuple[str, ...] = ()
    task_evidence: TaskEvidencePack | None = None
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Per-run execution envelope.

    Product defaults are deliberately long-running.  They must not turn a
    normal repository task into a three-minute failure.  They remain concrete
    values so cancellation, durable accounting, and recovery can always name
    a terminal boundary; users may still choose a narrower future-job envelope
    in global settings or a Graph Blueprint.
    """

    max_wall_time_ms: int = 86_400_000
    max_model_calls: int = 2_048
    max_tool_calls: int = 8_192
    max_input_tokens: int = 1_000_000
    max_output_tokens: int = 200_000
    max_cost_usd: float = 1_000_000.0
    max_consecutive_errors: int = 2
    max_result_bytes: int = 256_000
    max_tool_output_bytes: int = 256_000
    max_context_messages: int = 32
    max_context_chars: int = 120_000
    context_keep_recent_messages: int = 12
    cost_efficiency_mode: CostEfficiencyMode = CostEfficiencyMode.STANDARD


@dataclass(frozen=True, slots=True)
class ToolGrant:
    tool_name: str
    allowed_effects: tuple[ToolEffect, ...]
    resource_patterns: tuple[str, ...] = ("*",)
    max_calls: int = 1
    max_cost_usd: float | None = None
    requires_approval: bool = False


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    default_decision: PolicyDecision = PolicyDecision.DENY
    tool_grants: tuple[ToolGrant, ...] = ()
    approval_grants: tuple[str, ...] = ()
    network_policy: str = "DENY"
    filesystem_policy: str = "READ_ONLY"
    secret_refs: tuple[str, ...] = ()
    sandbox_profile: str = "none"
    # The policy is still default-deny: this is only a review-friction
    # projection for tools that are already explicitly granted below.  The
    # Company records every ToolIntent either way.  Keeping the trusted names
    # in the immutable Job policy means an ACP/CLI/TUI client cannot silently
    # broaden a running Job after it has started.
    capability_trust_mode: str = "strict"
    auto_approved_tool_names: tuple[str, ...] = ()

    def auto_approves(self, tool_name: str) -> bool:
        """Whether this frozen policy removes an interactive prompt for a grant.

        This does not grant a tool, change its effect, or bypass resource and
        call-count checks.  It merely replaces a repeated dialog with the
        operator's already-selected trust profile.
        """

        return tool_name in self.auto_approved_tool_names


@dataclass(frozen=True, slots=True)
class EmployeeRunRequest:
    request_id: str
    employee: EmployeeSnapshot
    task: TaskEnvelope
    context: ContextBundle
    limits: RunLimits
    action_policy: ActionPolicy
    # A caller-owned continuity key.  Execution implementations may use this
    # to resume conversation state, but the key and the resulting history stay
    # behind the first-party Employee Execution Port.
    session_key: str = ""
    session_retention: EmployeeSessionRetention = EmployeeSessionRetention.PERSIST
    requested_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class RunHandle:
    run_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class Usage:
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def plus(self, other: Usage) -> Usage:
        return Usage(
            model_calls=self.model_calls + other.model_calls,
            tool_calls=self.tool_calls + other.tool_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 12),
        )


@dataclass(frozen=True, slots=True)
class Failure:
    code: str
    category: FailureCategory
    message_safe: str
    retryable: bool = False
    origin: str = "native-runtime"
    details_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RunSignal:
    code: SignalCode
    value: str = ""
    evidence: tuple[str, ...] = ()
    semantic_replan: SemanticReplanDirective | None = None


@dataclass(frozen=True, slots=True)
class CompletionEnvelope:
    summary: str
    artifact_refs: tuple[str, ...] = ()
    acceptance_evidence: tuple[str, ...] = ()
    unresolved_issues: tuple[str, ...] = ()
    suggested_followups: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    signals: tuple[RunSignal, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletionValidation:
    passed: bool
    failed_checks: tuple[str, ...] = ()
    semantic_expectation: str = ""


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    action_id: str
    run_id: str
    job_id: str
    task_id: str
    employee_id: str
    tool_name: str
    effect: ToolEffect
    risk: ToolRisk
    resource_key: str
    preview: str
    allow_session: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    request: ApprovalRequest
    request_hash: str
    decision: ApprovalDecision | None
    decided_by: str | None
    resume_state: ApprovalResumeState
    created_at: datetime
    resolved_at: datetime | None = None
    resume_claimed_at: datetime | None = None
    resume_completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApprovalResolutionReceipt:
    approval: ApprovalRecord
    applied: bool


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    ok: bool
    content: str
    action_id: str
    error_code: str | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: Any
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSchema:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSchema, ...]
    model_profile: str
    run_id: str
    call_index: int


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    completion: CompletionEnvelope | None = None
    usage: Usage = field(default_factory=Usage)
    provider_request_id: str | None = None
    finish_reason: str = "stop"


@dataclass(frozen=True, slots=True)
class ModelStreamProgress:
    chunk_count: int
    received_chars: int
    finished: bool = False


@dataclass(frozen=True, slots=True)
class StructuredOutputRequest:
    messages: tuple[ModelMessage, ...]
    schema_name: str
    json_schema: Mapping[str, Any]
    model_profile: str
    request_id: str
    call_index: int = 1


@dataclass(frozen=True, slots=True)
class StructuredOutputResponse:
    value: Mapping[str, Any]
    usage: Usage = field(default_factory=Usage)
    provider_request_id: str | None = None
    finish_reason: str = "stop"


from .model_results import (  # noqa: E402
    CancelReceipt,
    EmployeeRunResult,
    PromptSnapshot,
    RunEvent,
    to_primitive,
)

from .model_validation import (  # noqa: E402
    failure_from_dict,
    result_from_dict,
    semantic_replan_directive_from_dict,
    signal_from_dict,
    usage_from_dict,
    validate_request,
)
