from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import PurePosixPath

from dynamic_firm.kernel.models import (
    ExecutionReplicaPreference,
    ExecutionReplicaStrategy,
    JobTask,
    PlanProposal,
)
from dynamic_firm.kernel.workflow_shape import canonical_workflow_shape
from dynamic_firm.runtime.models import ModelMessage, StructuredOutputRequest, Usage
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError

from .models import (
    CompilerDecision,
    CompilerExecutionProfile,
    CompilerReason,
    CompilerRequest,
    ManagerPlanningBrief,
    PlanningOwner,
    PlanningMode,
    WorkflowPrior,
)
from .parser import PlanOutputError, PlanProposalError, parse_plan_proposal, plan_json_schema
from .fallback_capabilities import (
    capability_may_own_effect as _capability_may_own_effect,
    has_required_review_boundary as _has_required_review_boundary,
    host_action_capability as _host_action_capability,
    reporting_capability as _reporting_capability,
    review_capability as _review_capability,
    solo_first_capability as _solo_first_capability,
    task_has_review_capability as _task_has_review_capability,
    valid_host_action_shape as _valid_host_action_shape,
)


_SYSTEM_PROMPT = """You are the bounded Dynamic Workflow Compiler for one read-only company job.
Return only the requested structured plan object.

Choose SOLO when one employee can produce the integrated outcome in one context. A sequence of
ordinary reasoning steps is not a reason to create a team. Choose GRAPH only when there are useful
independent intermediate results or a genuinely different specialist capability. Express actual
data dependencies only. Do not maximize parallelism; the runtime derives concurrency from the DAG.

Every task must contribute through a dependency path to final_task_id. The final task integrates the
goal and, in GRAPH mode, depends on at least one prior task. Use stable lowercase_slug identifiers.
Request capabilities, not employee or role names. All actions are LOW risk and read-only. Do not add
implementation, writing, shell, network, deployment, purchase, approval, or irreversible work."""


_SHADOW_CODING_SYSTEM_PROMPT = """You are the bounded Dynamic Workflow Compiler for one coding job.
Return only the requested structured plan object.

Choose SOLO when one implementation employee can complete the bounded change in one context. Choose
GRAPH only when independent read-only evidence gathering or a genuinely different specialist improves
the final implementation. Express actual data dependencies only; the runtime derives concurrency.

Exactly the final task must request the implementation capability. Every non-final task is read-only,
must contribute through a dependency path to the final task, and must not request implementation.
The external coding worker can edit only a disposable shadow. Noruct owns change-set validation,
approval, and real-workspace apply. Use stable lowercase_slug identifiers and LOW risk only. Do not add
network, external communication, dependency installation, deployment, purchase, privileged, destructive,
secret-bearing, or irreversible work."""


_HOST_DIRECT_SYSTEM_PROMPT = """You are the bounded Dynamic Workflow Compiler for one coding job.
Return only the requested structured plan object.

Choose SOLO when one implementation employee can complete the bounded change in one context. Choose
GRAPH only when independent read-only evidence gathering or a genuinely different specialist improves
the final implementation. Express actual data dependencies only; the runtime derives concurrency.

Exactly the final task must request the implementation capability. Every non-final task is read-only,
must contribute through a dependency path to the final task, and must not request implementation.
The final employee may use only approved host-direct workspace tools; Noruct owns path validation,
approval, audit, and real-workspace mutation. Use stable lowercase_slug identifiers and LOW risk only.
Do not add network, external communication, dependency installation, deployment, purchase, privileged,
destructive, secret-bearing, or irreversible work."""


_HOST_ACTION_SYSTEM_PROMPT = """You are the bounded Dynamic Workflow Compiler for one approved-action company job.
Return only the requested structured plan object.

Choose SOLO when one employee can complete the explicitly requested action in one context. Choose
GRAPH only when independent read-only evidence or a genuinely different specialist must precede the
action. Express actual data dependencies only; the runtime derives concurrency.

HOST_ACTION describes an approval-gated action lane, not a coding intent. Do not request the
implementation capability solely because host tools are available. Tasks may request only capabilities
needed by the stated goal. Exactly final_task_id must include the user payload's
required_final_action_capability. Non-final tasks must be read-only and must not request that capability
or any write, execute, command, process, deployment, messaging, or other action capability. Every task
must contribute through a dependency path to final_task_id. Use stable lowercase_slug identifiers and
LOW risk only. Do not infer additional commands, file changes, network communication, deployment,
purchase, privilege, destructive work, secrets, or irreversible work."""


