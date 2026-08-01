"""Graphless Company-owned execution for the DIRECT operating mode.

DIRECT is not a chatbot outside the Company.  It freezes the same Company,
ROSTER, employee skill, memory, action-policy, session, and cost-budget facts
as a managed Job, then assigns the request to exactly one active persistent
employee.  It deliberately does not construct a JobGraph, open an ACTIVE JOB
ledger, staff a temporary role, or invoke a replanner.

``CompanyRunRequest`` and ``JobResult`` remain the product ABI while callers
migrate to the Company Front Door.  The one-task ``PlanProposal`` and
``TaskEnvelope.job_graph_version == 1`` below are therefore compatibility
envelopes for the existing Employee Execution Port, not a managed graph.  A
DIRECT ``JobResult`` makes this explicit with ``final_graph_version == 0``, no
``final_tasks``, no ``final_task_id``, and zero graph/mutation metrics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobMetrics,
    JobResult,
    JobStatus,
    JobTask,
    TaskAssignmentEvent,
)
from dynamic_firm.runtime.company_budget import (
    CompanyBudgetAuthorityPort,
    CompanyBudgetLease,
)
from dynamic_firm.runtime.knowledge_retrieval import BoundedKnowledgeRetriever
from dynamic_firm.runtime.employee_capability import build_employee_capability_profile
from dynamic_firm.runtime.liveness import enforce_employee_completion_liveness
from dynamic_firm.runtime.manager_tool_policy import is_manager_tool
from dynamic_firm.runtime.models import (
    ContextBundle,
    EmployeeRunRequest,
    EmployeeRunResult,
    EmployeeSessionRetention,
    EmployeeSnapshot,
    Failure,
    FailureCategory,
    RunHandle,
    RunLimits,
    RunStatus,
    TaskEnvelope,
    TaskEvidencePack,
    Usage,
    VersionedContent,
)
from dynamic_firm.runtime.ports import EmployeeExecutionPort


_EMPLOYEE_PORT_GRAPH_VERSION = 1


class DirectCompanyExecutor:
    """Run one Company-owned assignment without a managed Job Graph."""

    _CANCEL_GRACE_SECONDS = 0.25

    def __init__(
        self,
        *,
        employee_execution: EmployeeExecutionPort,
        assignment_sink: Callable[[TaskAssignmentEvent], None] | None = None,
        company_budget_authority: CompanyBudgetAuthorityPort | None = None,
    ) -> None:
        self.employee_execution = employee_execution
        self.assignment_sink = assignment_sink
        self.company_budget_authority = company_budget_authority

    async def run(self, request: CompanyRunRequest) -> JobResult:
        """Execute one persistent employee run and return the JobResult ABI."""

        task = self._validate_request(request)
        if request.planning_reason == "JOB_WALL_TIME_EXHAUSTED_BEFORE_DISPATCH":
            return self._result(
                request=request,
                employee_result=None,
                status=JobStatus.BUDGET_EXHAUSTED,
                failure_reason=(
                    "The Company Job wall-time budget expired before Employee dispatch."
                ),
            )
        employee, selection_reason = self._select_employee(
            task,
            request.roster,
            manager_employee_id=request.manager_employee_id,
        )
        lease: CompanyBudgetLease | None = None
        if self.company_budget_authority is not None:
            admission = self.company_budget_authority.admit_job(request)
            if not admission.allowed:
                detail = admission.reason or "Company cost budget denied this direct run."
                if admission.incident is not None:
                    detail += " Explicit operator budget resolution is required."
                return self._result(
                    request=request,
                    employee_result=None,
                    status=JobStatus.BUDGET_EXHAUSTED,
                    failure_reason=detail,
                )
            lease = admission.lease

        try:
            employee_request = self._employee_request(request, task, employee)
            capability_profile = employee_request.employee.capability_profile
            assert capability_profile is not None
            alternatives = tuple(
                sorted(
                    item.employee_id
                    for item in request.roster
                    if item.active
                    and not item.temporary
                    and item.employee_id != employee.employee_id
                    and set(task.required_capabilities).issubset(item.capabilities)
                )
            )
            self._emit_assignment(
                TaskAssignmentEvent(
                    job_id=request.job_id,
                    task_id=task.task_id,
                    # Zero is reserved for the public graphless result.  The
                    # EmployeeExecutionPort still requires a positive ABI value.
                    graph_version=0,
                    employee_id=employee.employee_id,
                    employee_role=employee.role,
                    employee_temporary=False,
                    required_capabilities=task.required_capabilities,
                    depends_on=(),
                    attempt=1,
                    final_task=True,
                    selection_reason=selection_reason,
                    active_task_count=1,
                    capability_profile_digest=capability_profile.profile_digest,
                    capability_material_digest=capability_profile.material_digest,
                    task_relevance=tuple(
                        sorted(set(task.required_capabilities) & set(employee.capabilities))
                    ),
                    chosen_over_employee_ids=alternatives,
                )
            )

            employee_result = await self._execute(employee_request)
            employee_result = self._validate_result_boundary(
                request,
                task,
                employee,
                employee_result,
            )
            employee_result, _ = enforce_employee_completion_liveness(
                objective=task.objective,
                result=employee_result,
            )
            if self._exceeds_limits(employee_result.usage, employee_request.limits):
                employee_result = replace(
                    employee_result,
                    status=RunStatus.BUDGET_EXHAUSTED,
                    summary="Employee execution exceeded its reserved direct-run budget.",
                    acceptance_evidence=(),
                    unresolved_issues=(
                        "Employee execution exceeded its reserved direct-run budget.",
                    ),
                    failure=Failure(
                        code="DIRECT_RUN_BUDGET_EXCEEDED",
                        category=FailureCategory.POLICY,
                        message_safe="Employee execution exceeded its reserved direct-run budget.",
                    ),
                )

            result = self._result(
                request=request,
                employee_result=employee_result,
                status=self._job_status(employee_result.status),
                failure_reason=self._failure_reason(employee_result),
            )
            if lease is not None:
                # Use the same durable Company budget settlement contract as a
                # managed Job.  No ACTIVE JOB ledger is involved.
                assert self.company_budget_authority is not None
                forfeit_reason = self._budget_forfeit_reason(employee_result)
                if forfeit_reason is None:
                    self.company_budget_authority.settle_job(lease, result)
                else:
                    self.company_budget_authority.forfeit_job(
                        lease,
                        reason=forfeit_reason,
                    )
            return result
        except BaseException as error:
            if lease is not None:
                # No trustworthy JobResult crossed the Company boundary. Charge
                # the entire reservation rather than leaving a ghost ACTIVE
                # lease or releasing uncertain spend back into the budget.
                assert self.company_budget_authority is not None
                reason = (
                    "DIRECT_RUN_CANCELLED"
                    if isinstance(error, asyncio.CancelledError)
                    else "DIRECT_RUN_ABORTED"
                )
                try:
                    self.company_budget_authority.forfeit_job(lease, reason=reason)
                except BaseException as finalization_error:
                    error.add_note(
                        "Company budget lease forfeiture failed; the durable "
                        "reservation remains fail-closed. "
                        f"{type(finalization_error).__name__}: {finalization_error}"
                    )
            raise

    @staticmethod
    def _validate_request(request: CompanyRunRequest) -> JobTask:
        required = {
            "request_id": request.request_id,
            "job_id": request.job_id,
            "goal": request.goal,
            "proposal_id": request.plan_proposal.proposal_id,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"Missing direct company request fields: {', '.join(missing)}")
        if request.company_work_mode != "DIRECT":
            raise ValueError("DirectCompanyExecutor accepts only DIRECT company work")
        if request.coordination_policy != "DIRECT":
            raise ValueError("DIRECT company work requires the DIRECT coordination policy")
        if len(request.plan_proposal.tasks) != 1:
            raise ValueError("DIRECT compatibility proposal must contain exactly one assignment")
        task = request.plan_proposal.tasks[0]
        if task.task_id != request.plan_proposal.final_task_id:
            raise ValueError("DIRECT compatibility assignment must be the proposal final task")
        if task.depends_on:
            raise ValueError("DIRECT assignment cannot declare graph dependencies")
        if task.execution_replica is not None:
            raise ValueError("DIRECT assignment cannot declare an execution replica")
        active_persistent = tuple(
            employee
            for employee in request.roster
            if employee.active and not employee.temporary
        )
        if not active_persistent:
            raise ValueError("DIRECT execution requires an active persistent employee")
        limits = request.job_limits
        if (
            limits.max_total_model_calls < 1
            or limits.max_total_tool_calls < 1
            or limits.max_wall_time_ms < 1
            or limits.max_total_cost_usd < 0
        ):
            raise ValueError("DIRECT Job limits must provide positive call/time bounds")
        runtime = request.runtime_limits
        if (
            runtime.max_model_calls < 1
            or runtime.max_tool_calls < 1
            or runtime.max_wall_time_ms < 1
            or runtime.max_cost_usd < 0
        ):
            raise ValueError("DIRECT runtime limits must provide positive call/time bounds")
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
                raise ValueError(
                    "Knowledge execution origin does not match the frozen Evidence Pack"
                )
            epistemic_identity = (
                origin.decision_context_id,
                origin.decision_context_digest,
                origin.oracle_contract_id,
                origin.oracle_contract_digest,
            )
            if any(epistemic_identity) and not all(epistemic_identity):
                raise ValueError(
                    "Knowledge execution origin has incomplete epistemic control identity"
                )
        elif origin is not None:
            raise ValueError("Knowledge execution origin requires a frozen Evidence Pack")
        return task

    @staticmethod
    def _select_employee(
        task: JobTask,
        roster: tuple[EmployeeRecord, ...],
        *,
        manager_employee_id: str = "",
    ) -> tuple[EmployeeRecord, str]:
        persistent = tuple(
            employee for employee in roster if employee.active and not employee.temporary
        )
        if manager_employee_id:
            manager = next(
                (
                    employee
                    for employee in persistent
                    if employee.employee_id == manager_employee_id
                ),
                None,
            )
            if manager is None:
                raise ValueError("DIRECT Manager is not active in the frozen ROSTER")
            if "company_management" not in manager.capabilities:
                raise ValueError("DIRECT Manager lacks the company_management capability")
            return manager, "PERSISTENT_MANAGER_DIRECT"
        required = set(task.required_capabilities)
        capable = tuple(
            employee
            for employee in persistent
            if required.issubset(employee.capabilities)
        )
        if capable:
            return (
                min(capable, key=lambda item: (len(item.capabilities), item.employee_id)),
                "DIRECT_PERSISTENT_CAPABILITY_MATCH",
            )

        # DIRECT cannot invent a specialist.  The best existing employee gets
        # the bounded assignment and can return a typed capability gap through
        # the ordinary Employee completion contract.
        return (
            min(
                persistent,
                key=lambda item: (
                    -len(required.intersection(item.capabilities)),
                    0 if "general_reasoning" in item.capabilities else 1,
                    len(item.capabilities),
                    item.employee_id,
                ),
            ),
            "DIRECT_PERSISTENT_BEST_FIT",
        )

    @staticmethod
    def _employee_request(
        company: CompanyRunRequest,
        task: JobTask,
        employee: EmployeeRecord,
    ) -> EmployeeRunRequest:
        base = company.context_snapshot
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
        selected_skills = BoundedKnowledgeRetriever().select(
            company.employee_skill_snapshots.get(employee.employee_id, ()),
            query=task.objective,
            limit=3,
            max_bytes=12_000,
            allowed_prefixes=(
                f"employee-skill:{employee.employee_id}:",
                "external-skill:",
            ),
            fallback_count=1,
        ).items
        task_evidence = DirectCompanyExecutor._select_task_evidence(base, task.objective)
        context = ContextBundle(
            company_policy_excerpt=base.company_policy_excerpt,
            task_dependencies=base.task_dependencies,
            selected_facts=base.selected_facts,
            selected_memory=selected_memory,
            ephemeral_instructions=base.ephemeral_instructions,
            task_evidence=task_evidence,
            workspace_id=base.workspace_id,
        )
        runtime = company.runtime_limits
        job = company.job_limits
        limits = RunLimits(
            max_wall_time_ms=min(runtime.max_wall_time_ms, job.max_wall_time_ms),
            max_model_calls=min(runtime.max_model_calls, job.max_total_model_calls),
            max_tool_calls=min(runtime.max_tool_calls, job.max_total_tool_calls),
            max_input_tokens=runtime.max_input_tokens,
            max_output_tokens=runtime.max_output_tokens,
            max_cost_usd=min(runtime.max_cost_usd, job.max_total_cost_usd),
            max_consecutive_errors=runtime.max_consecutive_errors,
            max_result_bytes=runtime.max_result_bytes,
            max_tool_output_bytes=runtime.max_tool_output_bytes,
            max_context_messages=runtime.max_context_messages,
            max_context_chars=runtime.max_context_chars,
            context_keep_recent_messages=runtime.context_keep_recent_messages,
            cost_efficiency_mode=runtime.cost_efficiency_mode,
        )
        action_policy = DirectCompanyExecutor._action_policy_for_employee(
            company.action_policy,
            employee_id=employee.employee_id,
            manager_employee_id=company.manager_employee_id,
        )
        session_retention = (
            EmployeeSessionRetention.RUN_ONLY
            if task_evidence is not None
            else EmployeeSessionRetention.PERSIST
        )
        memory_namespace = f"employee:{employee.employee_id}"
        validator_ids = ["structured-completion-v1"]
        if task.acceptance_criteria:
            validator_ids.append("task-acceptance-v1")
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
        )
        return EmployeeRunRequest(
            request_id=f"{company.request_id}:direct:attempt-1",
            employee=EmployeeSnapshot(
                employee_id=employee.employee_id,
                role=employee.role,
                capabilities=employee.capabilities,
                temporary=False,
                skills=selected_skills,
                model_profile=employee.model_profile,
                memory_namespace=memory_namespace,
                selected_memory_refs=tuple(item.content_id for item in selected_memory),
                capability_profile=capability_profile,
            ),
            task=TaskEnvelope(
                job_id=company.job_id,
                job_graph_version=_EMPLOYEE_PORT_GRAPH_VERSION,
                task_id=task.task_id,
                attempt=1,
                objective=task.objective,
                required_capabilities=task.required_capabilities,
                acceptance_criteria=task.acceptance_criteria,
                risk_level=task.risk_level,
            ),
            context=context,
            limits=limits,
            action_policy=action_policy,
            session_key=(
                company.manager_session_key
                if employee.employee_id == company.manager_employee_id
                and company.manager_session_key
                else company.session_key
            ),
            session_retention=session_retention,
        )

    @staticmethod
    def _action_policy_for_employee(
        policy: ActionPolicy,
        *,
        employee_id: str,
        manager_employee_id: str,
    ) -> ActionPolicy:
        """Project the frozen Company authority into the selected employee.

        The Manager never creates or changes this policy. It may use the same
        already user-granted, approval-gated action tools as a direct employee,
        plus the authority-free Manager read catalog. Ordinary employees never
        receive that catalog merely because a Manager exists in the ROSTER.
        """
        if manager_employee_id and employee_id == manager_employee_id:
            return policy
        return replace(
            policy,
            tool_grants=tuple(
                grant
                for grant in policy.tool_grants
                if not is_manager_tool(grant.tool_name)
            ),
        )

    @staticmethod
    def _select_task_evidence(
        base: ContextBundle,
        objective: str,
    ) -> TaskEvidencePack | None:
        if base.task_evidence is None:
            return None
        evidence_by_id = {
            f"user-knowledge-evidence:{item.citation_id}": item
            for item in base.task_evidence.items
        }
        candidates = tuple(
            VersionedContent(
                content_id=content_id,
                revision=item.source_revision,
                content=item.content,
                content_hash=item.content_hash,
            )
            for content_id, item in evidence_by_id.items()
        )
        selected = BoundedKnowledgeRetriever().select(
            candidates,
            query=objective,
            limit=6,
            max_bytes=16_000,
            allowed_prefixes=("user-knowledge-evidence:",),
            fallback_count=0,
        ).items
        provisional = replace(
            base.task_evidence,
            items=tuple(evidence_by_id[item.content_id] for item in selected),
            delivery_digest="",
        )
        projected = replace(
            provisional,
            delivery_digest=provisional.computed_delivery_digest(),
        )
        projected.verify()
        return projected

    async def _execute(self, request: EmployeeRunRequest) -> EmployeeRunResult:
        started_at = datetime.now(UTC)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.limits.max_wall_time_ms / 1000
        handle: RunHandle | None = None
        start_task: asyncio.Task[RunHandle] | None = None
        collector: asyncio.Task[EmployeeRunResult] | None = None
        try:
            start_task = asyncio.create_task(self.employee_execution.start(request))
            done, _ = await asyncio.wait(
                {start_task},
                timeout=max(0.0, deadline - loop.time()),
            )
            if start_task not in done:
                start_task.cancel()
                start_task.add_done_callback(self._consume_task_terminal)
                return self._boundary_failure(
                    request,
                    started_at=started_at,
                    status=RunStatus.BUDGET_EXHAUSTED,
                    code="DIRECT_RUN_WALL_TIME_EXHAUSTED",
                    category=FailureCategory.TIMEOUT,
                    message="Direct employee execution exhausted its wall-time budget.",
                )
            handle = start_task.result()
            collector = asyncio.create_task(self.employee_execution.collect(handle))
            done, _ = await asyncio.wait(
                {collector},
                timeout=max(0.0, deadline - loop.time()),
            )
            if collector in done:
                return collector.result()
            await self._cancel_handle_bounded(
                handle,
                "Direct company wall-time exhausted",
            )
            collector.cancel()
            collector.add_done_callback(self._consume_task_terminal)
            return self._boundary_failure(
                request,
                started_at=started_at,
                status=RunStatus.BUDGET_EXHAUSTED,
                code="DIRECT_RUN_WALL_TIME_EXHAUSTED",
                category=FailureCategory.TIMEOUT,
                message="Direct employee execution exhausted its wall-time budget.",
            )
        except asyncio.CancelledError:
            if handle is not None:
                await self._cancel_handle_bounded(handle, "Direct company run cancelled")
            for pending in (start_task, collector):
                if pending is not None and not pending.done():
                    pending.cancel()
                    pending.add_done_callback(self._consume_task_terminal)
            raise
        except Exception as exc:
            return self._boundary_failure(
                request,
                started_at=started_at,
                status=RunStatus.FAILED,
                code="DIRECT_EMPLOYEE_EXECUTION_FAILED",
                category=FailureCategory.INTERNAL,
                message=f"Employee runtime failed with {type(exc).__name__}.",
            )

    async def _cancel_handle_bounded(self, handle: RunHandle, reason: str) -> None:
        cancellation = asyncio.create_task(
            self.employee_execution.cancel(handle, reason)
        )
        done, pending = await asyncio.wait(
            {cancellation},
            timeout=self._CANCEL_GRACE_SECONDS,
        )
        for completed in done:
            self._consume_task_terminal(completed)
        for unfinished in pending:
            unfinished.cancel()
            unfinished.add_done_callback(self._consume_task_terminal)

    @staticmethod
    def _consume_task_terminal(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            return

    @staticmethod
    def _budget_forfeit_reason(result: EmployeeRunResult) -> str | None:
        if result.status == RunStatus.CANCELLED:
            return "DIRECT_RUN_USAGE_UNCERTAIN"
        failure = result.failure
        if failure is None:
            return None
        if failure.category in {FailureCategory.TIMEOUT, FailureCategory.CANCEL}:
            return "DIRECT_RUN_USAGE_UNCERTAIN"
        if failure.code in {
            "DIRECT_EMPLOYEE_EXECUTION_FAILED",
            "EMPLOYEE_EXECUTION_BOUNDARY_FAILED",
            "MODEL_PROVIDER_ERROR",
        }:
            return "DIRECT_RUN_USAGE_UNCERTAIN"
        return None

    @staticmethod
    def _boundary_failure(
        request: EmployeeRunRequest,
        *,
        started_at: datetime,
        status: RunStatus,
        code: str,
        category: FailureCategory,
        message: str,
    ) -> EmployeeRunResult:
        return EmployeeRunResult(
            run_id=f"direct-boundary:{request.request_id}",
            request_id=request.request_id,
            job_id=request.task.job_id,
            task_id=request.task.task_id,
            employee_id=request.employee.employee_id,
            status=status,
            summary=message,
            output_artifact_refs=(),
            acceptance_evidence=(),
            unresolved_issues=(message,),
            observations=(),
            suggested_followups=(),
            signals=(),
            partial_result=False,
            usage=Usage(),
            last_event_seq=0,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            failure=Failure(
                code=code,
                category=category,
                message_safe=message,
            ),
        )

    @staticmethod
    def _validate_result_boundary(
        company: CompanyRunRequest,
        task: JobTask,
        employee: EmployeeRecord,
        result: EmployeeRunResult,
    ) -> EmployeeRunResult:
        if (
            result.job_id == company.job_id
            and result.task_id == task.task_id
            and result.employee_id == employee.employee_id
        ):
            return result
        return replace(
            result,
            job_id=company.job_id,
            task_id=task.task_id,
            employee_id=employee.employee_id,
            status=RunStatus.FAILED,
            summary="Employee runtime returned a mismatched direct result envelope.",
            acceptance_evidence=(),
            failure=Failure(
                code="DIRECT_EMPLOYEE_RESULT_IDENTITY_MISMATCH",
                category=FailureCategory.INTERNAL,
                message_safe="Employee runtime result identity did not match the direct assignment.",
            ),
        )

    @staticmethod
    def _exceeds_limits(usage: Usage, limits: RunLimits) -> bool:
        return (
            usage.model_calls > limits.max_model_calls
            or usage.tool_calls > limits.max_tool_calls
            or usage.cost_usd > limits.max_cost_usd + 1e-12
        )

    @staticmethod
    def _job_status(status: RunStatus) -> JobStatus:
        if status == RunStatus.SUCCEEDED:
            return JobStatus.SUCCEEDED
        if status == RunStatus.BUDGET_EXHAUSTED:
            return JobStatus.BUDGET_EXHAUSTED
        return JobStatus.FAILED

    @staticmethod
    def _failure_reason(result: EmployeeRunResult) -> str:
        if result.status == RunStatus.SUCCEEDED:
            return ""
        if result.failure is not None:
            return result.failure.message_safe
        if result.unresolved_issues:
            return result.unresolved_issues[0]
        return f"Direct employee execution ended with status {result.status.value}."

    @staticmethod
    def _result(
        *,
        request: CompanyRunRequest,
        employee_result: EmployeeRunResult | None,
        status: JobStatus,
        failure_reason: str,
    ) -> JobResult:
        usage = employee_result.usage if employee_result is not None else Usage()
        return JobResult(
            job_id=request.job_id,
            request_id=request.request_id,
            status=status,
            summary=(
                employee_result.summary
                if employee_result is not None
                else failure_reason or f"Direct run ended with status {status.value}."
            ),
            acceptance_evidence=(
                employee_result.acceptance_evidence
                if employee_result is not None
                else ()
            ),
            unresolved_issues=(
                employee_result.unresolved_issues
                if employee_result is not None
                else ((failure_reason,) if failure_reason else ())
            ),
            task_results=((employee_result,) if employee_result is not None else ()),
            final_graph_version=0,
            final_tasks=(),
            metrics=JobMetrics(
                unique_employee_count=1 if employee_result is not None else 0,
                temporary_role_count=0,
                maximum_parallelism=1 if employee_result is not None else 0,
                graph_patch_count=0,
                usage=usage,
                task_mutation_count=0,
                organization_admission_count=0,
            ),
            final_task_id="",
            failure_reason=failure_reason,
            planning_mode="DIRECT",
            planning_reason=request.operating_reason,
            manager_employee_id=request.manager_employee_id,
            work_order_id=request.work_order_id,
            work_order_digest=request.work_order_digest,
            work_order_authority_digest=request.work_order_authority_digest,
            firm_admission_digest=request.firm_admission_digest,
            initial_company_work_mode="DIRECT",
            company_work_mode="DIRECT",
            coordination_policy="DIRECT",
            requested_effect=request.requested_effect,
            operating_reason=request.operating_reason,
            attempt_records=(),
            mutation_events=(),
            graph_patch_events=(),
        )

    def _emit_assignment(self, event: TaskAssignmentEvent) -> None:
        if self.assignment_sink is None:
            return
        try:
            self.assignment_sink(event)
        except Exception:
            # UI/product projection never changes Company execution semantics.
            return
