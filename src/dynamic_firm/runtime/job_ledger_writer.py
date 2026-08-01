from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Mapping

from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    GraphPatchEvent,
    GraphPatchProposalEvent,
    GraphPatchProposalStatus,
    JobGraph,
    JobMutationEvent,
    JobResult,
    TaskStatus,
    TaskAttemptRecord,
)
from dynamic_firm.kernel.graph import apply_patch, graph_from_proposal, replace_task
from dynamic_firm.kernel.mutation import (
    graph_patch_from_primitive,
    graph_patch_proposal_event_from_primitive,
    graph_structure_digest,
)
from dynamic_firm.runtime.models import (
    EmployeeRunResult,
    RunSignal,
    RunStatus,
    SignalCode,
    to_primitive,
)

from .company_coordination import (
    CompanyCoordinationError,
    RemoteCompanyCoordinationClient,
)
from .job_ledger_primitives import (
    ActiveJobApprovedGraphContinuation,
    ActiveJobAuditStatus,
    ActiveJobPartialContinuation,
    ActiveJobSameJobContinuation,
    SNAPSHOT_SCHEMA,
    TERMINAL_SCHEMA,
    _digest,
    _payload,
    _resolved_company_work_mode,
    _terminal_graph_proposal_payload,
    remote_continuation_id,
)
from .store import RunStore

