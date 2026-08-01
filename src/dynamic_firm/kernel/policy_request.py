from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from dynamic_firm.runtime.models import (
    ApprovalDecision,
    ApprovalRequest,
    ActionPolicy,
    ContextBundle,
    EmployeeCapabilityProfile,
    EmployeeRunRequest,
    EmployeeRunResult,
    EmployeeSessionRetention,
    EmployeeSnapshot,
    Failure,
    FailureCategory,
    RunHandle,
    RunLimits,
    RunSignal,
    RunStatus,
    SignalCode,
    TaskEnvelope,
    TaskEvidencePack,
    ToolEffect,
    ToolRisk,
    Usage,
    VersionedContent,
)
from dynamic_firm.runtime.employee_capability import (
    build_employee_capability_profile,
    material_profile_difference,
    materially_equivalent,
)
from dynamic_firm.runtime.knowledge_retrieval import BoundedKnowledgeRetriever
from dynamic_firm.runtime.liveness import (
    LIVENESS_CONTINUATION_INSTRUCTION,
    enforce_employee_completion_liveness,
)
from dynamic_firm.runtime.manager_tool_policy import is_manager_tool
from dynamic_firm.runtime.ports import ApprovalPort, CancellationToken, EmployeeExecutionPort
from dynamic_firm.runtime.redaction import redact_prompt_text
from dynamic_firm.runtime.company_budget import (
    CompanyBudgetAdmission,
    CompanyBudgetAuthorityPort,
    CompanyBudgetForfeit,
    CompanyBudgetLease,
    CompanyBudgetSettlement,
)

from .graph import (
    GraphValidationError,
    apply_patch,
    graph_from_proposal,
    ready_tasks,
    replace_task,
    task_map,
)
from .ledger import ActiveJobLedgerPort
from .models import (
    AttemptBudgetEvidence,
    AttemptFailureKind,
    CompanyRunRequest,
    EmployeeRecord,
    GraphPatch,
    GraphPatchEvent,
    GraphPatchProposalEvent,
    GraphPatchProposalStatus,
    GraphMutationLease,
    JobGraph,
    JobMetrics,
    JobMutationEvent,
    JobResult,
    JobStatus,
    JobTask,
    ReplanContext,
    SemanticOperation,
    TaskAssignmentEvent,
    TaskStatus,
    TaskAttemptRecord,
    TaskMutationType,
)
from .mutation import (
    RECOVERABLE_FAILURE_KINDS,
    attempt_identity,
    attempt_record,
    classify_attempt_failure,
    content_digest,
    frozen_snapshot_digest,
    graph_patch_event,
    graph_patch_proposal_event,
    mutation_event,
    reroute_candidate,
    structurally_replica_safe,
    structurally_read_only,
)
from .staffing import staff_task
from .supervision import (
    ManagerSupervisionPort,
    supervision_context,
)
from .primitives import (
    ReplannerPort,
    _MutationCandidate,
    _Reservation,
    _RunningTask,
    _TrackedCompanyBudgetAuthority,
    _dependency_result_projection,
)
from .mutation_execution import FirmKernelMutationExecutionMixin


