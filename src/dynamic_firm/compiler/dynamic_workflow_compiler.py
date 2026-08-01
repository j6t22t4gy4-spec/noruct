"""Structured Dynamic Workflow Compiler implementation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from dynamic_firm.kernel.models import ExecutionReplicaPreference, ExecutionReplicaStrategy
from dynamic_firm.kernel.workflow_shape import canonical_workflow_shape
from dynamic_firm.runtime.models import ModelMessage, StructuredOutputRequest, Usage
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError

from .models import (
    CompilerDecision,
    CompilerExecutionProfile,
    CompilerReason,
    CompilerRequest,
    ManagerPlanningBrief,
    PlanningMode,
    PlanningOwner,
)
from .parser import PlanOutputError, PlanProposalError, parse_plan_proposal, plan_json_schema
from . import service as _service


def _fallback_decision(*args, **kwargs):
    # The public facade defines deterministic fallbacks after this component is
    # loaded; resolve it at execution time to avoid an import-time cycle.
    from .service import fallback_decision
    return fallback_decision(*args, **kwargs)


def _employee_model_call_ceiling(*args, **kwargs):
    return _service._employee_model_call_ceiling(*args, **kwargs)


def _has_duplicate_nonfinal_tasks(*args, **kwargs):
    return _service._has_duplicate_nonfinal_tasks(*args, **kwargs)


def _has_required_review_boundary(*args, **kwargs):
    return _service._has_required_review_boundary(*args, **kwargs)


def _host_action_capability(*args, **kwargs):
    return _service._host_action_capability(*args, **kwargs)


def _structured_model_call_ceiling(*args, **kwargs):
    return _service._structured_model_call_ceiling(*args, **kwargs)


def _system_prompt_for(*args, **kwargs):
    return _service._system_prompt_for(*args, **kwargs)


def _valid_host_action_shape(*args, **kwargs):
    return _service._valid_host_action_shape(*args, **kwargs)


def aligned_workflow_prior_ids(*args, **kwargs):
    return _service.aligned_workflow_prior_ids(*args, **kwargs)

class DynamicWorkflowCompiler:
    """One model proposal followed by deterministic parsing and graph validation."""

    def __init__(self, provider: object | None) -> None:
        self.provider = provider

    async def compile(self, request: CompilerRequest) -> CompilerDecision:
        self._validate_request(request)
        if request.max_total_model_calls < 2:
            return _fallback_decision(request, CompilerReason.COMPILER_SKIPPED_BUDGET)
        complete_structured = getattr(self.provider, "complete_structured", None)
        if not callable(complete_structured):
            return _fallback_decision(request, CompilerReason.COMPILER_UNAVAILABLE)
        structured_call_ceiling = _structured_model_call_ceiling(self.provider)
        if structured_call_ceiling is None:
            return _fallback_decision(request, CompilerReason.COMPILER_UNAVAILABLE)
        employee_call_ceiling = _employee_model_call_ceiling(self.provider)
        # Planning is optional. Reserve the provider wrapper's worst-case
        # structured fan-out before calling it and preserve at least one model
        # call for the Employee that must produce the user-visible result.
        if structured_call_ceiling + employee_call_ceiling > request.max_total_model_calls:
            return _fallback_decision(request, CompilerReason.COMPILER_SKIPPED_BUDGET)

        structured_request = StructuredOutputRequest(
            messages=(
                ModelMessage(
                    "system",
                    _system_prompt_for(
                        request.execution_profile,
                        requires_independent_review=(
                            request.requires_independent_review
                        ),
                        execution_replica_preference=(
                            request.execution_replica_preference
                        ),
                        manager_planning=request.planning_owner is not None,
                    )
                    + (
                        _service._WORKFLOW_PRIOR_INSTRUCTION
                        if request.workflow_priors
                        else ""
                    ),
                ),
                ModelMessage("user", self._user_payload(request)),
            ),
            schema_name="dynamic_firm_plan_proposal",
            json_schema=plan_json_schema(max_tasks=request.max_tasks),
            model_profile=request.model_profile,
            request_id=request.request_id,
        )
        cancellation = CancellationToken()
        try:
            response = await asyncio.wait_for(
                complete_structured(structured_request, cancellation),
                timeout=request.max_wall_time_ms / 1000,
            )
        except TimeoutError:
            cancellation.cancel("Compiler wall-time budget exhausted")
            return _fallback_decision(
                request,
                CompilerReason.COMPILER_WALL_TIME_EXHAUSTED,
                # The provider did not return a trustworthy usage aggregate.
                # Charge the pre-admitted ceiling because parallel advisors or
                # fallback routes may already have started before cancellation.
                usage=Usage(model_calls=structured_call_ceiling),
            )
        except ModelProviderError as exc:
            reason = (
                CompilerReason.COMPILER_OUTPUT_INVALID
                if exc.code == "MODEL_STRUCTURED_OUTPUT_INVALID"
                else CompilerReason.COMPILER_PROVIDER_FAILURE
            )
            usage = replace(
                exc.usage,
                model_calls=max(1, exc.usage.model_calls),
                tool_calls=0,
            )
            return _fallback_decision(request, reason, usage=usage)
        except Exception:
            return _fallback_decision(
                request,
                CompilerReason.COMPILER_PROVIDER_FAILURE,
                # The provider boundary failed before returning a trustworthy
                # aggregate.  Composite routes may already have consumed their
                # full physical-call closure.
                usage=Usage(model_calls=structured_call_ceiling),
            )

        usage = replace(
            response.usage,
            model_calls=max(1, response.usage.model_calls),
            tool_calls=0,
        )
        if usage.model_calls >= request.max_total_model_calls:
            return _fallback_decision(
                request,
                CompilerReason.COMPILER_BUDGET_EXHAUSTED,
                usage=usage,
                provider_request_id=response.provider_request_id,
            )
        try:
            parsed = parse_plan_proposal(
                response.value,
                proposal_id=f"compiler-{request.request_id}",
                goal=request.goal,
                max_tasks=request.max_tasks,
                available_capabilities=request.available_capabilities,
                max_temporary_roles=request.max_temporary_roles,
            )
        except PlanOutputError:
            return _fallback_decision(
                request,
                CompilerReason.COMPILER_OUTPUT_INVALID,
                usage=usage,
                provider_request_id=response.provider_request_id,
            )
        except PlanProposalError:
            return _fallback_decision(
                request,
                CompilerReason.COMPILER_PROPOSAL_REJECTED,
                usage=usage,
                provider_request_id=response.provider_request_id,
            )

        if _has_duplicate_nonfinal_tasks(parsed.proposal):
            return _fallback_decision(
                request,
                CompilerReason.COMPILER_PROPOSAL_REJECTED,
                usage=usage,
                provider_request_id=response.provider_request_id,
            )

        if request.execution_profile.requires_implementation:
            final = next(
                task
                for task in parsed.proposal.tasks
                if task.task_id == parsed.proposal.final_task_id
            )
            invalid_coding_shape = "implementation" not in final.required_capabilities or any(
                "implementation" in task.required_capabilities
                for task in parsed.proposal.tasks
                if task.task_id != parsed.proposal.final_task_id
            )
            if invalid_coding_shape:
                return _fallback_decision(
                    request,
                    CompilerReason.COMPILER_PROPOSAL_REJECTED,
                    usage=usage,
                    provider_request_id=response.provider_request_id,
                )

        if request.execution_profile == CompilerExecutionProfile.HOST_ACTION:
            if not _valid_host_action_shape(parsed.proposal, request):
                return _fallback_decision(
                    request,
                    CompilerReason.COMPILER_PROPOSAL_REJECTED,
                    usage=usage,
                    provider_request_id=response.provider_request_id,
                )

        if request.requires_independent_review and not _has_required_review_boundary(
            parsed.proposal
        ):
            return _fallback_decision(
                request,
                CompilerReason.COMPILER_REQUIRED_REVIEW_MISSING,
                usage=usage,
                provider_request_id=response.provider_request_id,
            )

        remaining_employee_calls = request.max_total_model_calls - usage.model_calls
        if len(parsed.proposal.tasks) * employee_call_ceiling > remaining_employee_calls:
            return _fallback_decision(
                request,
                CompilerReason.COMPILER_PROPOSAL_REJECTED,
                usage=usage,
                provider_request_id=response.provider_request_id,
            )

        if parsed.source_mode == "SOLO":
            return CompilerDecision(
                proposal=parsed.proposal,
                mode=PlanningMode.SOLO,
                reason=CompilerReason.VALID_SOLO,
                rationale=parsed.rationale,
                usage=usage,
                provider_request_id=response.provider_request_id,
                exposed_workflow_prior_ids=tuple(
                    prior.pattern_id for prior in request.workflow_priors
                ),
                aligned_workflow_prior_ids=aligned_workflow_prior_ids(
                    parsed.proposal, request.workflow_priors
                ),
                planning_owner_id=(
                    request.planning_owner.employee_id
                    if request.planning_owner is not None
                    else ""
                ),
                planning_owner_assignment_digest=(
                    request.planning_owner.assignment_digest
                    if request.planning_owner is not None
                    else ""
                ),
                manager_planning_brief_digest=(
                    request.manager_planning_brief.content_digest
                    if request.manager_planning_brief is not None
                    else ""
                ),
            )
        return CompilerDecision(
            proposal=parsed.proposal,
            mode=PlanningMode.DYNAMIC,
            reason=CompilerReason.VALID_DYNAMIC,
            rationale=parsed.rationale,
            usage=usage,
            provider_request_id=response.provider_request_id,
            exposed_workflow_prior_ids=tuple(
                prior.pattern_id for prior in request.workflow_priors
            ),
            aligned_workflow_prior_ids=aligned_workflow_prior_ids(
                parsed.proposal, request.workflow_priors
            ),
            planning_owner_id=(
                request.planning_owner.employee_id
                if request.planning_owner is not None
                else ""
            ),
            planning_owner_assignment_digest=(
                request.planning_owner.assignment_digest
                if request.planning_owner is not None
                else ""
            ),
            manager_planning_brief_digest=(
                request.manager_planning_brief.content_digest
                if request.manager_planning_brief is not None
                else ""
            ),
        )

    @staticmethod
    def _validate_request(request: CompilerRequest) -> None:
        if not request.request_id.strip() or not request.goal.strip() or not request.model_profile.strip():
            raise ValueError("Compiler request requires request id, goal, and model profile")
        if not 1 <= request.max_tasks <= 6:
            raise ValueError("Compiler task limit must be between 1 and 6")
        if request.max_temporary_roles < 0 or request.max_total_model_calls < 1:
            raise ValueError("Compiler limits are invalid")
        if type(request.max_wall_time_ms) is not int or request.max_wall_time_ms < 1:
            raise ValueError("Compiler wall-time limit must be a positive integer")
        if not isinstance(request.requires_independent_review, bool):
            raise ValueError("Independent review constraint must be boolean")
        if request.planning_owner is not None:
            # Dataclass construction validates the complete immutable binding;
            # repeat the relationship check here so forged duck-typed values
            # cannot enter the provider boundary.
            if not isinstance(request.planning_owner, PlanningOwner):
                raise ValueError("Planning owner must be typed")
        if request.manager_planning_brief is not None:
            if request.planning_owner is None:
                raise ValueError("Manager planning brief requires a planning owner")
            if not isinstance(request.manager_planning_brief, ManagerPlanningBrief):
                raise ValueError("Manager planning brief must be typed")
        if not isinstance(
            request.execution_replica_preference,
            ExecutionReplicaPreference,
        ):
            raise ValueError("Execution replica preference must be typed")
        if (
            request.suggested_execution_replica_strategy is not None
            and not isinstance(
                request.suggested_execution_replica_strategy,
                ExecutionReplicaStrategy,
            )
        ):
            raise ValueError("Suggested execution replica strategy must be typed")
        if (
            request.execution_replica_preference
            is ExecutionReplicaPreference.DISABLED
            and request.suggested_execution_replica_strategy is not None
        ):
            raise ValueError("Disabled replica planning cannot suggest a strategy")
        if request.required_final_action_capability:
            if (
                request.execution_profile != CompilerExecutionProfile.HOST_ACTION
                or not _service._CAPABILITY_SLUG.fullmatch(
                    request.required_final_action_capability
                )
                or request.required_final_action_capability
                not in request.available_capabilities
            ):
                raise ValueError("Required final action capability is invalid")
        if len(request.workspace_manifest) > 500:
            raise ValueError("Compiler workspace manifest exceeds the entry limit")
        if len(request.goal) > 20_000:
            raise ValueError("Compiler goal exceeds the input limit")
        if len(request.workflow_priors) > 8:
            raise ValueError("Compiler workflow prior limit exceeded")
        if len(request.workflow_context_fingerprint) > 128:
            raise ValueError("Compiler workflow context fingerprint is invalid")
        for prior in request.workflow_priors:
            if (
                not prior.pattern_id.strip()
                or len(prior.pattern_id) > 128
                or not prior.task_family.strip()
                or len(prior.task_family) > 128
                or len(prior.context_fingerprint) > 128
                or len(prior.rationale) > 1_000
                or not 1 <= prior.evidence_count <= 10_000
                or not 1 <= len(prior.tasks) <= request.max_tasks
            ):
                raise ValueError("Compiler workflow prior is invalid or unbounded")
            if prior.execution_profile != request.execution_profile:
                raise ValueError("Compiler workflow prior profile does not match the request")
            if (
                not request.workflow_context_fingerprint
                or prior.context_fingerprint != request.workflow_context_fingerprint
            ):
                raise ValueError("Compiler workflow prior context does not match the request")
            task_keys = {task.task_key for task in prior.tasks}
            if len(task_keys) != len(prior.tasks) or sum(task.final for task in prior.tasks) != 1:
                raise ValueError("Compiler workflow prior task template is invalid")
            for task in prior.tasks:
                if (
                    not task.task_key.strip()
                    or len(task.task_key) > 128
                    or not task.required_capabilities
                    or any(len(item) > 128 or not item for item in task.required_capabilities)
                    or any(item not in task_keys for item in task.depends_on)
                ):
                    raise ValueError("Compiler workflow prior task template is invalid")
            canonical_workflow_shape(
                prior.tasks,
                key_of=lambda task: task.task_key,
                capabilities_of=lambda task: task.required_capabilities,
                dependencies_of=lambda task: task.depends_on,
                final_of=lambda task: task.final,
            )
        manifest_bytes = 0
        for path in request.workspace_manifest:
            if (
                not isinstance(path, str)
                or not path
                or len(path) > 512
                or path.startswith("/")
                or ".." in path.split("/")
                or "\x00" in path
            ):
                raise ValueError("Compiler workspace manifest contains an invalid path")
            manifest_bytes += len(path.encode("utf-8"))
        if manifest_bytes > 64_000:
            raise ValueError("Compiler workspace manifest exceeds the byte limit")

    @staticmethod
    def _user_payload(request: CompilerRequest) -> str:
        return json.dumps(
            {
                "goal": request.goal,
                "planning_owner": (
                    None
                    if request.planning_owner is None
                    else {
                        "employee_id": request.planning_owner.employee_id,
                        "role": request.planning_owner.role,
                        "assignment_digest": request.planning_owner.assignment_digest,
                    }
                ),
                "manager_planning_brief": (
                    None
                    if request.manager_planning_brief is None
                    else {
                        "company_revision": request.manager_planning_brief.company_revision,
                        "company_purpose": request.manager_planning_brief.company_purpose,
                        "work_order_constraints": list(
                            request.manager_planning_brief.work_order_constraints
                        ),
                        "skills": [
                            {
                                "skill_key": item.skill_key,
                                "revision": item.revision,
                                "purpose": item.purpose,
                                "content_hash": item.content_hash,
                            }
                            for item in request.manager_planning_brief.skills
                        ],
                        "outcome_summary": {
                            "context_fingerprint": (
                                request.manager_planning_brief.outcome_summary.context_fingerprint
                            ),
                            "observed_count": (
                                request.manager_planning_brief.outcome_summary.observed_count
                            ),
                            "succeeded_count": (
                                request.manager_planning_brief.outcome_summary.succeeded_count
                            ),
                            "safety_passed_count": (
                                request.manager_planning_brief.outcome_summary.safety_passed_count
                            ),
                            "effect_passed_count": (
                                request.manager_planning_brief.outcome_summary.effect_passed_count
                            ),
                        },
                        "knowledge_brief": (
                            None
                            if not request.manager_planning_brief.knowledge_pack_id
                            else {
                                "pack_id": request.manager_planning_brief.knowledge_pack_id,
                                "pack_digest": request.manager_planning_brief.knowledge_pack_digest,
                                "delivery_digest": request.manager_planning_brief.knowledge_delivery_digest,
                                "citations": [
                                    {
                                        "citation_id": item.citation_id,
                                        "source_id": item.source_id,
                                        "source_revision": item.source_revision,
                                    }
                                    for item in request.manager_planning_brief.knowledge_citations
                                ],
                                "content_retained": False,
                            }
                        ),
                        "content_digest": request.manager_planning_brief.content_digest,
                    }
                ),
                "workspace_manifest": list(request.workspace_manifest),
                "available_capabilities": list(request.available_capabilities),
                "execution_profile": request.execution_profile.value,
                "requires_independent_review": request.requires_independent_review,
                "execution_replica_preference": (
                    request.execution_replica_preference.value
                ),
                "suggested_execution_replica_strategy": (
                    None
                    if request.suggested_execution_replica_strategy is None
                    else request.suggested_execution_replica_strategy.value
                ),
                "required_final_action_capability": (
                    _host_action_capability(request)
                    if request.execution_profile
                    == CompilerExecutionProfile.HOST_ACTION
                    else ""
                ),
                "workflow_context_fingerprint": request.workflow_context_fingerprint,
                "verified_workflow_priors": [
                    {
                        "pattern_id": prior.pattern_id,
                        "task_family": prior.task_family,
                        "context_fingerprint": prior.context_fingerprint,
                        "execution_profile": prior.execution_profile.value,
                        "rationale": prior.rationale,
                        "evidence_count": prior.evidence_count,
                        "tasks": [
                            {
                                "task_key": task.task_key,
                                "required_capabilities": list(task.required_capabilities),
                                "depends_on": list(task.depends_on),
                                "final": task.final,
                            }
                            for task in prior.tasks
                        ],
                    }
                    for prior in request.workflow_priors
                ],
                "limits": {
                    "max_tasks": request.max_tasks,
                    "max_temporary_roles": request.max_temporary_roles,
                    "max_total_model_calls": request.max_total_model_calls,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