_REVIEW_STAGE_INSTRUCTION = """

When independent validation materially reduces risk or resolves conflicting evidence, add a bounded
reviewer task immediately before the final integration task. The reviewer must depend on the evidence
it validates, must not be final_task_id, and must not write the integrated user answer. The final task
must depend on the reviewer and remains the only final writer. Do not add a reviewer merely to create a
team; ordinary low-risk work stays SOLO."""


_EXECUTION_REPLICA_INSTRUCTION = """

The same persistent Employee profile may be executed 2-4 times for one bounded workload when doing so
has explicit expected marginal value. Use execution_replica only for: PARTITION (disjoint scopes,
JOIN), CANDIDATE (same scope, distinct candidates, VALIDATOR_SELECT by a separate validation/review
task), or DIAGNOSTIC (distinct probes, JOIN or MANAGER_SYNTHESIS). Every member must share capabilities
and upstream dependencies, must be non-final and read-only, and must name one separate
aggregation_task_id that directly depends on every member. Replicas are not different specialists or
independent reviewers. State the expected quality, coverage, diagnosis, recovery, or latency gain in
each marginal_value_reason; do not fan out merely to imitate a team. The supplied task/model-call
limits are hard ceilings and may never be enlarged or hidden by replica planning."""


_PERFORMANCE_FIRST_REPLICA_INSTRUCTION = """

execution_replica_preference is PERFORMANCE_FIRST. Actively test whether a 2-4 run replica group is
worth proposing whenever the goal exposes disjoint breadth, multiple valuable candidates, or an
unclear failure cause. A single execution being technically possible is not a sufficient reason to
reject fan-out: compare expected accepted-result quality, coverage, recovery probability, and useful
latency under the existing hard ceilings. Prefer the smallest 2-3 run group that captures the gain;
use four only when the scopes or candidate set justify it. The suggested strategy is advisory: use it
when exact safe scopes and aggregation can be expressed, otherwise return the best SOLO/GRAPH plan.
Never replicate the final writer or an effectful task."""


_BALANCED_REPLICA_INSTRUCTION = """

execution_replica_preference is BALANCED. Propose a replica group only when its expected marginal
quality, coverage, diagnosis, recovery, or latency gain is concrete enough to justify its share of the
fixed hard budget. Technical feasibility of SOLO is relevant but not decisive."""


_DISABLED_REPLICA_INSTRUCTION = """

execution_replica_preference is DISABLED. Do not emit execution_replica metadata or create duplicate
same-capability tasks solely for fan-out. Respect this explicit coordination constraint."""


_REQUIRED_REVIEW_INSTRUCTION = """

The user payload sets requires_independent_review=true. This is a mandatory coordination constraint,
not an advisory preference. Return GRAPH, include a non-final reviewer task whose capability is
independent_review, review, or a *_review capability, and make the final task depend directly on that
reviewer. A SOLO proposal or a graph without that direct review boundary is invalid."""


_CAPABILITY_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
def _system_prompt_for(
    profile: CompilerExecutionProfile,
    *,
    requires_independent_review: bool = False,
    execution_replica_preference: ExecutionReplicaPreference = (
        ExecutionReplicaPreference.PERFORMANCE_FIRST
    ),
    manager_planning: bool = False,
) -> str:
    if profile == CompilerExecutionProfile.SHADOW_CODING:
        prompt = _SHADOW_CODING_SYSTEM_PROMPT
    elif profile == CompilerExecutionProfile.HOST_DIRECT:
        prompt = _HOST_DIRECT_SYSTEM_PROMPT
    elif profile == CompilerExecutionProfile.HOST_ACTION:
        prompt = _HOST_ACTION_SYSTEM_PROMPT
    else:
        prompt = _SYSTEM_PROMPT
    prompt += _REVIEW_STAGE_INSTRUCTION
    prompt += _EXECUTION_REPLICA_INSTRUCTION
    prompt += _replica_preference_instruction(execution_replica_preference)
    if requires_independent_review:
        prompt += _REQUIRED_REVIEW_INSTRUCTION
    if manager_planning:
        prompt += (
            "\nYou are acting as the persistent Executive Manager for this one "
            "Company Work Order. Make a staffing and dependency proposal, not a "
            "role-play conversation. Assign only bounded deliverables that have a "
            "capability, acceptance, and artifact reason. You do not gain permission, "
            "budget, approval, or state-mutation authority from this role."
        )
    return prompt