class FirmKernelPolicyMixin:
    @staticmethod
    def _validate_manager_delegation(request: CompanyRunRequest) -> None:
        """Verify Manager identity and proposal adoption without granting authority."""

        identity = (
            request.manager_employee_id,
            request.manager_assignment_digest,
            request.manager_session_key,
        )
        has_identity = any(identity)
        if has_identity and not all(identity):
            raise ValueError("Manager execution identity is incomplete")
        if not has_identity:
            if (
                request.manager_employee is not None
                or request.manager_delegation_payload
                or request.manager_delegation_digest
            ):
                raise ValueError("Manager delegation requires a Manager identity")
            return

        manager = request.manager_employee
        if manager is None:
            raise ValueError("Manager identity requires a frozen Employee record")
        if (
            manager.employee_id != request.manager_employee_id
            or not manager.active
            or manager.temporary
            or "company_management" not in manager.capabilities
        ):
            raise ValueError("Manager Employee is invalid for this frozen Job")
        if request.company_work_mode != "DIRECT" and any(
            employee.employee_id == manager.employee_id for employee in request.roster
        ):
            raise ValueError("Manager cannot appear in the specialist staffing roster")
        for label, value in (
            ("Manager assignment digest", request.manager_assignment_digest),
            ("Manager delegation digest", request.manager_delegation_digest),
        ):
            if value and (
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{label} is invalid")
        if (
            not request.manager_session_key
            or len(request.manager_session_key.encode("utf-8")) > 320
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in request.manager_session_key
            )
        ):
            raise ValueError("Manager session key is invalid")

        if request.company_work_mode == "DIRECT":
            if request.manager_delegation_payload or request.manager_delegation_digest:
                raise ValueError("DIRECT Manager work cannot contain delegation")
            return
        if not request.manager_delegation_payload or not request.manager_delegation_digest:
            raise ValueError("Managed Manager work requires an immutable delegation")

        proposal = request.plan_proposal
        expected_payload: dict[str, object] = {
            "schema": "noruct.executive-manager-delegation.v1",
            "assignment_digest": request.manager_assignment_digest,
            "manager_employee_id": request.manager_employee_id,
            "work_order_id": request.work_order_id,
            "work_order_digest": request.work_order_digest,
            "proposal_id": proposal.proposal_id,
            "final_task_id": proposal.final_task_id,
            "tasks": tuple(
                {
                    "task_id": task.task_id,
                    "objective": task.objective,
                    "depends_on": task.depends_on,
                    "required_capabilities": task.required_capabilities,
                    "acceptance_criteria": task.acceptance_criteria,
                    "context_lane": (
                        "DEPENDENCY_ARTIFACTS"
                        if task.depends_on
                        else "WORK_ORDER_BRIEF"
                    ),
                    "dependency_artifact_ids": task.depends_on,
                    "deliverable_kind": (
                        "USER_REPORT"
                        if task.task_id == proposal.final_task_id
                        else "SPECIALIST_ARTIFACT"
                    ),
                    "validator_ids": tuple(
                        dict.fromkeys(
                            ("structured-completion-v1",)
                            + (("task-acceptance-v1",) if task.acceptance_criteria else ())
                            + (
                                ("independent-review-v1",)
                                if any(
                                    capability in {
                                        "review",
                                        "independent_review",
                                        "validation",
                                        "verification",
                                    }
                                    or capability.endswith("_review")
                                    for capability in task.required_capabilities
                                )
                                else ()
                            )
                        )
                    ),
                    "final": task.task_id == proposal.final_task_id,
                    "execution_replica": (
                        None
                        if task.execution_replica is None
                        else {
                            "group_id": task.execution_replica.group_id,
                            "replica_id": task.execution_replica.replica_id,
                            "strategy": task.execution_replica.strategy.value,
                            "scope": task.execution_replica.scope,
                            "aggregation_task_id": (
                                task.execution_replica.aggregation_task_id
                            ),
                            "aggregation": task.execution_replica.aggregation.value,
                            "marginal_value_reason": (
                                task.execution_replica.marginal_value_reason
                            ),
                        }
                    ),
                }
                for task in proposal.tasks
            ),
            "authority_granted": False,
        }
        if content_digest(request.manager_delegation_payload) != request.manager_delegation_digest:
            raise ValueError("Manager delegation payload digest is invalid")
        if content_digest(expected_payload) != request.manager_delegation_digest:
            raise ValueError("Manager delegation does not match the admitted proposal")

    @staticmethod
    def _validate_request(request: CompanyRunRequest) -> None:
        required = {
            "request_id": request.request_id,
            "job_id": request.job_id,
            "goal": request.goal,
            "proposal_id": request.plan_proposal.proposal_id,
        }
        missing = [key for key, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"Missing company request fields: {', '.join(missing)}")
        employee_ids = [employee.employee_id for employee in request.roster]
        if not request.roster or not any(
            employee.active and not employee.temporary for employee in request.roster
        ):
            raise ValueError("Company roster requires at least one active persistent employee")
        if len(employee_ids) != len(set(employee_ids)):
            raise ValueError("Roster employee ids must be unique")
        for employee in request.roster:
            if not employee.employee_id.strip() or not employee.role.strip() or not employee.capabilities:
                raise ValueError("Roster employees require id, role, and capabilities")
            if any(not capability.strip() for capability in employee.capabilities):
                raise ValueError("Roster employee capabilities must be non-empty strings")
            if len(employee.capabilities) != len(set(employee.capabilities)):
                raise ValueError("Roster employee capabilities must be unique")
        job_local_skills = request.job_local_skill_snapshots
        if not isinstance(job_local_skills, tuple) or len(job_local_skills) > 3:
            raise ValueError("Job-local skills must be a bounded immutable tuple")
        identities = tuple((item.content_id, item.revision) for item in job_local_skills)
        if len(identities) != len(set(identities)):
            raise ValueError("Job-local skill identities must be unique")
        if any(
            not item.content_id.startswith("external-skill:")
            or not item.revision.strip()
            or not item.content.strip()
            for item in job_local_skills
        ):
            raise ValueError(
                "Job-local specialist skills must be bounded external skill snapshots"
            )
        FirmKernelPolicyMixin._validate_manager_delegation(request)
        limits = request.job_limits
        integral_limits = (
            limits.max_tasks,
            limits.max_concurrency,
            limits.max_graph_patches,
            limits.max_task_mutations,
            limits.max_temporary_roles,
            limits.max_total_model_calls,
            limits.max_total_tool_calls,
            limits.max_wall_time_ms,
        )
        if any(value <= 0 for value in integral_limits) or limits.max_total_cost_usd < 0:
            raise ValueError("Job limits must be concrete bounds and cost cannot be negative")
        revisions = (
            request.company_revision,
            request.roster_revision,
            request.playbook_revision,
        )
        if any(type(value) is not int or value < 0 for value in revisions):
            raise ValueError("Company snapshot revisions must be non-negative integers")
        if request.workspace_identity_status not in {
            "NOT_APPLICABLE",
            "READY",
            "FAILED",
        }:
            raise ValueError("Workspace identity status is invalid")
        if request.company_work_mode not in {
            "DIRECT",
            "SOLO_JOB",
            "TEAM_JOB",
            "UNSPECIFIED",
        }:
            raise ValueError("Company work mode is invalid")
        if request.coordination_policy not in {
            "DIRECT",
            "SOLO_FIRST",
            "PLAN_FIRST",
            "PRECOMPILED",
        }:
            raise ValueError("Company coordination policy is invalid")
        if request.requested_effect not in {
            "READ",
            "WORKSPACE_CHANGE",
            "HOST_ACTION",
            "UNSPECIFIED",
        }:
            raise ValueError("Company requested effect is invalid")
        if request.planning_mode not in {
            "DIRECT",
            "BLUEPRINT",
            "DYNAMIC",
            "SOLO",
            "SOLO_FALLBACK",
            "PRECOMPILED",
        }:
            raise ValueError("Company planning mode is invalid")
        if (
            not request.planning_reason
            or len(request.planning_reason) > 64
            or request.planning_reason.upper() != request.planning_reason
            or not request.planning_reason.replace("_", "").isalnum()
        ):
            raise ValueError("Company planning reason is invalid")
        compiler_usage = request.compiler_usage
        if any(
            type(value) is not int or value < 0
            for value in (
                compiler_usage.model_calls,
                compiler_usage.tool_calls,
                compiler_usage.input_tokens,
                compiler_usage.cached_input_tokens,
                compiler_usage.output_tokens,
            )
        ) or not math.isfinite(compiler_usage.cost_usd) or compiler_usage.cost_usd < 0:
            raise ValueError("Compiler usage must be finite and non-negative")
        provider_request_id = request.compiler_provider_request_id
        if provider_request_id is not None and (
            not provider_request_id
            or len(provider_request_id) > 160
            or any(ord(char) < 32 or ord(char) == 127 for char in provider_request_id)
        ):
            raise ValueError("Compiler provider request id is invalid")
        for label, value, maximum_bytes in (
            ("work order id", request.work_order_id, 256),
            ("work order digest", request.work_order_digest, 128),
            (
                "work order authority digest",
                request.work_order_authority_digest,
                128,
            ),
            ("Firm admission digest", request.firm_admission_digest, 128),
        ):
            if value and (
                len(value.encode("utf-8")) > maximum_bytes
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError(f"Company {label} is invalid")
        for label, value in (
            ("runtime provider binding digest", request.runtime_provider_binding_digest),
            ("runtime tool contract digest", request.runtime_tool_contract_digest),
            (
                "runtime Company coordination digest",
                request.runtime_company_coordination_digest,
            ),
        ):
            if value and (
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"Company {label} is invalid")
        if (
            not request.operating_reason
            or len(request.operating_reason) > 64
            or request.operating_reason.upper() != request.operating_reason
            or not request.operating_reason.replace("_", "").isalnum()
        ):
            raise ValueError("Company operating reason is invalid")
        blueprint_identity = (
            request.graph_blueprint_id,
            request.graph_blueprint_version,
            request.graph_blueprint_digest,
        )
        if any(blueprint_identity) and not all(blueprint_identity):
            raise ValueError("Graph Blueprint provenance is incomplete")
        if request.graph_blueprint_id and (
            len(request.graph_blueprint_id) > 160
            or not request.graph_blueprint_id.replace("-", "").replace("_", "").isalnum()
        ):
            raise ValueError("Graph Blueprint id is invalid")
        if request.graph_blueprint_version and (
            type(request.graph_blueprint_version) is not int
            or request.graph_blueprint_version < 1
        ):
            raise ValueError("Graph Blueprint version is invalid")
        for label, value in (
            ("Graph Blueprint digest", request.graph_blueprint_digest),
            ("Graph constraints digest", request.graph_constraints_digest),
        ):
            if value and (
                len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"{label} is invalid")
        if request.graph_mutation_policy not in {
            "LOCKED",
            "PROPOSE",
            "BOUNDED_AUTO",
        }:
            raise ValueError("Graph mutation policy is invalid")
        graph_employee_ids = (
            request.graph_pinned_employee_ids,
            request.graph_excluded_employee_ids,
        )
        if any(not isinstance(values, tuple) for values in graph_employee_ids):
            raise ValueError("Graph employee constraints must be tuples")
        selected_employee_ids = (
            *request.graph_pinned_employee_ids,
            *request.graph_excluded_employee_ids,
        )
        if any(
            not isinstance(employee_id, str)
            or not employee_id
            or len(employee_id) > 160
            or not employee_id.replace("-", "").replace("_", "").isalnum()
            for employee_id in selected_employee_ids
        ):
            raise ValueError("Graph constrained employee id is invalid")
        if len(request.graph_pinned_employee_ids) != len(
            set(request.graph_pinned_employee_ids)
        ) or len(request.graph_excluded_employee_ids) != len(
            set(request.graph_excluded_employee_ids)
        ):
            raise ValueError("Graph constrained employee ids must be unique")
        if set(request.graph_pinned_employee_ids) & set(
            request.graph_excluded_employee_ids
        ):
            raise ValueError("Graph employee cannot be both pinned and excluded")
        persistent_employee_ids = {
            employee.employee_id
            for employee in request.roster
            if employee.active and not employee.temporary
        }
        if request.manager_employee is not None:
            persistent_employee_ids.add(request.manager_employee.employee_id)
        if not set(selected_employee_ids).issubset(persistent_employee_ids):
            raise ValueError("Graph constrained employee is unavailable in this roster")
        if type(request.graph_require_independent_review) is not bool:
            raise ValueError("Graph independent review constraint is invalid")
        if request.graph_max_concurrency is not None and (
            type(request.graph_max_concurrency) is not int
            or not 1 <= request.graph_max_concurrency <= limits.max_concurrency
        ):
            raise ValueError("Graph concurrency constraint cannot widen the Job limit")
        if request.graph_max_cost_usd is not None and (
            not isinstance(request.graph_max_cost_usd, (int, float))
            or not math.isfinite(request.graph_max_cost_usd)
            or request.graph_max_cost_usd < 0
        ):
            raise ValueError("Graph cost constraint is invalid")
        if request.graph_max_wall_time_ms is not None and (
            type(request.graph_max_wall_time_ms) is not int
            or request.graph_max_wall_time_ms < 1
        ):
            raise ValueError("Graph wall-time constraint is invalid")
        if (
            selected_employee_ids
            or request.graph_require_independent_review
            or request.graph_max_concurrency is not None
            or request.graph_max_cost_usd is not None
            or request.graph_max_wall_time_ms is not None
        ) and not request.graph_constraints_digest:
            raise ValueError("Graph constraints require immutable provenance")
        if (
            len(request.workflow_context_fingerprint) > 128
            or len(request.workspace_identity_revision) > 128
            or len(request.workspace_identity_failure_code) > 64
        ):
            raise ValueError("Workspace identity metadata exceeds its bounded contract")
        if request.workspace_identity_status == "READY" and (
            not request.workflow_context_fingerprint
            or not request.workspace_identity_revision
            or request.workspace_identity_failure_code
        ):
            raise ValueError("Ready workspace identity metadata is incomplete")
        if request.workspace_identity_status == "FAILED" and (
            request.workflow_context_fingerprint
            or not request.workspace_identity_failure_code
        ):
            raise ValueError("Failed workspace identity metadata is inconsistent")
        evidence = request.context_snapshot.task_evidence
        origin = request.execution_origin
        if evidence is not None:
            evidence.verify()
            if origin is None:
                raise ValueError("Knowledge evidence requires an execution origin binding")
            if (
                origin.pack_id != evidence.pack_id
                or origin.pack_revision != evidence.revision
                or origin.pack_digest != evidence.pack_digest
                or origin.delivery_digest != evidence.delivery_digest
                or origin.item_count != len(evidence.items)
                or origin.selected_bytes != evidence.selected_bytes
                or origin.access_scope != evidence.access_scope
            ):
                raise ValueError("Knowledge execution origin does not match the frozen Evidence Pack")
            epistemic_identity = (
                origin.decision_context_id,
                origin.decision_context_digest,
                origin.oracle_contract_id,
                origin.oracle_contract_digest,
            )
            if any(epistemic_identity) and not all(epistemic_identity):
                raise ValueError("Knowledge execution origin has incomplete epistemic control identity")
            for value in epistemic_identity:
                if value and (
                    len(value.encode("utf-8")) > 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)
                ):
                    raise ValueError("Knowledge epistemic control identity is invalid")
        elif origin is not None:
            raise ValueError("Knowledge execution origin requires a frozen Evidence Pack")

    @staticmethod
    def _reserve_budget(
        request: CompanyRunRequest,
        usage: Usage,
        running: dict[asyncio.Task[EmployeeRunResult], _RunningTask],
        *,
        committed_reservations: Iterable[_Reservation] = (),
        allocation_slots: int = 1,
    ) -> _Reservation | None:
        reserved_model = sum(item.reservation.model_calls for item in running.values())
        reserved_tool = sum(item.reservation.tool_calls for item in running.values())
        reserved_cost = sum(item.reservation.cost_usd for item in running.values())
        reserved_model += sum(item.model_calls for item in committed_reservations)
        reserved_tool += sum(item.tool_calls for item in committed_reservations)
        reserved_cost += sum(item.cost_usd for item in committed_reservations)
        available_model = request.job_limits.max_total_model_calls - usage.model_calls - reserved_model
        available_tool = request.job_limits.max_total_tool_calls - usage.tool_calls - reserved_tool
        available_cost = request.job_limits.max_total_cost_usd - usage.cost_usd - reserved_cost
        if available_model <= 0 or available_tool <= 0 or available_cost < 0:
            return None
        return _Reservation(
            model_calls=min(
                request.runtime_limits.max_model_calls,
                max(1, available_model // allocation_slots),
            ),
            tool_calls=min(
                request.runtime_limits.max_tool_calls,
                max(1, available_tool // allocation_slots),
            ),
            cost_usd=max(
                0.0,
                min(request.runtime_limits.max_cost_usd, available_cost / allocation_slots),
            ),
        )

    @staticmethod
    def _compiler_consumed_job_budget(request: CompanyRunRequest) -> bool:
        usage = request.compiler_usage
        limits = request.job_limits
        return (
            request.planning_reason == "COMPILER_WALL_TIME_EXHAUSTED"
            or request.planning_reason == "JOB_WALL_TIME_EXHAUSTED_BEFORE_DISPATCH"
            or usage.model_calls >= limits.max_total_model_calls
            or usage.tool_calls >= limits.max_total_tool_calls
            or (
                usage.cost_usd > 0
                and usage.cost_usd >= limits.max_total_cost_usd
            )
        )

    @staticmethod
    def _employee_request(
        company: CompanyRunRequest,
        graph: JobGraph,
        task: JobTask,
        employee: EmployeeRecord,
        results: dict[str, EmployeeRunResult],
        reservation: _Reservation,
        remaining_wall_ms: int,
        *,
        retry_instruction: str = "",
        task_action_policy_override: Callable[
            [JobTask, EmployeeRecord, ActionPolicy], ActionPolicy
        ] | None = None,
        execution_session_key: str | None = None,
    ) -> EmployeeRunRequest:
        dependency_content = tuple(
            VersionedContent(
                content_id=f"task-result:{dependency_id}",
                revision=str(graph.version),
                content=_dependency_result_projection(
                    dependency_id,
                    results[dependency_id],
                ),
            )
            for dependency_id in task.depends_on
            if dependency_id in results
        )
        base = company.context_snapshot
        task_evidence = None
        if base.task_evidence is not None:
            evidence_by_id = {
                f"user-knowledge-evidence:{item.citation_id}": item
                for item in base.task_evidence.items
            }
            evidence_candidates = tuple(
                VersionedContent(
                    content_id=content_id,
                    revision=item.source_revision,
                    content=item.content,
                    content_hash=item.content_hash,
                )
                for content_id, item in evidence_by_id.items()
            )
            selected_evidence = BoundedKnowledgeRetriever().select(
                evidence_candidates,
                query=task.objective,
                limit=6,
                max_bytes=16_000,
                allowed_prefixes=("user-knowledge-evidence:",),
                fallback_count=0,
            ).items
            selected_items = tuple(
                evidence_by_id[item.content_id] for item in selected_evidence
            )
            provisional = replace(
                base.task_evidence,
                items=selected_items,
                delivery_digest="",
            )
            task_evidence = replace(
                provisional,
                delivery_digest=provisional.computed_delivery_digest(),
            )
            task_evidence.verify()
        selected_memory = BoundedKnowledgeRetriever().select(
            base.selected_memory,
            query=task.objective,
            limit=4,
            max_bytes=12_000,
            allowed_prefixes=(
                f"employee-memory:{employee.employee_id}:",
                "company-memory:",
            ),
            fallback_count=1,
        ).items
        skill_candidates = (
            company.job_local_skill_snapshots
            if employee.temporary
            else company.employee_skill_snapshots.get(employee.employee_id, ())
        )
        skill_prefixes = (
            ("external-skill:",)
            if employee.temporary
            else (f"employee-skill:{employee.employee_id}:", "external-skill:")
        )
        selected_skills = BoundedKnowledgeRetriever().select(
            skill_candidates,
            query=task.objective,
            limit=3,
            max_bytes=12_000,
            # A configured external SKILL.md has already been bounded and
            # converted into an immutable, Job-local snapshot at the product
            # boundary.  It deliberately shares the employee-skill prompt
            # lane without becoming persistent Company state.
            allowed_prefixes=skill_prefixes,
            # A frozen procedure can still be useful when the task wording
            # has no lexical overlap. Keep one deterministic fallback while
            # avoiding a whole employee procedure catalog in every prompt.
            fallback_count=1,
        ).items
        manager_instructions = (
            (
                "Act as the persistent Company Manager for final integration only. "
                "Use the typed dependency artifacts to produce one user-facing report; "
                "preserve evidence, conflicts, assumptions, and unresolved issues. "
                "Do not redo specialist work, request new authority, or perform a mutation."
            ),
        ) if (
            employee.employee_id == company.manager_employee_id
            and task.task_id == graph.final_task_id
        ) else ()
        # A Manager-owned graph is allowed to abbreviate a specialist task,
        # but that must not sever the specialist from the user-owned Work
        # Order.  This is scope context only: it grants no tool, approval,
        # budget, or state authority.  Keep the delivery bounded and redact
        # credential-shaped text before any external-worker projection.
        work_order_context = ()
        if company.manager_employee_id:
            safe_goal = " ".join(redact_prompt_text(company.goal).split())[:4_000]
            if safe_goal:
                work_order_context = (
                    "Bounded Work Order objective (context only; not authority): "
                    + safe_goal,
                )
        context = ContextBundle(
            company_policy_excerpt=base.company_policy_excerpt,
            task_dependencies=base.task_dependencies + dependency_content,
            selected_facts=base.selected_facts,
            selected_memory=selected_memory,
            ephemeral_instructions=(
                base.ephemeral_instructions
                + work_order_context
                + manager_instructions
                + ((retry_instruction,) if retry_instruction else ())
            ),
            task_evidence=task_evidence,
            workspace_id=base.workspace_id,
        )
        limits = RunLimits(
            max_wall_time_ms=min(company.runtime_limits.max_wall_time_ms, remaining_wall_ms),
            max_model_calls=reservation.model_calls,
            max_tool_calls=reservation.tool_calls,
            max_input_tokens=company.runtime_limits.max_input_tokens,
            max_output_tokens=company.runtime_limits.max_output_tokens,
            max_cost_usd=reservation.cost_usd,
            max_consecutive_errors=company.runtime_limits.max_consecutive_errors,
            max_result_bytes=company.runtime_limits.max_result_bytes,
            max_tool_output_bytes=company.runtime_limits.max_tool_output_bytes,
            max_context_messages=company.runtime_limits.max_context_messages,
            max_context_chars=company.runtime_limits.max_context_chars,
            context_keep_recent_messages=company.runtime_limits.context_keep_recent_messages,
            cost_efficiency_mode=company.runtime_limits.cost_efficiency_mode,
        )
        is_manager_integration = (
            employee.employee_id == company.manager_employee_id
            and task.task_id == graph.final_task_id
        )
        action_policy = frozen_employee_action_policy(
            company=company,
            graph=graph,
            task=task,
            employee_id=employee.employee_id,
        )
        if task_action_policy_override is not None:
            action_policy = task_action_policy_override(task, employee, action_policy)
            if not isinstance(action_policy, ActionPolicy):
                raise TypeError("Kernel task action policy override must return ActionPolicy")
        session_retention = (
            EmployeeSessionRetention.RUN_ONLY
            if employee.temporary
            or task_evidence is not None
            or task.execution_replica is not None
            else EmployeeSessionRetention.PERSIST
        )
        memory_namespace = f"employee:{employee.employee_id}"
        validator_ids = ["structured-completion-v1"]
        if task.acceptance_criteria:
            validator_ids.append("task-acceptance-v1")
        if "review" in employee.capabilities:
            validator_ids.append("independent-review-v1")
        if is_manager_integration:
            validator_ids.append("manager-integration-v1")
        capability_profile = build_employee_capability_profile(
            employee_id=employee.employee_id,
            roster_revision=company.roster_revision,
            model_profile=employee.model_profile,
            capabilities=employee.capabilities,
            skills=selected_skills,
            action_policy=action_policy,
            task_evidence=task_evidence,
            memory_namespace=memory_namespace,
            selected_memory=selected_memory,
            session_retention=session_retention,
            validator_ids=validator_ids,
            evaluation_revision=(
                "job-local-evaluation-v0"
                if employee.temporary
                else "employee-evaluation-v0"
            ),
        )
        return EmployeeRunRequest(
            request_id=(
                f"{company.request_id}:{task.task_id}:attempt-{task.attempt}:graph-{graph.version}"
            ),
            employee=EmployeeSnapshot(
                employee_id=employee.employee_id,
                role=employee.role,
                capabilities=employee.capabilities,
                temporary=employee.temporary,
                skills=selected_skills,
                model_profile=employee.model_profile,
                memory_namespace=memory_namespace,
                selected_memory_refs=tuple(item.content_id for item in selected_memory),
                capability_profile=capability_profile,
            ),
            task=TaskEnvelope(
                job_id=company.job_id,
                job_graph_version=graph.version,
                task_id=task.task_id,
                attempt=task.attempt,
                objective=task.objective,
                required_capabilities=task.required_capabilities,
                acceptance_criteria=task.acceptance_criteria,
                risk_level=task.risk_level,
            ),
            context=context,
            limits=limits,
            action_policy=action_policy,
            session_key=(
                execution_session_key
                if execution_session_key is not None
                else (
                    company.manager_session_key
                    if employee.employee_id == company.manager_employee_id
                    and company.manager_session_key
                    else company.session_key
                )
            ),
            session_retention=session_retention,
        )

    @staticmethod
    def _execution_instance_id(
        company: CompanyRunRequest,
        task: JobTask,
    ) -> str:
        replica = task.execution_replica
        if replica is None:
            return f"{company.job_id}:{task.task_id}:attempt-{task.attempt}"
        return (
            f"{company.job_id}:{replica.group_id}:{replica.replica_id}:"
            f"attempt-{task.attempt}"
        )

    @staticmethod
    def _task_action_policy(
        company: CompanyRunRequest,
        graph: JobGraph,
        task: JobTask,
    ) -> ActionPolicy:
        if company.plan_proposal.proposal_id.startswith("review-constraint-"):
            # The fallback may only report that mandatory review capacity was
            # unavailable. Enforce that refusal at the tool-authority boundary.
            return ActionPolicy()
        if task.task_id == graph.final_task_id:
            return FirmKernelPolicyMixin._without_manager_tools(company.action_policy)
        return FirmKernelPolicyMixin._read_only_action_policy(company)

    @staticmethod
    def _without_manager_tools(policy: ActionPolicy) -> ActionPolicy:
        return replace(
            policy,
            tool_grants=tuple(
                grant
                for grant in policy.tool_grants
                if not is_manager_tool(grant.tool_name)
            ),
        )

    @staticmethod
    def _read_only_action_policy(
        company: CompanyRunRequest,
        *,
        include_manager_tools: bool = False,
    ) -> ActionPolicy:
        """Project bounded local/external reads without mutation authority."""

        read_grants = []
        for grant in company.action_policy.tool_grants:
            if is_manager_tool(grant.tool_name) and not include_manager_tools:
                continue
            if ToolEffect.READ in grant.allowed_effects:
                read_grants.append(
                    replace(
                        grant,
                        allowed_effects=(ToolEffect.READ,),
                    )
                )
                continue
            # External evidence acquisition is modelled as NETWORK by the
            # tool boundary even though it is read-only.  Preserve only the
            # allowlisted first-party external-read family for non-final
            # research tasks; host actions, media generation, messaging and
            # every mutation effect remain exclusive to the final owner.
            if (
                ToolEffect.NETWORK in grant.allowed_effects
                and grant.resource_patterns
                and all(
                    resource.startswith("external-read:")
                    for resource in grant.resource_patterns
                )
            ):
                read_grants.append(
                    replace(
                        grant,
                        allowed_effects=(ToolEffect.NETWORK,),
                    )
                )
        has_external_read = any(
            ToolEffect.NETWORK in grant.allowed_effects for grant in read_grants
        )
        return replace(
            company.action_policy,
            tool_grants=tuple(read_grants),
            approval_grants=(),
            network_policy=("EXTERNAL_READ_ONLY" if has_external_read else "DENY"),
            filesystem_policy="READ_ONLY",
            sandbox_profile="none",
        )

    @staticmethod
    def _manager_integration_action_policy(company: CompanyRunRequest) -> ActionPolicy:
        if company.plan_proposal.proposal_id.startswith("review-constraint-"):
            return ActionPolicy()
        return FirmKernelPolicyMixin._read_only_action_policy(
            company,
            include_manager_tools=True,
        )


def frozen_employee_action_policy(
    *,
    company: CompanyRunRequest,
    graph: JobGraph,
    task: JobTask,
    employee_id: str,
) -> ActionPolicy:
    """Return the Kernel-owned task policy used by binding and dispatch.

    This is a read-only projection from one frozen Company request. It does
    not admit a task or create a grant; the Kernel remains the only caller
    that can attach the result to an executable EmployeeRunRequest.
    """

    if employee_id == company.manager_employee_id and task.task_id == graph.final_task_id:
        return FirmKernelPolicyMixin._manager_integration_action_policy(company)
    return FirmKernelPolicyMixin._task_action_policy(company, graph, task)
