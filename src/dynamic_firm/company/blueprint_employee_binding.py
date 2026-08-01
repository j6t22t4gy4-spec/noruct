"""Content-free Employee boundary pins for reusable Graph Blueprints.

The binding is evidence about EmployeeRunRequests already constructed from a
frozen CompanyRunRequest.  It is not a staffing command, provider fallback,
Work Order mutation, or Kernel admission bypass.  Substitution results are
data-only proposals for a future ordinary Work Order.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobTask,
    TaskAttemptRecord,
)
from dynamic_firm.kernel.mutation import content_digest, frozen_snapshot_digest
from dynamic_firm.kernel.policy_request import frozen_employee_action_policy
from dynamic_firm.runtime.employee_capability import (
    EMPLOYEE_MATERIAL_PROFILE_DIMENSIONS,
    build_employee_capability_profile,
    material_profile_dimension_digests,
)
from dynamic_firm.runtime.knowledge_retrieval import BoundedKnowledgeRetriever
from dynamic_firm.runtime.models import (
    EmployeeCapabilityProfile,
    EmployeeRunRequest,
    EmployeeSessionRetention,
    RunStatus,
)

from .graph_blueprint_models import BlueprintBinding


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CANDIDATES = 64
_MAX_PROJECTED_CHOICES = 8
_SUBSTITUTION_DIFFERENCE_DIMENSIONS = frozenset(
    (
        *EMPLOYEE_MATERIAL_PROFILE_DIMENSIONS,
        "runtime_provider_binding",
        "runtime_tool_contract",
        "runtime_action_policy",
        "runtime_company_coordination",
    )
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _require_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a SHA-256 digest")


def _require_bounded_text(value: str, label: str, *, maximum_bytes: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum_bytes
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} is invalid")


def _runtime_boundary_digest(
    *,
    provider_binding_digest: str,
    tool_contract_digest: str,
    action_policy_digest: str,
    company_coordination_digest: str,
    session_policy: str,
    evaluation_revision: str,
) -> str:
    return _digest(
        {
            "provider_binding_digest": provider_binding_digest,
            "tool_contract_digest": tool_contract_digest,
            "action_policy_digest": action_policy_digest,
            "company_coordination_digest": company_coordination_digest,
            "session_policy": session_policy,
            "evaluation_revision": evaluation_revision,
        }
    )


@dataclass(frozen=True, slots=True)
class BlueprintEmployeePin:
    """One actual task dispatch surface, reduced to content-free digests."""

    task_id: str
    employee_id: str
    required_capabilities: tuple[str, ...]
    profile_digest: str
    material_digest: str
    model_profile: str
    material_dimension_digests: tuple[tuple[str, str], ...]
    runtime_provider_binding_digest: str
    runtime_tool_contract_digest: str
    runtime_action_policy_digest: str
    runtime_company_coordination_digest: str
    runtime_boundary_digest: str
    run_limits_digest: str
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_bounded_text(self.task_id, "Blueprint task id", maximum_bytes=256)
        _require_bounded_text(
            self.employee_id,
            "Blueprint Employee id",
            maximum_bytes=256,
        )
        _require_bounded_text(
            self.model_profile,
            "Blueprint Employee model profile",
            maximum_bytes=512,
        )
        if (
            not self.required_capabilities
            or tuple(sorted(set(self.required_capabilities)))
            != self.required_capabilities
        ):
            raise ValueError("Blueprint Employee pin capabilities are not canonical")
        names = tuple(name for name, _ in self.material_dimension_digests)
        if names != EMPLOYEE_MATERIAL_PROFILE_DIMENSIONS:
            raise ValueError("Blueprint Employee pin dimensions are invalid")
        for _, value in self.material_dimension_digests:
            _require_digest(value, "Material dimension digest")
        for label, value in (
            ("Profile digest", self.profile_digest),
            ("Material digest", self.material_digest),
            ("Provider binding digest", self.runtime_provider_binding_digest),
            ("Tool contract digest", self.runtime_tool_contract_digest),
            ("ActionPolicy digest", self.runtime_action_policy_digest),
            ("Company coordination digest", self.runtime_company_coordination_digest),
            ("Runtime boundary digest", self.runtime_boundary_digest),
            ("Run limits digest", self.run_limits_digest),
        ):
            _require_digest(value, label)
        object.__setattr__(self, "content_digest", content_digest(self.canonical_payload()))

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "task_id": self.task_id,
            "employee_id": self.employee_id,
            "required_capabilities": self.required_capabilities,
            "profile_digest": self.profile_digest,
            "material_digest": self.material_digest,
            "model_profile": self.model_profile,
            "material_dimension_digests": self.material_dimension_digests,
            "runtime_provider_binding_digest": self.runtime_provider_binding_digest,
            "runtime_tool_contract_digest": self.runtime_tool_contract_digest,
            "runtime_action_policy_digest": self.runtime_action_policy_digest,
            "runtime_company_coordination_digest": self.runtime_company_coordination_digest,
            "runtime_boundary_digest": self.runtime_boundary_digest,
            "run_limits_digest": self.run_limits_digest,
        }


@dataclass(frozen=True, slots=True)
class BlueprintExecutionBinding:
    """Bind actual Employee boundaries to one Blueprint and frozen request."""

    blueprint_binding_digest: str
    request_snapshot_digest: str
    request_execution_envelope_digest: str
    work_order_id: str
    work_order_digest: str
    employee_pins: tuple[BlueprintEmployeePin, ...]
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("Blueprint binding digest", self.blueprint_binding_digest),
            ("Request snapshot digest", self.request_snapshot_digest),
            (
                "Request execution envelope digest",
                self.request_execution_envelope_digest,
            ),
            ("Work Order digest", self.work_order_digest),
        ):
            _require_digest(value, label)
        _require_bounded_text(
            self.work_order_id,
            "Blueprint execution Work Order id",
            maximum_bytes=256,
        )
        task_ids = tuple(pin.task_id for pin in self.employee_pins)
        if (
            not task_ids
            or len(task_ids) > 64
            or tuple(sorted(task_ids)) != task_ids
            or len(task_ids) != len(set(task_ids))
        ):
            raise ValueError("Blueprint execution binding pins must be sorted and unique")
        object.__setattr__(self, "content_digest", content_digest(self.canonical_payload()))

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "schema": "noruct.blueprint-execution-binding.v1",
            "blueprint_binding_digest": self.blueprint_binding_digest,
            "request_snapshot_digest": self.request_snapshot_digest,
            "request_execution_envelope_digest": (
                self.request_execution_envelope_digest
            ),
            "work_order_id": self.work_order_id,
            "work_order_digest": self.work_order_digest,
            "employee_pins": tuple(pin.canonical_payload() for pin in self.employee_pins),
        }

    def verify(self) -> None:
        if content_digest(self.canonical_payload()) != self.content_digest:
            raise ValueError("Blueprint execution binding digest is invalid")
        for pin in self.employee_pins:
            if content_digest(pin.canonical_payload()) != pin.content_digest:
                raise ValueError("Blueprint Employee pin digest is invalid")


def _employees(request: CompanyRunRequest) -> Mapping[str, EmployeeRecord]:
    values = {employee.employee_id: employee for employee in request.roster}
    if request.manager_employee is not None:
        values[request.manager_employee.employee_id] = request.manager_employee
    return values


def _validate_binding_identity(
    binding: BlueprintBinding,
    request: CompanyRunRequest,
) -> None:
    ref = binding.blueprint_ref
    if content_digest(binding.canonical_payload()) != binding.content_digest:
        raise ValueError("Blueprint binding content digest is invalid")
    if (
        binding.work_order_id != request.work_order_id
        or binding.work_order_digest != request.work_order_digest
        or binding.proposal != request.plan_proposal
        or ref.blueprint_id != request.graph_blueprint_id
        or ref.version != request.graph_blueprint_version
        or ref.content_digest != request.graph_blueprint_digest
        or binding.constraints.mutation_policy.value != request.graph_mutation_policy
        or binding.constraints.pinned_employee_ids != request.graph_pinned_employee_ids
        or binding.constraints.excluded_employee_ids != request.graph_excluded_employee_ids
        or binding.constraints.require_independent_review
        is not request.graph_require_independent_review
        or binding.constraints.max_concurrency != request.graph_max_concurrency
        or binding.constraints.max_cost_usd != request.graph_max_cost_usd
        or binding.constraints.max_wall_time_ms != request.graph_max_wall_time_ms
        or content_digest(binding.constraints) != request.graph_constraints_digest
    ):
        raise ValueError("Blueprint binding does not match the frozen Company request")
    for label, value in (
        ("Runtime provider binding", request.runtime_provider_binding_digest),
        ("Runtime tool contract", request.runtime_tool_contract_digest),
        ("Runtime Company coordination", request.runtime_company_coordination_digest),
    ):
        _require_digest(value, label)


def _expected_validators(
    request: CompanyRunRequest,
    task: JobTask,
    employee: EmployeeRecord,
) -> tuple[str, ...]:
    values = ["structured-completion-v1"]
    if task.acceptance_criteria:
        values.append("task-acceptance-v1")
    if "review" in employee.capabilities:
        values.append("independent-review-v1")
    if employee.employee_id == request.manager_employee_id and task.task_id == request.plan_proposal.final_task_id:
        values.append("manager-integration-v1")
    return tuple(sorted(values))


def _validate_runtime_request(
    *,
    request: CompanyRunRequest,
    task: JobTask,
    runtime_request: EmployeeRunRequest,
) -> EmployeeCapabilityProfile:
    employees = _employees(request)
    employee = employees.get(runtime_request.employee.employee_id)
    if employee is None or not employee.active:
        raise ValueError("Employee dispatch is absent from the frozen ROSTER")
    if employee.employee_id in request.graph_excluded_employee_ids:
        raise ValueError("Employee dispatch violates the frozen Blueprint exclusion")
    graph = graph_from_proposal(
        request.plan_proposal,
        max_tasks=request.job_limits.max_tasks,
    )
    if (
        runtime_request.request_id
        != (
            f"{request.request_id}:{task.task_id}:attempt-{task.attempt}:"
            f"graph-{graph.version}"
        )
        or runtime_request.task.job_id != request.job_id
        or runtime_request.task.task_id != task.task_id
        or runtime_request.task.job_graph_version != graph.version
        or runtime_request.task.attempt != task.attempt
        or runtime_request.task.objective != task.objective
        or runtime_request.task.required_capabilities != task.required_capabilities
        or runtime_request.task.acceptance_criteria != task.acceptance_criteria
        or runtime_request.task.risk_level != task.risk_level
        or runtime_request.task.input_artifact_refs
        or runtime_request.task.expected_output_kind != "structured_completion"
        or runtime_request.employee.role != employee.role
        or runtime_request.employee.temporary is not employee.temporary
        or runtime_request.employee.model_profile != employee.model_profile
        or runtime_request.employee.capabilities != employee.capabilities
        or not set(task.required_capabilities).issubset(employee.capabilities)
    ):
        raise ValueError("Employee dispatch does not match the frozen task or ROSTER")
    expected_policy = frozen_employee_action_policy(
        company=request,
        graph=graph,
        task=task,
        employee_id=employee.employee_id,
    )
    if runtime_request.action_policy != expected_policy:
        raise ValueError("Employee dispatch exceeds the frozen task ActionPolicy")

    limits = runtime_request.limits
    frozen_limits = request.runtime_limits
    job_limits = request.job_limits
    integer_limits = (
        limits.max_wall_time_ms,
        limits.max_model_calls,
        limits.max_tool_calls,
        limits.max_input_tokens,
        limits.max_output_tokens,
        limits.max_consecutive_errors,
        limits.max_result_bytes,
        limits.max_tool_output_bytes,
        limits.max_context_messages,
        limits.max_context_chars,
        limits.context_keep_recent_messages,
    )
    if (
        any(type(value) is not int or value < 1 for value in integer_limits)
        or isinstance(limits.max_cost_usd, bool)
        or not isinstance(limits.max_cost_usd, (int, float))
        or not math.isfinite(float(limits.max_cost_usd))
        or limits.max_cost_usd < 0
        or limits.max_wall_time_ms
        > min(frozen_limits.max_wall_time_ms, job_limits.max_wall_time_ms)
        or limits.max_model_calls
        > min(frozen_limits.max_model_calls, job_limits.max_total_model_calls)
        or limits.max_tool_calls
        > min(frozen_limits.max_tool_calls, job_limits.max_total_tool_calls)
        or limits.max_cost_usd
        > min(frozen_limits.max_cost_usd, job_limits.max_total_cost_usd)
        or limits.max_input_tokens != frozen_limits.max_input_tokens
        or limits.max_output_tokens != frozen_limits.max_output_tokens
        or limits.max_consecutive_errors != frozen_limits.max_consecutive_errors
        or limits.max_result_bytes != frozen_limits.max_result_bytes
        or limits.max_tool_output_bytes != frozen_limits.max_tool_output_bytes
        or limits.max_context_messages != frozen_limits.max_context_messages
        or limits.max_context_chars != frozen_limits.max_context_chars
        or limits.context_keep_recent_messages
        != frozen_limits.context_keep_recent_messages
        or limits.cost_efficiency_mode is not frozen_limits.cost_efficiency_mode
    ):
        raise ValueError("Employee dispatch exceeds the frozen runtime limits")

    expected_session = (
        EmployeeSessionRetention.RUN_ONLY
        if employee.temporary
        or runtime_request.context.task_evidence is not None
        or task.execution_replica is not None
        else EmployeeSessionRetention.PERSIST
    )
    if runtime_request.session_retention is not expected_session:
        raise ValueError("Employee dispatch session policy exceeds its runtime boundary")
    expected_session_key = (
        request.manager_session_key
        if employee.employee_id == request.manager_employee_id
        and request.manager_session_key
        else request.session_key
    )
    if runtime_request.session_key != expected_session_key:
        raise ValueError("Employee dispatch session identity changed")
    if runtime_request.employee.memory_namespace != f"employee:{employee.employee_id}":
        raise ValueError("Employee dispatch memory namespace changed")

    skill_candidates = (
        request.job_local_skill_snapshots
        if employee.temporary
        else request.employee_skill_snapshots.get(employee.employee_id, ())
    )
    skill_prefixes = (
        ("external-skill:",)
        if employee.temporary
        else (f"employee-skill:{employee.employee_id}:", "external-skill:")
    )
    expected_skills = BoundedKnowledgeRetriever().select(
        skill_candidates,
        query=task.objective,
        limit=3,
        max_bytes=12_000,
        allowed_prefixes=skill_prefixes,
        fallback_count=1,
    ).items
    if runtime_request.employee.skills != expected_skills:
        raise ValueError("Employee dispatch does not match its frozen Skill selection")
    expected_memory = BoundedKnowledgeRetriever().select(
        request.context_snapshot.selected_memory,
        query=task.objective,
        limit=4,
        max_bytes=12_000,
        allowed_prefixes=(
            f"employee-memory:{employee.employee_id}:",
            "company-memory:",
        ),
        fallback_count=1,
    ).items
    if runtime_request.context.selected_memory != expected_memory:
        raise ValueError("Employee dispatch crosses its frozen Knowledge or memory boundary")

    source_evidence = request.context_snapshot.task_evidence
    selected_evidence = runtime_request.context.task_evidence
    if source_evidence is None and selected_evidence is not None:
        raise ValueError("Employee dispatch added an unfrozen Evidence Pack")
    if source_evidence is not None and selected_evidence is None:
        raise ValueError("Employee dispatch omitted its frozen Evidence Pack boundary")
    if source_evidence is not None and selected_evidence is not None:
        selected_evidence.verify()
        source_items = {item.citation_id: item for item in source_evidence.items}
        if (
            selected_evidence.pack_id != source_evidence.pack_id
            or selected_evidence.revision != source_evidence.revision
            or selected_evidence.pack_digest != source_evidence.pack_digest
            or selected_evidence.access_scope != source_evidence.access_scope
            or any(source_items.get(item.citation_id) != item for item in selected_evidence.items)
        ):
            raise ValueError("Employee dispatch crosses its frozen Evidence Pack boundary")

    profile = runtime_request.employee.capability_profile
    if profile is None:
        raise ValueError("Employee dispatch is missing a capability profile")
    profile.verify()
    validators = _expected_validators(request, task, employee)
    evaluation_revision = (
        "job-local-evaluation-v0" if employee.temporary else "employee-evaluation-v0"
    )
    rebuilt = build_employee_capability_profile(
        employee_id=employee.employee_id,
        roster_revision=request.roster_revision,
        model_profile=employee.model_profile,
        capabilities=employee.capabilities,
        skills=runtime_request.employee.skills,
        action_policy=runtime_request.action_policy,
        task_evidence=selected_evidence,
        memory_namespace=runtime_request.employee.memory_namespace,
        selected_memory=runtime_request.context.selected_memory,
        session_retention=runtime_request.session_retention,
        validator_ids=validators,
        evaluation_revision=evaluation_revision,
    )
    if profile != rebuilt:
        raise ValueError("Employee capability profile exceeds the frozen dispatch boundary")
    if runtime_request.employee.selected_memory_refs != tuple(
        item.content_id for item in runtime_request.context.selected_memory
    ):
        raise ValueError("Employee memory references do not match the frozen projection")
    return profile


def bind_blueprint_execution(
    binding: BlueprintBinding,
    *,
    request: CompanyRunRequest,
    runtime_requests: Sequence[EmployeeRunRequest],
    attempt_records: Sequence[TaskAttemptRecord],
) -> BlueprintExecutionBinding:
    """Bind one proven successful dispatch per task without granting reuse."""

    _validate_binding_identity(binding, request)
    tasks = {task.task_id: task for task in request.plan_proposal.tasks}
    by_task: dict[str, EmployeeRunRequest] = {}
    for runtime_request in runtime_requests:
        task_id = runtime_request.task.task_id
        if task_id not in tasks or task_id in by_task:
            raise ValueError("Blueprint execution binding requires one dispatch per task")
        by_task[task_id] = runtime_request
    if set(by_task) != set(tasks):
        raise ValueError("Blueprint execution binding is missing a task dispatch")

    frozen_digest = frozen_snapshot_digest(request)
    records_by_task: dict[str, TaskAttemptRecord] = {}
    for record in attempt_records:
        if record.task_id not in tasks or record.task_id in records_by_task:
            raise ValueError("Blueprint execution binding requires one attempt per task")
        if content_digest(replace(record, content_hash="")) != record.content_hash:
            raise ValueError("Blueprint attempt evidence content hash is invalid")
        records_by_task[record.task_id] = record
    if set(records_by_task) != set(tasks):
        raise ValueError("Blueprint execution binding is missing Kernel attempt evidence")

    pins = []
    for task_id in sorted(tasks):
        task = tasks[task_id]
        profile = _validate_runtime_request(
            request=request,
            task=task,
            runtime_request=by_task[task_id],
        )
        record = records_by_task[task_id]
        if (
            record.status is not RunStatus.SUCCEEDED
            or record.employee_id != profile.employee_id
            or record.graph_version != by_task[task_id].task.job_graph_version
            or record.company_revision != request.company_revision
            or record.roster_revision != request.roster_revision
            or record.playbook_revision != request.playbook_revision
            or record.frozen_snapshot_hash != frozen_digest
            or record.capability_profile_digest != profile.profile_digest
            or record.capability_material_digest != profile.material_digest
        ):
            raise ValueError(
                "Blueprint attempt evidence does not match the frozen successful dispatch"
            )
        pins.append(
            BlueprintEmployeePin(
                task_id=task_id,
                employee_id=profile.employee_id,
                required_capabilities=tuple(sorted(task.required_capabilities)),
                profile_digest=profile.profile_digest,
                material_digest=profile.material_digest,
                model_profile=profile.model_profile,
                material_dimension_digests=material_profile_dimension_digests(profile),
                runtime_provider_binding_digest=request.runtime_provider_binding_digest,
                runtime_tool_contract_digest=request.runtime_tool_contract_digest,
                runtime_action_policy_digest=content_digest(
                    by_task[task_id].action_policy
                ),
                runtime_company_coordination_digest=request.runtime_company_coordination_digest,
                runtime_boundary_digest=_runtime_boundary_digest(
                    provider_binding_digest=request.runtime_provider_binding_digest,
                    tool_contract_digest=request.runtime_tool_contract_digest,
                    action_policy_digest=content_digest(
                        by_task[task_id].action_policy
                    ),
                    company_coordination_digest=request.runtime_company_coordination_digest,
                    session_policy=profile.session_policy,
                    evaluation_revision=profile.evaluation_revision,
                ),
                run_limits_digest=content_digest(by_task[task_id].limits),
            )
        )
    result = BlueprintExecutionBinding(
        blueprint_binding_digest=binding.content_digest,
        request_snapshot_digest=frozen_digest,
        request_execution_envelope_digest=content_digest(
            {
                "request_id": request.request_id,
                "job_id": request.job_id,
                "goal": request.goal,
                "plan_proposal": request.plan_proposal,
                "runtime_limits": request.runtime_limits,
                "job_limits": request.job_limits,
                "session_key": request.session_key,
                "runtime_provider_binding_digest": (
                    request.runtime_provider_binding_digest
                ),
                "runtime_tool_contract_digest": (
                    request.runtime_tool_contract_digest
                ),
                "runtime_company_coordination_digest": (
                    request.runtime_company_coordination_digest
                ),
            }
        ),
        work_order_id=request.work_order_id,
        work_order_digest=request.work_order_digest,
        employee_pins=tuple(pins),
    )
    result.verify()
    return result


@dataclass(frozen=True, slots=True)
class EmployeeBoundaryCandidate:
    """Current content-free availability facts for one possible Employee."""

    profile: EmployeeCapabilityProfile
    runtime_provider_binding_digest: str
    runtime_tool_contract_digest: str
    runtime_action_policy_digest: str
    runtime_company_coordination_digest: str
    active: bool = True

    def __post_init__(self) -> None:
        self.profile.verify()
        if type(self.active) is not bool:
            raise ValueError("Employee boundary candidate activity is invalid")
        for label, value in (
            ("Runtime provider binding", self.runtime_provider_binding_digest),
            ("Runtime tool contract", self.runtime_tool_contract_digest),
            ("Runtime ActionPolicy", self.runtime_action_policy_digest),
            ("Runtime Company coordination", self.runtime_company_coordination_digest),
        ):
            _require_digest(value, label)

    @property
    def dimension_digests(self) -> Mapping[str, str]:
        return dict(material_profile_dimension_digests(self.profile))


class EmployeeSubstitutionDisposition(StrEnum):
    PIN_AVAILABLE = "PIN_AVAILABLE"
    EXACT_COMPATIBLE_SUBSTITUTE = "EXACT_COMPATIBLE_SUBSTITUTE"
    DEGRADED_USER_CHOICE = "DEGRADED_USER_CHOICE"
    SAFE_REFUSAL = "SAFE_REFUSAL"


@dataclass(frozen=True, slots=True)
class EmployeeSubstitutionChoice:
    employee_id: str
    difference_dimensions: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.employee_id,
            "Employee substitution choice id",
            maximum_bytes=256,
        )
        if (
            not isinstance(self.difference_dimensions, tuple)
            or self.difference_dimensions
            != tuple(sorted(set(self.difference_dimensions)))
            or not set(self.difference_dimensions).issubset(
                _SUBSTITUTION_DIFFERENCE_DIMENSIONS
            )
        ):
            raise ValueError("Employee substitution difference dimensions are invalid")


@dataclass(frozen=True, slots=True)
class EmployeeSubstitutionDecision:
    task_id: str
    pinned_employee_id: str
    disposition: EmployeeSubstitutionDisposition
    selected_employee_id: str | None
    choices: tuple[EmployeeSubstitutionChoice, ...]
    reason: str
    requires_user_choice: bool
    requires_new_frozen_request: bool = True
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.task_id,
            "Employee substitution task id",
            maximum_bytes=256,
        )
        _require_bounded_text(
            self.pinned_employee_id,
            "Pinned Employee id",
            maximum_bytes=256,
        )
        _require_bounded_text(
            self.reason,
            "Employee substitution reason",
            maximum_bytes=1_024,
        )
        if not isinstance(self.disposition, EmployeeSubstitutionDisposition):
            raise TypeError("Employee substitution disposition must be typed")
        if self.selected_employee_id is not None:
            _require_bounded_text(
                self.selected_employee_id,
                "Selected Employee id",
                maximum_bytes=256,
            )
        if type(self.requires_user_choice) is not bool:
            raise ValueError("Employee substitution user-choice flag is invalid")
        if self.requires_new_frozen_request is not True:
            raise ValueError("Employee substitution cannot reuse a running request")
        if not isinstance(self.choices, tuple) or any(
            not isinstance(item, EmployeeSubstitutionChoice) for item in self.choices
        ):
            raise TypeError("Employee substitution choices must be typed")
        if len(self.choices) > _MAX_PROJECTED_CHOICES:
            raise ValueError("Employee substitution choices exceed their bound")
        choice_ids = tuple(item.employee_id for item in self.choices)
        if (
            any(not employee_id for employee_id in choice_ids)
            or choice_ids != tuple(sorted(choice_ids))
            or len(choice_ids) != len(set(choice_ids))
            or any(
                item.difference_dimensions
                != tuple(sorted(set(item.difference_dimensions)))
                for item in self.choices
            )
        ):
            raise ValueError("Employee substitution choices are not canonical")
        if self.disposition is EmployeeSubstitutionDisposition.DEGRADED_USER_CHOICE:
            if (
                not self.requires_user_choice
                or self.selected_employee_id is not None
                or not self.choices
                or any(not item.difference_dimensions for item in self.choices)
            ):
                raise ValueError("Degraded substitution must require a bounded user choice")
        elif self.requires_user_choice:
            raise ValueError("Only a degraded substitution may require user choice")
        if self.disposition is EmployeeSubstitutionDisposition.SAFE_REFUSAL:
            if self.selected_employee_id is not None or self.choices:
                raise ValueError("Safe refusal cannot select an Employee")
        if self.disposition in {
            EmployeeSubstitutionDisposition.PIN_AVAILABLE,
            EmployeeSubstitutionDisposition.EXACT_COMPATIBLE_SUBSTITUTE,
        } and not self.selected_employee_id:
            raise ValueError("Available or exact substitution requires an Employee")
        if self.disposition is EmployeeSubstitutionDisposition.PIN_AVAILABLE and (
            self.selected_employee_id != self.pinned_employee_id or self.choices
        ):
            raise ValueError("Available pin must preserve the exact pinned Employee")
        if self.disposition is EmployeeSubstitutionDisposition.EXACT_COMPATIBLE_SUBSTITUTE:
            if (
                not self.choices
                or self.selected_employee_id not in choice_ids
                or any(item.difference_dimensions for item in self.choices)
            ):
                raise ValueError("Exact substitution choices must be materially identical")
        object.__setattr__(self, "content_digest", content_digest(self.canonical_payload()))

    @property
    def execution_authority(self) -> bool:
        return False

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "schema": "noruct.employee-substitution-decision.v1",
            "task_id": self.task_id,
            "pinned_employee_id": self.pinned_employee_id,
            "disposition": self.disposition.value,
            "selected_employee_id": self.selected_employee_id,
            "choices": tuple(
                {
                    "employee_id": item.employee_id,
                    "difference_dimensions": item.difference_dimensions,
                }
                for item in self.choices
            ),
            "reason": self.reason,
            "requires_user_choice": self.requires_user_choice,
            "requires_new_frozen_request": self.requires_new_frozen_request,
            "execution_authority": False,
        }

    def verify(self) -> None:
        if content_digest(self.canonical_payload()) != self.content_digest:
            raise ValueError("Employee substitution decision digest is invalid")


def plan_employee_substitution(
    binding: BlueprintExecutionBinding,
    *,
    task_id: str,
    candidates: Sequence[EmployeeBoundaryCandidate],
) -> EmployeeSubstitutionDecision:
    """Classify current candidates without mutating a pin or request."""

    binding.verify()
    pin = next((item for item in binding.employee_pins if item.task_id == task_id), None)
    if pin is None:
        raise KeyError(f"Unknown Blueprint Employee pin: {task_id}")
    if len(candidates) > _MAX_CANDIDATES:
        raise ValueError("Employee substitution candidate set exceeds its bound")
    by_employee: dict[str, EmployeeBoundaryCandidate] = {}
    for candidate in candidates:
        employee_id = candidate.profile.employee_id
        if employee_id in by_employee:
            raise ValueError("Employee substitution candidates must be unique")
        by_employee[employee_id] = candidate
    viable = tuple(
        candidate
        for _, candidate in sorted(by_employee.items())
        if candidate.active
        and set(pin.required_capabilities).issubset(candidate.profile.capability_ids)
    )

    def boundaries_match(candidate: EmployeeBoundaryCandidate) -> bool:
        return (
            candidate.runtime_provider_binding_digest
            == pin.runtime_provider_binding_digest
            and candidate.runtime_tool_contract_digest
            == pin.runtime_tool_contract_digest
            and candidate.runtime_action_policy_digest
            == pin.runtime_action_policy_digest
            and candidate.runtime_company_coordination_digest
            == pin.runtime_company_coordination_digest
        )

    current = next(
        (
            candidate
            for candidate in viable
            if candidate.profile.employee_id == pin.employee_id
            and candidate.profile.profile_digest == pin.profile_digest
            and boundaries_match(candidate)
        ),
        None,
    )
    if current is not None:
        return EmployeeSubstitutionDecision(
            task_id=task_id,
            pinned_employee_id=pin.employee_id,
            disposition=EmployeeSubstitutionDisposition.PIN_AVAILABLE,
            selected_employee_id=pin.employee_id,
            choices=(),
            reason="The exact pinned Employee and runtime boundary remain available.",
            requires_user_choice=False,
        )

    exact = tuple(
        candidate
        for candidate in viable
        if candidate.profile.material_digest == pin.material_digest
        and boundaries_match(candidate)
    )
    if exact:
        selected = exact[0]
        return EmployeeSubstitutionDecision(
            task_id=task_id,
            pinned_employee_id=pin.employee_id,
            disposition=EmployeeSubstitutionDisposition.EXACT_COMPATIBLE_SUBSTITUTE,
            selected_employee_id=selected.profile.employee_id,
            choices=tuple(
                EmployeeSubstitutionChoice(item.profile.employee_id, ())
                for item in exact[:_MAX_PROJECTED_CHOICES]
            ),
            reason=(
                "A materially equivalent Employee is available under the exact "
                "provider, tool, permission, knowledge, and runtime boundary."
            ),
            requires_user_choice=False,
        )

    if not viable:
        return EmployeeSubstitutionDecision(
            task_id=task_id,
            pinned_employee_id=pin.employee_id,
            disposition=EmployeeSubstitutionDisposition.SAFE_REFUSAL,
            selected_employee_id=None,
            choices=(),
            reason="No active Employee covers the frozen required capabilities.",
            requires_user_choice=False,
        )

    pinned_dimensions = dict(pin.material_dimension_digests)
    choices = []
    for candidate in viable[:_MAX_PROJECTED_CHOICES]:
        dimensions = [
            name
            for name, value in candidate.dimension_digests.items()
            if pinned_dimensions.get(name) != value
        ]
        if candidate.runtime_provider_binding_digest != pin.runtime_provider_binding_digest:
            dimensions.append("runtime_provider_binding")
        if candidate.runtime_tool_contract_digest != pin.runtime_tool_contract_digest:
            dimensions.append("runtime_tool_contract")
        if candidate.runtime_action_policy_digest != pin.runtime_action_policy_digest:
            dimensions.append("runtime_action_policy")
        if candidate.runtime_company_coordination_digest != pin.runtime_company_coordination_digest:
            dimensions.append("runtime_company_coordination")
        choices.append(
            EmployeeSubstitutionChoice(
                employee_id=candidate.profile.employee_id,
                difference_dimensions=tuple(sorted(set(dimensions))),
            )
        )
    return EmployeeSubstitutionDecision(
        task_id=task_id,
        pinned_employee_id=pin.employee_id,
        disposition=EmployeeSubstitutionDisposition.DEGRADED_USER_CHOICE,
        selected_employee_id=None,
        choices=tuple(choices),
        reason=(
            "Only capability-covering candidates with changed provider, tool, "
            "permission, knowledge, or runtime boundaries remain. A new frozen "
            "Work Order requires explicit user choice."
        ),
        requires_user_choice=True,
    )


__all__ = (
    "BlueprintEmployeePin",
    "BlueprintExecutionBinding",
    "EmployeeBoundaryCandidate",
    "EmployeeSubstitutionChoice",
    "EmployeeSubstitutionDecision",
    "EmployeeSubstitutionDisposition",
    "bind_blueprint_execution",
    "plan_employee_substitution",
)