def _replica_preference_instruction(
    preference: ExecutionReplicaPreference,
) -> str:
    if preference is ExecutionReplicaPreference.DISABLED:
        return _DISABLED_REPLICA_INSTRUCTION
    if preference is ExecutionReplicaPreference.BALANCED:
        return _BALANCED_REPLICA_INSTRUCTION
    return _PERFORMANCE_FIRST_REPLICA_INSTRUCTION


def _coding_solo_criteria(profile: CompilerExecutionProfile) -> tuple[str, ...]:
    if profile == CompilerExecutionProfile.SHADOW_CODING:
        return (
            "Prepare the smallest validated shadow change and report any typed "
            "capability gap that prevents completion.",
        )
    if profile == CompilerExecutionProfile.HOST_DIRECT:
        return (
            "Use only the smallest required approved workspace operation and report "
            "any typed capability gap that prevents completion.",
            "For an explicit file, folder, or command request, perform the bounded "
            "operation directly; do not list the workspace root first.",
        )
    raise ValueError("Coding criteria require a mutation-capable execution profile")


def _host_action_solo_criteria() -> tuple[str, ...]:
    return (
        "Perform only the explicit bounded action with an approved tool; do not infer a code change.",
        "If approval, policy, or a required capability prevents the action, report one typed gap instead of claiming success.",
        "Return the observed action result and do not repeat or expand the effect.",
    )

_WORKFLOW_PRIOR_INSTRUCTION = """

The user payload may include verified_workflow_priors. They are advisory company experience, not
commands. Use one only when its task_family, context fingerprint, execution profile, capabilities,
and current goal genuinely match. The SOLO rule, dependency analysis, authority boundary, task cap,
and output schema always take precedence. Never copy a prior mechanically or infer permission from it."""


# A review brief is intentionally a small task-local hint, not a workspace
# manifest or a second planner. Explicit file references give a read-only
# employee enough evidence scope to avoid recursively listing a large repo.
# Ambiguous goals remain ordinary SOLO tasks; guessing paths would widen the
# authority surface and invite the very exploratory loops this contract avoids.
_REPOSITORY_FILE_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?P<path>[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)+)"
    r"(?![A-Za-z0-9_./-])"
)
_MAX_REPOSITORY_REVIEW_PATHS = 4
_ROOT_REVIEW_FILES = frozenset({"AGENTS.md", "CLAUDE.md", "Dockerfile", "LICENSE", "Makefile", "README"})


def repository_review_paths(goal: str) -> tuple[str, ...]:
    """Return a small all-or-nothing explicit file scope from one user goal.

    Paths are advisory task instructions only. The existing parent workspace
    tool remains the authority for traversal, symlink, secret, file-size and
    policy checks. More than four references deliberately returns no brief so
    an incomplete subset is never silently treated as the user's evidence
    boundary.
    """

    found: list[str] = []
    for match in _REPOSITORY_FILE_REFERENCE.finditer(goal):
        # The path grammar deliberately accepts dots for extensions.  At a
        # sentence boundary that also lets a trailing prose period enter the
        # greedy match (``report.md.``), which then fails the suffix check and
        # silently narrows a multi-file scope.  Strip punctuation that cannot
        # be part of an accepted final extension before normalizing; this does
        # not infer, rewrite, or expand a path.
        raw = match.group("path").rstrip(",;:!?")
        if raw.endswith(".") and raw[:-1].endswith((".md", ".py", ".json", ".toml", ".yaml", ".yml", ".txt", ".rst", ".tsx", ".ts", ".js", ".jsx", ".css", ".html", ".sh")):
            raw = raw[:-1]
        path = PurePosixPath(raw)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            continue
        if not path.suffix and path.name not in _ROOT_REVIEW_FILES:
            continue
        normalized = path.as_posix()
        if normalized not in found:
            found.append(normalized)
    if not 1 <= len(found) <= _MAX_REPOSITORY_REVIEW_PATHS:
        return ()
    return tuple(found)


