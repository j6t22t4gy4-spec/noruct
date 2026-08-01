from __future__ import annotations

from typing import Any, Mapping

from dynamic_firm.company.frontdoor import WorkOrder
from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import CompanyRunRequest
from dynamic_firm.kernel.mutation import (
    frozen_snapshot_digest,
    graph_structure_digest,
    structurally_read_only,
)
from dynamic_firm.runtime.models import (
    EventType,
    FailureCategory,
    IdempotencyMode,
    RunStatus,
    ToolEffect,
)
from dynamic_firm.runtime.interruption import (
    InterruptionCause,
    RecoveryActionPreview,
    RecoveryDisposition,
)

from .company_coordination import CompanyCoordinationError
from .job_ledger_primitives import (
    ActiveJobAuditStatus,
    ActiveJobEffectRecovery,
    ActiveJobInterruptionEvidence,
    ActiveJobInspection,
    ActiveJobPartialContinuation,
    ActiveJobRecoveryAdvice,
    ActiveJobRecoveryPreparation,
    ActiveJobSameJobContinuation,
    _digest,
    remote_continuation_id,
)


class ActiveJobRecoveryMixin:

    def _interruption_evidence(
        self,
        inspection: ActiveJobInspection,
    ) -> ActiveJobInterruptionEvidence:
        """Summarize cancellation/timeout evidence without exposing payloads.

        This is deliberately a read-only projection from the Employee Runtime
        ledger.  A complete cancellation event must retain both the original
        model-call index and an opaque provider request identity; anything
        else remains unknown rather than being upgraded by inference.
        """

        provider_cancellations = 0
        malformed_cancellations = 0
        timeout_runs = 0
        nonterminal_runs = 0
        causes: set[InterruptionCause] = set()
        for run in inspection.runtime_runs:
            if run.status not in {
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
                RunStatus.BUDGET_EXHAUSTED.value,
            }:
                nonterminal_runs += 1
                causes.add(InterruptionCause.PROCESS_OR_MACHINE_LOSS)
            result = self.store.get_result(run.run_id)
            if result is not None:
                if result.status is RunStatus.CANCELLED:
                    causes.add(InterruptionCause.USER_CANCEL)
                if result.failure is not None:
                    if result.failure.category is FailureCategory.TIMEOUT:
                        timeout_runs += 1
                        causes.add(InterruptionCause.DEADLINE_TIMEOUT)
                    if result.failure.category is FailureCategory.CANCEL:
                        causes.add(InterruptionCause.USER_CANCEL)
                    if result.failure.code == "PROCESS_INTERRUPTED":
                        causes.add(InterruptionCause.PROCESS_OR_MACHINE_LOSS)
                    if result.failure.code in {
                        "MODEL_TRANSPORT_ERROR",
                        "MODEL_PROVIDER_ERROR",
                        "MODEL_PROVIDER_UNEXPECTED",
                    }:
                        causes.add(InterruptionCause.PROVIDER_DISCONNECT)
            for event in self.store.list_events(run.run_id):
                if event.type is not EventType.MODEL_CALL_CANCELLED:
                    continue
                call_index = event.payload.get("call_index")
                provider_request_id = event.payload.get("provider_request_id")
                if (
                    isinstance(call_index, int)
                    and not isinstance(call_index, bool)
                    and call_index >= 0
                    and isinstance(provider_request_id, str)
                    and provider_request_id.strip()
                ):
                    provider_cancellations += 1
                    causes.add(InterruptionCause.USER_CANCEL)
                else:
                    malformed_cancellations += 1
                    causes.add(InterruptionCause.UNKNOWN)
        if not causes:
            causes.add(InterruptionCause.UNKNOWN)
        return ActiveJobInterruptionEvidence(
            provider_cancellation_receipt_count=provider_cancellations,
            malformed_provider_cancellation_event_count=malformed_cancellations,
            timeout_terminal_run_count=timeout_runs,
            nonterminal_runtime_run_count=nonterminal_runs,
            causes=tuple(sorted(causes, key=lambda item: item.value)),
        )

    @staticmethod
    def _recovery_action_previews(
        *,
        disposition: RecoveryDisposition,
        continuation_available: bool = False,
        effect_resolution_available: bool = False,
        terminal: bool = False,
        invalid: bool = False,
    ) -> tuple[RecoveryActionPreview, ...]:
        blocked = terminal or invalid
        effect_blocked = disposition in {
            RecoveryDisposition.RECONCILE_OR_COMPENSATE_REQUIRED,
            RecoveryDisposition.FAIL_CLOSED,
        }
        return (
            RecoveryActionPreview(
                action="resume",
                enabled=continuation_available and not blocked and not effect_blocked,
                requires_confirmation=True,
                creates_new_work_order=False,
                expected_effect="Consume one receipt-bound read-only continuation; never revive an in-flight worker.",
                reason=(
                    "A replay-verified read-only continuation envelope is available."
                    if continuation_available and not blocked and not effect_blocked
                    else "No safe receipt-bound continuation is currently available."
                ),
            ),
            RecoveryActionPreview(
                action="retry",
                enabled=not blocked and not effect_blocked,
                requires_confirmation=True,
                creates_new_work_order=True,
                expected_effect="Create a new Work Order and Kernel audit chain; no prior tool action is replayed.",
                reason=(
                    "Resolve or compensate the unknown external effect first."
                    if effect_blocked
                    else (
                        "A replacement may be submitted if the original goal is still desired."
                        if not blocked
                        else "Terminal or invalid audits cannot be retried from this surface."
                    )
                ),
            ),
            RecoveryActionPreview(
                action="skip",
                enabled=False,
                requires_confirmation=True,
                creates_new_work_order=False,
                expected_effect="Skipping would change graph/result authority and is unsupported.",
                reason="An interrupted task cannot be declared complete without a receipt.",
            ),
            RecoveryActionPreview(
                action="reconcile",
                enabled=effect_resolution_available and not invalid,
                requires_confirmation=True,
                creates_new_work_order=False,
                expected_effect="Append a content-free operator outcome receipt; never execute an external effect.",
                reason=(
                    "At least one indeterminate effect requires explicit evidence, compensation, or sealing."
                    if effect_resolution_available
                    else "No open effect recovery case is available for resolution."
                ),
            ),
            RecoveryActionPreview(
                action="cancel",
                enabled=not blocked,
                requires_confirmation=True,
                creates_new_work_order=False,
                expected_effect="Change the Job lifecycle hold only; do not undo or stop an external effect.",
                reason=(
                    "Cancellation can preserve operator intent without claiming effect rollback."
                    if not blocked
                    else "No lifecycle transition is available from this recovery view."
                ),
            ),
        )

    def assess_effectful_recovery(self, job_id: str) -> ActiveJobEffectRecovery:
        """Classify effect recovery without replaying a Job or tool action.

        A completed idempotent effect is never run again.  Only its exact
        successful prefix can be settled, after which an operator may submit
        an explicit replacement Work Order for the still-pending tasks.  Any
        missing receipt, legacy contract, live attempt or failed action is
        deliberately fail-closed.
        """

        inspection = self.inspect(job_id)
        def rejected(reason: str, *checks: str) -> ActiveJobEffectRecovery:
            return ActiveJobEffectRecovery(
                job_id=inspection.job_id,
                disposition="FAIL_CLOSED",
                completed_task_ids=(),
                pending_task_ids=(),
                observed_cost_usd=None,
                required_checks=checks,
                reason=reason,
            )

        if inspection.audit_status is not ActiveJobAuditStatus.INTERRUPTED or not inspection.replay_matches:
            return rejected("The ACTIVE JOB is not a replay-verified interrupted Job.", "replay_verified_interrupted_audit")
        if inspection.mutation_count or inspection.graph_patch_count or inspection.graph_proposal_decisions:
            return rejected("Graph mutation or proposal history makes effect recovery indeterminate.", "unmodified_initial_graph")
        lifecycle = self.store.get_job_lifecycle(job_id)
        if lifecycle is None or str(lifecycle["state"]) != "ADMITTED":
            return rejected("The Job lifecycle is not an admitted interrupted state.", "admitted_job_lifecycle")
        receipts = self.store.list_job_dependency_result_receipts(job_id)
        completed = tuple(sorted(str(item["task_id"]) for item in receipts))
        replayed_completed = tuple(sorted(
            str(task["task_id"])
            for task in inspection.reconstructed_tasks
            if task.get("status") == "SUCCEEDED"
        ))
        pending = tuple(sorted(
            str(task["task_id"])
            for task in inspection.reconstructed_tasks
            if task.get("status") == "PENDING"
        ))
        if not completed or completed != replayed_completed or not pending:
            return rejected("The completed receipt prefix or pending boundary is not exact.", "successful_prefix_receipts", "remaining_unstarted_tasks")
        completed_set = set(completed)
        if any(
            run.task_id not in completed_set or run.status != RunStatus.SUCCEEDED.value
            for run in inspection.runtime_runs
        ):
            return rejected("An Employee run is not a receipt-proven successful prefix result.", "no_inflight_or_unreplayed_employee_run")
        effectful = [
            action for action in self.store.list_job_tool_receipts(job_id)
            if action.get("effect") in {
                ToolEffect.WRITE.value,
                ToolEffect.EXECUTE.value,
                ToolEffect.EXTERNAL_COMMUNICATION.value,
            }
        ]
        if not effectful:
            return rejected("No durable effect receipt was recorded for this recovery path.", "durable_effect_receipt")
        allowed_modes = {IdempotencyMode.CALL_KEY.value, IdempotencyMode.NATURAL_KEY.value}
        if any(
            action.get("status") != "SUCCEEDED"
            or action.get("run_status") != RunStatus.SUCCEEDED.value
            or action.get("idempotency_mode") not in allowed_modes
            or action.get("task_id") not in completed_set
            for action in effectful
        ):
            return rejected("An effect is missing a terminal idempotent receipt or remains unsafe.", "terminal_idempotent_effect_receipts")
        return ActiveJobEffectRecovery(
            job_id=inspection.job_id,
            disposition="REPLACEMENT_WORK_ORDER_REQUIRED",
            completed_task_ids=completed,
            pending_task_ids=pending,
            observed_cost_usd=sum(item["result"].usage.cost_usd for item in receipts),
            required_checks=(
                "replay_verified_interrupted_audit",
                "terminal_idempotent_effect_receipts",
                "exact_successful_prefix_receipts",
                "remaining_tasks_unstarted",
                "local_only_no_remote_ownership_transfer",
            ),
            reason="Completed effects will not be replayed; submit a replacement Work Order for remaining tasks.",
        )

    def recovery_advice(self, job_id: str) -> ActiveJobRecoveryAdvice:
        """Project a safe, bounded recovery decision without mutating state."""

        inspection = self.inspect(job_id)
        statuses = tuple(sorted({run.status for run in inspection.runtime_runs}))
        effect_recovery_cases = self.store.list_job_effect_recovery_cases(job_id)
        remote_effect_resource_claims = (
            self.store.list_job_remote_effect_resource_claims(job_id)
        )
        blocking_effect_cases = tuple(
            item
            for item in effect_recovery_cases
            if not bool(item.get("resource_released"))
        )
        open_effect_cases = tuple(
            item
            for item in blocking_effect_cases
            if item.get("case_status") == "OPEN"
        )
        sealed_effect_cases = tuple(
            item
            for item in blocking_effect_cases
            if item.get("case_status") != "OPEN"
        )
        blocking_remote_claims = tuple(
            item
            for item in remote_effect_resource_claims
            if item.get("case_status") != "CLOSED"
        )
        resolvable_remote_claims = tuple(
            item
            for item in blocking_remote_claims
            if item.get("next_action")
            in {
                "CONFIRM_NO_EFFECT_AND_RELEASE_EXACT_OWNER",
                "CONFIRM_SUCCEEDED_AND_RELEASE_EXACT_OWNER",
            }
        )
        fail_closed_remote_claims = tuple(
            item
            for item in blocking_remote_claims
            if item not in resolvable_remote_claims
        )
        if inspection.audit_status == ActiveJobAuditStatus.TERMINAL:
            if blocking_effect_cases or blocking_remote_claims:
                disposition = (
                    RecoveryDisposition.FAIL_CLOSED
                    if sealed_effect_cases or fail_closed_remote_claims
                    else RecoveryDisposition.RECONCILE_OR_COMPENSATE_REQUIRED
                )
                recommended = []
                if open_effect_cases:
                    recommended.append(
                        "Resolve each open effect case from trustworthy external evidence, compensate it, or seal it unknown; none of these actions re-executes the effect."
                    )
                if sealed_effect_cases:
                    recommended.append(
                        "Keep every sealed-unknown resource blocked. Its append-only outcome is final and cannot be upgraded by inference or process recovery."
                    )
                for claim in resolvable_remote_claims:
                    evidence = (
                        "confirmed-no-effect"
                        if claim.get("next_action")
                        == "CONFIRM_NO_EFFECT_AND_RELEASE_EXACT_OWNER"
                        else "confirmed-succeeded"
                    )
                    recommended.append(
                        "Inspect remote effect claim "
                        f"{claim['action_id']} and use job effect-resolve with "
                        f"trustworthy {evidence} evidence; resolution releases only "
                        "the exact durable remote owner and never replays the action."
                    )
                for claim in fail_closed_remote_claims:
                    recommended.append(
                        "Keep remote effect claim "
                        f"{claim['action_id']} blocked for manual investigation; "
                        "its retained state does not prove that release is safe."
                    )
                recommended.append(
                    "Submit any still-desired work as a new Job only after its required resources are proven available."
                )
                if blocking_effect_cases:
                    recovery_state = (
                        "TERMINAL_EFFECT_OUTCOME_SEALED_FAIL_CLOSED"
                        if sealed_effect_cases
                        else "TERMINAL_EFFECT_OUTCOME_UNKNOWN"
                    )
                else:
                    recovery_state = (
                        "TERMINAL_REMOTE_EFFECT_CLAIM_FAIL_CLOSED"
                        if fail_closed_remote_claims
                        else "TERMINAL_REMOTE_EFFECT_CLAIM_REQUIRES_CLOSURE"
                    )
                return ActiveJobRecoveryAdvice(
                    job_id=inspection.job_id,
                    audit_status=inspection.audit_status,
                    recovery_state=recovery_state,
                    requires_new_kernel_attempt=False,
                    runtime_run_statuses=statuses,
                    recommended_actions=tuple(recommended),
                    prohibited_actions=(
                        "Do not treat terminal Job status as proof that a started external effect did not occur.",
                        "Do not retry, skip, or release an indeterminate effect resource automatically.",
                    ),
                    disposition=disposition,
                    effect_recovery_cases=effect_recovery_cases,
                    remote_effect_resource_claims=remote_effect_resource_claims,
                    action_previews=self._recovery_action_previews(
                        disposition=disposition,
                        effect_resolution_available=bool(
                            open_effect_cases or resolvable_remote_claims
                        ),
                        terminal=True,
                    ),
                )
            disposition = RecoveryDisposition.NO_RECOVERY_REQUIRED
            return ActiveJobRecoveryAdvice(
                job_id=inspection.job_id,
                audit_status=inspection.audit_status,
                recovery_state="TERMINAL_NO_RECOVERY",
                requires_new_kernel_attempt=False,
                runtime_run_statuses=statuses,
                recommended_actions=(
                    "Use the terminal audit and bounded timeline for review; no recovery is required.",
                ),
                prohibited_actions=(
                    "Do not create a continuation from a terminal ACTIVE JOB.",
                ),
                disposition=disposition,
                effect_recovery_cases=effect_recovery_cases,
                remote_effect_resource_claims=remote_effect_resource_claims,
                action_previews=self._recovery_action_previews(
                    disposition=disposition,
                    terminal=True,
                ),
            )
        if inspection.audit_status == ActiveJobAuditStatus.INVALID:
            disposition = RecoveryDisposition.FAIL_CLOSED
            return ActiveJobRecoveryAdvice(
                job_id=inspection.job_id,
                audit_status=inspection.audit_status,
                recovery_state="AUDIT_INVALID_MANUAL_INVESTIGATION",
                requires_new_kernel_attempt=False,
                runtime_run_statuses=statuses,
                recommended_actions=(
                    "Stop automated handling and inspect the ledger mismatch before any new execution.",
                    "Export the local state or support bundle only through an operator-approved path.",
                ),
                prohibited_actions=(
                    "Do not resume, replay, or infer a replacement attempt from an invalid audit.",
                ),
                disposition=disposition,
                effect_recovery_cases=effect_recovery_cases,
                remote_effect_resource_claims=remote_effect_resource_claims,
                action_previews=self._recovery_action_previews(
                    disposition=disposition,
                    invalid=True,
                ),
            )

        interruption = self._interruption_evidence(inspection)
        effectful_receipts = any(
            action.get("effect") in {
                ToolEffect.WRITE.value,
                ToolEffect.EXECUTE.value,
                ToolEffect.EXTERNAL_COMMUNICATION.value,
            }
            for action in self.store.list_job_tool_receipts(job_id)
        )
        effect_recovery = (
            self.assess_effectful_recovery(job_id) if effectful_receipts else None
        )
        recommended = [
            "Inspect this job and its bounded timeline before deciding whether the original goal is still desired.",
            "If work should continue, submit the user goal as a new Company job so the Firm Kernel creates a fresh graph, budget lease, and audit chain.",
        ]
        prohibited = [
            "Do not resume the original ACTIVE JOB from its privacy-bounded audit snapshot.",
            "Do not replay an in-flight employee run or tool action.",
            "Do not resolve an approval in this interrupted job as a way to restart execution.",
        ]
        recovery_state = "INTERRUPTED_NEW_KERNEL_ATTEMPT_REQUIRED"
        disposition = RecoveryDisposition.NEW_KERNEL_ATTEMPT_REQUIRED
        if interruption.malformed_provider_cancellation_event_count:
            recovery_state = "INTERRUPTED_INCOMPLETE_CANCELLATION_EVIDENCE"
            recommended.insert(
                1,
                "A retained provider cancellation event is incomplete; treat completion, usage, and any remote effect as unknown before submitting a replacement.",
            )
            prohibited.append(
                "Do not infer a successful provider cancellation from an incomplete cancellation event.",
            )
        elif interruption.provider_cancellation_receipt_count:
            recovery_state = "INTERRUPTED_PROVIDER_CANCELLATION_REPLACEMENT_REQUIRED"
            recommended.insert(
                1,
                "A provider cancellation receipt was retained. It confirms observed cancellation only; review the bounded timeline and submit an explicit replacement for unfinished work.",
            )
            prohibited.append(
                "Do not infer zero usage, no completion, or no external effect from a provider cancellation receipt.",
            )
        elif interruption.timeout_terminal_run_count:
            recovery_state = "INTERRUPTED_TIMEOUT_REPLACEMENT_REQUIRED"
            recommended.insert(
                1,
                "A runtime timeout was retained. Treat its unfinished boundary as indeterminate and use an explicit replacement rather than a replay.",
            )
        if effect_recovery is not None:
            if effect_recovery.disposition == "REPLACEMENT_WORK_ORDER_REQUIRED":
                recovery_state = "INTERRUPTED_EFFECTFUL_REPLACEMENT_REQUIRED"
                recommended.insert(
                    1,
                    "A completed idempotent effect prefix is retained as evidence only; do not replay it and submit a replacement Work Order for the remaining tasks.",
                )
            else:
                recovery_state = "INTERRUPTED_EFFECTFUL_FAIL_CLOSED"
                recommended.insert(
                    1,
                    "Effect recovery cannot prove an exact completed prefix; preserve the original as evidence and treat remote completion and usage as unknown.",
                )
            prohibited.append(
                "Do not replay an effectful tool action from the interrupted Job, even when an idempotency key exists.",
            )
        if blocking_effect_cases:
            recovery_state = (
                "INTERRUPTED_EFFECT_OUTCOME_SEALED_FAIL_CLOSED"
                if sealed_effect_cases
                else "INTERRUPTED_EFFECT_OUTCOME_UNKNOWN"
            )
            disposition = (
                RecoveryDisposition.FAIL_CLOSED
                if sealed_effect_cases
                else RecoveryDisposition.RECONCILE_OR_COMPENSATE_REQUIRED
            )
            if open_effect_cases:
                recommended.insert(
                    1,
                    "An effect handler started without a trustworthy terminal receipt. Reconcile, compensate, or seal the open case explicitly before replacement work can touch that resource.",
                )
            if sealed_effect_cases:
                recommended.insert(
                    1,
                    "A sealed-unknown effect remains permanently unavailable to automatic recovery; retain its resource hold and investigate outside execution if needed.",
                )
            prohibited.extend(
                (
                    "Do not retry or skip an indeterminate effect.",
                    "Do not release its resource solely because the owning process or Job is terminal.",
                )
            )
        if blocking_remote_claims:
            if not blocking_effect_cases:
                recovery_state = (
                    "INTERRUPTED_REMOTE_EFFECT_CLAIM_FAIL_CLOSED"
                    if fail_closed_remote_claims
                    else "INTERRUPTED_REMOTE_EFFECT_CLAIM_REQUIRES_CLOSURE"
                )
            if fail_closed_remote_claims:
                disposition = RecoveryDisposition.FAIL_CLOSED
            elif not blocking_effect_cases:
                disposition = RecoveryDisposition.RECONCILE_OR_COMPENSATE_REQUIRED
            for claim in resolvable_remote_claims:
                evidence = (
                    "confirmed-no-effect"
                    if claim.get("next_action")
                    == "CONFIRM_NO_EFFECT_AND_RELEASE_EXACT_OWNER"
                    else "confirmed-succeeded"
                )
                recommended.insert(
                    1,
                    "Inspect remote effect claim "
                    f"{claim['action_id']} and use job effect-resolve with "
                    f"trustworthy {evidence} evidence before replacement work; "
                    "the action itself must not be replayed.",
                )
            for claim in fail_closed_remote_claims:
                recommended.insert(
                    1,
                    "Keep remote effect claim "
                    f"{claim['action_id']} blocked for manual investigation; "
                    "no retained proof permits automatic release.",
                )
            prohibited.extend(
                (
                    "Do not retry or skip an action while its remote effect claim remains open.",
                    "Do not release a remote effect claim without the exact durable owner and permitted terminal evidence.",
                )
            )
        if inspection.runtime_runs:
            recommended.insert(
                1,
                "Treat every retained runtime run as interrupted evidence, not as a live worker lease.",
            )
        candidate = self._local_continuation_candidate(inspection)
        if candidate is not None:
            recommended.insert(
                1,
                "A verified local continuation envelope is available for review; revalidate its required checks before a new Kernel-owned continuation.",
            )
            if disposition is RecoveryDisposition.NEW_KERNEL_ATTEMPT_REQUIRED:
                disposition = RecoveryDisposition.RECEIPT_BOUND_READ_ONLY_CONTINUATION
        return ActiveJobRecoveryAdvice(
            job_id=inspection.job_id,
            audit_status=inspection.audit_status,
            recovery_state=recovery_state,
            requires_new_kernel_attempt=True,
            runtime_run_statuses=statuses,
            recommended_actions=tuple(recommended),
            prohibited_actions=tuple(prohibited),
            disposition=disposition,
            local_continuation_candidate=candidate,
            interruption_evidence=interruption,
            effect_recovery=effect_recovery,
            effect_recovery_cases=effect_recovery_cases,
            remote_effect_resource_claims=remote_effect_resource_claims,
            action_previews=self._recovery_action_previews(
                disposition=disposition,
                continuation_available=candidate is not None,
                effect_resolution_available=bool(
                    open_effect_cases or resolvable_remote_claims
                ),
            ),
        )

    def _local_continuation_candidate(
        self,
        inspection: ActiveJobInspection,
    ) -> Mapping[str, Any] | None:
        """Expose only an audited, non-dispatchable candidate for future recovery.

        The envelope does not recreate a request, graph state, approval, or
        budget.  Its immutable digests must still agree with the accepted
        ACTIVE JOB audit before an operator can create a fresh Kernel attempt.
        """

        try:
            candidate = self.store.recovery_candidate(inspection.job_id)
        except KeyError:
            return None
        if candidate["work_order_digest"] != inspection.work_order_digest:
            raise RuntimeError("Resume envelope work-order digest does not match ACTIVE JOB")
        if candidate["graph_digest"] != inspection.initial_graph_digest:
            raise RuntimeError("Resume envelope graph digest does not match ACTIVE JOB")
        return candidate

    def prepare_work_order_recovery(
        self,
        job_id: str,
        *,
        work_order: WorkOrder,
        source_references: Mapping[str, str],
    ) -> ActiveJobRecoveryPreparation:
        """Validate a user-owned Work Order against an interrupted Job audit.

        This is the hand-off boundary for a later Kernel invocation, rather
        than a hidden resume mechanism. Every value is an opaque digest or
        reference; raw source text, prompt/context, tool data and approvals
        remain in their original authorities and must be independently
        reopened by the caller.
        """

        inspection = self.inspect(job_id)
        if (
            inspection.audit_status is not ActiveJobAuditStatus.INTERRUPTED
            or not inspection.replay_matches
        ):
            raise ValueError("Only a replay-verified interrupted Job can prepare recovery")
        candidate = self._local_continuation_candidate(inspection)
        if candidate is None:
            raise ValueError("Interrupted Job has no verified local recovery envelope")
        work_order.verify()
        if (
            work_order.work_order_id != inspection.work_order_id
            or work_order.content_digest != inspection.work_order_digest
        ):
            raise ValueError("Supplied Work Order does not match the interrupted Job")
        provided = {str(key): str(value) for key, value in source_references.items()}
        expected = dict(candidate["references"])
        authority_digest = expected.get("authority_digest")
        if authority_digest and authority_digest != work_order.authority_snapshot.identity_digest:
            raise ValueError("Supplied Work Order authority does not match recovery envelope")
        for key, value in expected.items():
            if key == "authority_digest":
                continue
            if provided.get(key) != value:
                raise ValueError(f"Recovery source reference does not match: {key}")
        unsafe_runs = tuple(
            run.status
            for run in inspection.runtime_runs
            if run.status not in {RunStatus.SUCCEEDED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value, RunStatus.BUDGET_EXHAUSTED.value}
        )
        if unsafe_runs:
            raise ValueError("Interrupted live Employee run requires explicit operator resolution")
        completed = tuple(
            task["task_id"]
            for task in inspection.reconstructed_tasks
            if task.get("status") == "SUCCEEDED"
        )
        pending = tuple(
            task["task_id"]
            for task in inspection.reconstructed_tasks
            if task.get("status") == "PENDING"
        )
        return ActiveJobRecoveryPreparation(
            job_id=inspection.job_id,
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            graph_version=inspection.final_graph_version,
            completed_task_ids=completed,
            pending_task_ids=pending,
            required_checks=tuple(candidate["required_checks"]),
        )

    def authorize_same_job_fresh_start(
        self,
        job_id: str,
        *,
        request: CompanyRunRequest,
        work_order: WorkOrder,
        source_references: Mapping[str, str],
    ) -> ActiveJobSameJobContinuation:
        """Authorize one same-Job re-entry only before any execution began.

        The public recovery preparation remains useful for a new Job.  This
        stricter path additionally proves that the retained Job has no task
        history to replay, that its action policy is structurally local-read
        only, and that the exact caller-held request still freezes to the
        original snapshot and graph.  It writes no execution record; the
        later Kernel entry consumes the one-shot local receipt atomically.
        """

        preparation = self.prepare_work_order_recovery(
            job_id,
            work_order=work_order,
            source_references=source_references,
        )
        inspection = self.inspect(job_id)
        if request.job_id != inspection.job_id or request.request_id != inspection.request_id:
            raise ValueError("Supplied request identity does not match the interrupted Job")
        if request.work_order_id != work_order.work_order_id:
            raise ValueError("Supplied request Work Order identity does not match")
        if request.work_order_digest != work_order.content_digest:
            raise ValueError("Supplied request Work Order digest does not match")
        if request.requested_effect != "READ" or inspection.requested_effect != "READ":
            raise ValueError("Same-Job continuation is limited to read-only Jobs")
        if not structurally_read_only(request.action_policy):
            raise ValueError("Same-Job continuation requires a structurally read-only action policy")
        if (
            inspection.attempt_count
            or inspection.mutation_count
            or inspection.graph_patch_count
            or inspection.graph_proposal_decisions
        ):
            raise ValueError("Same-Job continuation requires a Job with no execution history")
        if inspection.runtime_runs:
            raise ValueError("Same-Job continuation cannot claim a retained Employee runtime run")
        if preparation.completed_task_ids:
            raise ValueError("Same-Job continuation cannot reuse completed task output")
        lifecycle = self.store.get_job_lifecycle(job_id)
        if lifecycle is None or str(lifecycle["state"]) != "ADMITTED":
            raise ValueError("Same-Job continuation requires an admitted Job lifecycle")
        request_snapshot_hash = frozen_snapshot_digest(request)
        if request_snapshot_hash != inspection.frozen_snapshot_hash:
            raise ValueError("Supplied request no longer matches the frozen Job snapshot")
        graph = graph_from_proposal(
            request.plan_proposal,
            max_tasks=request.job_limits.max_tasks,
        )
        graph_digest = graph_structure_digest(graph)
        if graph_digest != inspection.initial_graph_digest:
            raise ValueError("Supplied request graph does not match the interrupted Job")
        self.store.authorize_same_job_continuation(
            job_id=job_id,
            request_snapshot_hash=request_snapshot_hash,
            graph_digest=graph_digest,
        )
        return ActiveJobSameJobContinuation(
            job_id=inspection.job_id,
            request_id=inspection.request_id,
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            graph_digest=graph_digest,
            required_checks=(
                *preparation.required_checks,
                "zero_execution_history",
                "structural_read_only_policy",
                "exact_frozen_request",
                "exact_initial_graph",
            ),
        )

    def authorize_partial_read_only_continuation(
        self,
        job_id: str,
        *,
        request: CompanyRunRequest,
        work_order: WorkOrder,
        source_references: Mapping[str, str],
    ) -> ActiveJobPartialContinuation:
        """Create a one-shot receipt for proven completed read-only work.

        This intentionally excludes graph mutations, approvals, non-terminal
        runs, and every effect other than local reads.  A later Kernel path may
        reuse only the persisted successful results whose identities and digest
        are frozen here; it must never revive an in-flight runtime instance.
        """

        preparation = self.prepare_work_order_recovery(
            job_id,
            work_order=work_order,
            source_references=source_references,
        )
        inspection = self.inspect(job_id)
        if request.job_id != inspection.job_id or request.request_id != inspection.request_id:
            raise ValueError("Supplied request identity does not match the interrupted Job")
        if request.work_order_id != work_order.work_order_id or request.work_order_digest != work_order.content_digest:
            raise ValueError("Supplied request Work Order does not match")
        if request.requested_effect != "READ" or inspection.requested_effect != "READ":
            raise ValueError("Partial continuation is limited to read-only Jobs")
        if not structurally_read_only(request.action_policy):
            raise ValueError("Partial continuation requires a structurally read-only action policy")
        if inspection.mutation_count or inspection.graph_patch_count or inspection.graph_proposal_decisions:
            raise ValueError("Partial continuation requires an unmodified initial graph")
        lifecycle = self.store.get_job_lifecycle(job_id)
        if lifecycle is None or str(lifecycle["state"]) != "ADMITTED":
            raise ValueError("Partial continuation requires an admitted Job lifecycle")
        request_snapshot_hash = frozen_snapshot_digest(request)
        if request_snapshot_hash != inspection.frozen_snapshot_hash:
            raise ValueError("Supplied request no longer matches the frozen Job snapshot")
        graph = graph_from_proposal(request.plan_proposal, max_tasks=request.job_limits.max_tasks)
        graph_digest = graph_structure_digest(graph)
        if graph_digest != inspection.initial_graph_digest:
            raise ValueError("Supplied request graph does not match the interrupted Job")

        completed: list[tuple[str, str, str]] = []
        for receipt in self.store.list_job_dependency_result_receipts(job_id):
            result = receipt["result"]
            if result.status is not RunStatus.SUCCEEDED:
                continue
            completed.append(
                (
                    str(receipt["task_id"]),
                    str(receipt["attempt_id"]),
                    str(receipt["result_digest"]),
                )
            )
        if not completed:
            raise ValueError("Partial continuation requires at least one completed task receipt")
        if len({task_id for task_id, _, _ in completed}) != len(completed):
            raise ValueError("Partial continuation requires one successful result per task")
        completed_task_ids = tuple(sorted(task_id for task_id, _, _ in completed))
        if tuple(sorted(preparation.completed_task_ids)) != completed_task_ids:
            raise ValueError("Completed result receipts do not match the replayed Job graph")
        # A dependency receipt proves the completed prefix only.  It does not
        # erase a retained Employee run for any other node.  In particular a
        # cancelled/failed final run has already consumed an execution attempt
        # and must start a fresh Job rather than being silently replayed by
        # this narrowly read-only continuation path.
        completed_task_set = set(completed_task_ids)
        replayed_or_unresolved_runs = tuple(
            run
            for run in inspection.runtime_runs
            if (
                run.task_id not in completed_task_set
                or run.status != RunStatus.SUCCEEDED.value
            )
        )
        if replayed_or_unresolved_runs:
            raise ValueError(
                "Partial continuation cannot replay a retained Employee run"
            )
        completed_run_ids = tuple(sorted(run_id for _, run_id, _ in completed))
        completed_results_digest = _digest(tuple(sorted(completed)))
        self.store.authorize_partial_job_continuation(
            job_id=job_id,
            request_snapshot_hash=request_snapshot_hash,
            graph_digest=graph_digest,
            completed_run_ids=completed_run_ids,
            completed_results_digest=completed_results_digest,
        )
        if self.company_coordination is not None:
            try:
                self.company_coordination.authorize_partial_continuation(
                    job_id=job_id,
                    continuation_id=remote_continuation_id(
                        job_id, request_snapshot_hash, graph_digest
                    ),
                    request_snapshot_hash=request_snapshot_hash,
                    graph_digest=graph_digest,
                    completed_attempt_ids=completed_run_ids,
                    completed_results_digest=completed_results_digest,
                )
            except CompanyCoordinationError as error:
                raise RuntimeError("Remote Company continuation authority is unavailable") from error
        return ActiveJobPartialContinuation(
            job_id=inspection.job_id,
            request_id=inspection.request_id,
            work_order_id=work_order.work_order_id,
            work_order_digest=work_order.content_digest,
            graph_digest=graph_digest,
            completed_task_ids=completed_task_ids,
            completed_run_ids=completed_run_ids,
            completed_results_digest=completed_results_digest,
            required_checks=(
                *preparation.required_checks,
                "structural_read_only_policy",
                "zero_graph_mutations",
                "terminal_success_result_receipts",
                "exact_frozen_request",
                "exact_initial_graph",
            ),
        )

    def handoff_partial_read_only_continuation(
        self,
        job_id: str,
        *,
        request: CompanyRunRequest,
        work_order: WorkOrder,
        source_references: Mapping[str, str],
        target_device_id: str,
    ) -> ActiveJobPartialContinuation:
        """Transfer only a proven, unclaimed read-only continuation authority.

        The recipient is required to retain the exact Work Order and local
        dependency receipts already; this method never copies job bodies or
        results.  The source writes its local handoff receipt only after the
        remote compare-and-swap succeeds.
        """

        if self.company_coordination is None:
            raise RuntimeError("Company coordination is required for a device handoff")
        admission = self.authorize_partial_read_only_continuation(
            job_id,
            request=request,
            work_order=work_order,
            source_references=source_references,
        )
        request_snapshot_hash = frozen_snapshot_digest(request)
        self.store.prepare_partial_job_continuation_handoff(
            job_id=job_id,
            target_device_id=target_device_id,
            request_snapshot_hash=request_snapshot_hash,
            graph_digest=admission.graph_digest,
            completed_results_digest=admission.completed_results_digest,
        )
        try:
            self.company_coordination.handoff_partial_continuation(
                job_id=job_id,
                continuation_id=remote_continuation_id(
                    job_id, request_snapshot_hash, admission.graph_digest
                ),
                request_snapshot_hash=request_snapshot_hash,
                graph_digest=admission.graph_digest,
                completed_attempt_ids=admission.completed_run_ids,
                completed_results_digest=admission.completed_results_digest,
                target_device_id=target_device_id,
            )
        except CompanyCoordinationError as error:
            # A definite HTTP rejection means this request did not receive a
            # transfer receipt, so the exact local preparation may be removed.
            # Transport/shape failures remain blocked: their remote outcome is
            # uncertain and must never permit a second source claim.
            if str(error).startswith("Company coordination request was rejected"):
                self.store.cancel_partial_job_continuation_handoff_preparation(
                    job_id=job_id,
                    target_device_id=target_device_id,
                    request_snapshot_hash=request_snapshot_hash,
                    graph_digest=admission.graph_digest,
                    completed_results_digest=admission.completed_results_digest,
                )
            raise RuntimeError("Remote Company continuation handoff is unavailable") from error
        self.store.record_partial_job_continuation_handoff(
            job_id=job_id,
            target_device_id=target_device_id,
            request_snapshot_hash=request_snapshot_hash,
            graph_digest=admission.graph_digest,
            completed_results_digest=admission.completed_results_digest,
        )
        return admission