class SQLiteActiveJobLedger:
    """First-party ACTIVE JOB adapter over the existing RunStore transaction owner."""

    def __init__(
        self,
        store: RunStore,
        *,
        evolution_artifact_pins: tuple[Mapping[str, Any], ...] = (),
        evolution_artifact_effects: tuple[Mapping[str, Any], ...] = (),
        company_coordination: RemoteCompanyCoordinationClient | None = None,
    ) -> None:
        self.store = store
        # This is an audit projection only. Artifact manifests and employee
        # memory remain in their respective local stores; the ACTIVE JOB keeps
        # only immutable id/version/digest references for reproduction.
        self.evolution_artifact_pins = tuple(
            {
                "kind": str(item["kind"]),
                "artifact_id": str(item["artifact_id"]),
                "version": str(item["version"]),
                "manifest_digest": str(item["manifest_digest"]),
                "scope_key": str(item["scope_key"]),
            }
            for item in evolution_artifact_pins
        )
        # The runtime adapter's bounded decision is as important as the pin: it
        # distinguishes a Skill that was projected from a declarative Artifact
        # or a catalog that was excluded.  Never retain manifest content,
        # procedures, paths, exception text, or remote data in ACTIVE JOB.
        self.evolution_artifact_effects = tuple(
            {
                key: str(item[key])[:160]
                for key in (
                    "kind",
                    "artifact_id",
                    "version",
                    "scope_key",
                    "decision",
                )
                if key in item
            }
            for item in evolution_artifact_effects[:64]
        )
        self.company_coordination = company_coordination

    @staticmethod
    def _remote_continuation_id(
        job_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
    ) -> str:
        return remote_continuation_id(job_id, request_snapshot_hash, graph_digest)

    @staticmethod
    def _remote_graph_proposal_continuation_id(
        job_id: str,
        request_snapshot_hash: str,
        proposal_id: str,
    ) -> str:
        value = hashlib.sha256(
            f"noruct.graph-proposal-continuation.v1|{job_id}|{request_snapshot_hash}|{proposal_id}".encode("utf-8")
        ).hexdigest()
        return f"continuation-{value}"

    def _graph_proposal_remote_identity(
        self,
        job_id: str,
        event: GraphPatchProposalEvent,
    ) -> dict[str, str]:
        completed = tuple(
            sorted(
                (
                    str(item["task_id"]),
                    str(item["attempt_id"]),
                    str(item["result_digest"]),
                )
                for item in self.store.list_job_dependency_result_receipts(job_id)
            )
        )
        request_snapshot_hash = self.store.job_frozen_snapshot_hash(job_id)
        return {
            "continuation_id": self._remote_graph_proposal_continuation_id(
                job_id, request_snapshot_hash, event.proposal_id
            ),
            "request_snapshot_hash": request_snapshot_hash,
            "before_graph_digest": event.before_graph_digest,
            "after_graph_digest": event.after_graph_digest,
            "mutation_lease_digest": _digest(
                {
                    "model_calls": event.proposed_lease.model_calls,
                    "tool_calls": event.proposed_lease.tool_calls,
                    "cost_usd": event.proposed_lease.cost_usd,
                }
            ),
            "completed_results_digest": _digest(completed),
        }

    def start_job(
        self,
        request: CompanyRunRequest,
        graph: JobGraph,
        frozen_snapshot_hash: str,
    ) -> None:
        # Resume state is deliberately separate from the append-only audit.
        # It contains only immutable identifiers/digests; source bodies stay
        # in their local authorities and must be revalidated on recovery.
        references = {
            "authority_digest": request.work_order_authority_digest,
            "firm_admission_digest": request.firm_admission_digest,
            "workspace_context_fingerprint": request.workflow_context_fingerprint,
        }
        if request.execution_origin is not None:
            references["knowledge_pack_digest"] = request.execution_origin.pack_digest
        if len(request.work_order_digest) == 64:
            self.store.save_local_resume_envelope(
                job_id=request.job_id,
                work_order_digest=request.work_order_digest,
                graph_digest=graph_structure_digest(graph),
                references={key: value for key, value in references.items() if value},
            )
        payload = {
            "schema_version": SNAPSHOT_SCHEMA,
            "job_id": request.job_id,
            "request_id": request.request_id,
            "proposal_id": request.plan_proposal.proposal_id,
            "graph_version": graph.version,
            "final_task_id": graph.final_task_id,
            "company_revision": request.company_revision,
            "roster_revision": request.roster_revision,
            "playbook_revision": request.playbook_revision,
            "operating_decision": {
                "initial_company_work_mode": request.company_work_mode,
                "company_work_mode": _resolved_company_work_mode(
                    request.company_work_mode,
                    len(graph.tasks),
                ),
                "coordination_policy": request.coordination_policy,
                "requested_effect": request.requested_effect,
                "operating_reason": request.operating_reason,
            },
            "planning": {
                "planning_mode": request.planning_mode,
                "planning_reason": request.planning_reason,
                "compiler_usage": request.compiler_usage,
                "compiler_provider_request_id": (
                    request.compiler_provider_request_id
                ),
            },
            "work_order": {
                "work_order_id": request.work_order_id,
                "work_order_digest": request.work_order_digest,
                "work_order_authority_digest": (
                    request.work_order_authority_digest
                ),
                "firm_admission_digest": request.firm_admission_digest,
            },
            "graph_blueprint": {
                "blueprint_id": request.graph_blueprint_id,
                "blueprint_version": request.graph_blueprint_version,
                "blueprint_digest": request.graph_blueprint_digest,
                "mutation_policy": request.graph_mutation_policy,
                "constraints_digest": request.graph_constraints_digest,
                "constraints": {
                    "pinned_employee_ids": request.graph_pinned_employee_ids,
                    "excluded_employee_ids": request.graph_excluded_employee_ids,
                    "require_independent_review": request.graph_require_independent_review,
                    "max_concurrency": request.graph_max_concurrency,
                    "max_cost_usd": request.graph_max_cost_usd,
                    "max_wall_time_ms": request.graph_max_wall_time_ms,
                },
                "initial_graph_digest": graph_structure_digest(graph),
            },
            "workspace_identity": {
                "status": request.workspace_identity_status,
                "revision": request.workspace_identity_revision,
                "context_fingerprint": request.workflow_context_fingerprint,
                "failure_code": request.workspace_identity_failure_code,
            },
            "frozen_snapshot_hash": frozen_snapshot_hash,
            "knowledge_binding": (
                to_primitive(request.execution_origin)
                if request.execution_origin is not None
                else None
            ),
            "job_limits": {
                "max_tasks": request.job_limits.max_tasks,
                "max_concurrency": request.job_limits.max_concurrency,
                "max_graph_patches": request.job_limits.max_graph_patches,
                "max_task_mutations": request.job_limits.max_task_mutations,
                "max_temporary_roles": request.job_limits.max_temporary_roles,
                "max_total_model_calls": request.job_limits.max_total_model_calls,
                "max_total_tool_calls": request.job_limits.max_total_tool_calls,
                "max_total_cost_usd": request.job_limits.max_total_cost_usd,
                "max_wall_time_ms": request.job_limits.max_wall_time_ms,
            },
            "tasks": tuple(
                {
                    "task_id": task.task_id,
                    "depends_on": task.depends_on,
                    "required_capabilities": task.required_capabilities,
                    "risk_level": task.risk_level,
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
                for task in graph.tasks
            ),
            "roster": tuple(
                {
                    "employee_id": employee.employee_id,
                    "capabilities": employee.capabilities,
                    "active": employee.active,
                    "temporary": employee.temporary,
                }
                for employee in request.roster
            ),
            "manager": (
                {
                    "employee_id": request.manager_employee_id,
                    "assignment_digest": request.manager_assignment_digest,
                    "delegation_digest": request.manager_delegation_digest,
                    "employee": (
                        to_primitive(request.manager_employee)
                        if request.manager_employee is not None
                        else None
                    ),
                }
                if request.manager_employee_id
                else None
            ),
            "evolution_artifact_pins": self.evolution_artifact_pins,
            "evolution_artifact_effects": self.evolution_artifact_effects,
        }
        self.store.create_job_snapshot(payload)
        self.store.admit_job_lifecycle(
            job_id=request.job_id,
            request_id=request.request_id,
        )

    def claim_same_job_continuation(
        self,
        request: CompanyRunRequest,
        graph: JobGraph,
        frozen_snapshot_hash: str,
    ) -> None:
        """Consume a pre-authorized receipt at the explicit Kernel boundary."""

        self.store.claim_same_job_continuation(
            job_id=request.job_id,
            request_snapshot_hash=frozen_snapshot_hash,
            graph_digest=graph_structure_digest(graph),
        )

    def claim_partial_read_only_continuation(
        self,
        request: CompanyRunRequest,
        graph: JobGraph,
        frozen_snapshot_hash: str,
    ) -> Mapping[str, EmployeeRunResult]:
        """Claim a receipt and return only its proven completed task results."""

        if self.company_coordination is not None:
            receipt_rows = self.store.list_job_dependency_result_receipts(request.job_id)
            digest_rows = tuple(sorted(
                (str(item["task_id"]), str(item["attempt_id"]), str(item["result_digest"]))
                for item in receipt_rows
            ))
            attempt_ids = tuple(sorted(item[1] for item in digest_rows))
            results_digest = _digest(digest_rows)
            try:
                claimed = self.company_coordination.claim_partial_continuation(
                    job_id=request.job_id,
                    continuation_id=self._remote_continuation_id(
                        request.job_id, frozen_snapshot_hash, graph_structure_digest(graph)
                    ),
                    request_snapshot_hash=frozen_snapshot_hash,
                    graph_digest=graph_structure_digest(graph),
                    completed_attempt_ids=attempt_ids,
                    completed_results_digest=results_digest,
                )
            except CompanyCoordinationError as error:
                raise RuntimeError("Remote Company continuation authority is unavailable") from error
            if not claimed:
                raise RuntimeError("Partial continuation was already claimed by another device")
        receipts = self.store.claim_partial_job_continuation(
            job_id=request.job_id,
            request_snapshot_hash=frozen_snapshot_hash,
            graph_digest=graph_structure_digest(graph),
        )
        results: dict[str, EmployeeRunResult] = {}
        for receipt in receipts:
            result = receipt["result"]
            if not isinstance(result, EmployeeRunResult) or result.status is not RunStatus.SUCCEEDED:
                raise RuntimeError("Partial continuation receipt is not a successful Employee result")
            if result.task_id in results:
                raise RuntimeError("Partial continuation contains duplicate task results")
            results[result.task_id] = result
        if not results:
            raise RuntimeError("Partial continuation contains no dependency results")
        return results

    def assert_fresh_kernel_entry(
        self,
        request: CompanyRunRequest,
    ) -> None:
        """Reject accidental Kernel re-entry through the ordinary run path.

        The low-level snapshot writer remains exact-payload idempotent for
        crash-safe ledger persistence.  Kernel dispatch is different: seeing
        an existing ACTIVE JOB means an operator must choose the explicit
        receipt-bound continuation path instead of silently replaying it.
        """

        if self.store.get_job_ledger_rows(request.job_id) is not None:
            raise ValueError(
                "Existing ACTIVE JOB requires explicit same-Job continuation admission"
            )

    def append_attempt(self, job_id: str, record: TaskAttemptRecord) -> None:
        self.store.append_job_attempt(job_id, to_primitive(record))

    def append_dependency_result_receipt(
        self,
        job_id: str,
        record: TaskAttemptRecord,
        result: EmployeeRunResult,
    ) -> None:
        """Materialize a successful dependency result outside ACTIVE JOB audit."""

        self.store.save_job_dependency_result_receipt(
            job_id=job_id,
            attempt_id=record.attempt_id,
            result=result,
        )

    def append_mutation(self, job_id: str, event: JobMutationEvent) -> None:
        self.store.append_job_mutation(job_id, to_primitive(event))

    def append_graph_patch(self, job_id: str, event: GraphPatchEvent) -> None:
        lease_payload = {
            "model_calls": event.mutation_lease.model_calls,
            "tool_calls": event.mutation_lease.tool_calls,
            "cost_usd": event.mutation_lease.cost_usd,
        }
        # Reserve before appending the graph rewrite: the audit entry is not
        # executable until its added work has a durable Job-local commitment.
        # Idempotent lease identity is the graph-patch event identity, so a
        # crash between reserve and append can safely replay the same patch.
        self.store.reserve_job_lifecycle_lease(
            job_id=job_id,
            lease_id=event.event_id,
            lease=lease_payload,
            reason=f"GRAPH_PATCH_{event.sequence}",
        )
        try:
            self.store.append_job_graph_patch(job_id, to_primitive(event))
        except Exception:
            # The append did not make this capacity executable. Release only
            # this explicitly proven unused reserve; accepted patch leases
            # remain committed until terminal settlement.
            self.store.release_job_lifecycle_lease(
                job_id=job_id,
                lease_id=event.event_id,
                reason="GRAPH_PATCH_AUDIT_REJECTED",
            )
            raise

    def append_claimed_graph_proposal_patch(
        self,
        job_id: str,
        proposal: GraphPatchProposalEvent,
        event: GraphPatchEvent,
    ) -> None:
        """Persist the claimed proposal lease/patch while the Job remains paused.

        Ordinary automatic rewrites require an admitted lifecycle.  This path
        is narrower: the store accepts a paused reserve only when the exact
        approved continuation is already claimed and its graph digests match.
        Both reserve and append are idempotent, so an interruption before
        activation can be retried without a second approval or dispatch.
        """

        self.store.reserve_job_lifecycle_lease(
            job_id=job_id,
            lease_id=event.event_id,
            lease={
                "model_calls": event.mutation_lease.model_calls,
                "tool_calls": event.mutation_lease.tool_calls,
                "cost_usd": event.mutation_lease.cost_usd,
            },
            reason=f"GRAPH_PROPOSAL_PATCH_{event.sequence}",
            claimed_graph_proposal_id=proposal.proposal_id,
            before_graph_digest=event.before_graph_digest,
            after_graph_digest=event.after_graph_digest,
        )
        try:
            self.store.append_job_graph_patch(job_id, to_primitive(event))
        except Exception:
            # A later retry uses the same lease/event identities.  Releasing
            # here would make a crash between reserve and append ambiguous.
            raise

    def activate_claimed_graph_proposal(
        self,
        job_id: str,
        proposal: GraphPatchProposalEvent,
        event: GraphPatchEvent,
    ) -> None:
        """Resume dispatch only after the exact claimed patch is durable."""

        self.store.activate_claimed_graph_proposal_continuation(
            job_id=job_id,
            proposal_id=proposal.proposal_id,
            graph_patch_event_id=event.event_id,
        )

    def append_graph_proposal(
        self,
        job_id: str,
        event: GraphPatchProposalEvent,
    ) -> None:
        """Persist an immutable pending or terminal Graph proposal receipt."""

        self.store.append_job_graph_proposal(job_id, to_primitive(event))

    def resolve_graph_proposal(
        self,
        job_id: str,
        event: GraphPatchProposalEvent,
    ) -> None:
        """Append an exact terminal decision for a durable pending candidate.

        The store compares the full patch/digest/lease binding against the
        pending receipt.  This adapter intentionally exposes no update or
        arbitrary-status path to terminal or product surfaces.
        """

        if event.status not in {
            GraphPatchProposalStatus.APPROVED,
            GraphPatchProposalStatus.REJECTED,
        }:
            raise ValueError("Only an approved or rejected Graph proposal can be resolved")
        if self.company_coordination is not None:
            resolve = getattr(
                self.company_coordination,
                "resolve_graph_proposal_continuation",
                None,
            )
            if not callable(resolve):
                raise RuntimeError("Remote Company coordination lacks Graph proposal decision authority")
            try:
                resolve(
                    job_id=job_id,
                    proposal_id=event.proposal_id,
                    decision=event.status.value,
                    **self._graph_proposal_remote_identity(job_id, event),
                )
            except CompanyCoordinationError as error:
                raise RuntimeError("Remote Graph proposal decision is unavailable") from error
        self.store.resolve_pending_job_graph_proposal(job_id, to_primitive(event))

    def pending_graph_proposal(
        self,
        job_id: str,
        proposal_id: str,
    ) -> GraphPatchProposalEvent:
        """Load one verified pending candidate for a user-facing decision."""

        if not proposal_id:
            raise ValueError("Graph proposal identifier is required")
        # Import at the call boundary: the inspector reads this writer's
        # durable records, while this validation asks it to replay them.
        from .job_inspector import ActiveJobInspector

        inspection = ActiveJobInspector(
            self.store,
            company_coordination=self.company_coordination,
        ).inspect(job_id)
        if inspection.audit_status is ActiveJobAuditStatus.INVALID:
            raise ValueError("Graph proposal decision requires a replay-valid ACTIVE JOB audit")
        rows = self.store.get_job_ledger_rows(job_id)
        if rows is None:
            raise ValueError("Graph proposal Job was not found")
        matches = [
            _payload(row)
            for row in rows["graph_proposals"]
            if str(row.get("proposal_id", "")) == proposal_id
            and str(row.get("status", "")) == "PENDING"
        ]
        if len(matches) != 1:
            raise ValueError("Graph proposal is not pending for this Job")
        event = graph_patch_proposal_event_from_primitive(matches[0])
        if event.status is not GraphPatchProposalStatus.PENDING:
            raise ValueError("Graph proposal is not pending for this Job")
        return event

    def hold_graph_proposal(
        self,
        job_id: str,
        event: GraphPatchProposalEvent,
    ) -> None:
        """Durably pause dispatch after the exact pending receipt exists.

        No worker is retained and no budget capacity is released here.  The
        lifecycle pause is merely the observable hold boundary; a later
        continuation must still prove the frozen request, graph, result
        receipts and chosen terminal decision before dispatch can restart.
        """

        if event.status.value != "PENDING":
            raise ValueError("Only a pending graph proposal can hold a Job")
        self.store.transition_job_lifecycle(
            job_id=job_id,
            operation="PAUSE",
            reason=f"GRAPH_PROPOSAL_PENDING:{event.proposal_id[:48]}",
        )
        self.store.authorize_graph_proposal_continuation(
            job_id=job_id,
            proposal_id=event.proposal_id,
            request_snapshot_hash=self.store.job_frozen_snapshot_hash(job_id),
            before_graph_digest=event.before_graph_digest,
            after_graph_digest=event.after_graph_digest,
        )
        if self.company_coordination is not None:
            authorize = getattr(
                self.company_coordination,
                "authorize_graph_proposal_continuation",
                None,
            )
            if not callable(authorize):
                raise RuntimeError("Remote Company coordination lacks Graph proposal authority")
            try:
                authorize(
                    job_id=job_id,
                    proposal_id=event.proposal_id,
                    **self._graph_proposal_remote_identity(job_id, event),
                )
            except CompanyCoordinationError as error:
                raise RuntimeError("Remote Graph proposal authority is unavailable") from error

    def claim_approved_graph_proposal(
        self,
        job_id: str,
        event: GraphPatchProposalEvent,
    ) -> None:
        """Consume a resolved approval before any resumed dispatch occurs."""

        if event.status.value != "APPROVED":
            raise ValueError("Only an approved Graph proposal can be claimed")
        identity = self._graph_proposal_remote_identity(job_id, event)
        if self.company_coordination is not None:
            claim = getattr(self.company_coordination, "claim_graph_proposal_continuation", None)
            if not callable(claim):
                raise RuntimeError("Remote Company coordination lacks Graph proposal claim authority")
            try:
                claimed = claim(
                    job_id=job_id,
                    proposal_id=event.proposal_id,
                    decision=event.status.value,
                    **identity,
                )
            except CompanyCoordinationError as error:
                raise RuntimeError("Remote Graph proposal claim is unavailable") from error
            if not claimed:
                raise RuntimeError("Remote Graph proposal continuation was already claimed")
        self.store.claim_approved_graph_proposal_continuation(
            job_id=job_id,
            proposal_id=event.proposal_id,
            request_snapshot_hash=identity["request_snapshot_hash"],
            before_graph_digest=event.before_graph_digest,
            after_graph_digest=event.after_graph_digest,
        )

    def claim_rejected_graph_proposal(
        self,
        job_id: str,
        event: GraphPatchProposalEvent,
    ) -> None:
        """Consume a rejection before restoring the unchanged paused Graph."""

        if event.status is not GraphPatchProposalStatus.REJECTED:
            raise ValueError("Only a rejected Graph proposal can restore the prior Graph")
        identity = self._graph_proposal_remote_identity(job_id, event)
        if self.company_coordination is not None:
            claim = getattr(self.company_coordination, "claim_graph_proposal_continuation", None)
            if not callable(claim):
                raise RuntimeError("Remote Company coordination lacks Graph proposal claim authority")
            try:
                claimed = claim(
                    job_id=job_id,
                    proposal_id=event.proposal_id,
                    decision=event.status.value,
                    **identity,
                )
            except CompanyCoordinationError as error:
                raise RuntimeError("Remote Graph proposal claim is unavailable") from error
            if not claimed:
                raise RuntimeError("Remote Graph proposal continuation was already claimed")
        self.store.claim_rejected_graph_proposal_continuation(
            job_id=job_id,
            proposal_id=event.proposal_id,
            request_snapshot_hash=identity["request_snapshot_hash"],
            before_graph_digest=event.before_graph_digest,
            after_graph_digest=event.after_graph_digest,
        )

    def activate_rejected_graph_proposal(
        self,
        job_id: str,
        event: GraphPatchProposalEvent,
    ) -> None:
        """Expose the original Graph only after the rejection claim is durable."""

        self.store.activate_claimed_graph_proposal_rejection(
            job_id=job_id,
            proposal_id=event.proposal_id,
            before_graph_digest=event.before_graph_digest,
            after_graph_digest=event.after_graph_digest,
        )

    def prepare_claimed_graph_proposal_continuation(
        self,
        request: CompanyRunRequest,
        event: GraphPatchProposalEvent,
    ) -> ActiveJobApprovedGraphContinuation:
        """Reconstruct only the approved graph plus locally retained successes."""

        if event.status is not GraphPatchProposalStatus.APPROVED:
            raise ValueError("Graph continuation requires an approved proposal")
        return self._prepare_graph_proposal_continuation(
            request,
            event,
            terminal_status=GraphPatchProposalStatus.APPROVED,
        )

    def prepare_rejected_graph_proposal_continuation(
        self,
        request: CompanyRunRequest,
        event: GraphPatchProposalEvent,
    ) -> ActiveJobApprovedGraphContinuation:
        """Reconstruct the exact prior graph after one explicit rejection."""

        if event.status is not GraphPatchProposalStatus.REJECTED:
            raise ValueError("Graph rejection continuation requires a rejected proposal")
        return self._prepare_graph_proposal_continuation(
            request,
            event,
            terminal_status=GraphPatchProposalStatus.REJECTED,
        )

    def _prepare_graph_proposal_continuation(
        self,
        request: CompanyRunRequest,
        event: GraphPatchProposalEvent,
        *,
        terminal_status: GraphPatchProposalStatus,
    ) -> ActiveJobApprovedGraphContinuation:
        """Rebuild the pre-decision graph and receipt-proven successful work."""

        initial = graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks)
        persisted = self.store.get_job_ledger_rows(request.job_id)
        if persisted is None:
            raise ValueError("Graph continuation Job was not found")
        proposal_rows = [
            row for row in persisted["graph_proposals"]
            if str(row.get("proposal_id", "")) == event.proposal_id
        ]
        proposals = [_payload(row) for row in proposal_rows]
        if len(proposals) != 2 or {item.get("status") for item in proposals} != {
            "PENDING",
            terminal_status.value,
        }:
            raise ValueError("Graph continuation proposal history is invalid")
        results: dict[str, EmployeeRunResult] = {}
        for receipt in self.store.list_job_dependency_result_receipts(request.job_id):
            result = receipt["result"]
            if result.status is RunStatus.SUCCEEDED:
                results[result.task_id] = result
        pending_row = next(
            row for row in proposal_rows if str(row.get("status", "")) == "PENDING"
        )
        pending_ledger_seq = int(pending_row["ledger_seq"])
        before = initial
        prior_patch_count = 0
        prior_specialist_material_profiles: set[str] = set()
        ordered: list[tuple[int, str, Mapping[str, Any]]] = []
        ordered.extend(
            (int(row["ledger_seq"]), "ATTEMPT", row)
            for row in persisted["attempts"]
            if int(row["ledger_seq"]) < pending_ledger_seq
        )
        ordered.extend(
            (int(row["ledger_seq"]), "MUTATION", row)
            for row in persisted["mutations"]
            if int(row["ledger_seq"]) < pending_ledger_seq
        )
        ordered.extend(
            (int(row["ledger_seq"]), "GRAPH_PATCH", row)
            for row in persisted["graph_patches"]
            if int(row["ledger_seq"]) < pending_ledger_seq
        )
        for _, kind, row in sorted(ordered, key=lambda item: item[0]):
            payload = _payload(row)
            if kind == "ATTEMPT":
                task_id = str(payload.get("task_id", ""))
                material_digest = str(payload.get("capability_material_digest", ""))
                if material_digest and str(payload.get("employee_id", "")) != request.manager_employee_id:
                    prior_specialist_material_profiles.add(material_digest)
                task = next((item for item in before.tasks if item.task_id == task_id), None)
                if task is None:
                    raise ValueError("Graph continuation attempt task is absent from replayed graph")
                status = str(payload.get("status", ""))
                task_status = {
                    "SUCCEEDED": TaskStatus.SUCCEEDED,
                    "CANCELLED": TaskStatus.CANCELLED,
                }.get(status, TaskStatus.FAILED)
                result = results.get(task_id) if task_status is TaskStatus.SUCCEEDED else None
                if task_status is TaskStatus.SUCCEEDED and result is None:
                    raise ValueError("Graph continuation lacks a successful result receipt")
                before = replace_task(
                    before,
                    replace(
                        task,
                        status=task_status,
                        runtime_result=result,
                        assignee_id=str(payload.get("employee_id", "")),
                        attempt=int(payload.get("sequence", 0) or 0),
                    ),
                )
            elif kind == "MUTATION":
                task_id = str(payload.get("task_id", ""))
                task = next((item for item in before.tasks if item.task_id == task_id), None)
                if task is None:
                    raise ValueError("Graph continuation mutation task is absent from replayed graph")
                before = replace_task(
                    before,
                    replace(
                        task,
                        status=TaskStatus.PENDING,
                        runtime_result=None,
                        assignee_id=None,
                        attempt=int(payload.get("target_attempt_sequence", 0) or 0),
                    ),
                )
            else:
                if str(payload.get("before_graph_digest", "")) != graph_structure_digest(before):
                    raise ValueError("Prior Graph patch before digest does not match replayed graph")
                patch = graph_patch_from_primitive(payload.get("patch"))
                before = apply_patch(before, patch, max_tasks=request.job_limits.max_tasks)
                if str(payload.get("after_graph_digest", "")) != graph_structure_digest(before):
                    raise ValueError("Prior Graph patch after digest does not match replayed graph")
                prior_patch_count += 1
        if graph_structure_digest(before) != event.before_graph_digest:
            raise ValueError("Graph continuation prior receipts do not match proposal")
        graph = before
        if terminal_status is GraphPatchProposalStatus.APPROVED:
            pending = next(item for item in proposals if item.get("status") == "PENDING")
            patch = graph_patch_from_primitive(pending["patch"])
            graph = apply_patch(before, patch, max_tasks=request.job_limits.max_tasks)
            if graph_structure_digest(graph) != event.after_graph_digest:
                raise ValueError("Graph continuation patched graph does not match proposal")
        return ActiveJobApprovedGraphContinuation(
            job_id=request.job_id,
            proposal_id=event.proposal_id,
            before_graph=before,
            graph=graph,
            completed_results=results,
            prior_graph_patch_count=prior_patch_count,
            prior_specialist_material_profiles=frozenset(prior_specialist_material_profiles),
        )

    def append_supervision(
        self,
        *,
        job_id: str,
        attempt_id: str,
        task_id: str,
        manager_employee_id: str,
        action: str,
        signal_code: str | None,
        priority: str,
        remaining_wall_time_ms: int,
        capability_shortage_count: int,
        conflicting_outcome: bool,
    ) -> None:
        """Persist only typed Manager supervision evidence after an attempt."""

        deadline_bucket = (
            "EXPIRED"
            if remaining_wall_time_ms <= 0
            else "NEAR"
            if remaining_wall_time_ms <= 5_000
            else "READY"
        )
        unsigned = {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "manager_employee_id": manager_employee_id,
            "action": action,
            "signal_code": signal_code,
            "priority": priority,
            "deadline_bucket": deadline_bucket,
            "capability_shortage_count": capability_shortage_count,
            "conflicting_outcome": conflicting_outcome,
        }
        event_id = f"supervision-{_digest(unsigned)[:24]}"
        self.store.append_job_supervision(
            job_id,
            {"event_id": event_id, **unsigned},
        )

    def dispatch_state(self, job_id: str) -> str:
        """Return the current durable hold state without exposing Job content."""

        state = self.store.get_job_lifecycle(job_id)
        return "ADMITTED" if state is None else str(state["state"])

    def consume_operator_signals(
        self,
        *,
        job_id: str,
        task_id: str,
    ) -> tuple[RunSignal, ...]:
        """Claim user-owned typed corrections only at a result boundary."""

        rows = self.store.consume_job_operator_signals(
            job_id=job_id,
            target_task_id=task_id,
        )
        return tuple(
            RunSignal(
                SignalCode.USER_CORRECTION,
                value=str(row["reference"]),
                evidence=(f"operator-signal:{str(row['signal_id'])[:16]}",),
            )
            for row in rows
        )

    def finish_job(self, job_id: str, result: JobResult) -> None:
        from .job_ledger_terminal import finish_job

        finish_job(self, job_id, result)