def _read_only_solo_criteria(goal: str) -> tuple[str, ...]:
    paths = repository_review_paths(goal)
    generic = (
        "Return a concise answer grounded in workspace evidence, or emit one "
        "typed capability gap that prevents completion."
    )
    if not paths:
        return (generic,)
    rendered_paths = ", ".join(paths)
    return (
        f"Evidence scope is limited to these explicitly named workspace files: {rendered_paths}.",
        "Read those files directly with read_workspace_file; do not call list_workspace_files or explore other workspace paths.",
        "If a scoped read is rejected, do not retry it as an absolute or altered path; return one concise bounded limitation instead.",
        "After the scoped evidence is available, return the final structured completion instead of repeating a read.",
        generic,
    )


def aligned_workflow_prior_ids(
    proposal: PlanProposal,
    priors: tuple[WorkflowPrior, ...],
) -> tuple[str, ...]:
    proposal_shape = canonical_workflow_shape(
        proposal.tasks,
        key_of=lambda task: task.task_id,
        capabilities_of=lambda task: task.required_capabilities,
        dependencies_of=lambda task: task.depends_on,
        final_of=lambda task: task.task_id == proposal.final_task_id,
    )
    aligned: list[str] = []
    for prior in priors:
        prior_shape = canonical_workflow_shape(
            prior.tasks,
            key_of=lambda task: task.task_key,
            capabilities_of=lambda task: task.required_capabilities,
            dependencies_of=lambda task: task.depends_on,
            final_of=lambda task: task.final,
        )
        if proposal_shape == prior_shape:
            aligned.append(prior.pattern_id)
    return tuple(aligned)


def _structured_model_call_ceiling(provider: object | None) -> int | None:
    """Return a trustworthy worst-case structured-call count for admission.

    Plain providers are one-call implementations. Composite providers expose
    the exact ceiling they can fan out to. An invalid advertised value fails
    closed instead of letting the compiler spend an unbounded planning budget.
    """

    try:
        value = getattr(provider, "structured_model_call_ceiling", 1)
    except Exception:
        return None
    if type(value) is not int or value < 1:
        return None
    return value


def _employee_model_call_ceiling(provider: object | None) -> int:
    """Return the physical-call closure for one logical Employee turn."""

    try:
        value = getattr(provider, "model_call_ceiling", 1)
    except Exception:
        return 1
    if type(value) is not int or value < 1:
        return 1
    return value


def _has_duplicate_nonfinal_tasks(proposal: PlanProposal) -> bool:
    """Reject exact duplicate work hidden behind different task identifiers."""

    seen: dict[tuple[object, ...], JobTask] = {}
    for task in proposal.tasks:
        if task.task_id == proposal.final_task_id:
            continue
        signature = (
            " ".join(task.objective.casefold().split()),
            tuple(sorted(task.depends_on)),
            tuple(sorted(task.required_capabilities)),
            tuple(
                " ".join(criterion.casefold().split())
                for criterion in task.acceptance_criteria
            ),
        )
        prior = seen.get(signature)
        if prior is not None:
            # A validated CANDIDATE group deliberately repeats one bounded
            # scope. Graph validation has already required a selector and a
            # complete value contract, so it is not an identity-only clone.
            replica = task.execution_replica
            prior_replica = prior.execution_replica
            if (
                replica is None
                or prior_replica is None
                or replica.strategy.value != "CANDIDATE"
                or prior_replica.strategy.value != "CANDIDATE"
                or replica.group_id != prior_replica.group_id
            ):
                return True
        seen[signature] = task
    return False


from .dynamic_workflow_compiler import DynamicWorkflowCompiler  # noqa: E402


def fallback_decision(
    request: CompilerRequest,
    reason: CompilerReason,
    *,
    usage: Usage | None = None,
    provider_request_id: str | None = None,
) -> CompilerDecision:
    actual_usage = usage or Usage()
    if actual_usage.model_calls >= request.max_total_model_calls:
        return _with_planning_provenance(
            _budget_exhausted_decision(
                request,
                usage=actual_usage,
                provider_request_id=provider_request_id,
            ),
            request,
        )
    if request.requires_independent_review:
        return _with_planning_provenance(
            _required_review_fallback_decision(
                request,
                reason=reason,
                usage=actual_usage,
                provider_request_id=provider_request_id,
            ),
            request,
        )

    coding = request.execution_profile.requires_implementation
    host_action = request.execution_profile == CompilerExecutionProfile.HOST_ACTION
    task_id = "implement_change" if coding else "perform_action" if host_action else "analyze_goal"
    if coding:
        criteria = _coding_solo_criteria(request.execution_profile)
    elif host_action:
        criteria = _host_action_solo_criteria()
    else:
        criteria = _read_only_solo_criteria(request.goal)
    capability = (
        "implementation"
        if coding
        else _host_action_capability(request)
        if host_action
        else "repository_analysis"
    )
    proposal = PlanProposal(
        proposal_id=f"solo-fallback-{request.request_id}",
        goal=request.goal,
        tasks=(
            JobTask(
                task_id=task_id,
                objective=request.goal,
                depends_on=(),
                required_capabilities=(capability,),
                acceptance_criteria=criteria,
            ),
        ),
        final_task_id=task_id,
    )
    return _with_planning_provenance(CompilerDecision(
        proposal=proposal,
        mode=PlanningMode.SOLO_FALLBACK,
        reason=reason,
        rationale="The dynamic proposal was unavailable or unsafe; use the bounded solo path.",
        usage=actual_usage,
        provider_request_id=provider_request_id,
        exposed_workflow_prior_ids=tuple(
            prior.pattern_id for prior in request.workflow_priors
        ),
    ), request)


def _with_planning_provenance(
    decision: CompilerDecision,
    request: CompilerRequest,
) -> CompilerDecision:
    """Carry an admitted Manager planning binding through safe fallbacks.

    A fallback is still an output of the Manager-bound Compiler invocation,
    not a new unowned plan.  Omitting its binding makes a legitimate safe
    fallback impossible to audit and falsely turns it into an invalid Manager
    evaluation record.
    """

    return replace(
        decision,
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


def _required_review_fallback_decision(
    request: CompilerRequest,
    *,
    reason: CompilerReason,
    usage: Usage,
    provider_request_id: str | None,
) -> CompilerDecision:
    remaining_employee_calls = request.max_total_model_calls - usage.model_calls
    review_capability = _review_capability(request)
    coding = request.execution_profile.requires_implementation
    host_action = request.execution_profile == CompilerExecutionProfile.HOST_ACTION
    final_task_id = (
        "implement_change"
        if coding
        else "perform_action"
        if host_action
        else "integrate_goal"
    )
    final_capability = (
        "implementation"
        if coding
        else _host_action_capability(request)
        if host_action
        else _solo_first_capability(request)
    )
    available = {
        capability.strip()
        for capability in request.available_capabilities
        if capability.strip()
    }
    missing_capabilities = {
        capability
        for capability in (review_capability, final_capability)
        if capability and capability not in available
    }
    can_build_review_graph = (
        request.max_tasks >= 2
        and remaining_employee_calls >= 2
        and review_capability is not None
        and len(missing_capabilities) <= request.max_temporary_roles
    )
    if can_build_review_graph:
        proposal = PlanProposal(
            proposal_id=f"review-fallback-{request.request_id}",
            goal=request.goal,
            tasks=(
                JobTask(
                    task_id="independent_review",
                    objective=(
                        "Independently review the evidence, constraints, and proposed "
                        "approach before the final owner acts."
                    ),
                    depends_on=(),
                    required_capabilities=(review_capability,),
                    acceptance_criteria=(
                        "Return a bounded independent review for the final owner.",
                        "Do not perform the requested host action or workspace mutation.",
                    ),
                ),
                JobTask(
                    task_id=final_task_id,
                    objective=request.goal,
                    depends_on=("independent_review",),
                    required_capabilities=(final_capability,),
                    acceptance_criteria=(
                        _coding_solo_criteria(request.execution_profile)
                        if coding
                        else _host_action_solo_criteria()
                        if host_action
                        else _read_only_solo_criteria(request.goal)
                    ),
                ),
            ),
            final_task_id=final_task_id,
        )
        return CompilerDecision(
            proposal=proposal,
            mode=PlanningMode.DYNAMIC,
            reason=reason,
            rationale=(
                "The provider proposal could not satisfy the mandatory independent "
                "review boundary; use the bounded deterministic review graph."
            ),
            usage=usage,
            provider_request_id=provider_request_id,
            exposed_workflow_prior_ids=tuple(
                prior.pattern_id for prior in request.workflow_priors
            ),
        )

    # Refuse the requested effect instead of silently executing it without the
    # independent review the Work Order requires.
    reporting_capability = _reporting_capability(request)
    proposal = PlanProposal(
        proposal_id=f"review-constraint-{request.request_id}",
        goal=request.goal,
        tasks=(
            JobTask(
                task_id="report_review_constraint",
                objective=(
                    "Report that the required independent review cannot be admitted "
                    "within the current task, role, or model-call limits."
                ),
                depends_on=(),
                required_capabilities=(reporting_capability,),
                acceptance_criteria=(
                    "Do not perform the requested action or workspace mutation.",
                    "Return the independent-review capacity limitation explicitly.",
                ),
            ),
        ),
        final_task_id="report_review_constraint",
    )
    return CompilerDecision(
        proposal=proposal,
        mode=PlanningMode.SOLO_FALLBACK,
        reason=reason,
        rationale=(
            "Mandatory independent review does not fit the remaining bounded "
            "capacity, so the requested effect is refused explicitly."
        ),
        usage=usage,
        provider_request_id=provider_request_id,
        exposed_workflow_prior_ids=tuple(
            prior.pattern_id for prior in request.workflow_priors
        ),
    )


def _budget_exhausted_decision(
    request: CompilerRequest,
    *,
    usage: Usage,
    provider_request_id: str | None,
) -> CompilerDecision:
    proposal = PlanProposal(
        proposal_id=f"budget-exhausted-{request.request_id}",
        goal=request.goal,
        tasks=(
            JobTask(
                task_id="report_budget_exhausted",
                objective="Do not execute; the compiler consumed the model-call budget.",
                depends_on=(),
                required_capabilities=(_reporting_capability(request),),
                acceptance_criteria=(
                    "The Firm Kernel must terminalize before dispatch because no employee model call remains.",
                ),
            ),
        ),
        final_task_id="report_budget_exhausted",
    )
    return CompilerDecision(
        proposal=proposal,
        mode=PlanningMode.SOLO_FALLBACK,
        reason=CompilerReason.COMPILER_BUDGET_EXHAUSTED,
        rationale=(
            "The compiler used the complete model-call budget; no fallback employee "
            "call is available and the Job must terminalize as budget exhausted."
        ),
        usage=usage,
        provider_request_id=provider_request_id,
        exposed_workflow_prior_ids=tuple(
            prior.pattern_id for prior in request.workflow_priors
        ),
    )


def direct_conversation_decision(request: CompilerRequest) -> CompilerDecision:
    """Build a no-compiler, one-employee plan for ordinary conversation."""

    proposal = PlanProposal(
        proposal_id=f"direct-{request.request_id}",
        goal=request.goal,
        tasks=(
            JobTask(
                task_id="respond_to_user",
                objective=request.goal,
                depends_on=(),
                required_capabilities=("conversation",),
                acceptance_criteria=(
                    "Answer the user's actual message directly and completely.",
                    "Do not inspect the workspace or invent repository evidence.",
                ),
            ),
        ),
        final_task_id="respond_to_user",
    )
    return CompilerDecision(
        proposal=proposal,
        mode=PlanningMode.DIRECT,
        reason=CompilerReason.DIRECT_USER_MESSAGE,
        rationale="Ordinary conversation needs one direct employee turn and no workflow compiler.",
    )


def solo_first_decision(request: CompilerRequest) -> CompilerDecision:
    """Build the provider-free first attempt for a company goal."""

    if request.requires_independent_review:
        return _required_review_fallback_decision(
            request,
            reason=CompilerReason.COMPILER_REQUIRED_REVIEW_MISSING,
            usage=Usage(),
            provider_request_id=None,
        )

    coding = request.execution_profile.requires_implementation
    host_action = request.execution_profile == CompilerExecutionProfile.HOST_ACTION
    task_id = "implement_change" if coding else "perform_action" if host_action else "analyze_goal"
    capability = (
        "implementation"
        if coding
        else _host_action_capability(request)
        if host_action
        else _solo_first_capability(request)
    )
    proposal = PlanProposal(
        proposal_id=f"solo-first-{request.request_id}",
        goal=request.goal,
        tasks=(
            JobTask(
                task_id=task_id,
                objective=request.goal,
                depends_on=(),
                required_capabilities=(capability,),
                acceptance_criteria=(
                    _coding_solo_criteria(request.execution_profile)
                    if coding
                    else _host_action_solo_criteria()
                    if host_action
                    else _read_only_solo_criteria(request.goal)
                ),
            ),
        ),
        final_task_id=task_id,
    )
    return CompilerDecision(
        proposal=proposal,
        mode=PlanningMode.SOLO,
        reason=CompilerReason.SOLO_FIRST_ATTEMPT,
        rationale=(
            "Start with one bounded employee. Organization expansion is admitted only from "
            "typed runtime evidence."
        ),
    )
