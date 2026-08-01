"""Application adapter for parsed ACTIVE JOB commands.

``dynamic_firm.cli`` remains the sole parser and process error boundary.  This
module deliberately receives a parsed command and an explicit state path, so
the CLI, terminal UI, and a future GUI can share the same read/mutation
semantics without creating a second ACTIVE JOB authority.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, TextIO

from dynamic_firm.company.graph_blueprint_service import graph_run_record_from_active_job
from dynamic_firm.company.work_order_portfolio import WorkOrderPortfolioStore
from dynamic_firm.company.work_order_portfolio import read_work_order_read_only
from dynamic_firm.application.effect_recovery import resolve_effect_recovery
from dynamic_firm.product.company_commands import parse_operator_timestamp
from dynamic_firm.product.company_coordination_settings import (
    company_coordination_config_from_settings,
)
from dynamic_firm.runtime.company_coordination import RemoteCompanyCoordinationClient
from dynamic_firm.runtime.job_ledger import ActiveJobInspector
from dynamic_firm.runtime.interruption import EffectRecoveryOutcome
from dynamic_firm.runtime.models import to_primitive
from dynamic_firm.runtime.store import RunStore


JOB_COMMAND_OK = 0
JOB_COMMAND_FAILED = 4


def run_job_command(
    args: argparse.Namespace,
    *,
    state_path: Path,
    settings: Mapping[str, object],
    output: TextIO,
) -> int:
    """Execute one already-parsed ACTIVE JOB command.

    No parser, runtime configuration, Work Order, or approval authority is
    reconstructed here.  The existing Store and inspector retain those
    authorities; this adapter only coordinates their public lifecycle APIs and
    renders their public projection.
    """

    path = state_path
    if not path.exists():
        if args.job_command == "list":
            items = ()
        else:
            raise ValueError(f"Unknown ACTIVE JOB: {args.job_id}")
    else:
        store = RunStore(path)
        try:
            # Local inspection and lifecycle commands must remain usable when
            # an opt-in coordination credential is temporarily unavailable.
            # Construct the credential-reading remote client here only for a
            # command that creates a continuation claim. Effect resolution
            # constructs it later only when a durable remote owner exists.
            coordination = None
            if args.job_command == "authorize-read-only-continuation":
                coordination_config = company_coordination_config_from_settings(
                    settings
                )
                coordination = (
                    RemoteCompanyCoordinationClient(coordination_config)
                    if coordination_config is not None
                    else None
                )
            inspector = ActiveJobInspector(store, company_coordination=coordination)
            if args.job_command == "list":
                items = inspector.list(args.limit)
            elif args.job_command == "control":
                if not args.confirm:
                    raise ValueError("Job lifecycle transition requires --confirm")
                lifecycle = store.transition_job_lifecycle(
                    job_id=args.job_id,
                    operation={
                        "defer": "DEFER",
                        "pause": "PAUSE",
                        "resume": "RESUME",
                        "cancel": "CANCEL",
                    }[args.action],
                    reason=args.reason,
                    expected_revision=args.revision,
                )
            elif args.job_command == "settle-unknown":
                if not args.confirm:
                    raise ValueError("Unknown-usage settlement requires --confirm")
                settlement_count = store.forfeit_interrupted_job_lifecycle_leases(
                    job_id=args.job_id,
                    reason=args.reason,
                )
            elif args.job_command == "frozen-run-seal":
                if not args.confirm:
                    raise ValueError("Frozen run sealing requires --confirm")
                inspection = store.inspect_frozen_run_recovery(args.run_id)
                if inspection.run_id != args.run_id:
                    raise ValueError("Frozen run recovery inspection identity mismatch")
                run = store.get_run(args.run_id)
                if run is None or str(run["job_id"]) != args.job_id:
                    raise ValueError("Frozen run does not belong to the selected ACTIVE JOB")
                frozen_seal = store.claim_and_terminalize_frozen_run(
                    args.run_id,
                    expected_binding_digest=args.binding_digest,
                    recovery_id=args.recovery_id,
                    operator_confirmed_abandoned=True,
                )
            elif args.job_command == "effect-resolve":
                if not args.confirm:
                    raise ValueError("Indeterminate effect resolution requires --confirm")
                effect_resolution = resolve_effect_recovery(
                    store,
                    settings=settings,
                    job_id=args.job_id,
                    action_id=args.action_id,
                    outcome={
                        "confirmed-succeeded": EffectRecoveryOutcome.CONFIRMED_SUCCEEDED,
                        "confirmed-no-effect": EffectRecoveryOutcome.CONFIRMED_NO_EFFECT,
                        "compensated": EffectRecoveryOutcome.COMPENSATED,
                        "seal-unknown": EffectRecoveryOutcome.SEALED_UNKNOWN,
                    }[args.outcome],
                    evidence_digest=args.evidence_digest,
                    resolved_by=args.operator_id,
                    reason=args.reason,
                )
            elif args.job_command == "correct":
                if not args.confirm:
                    raise ValueError("User correction requires --confirm")
                correction = store.submit_job_user_correction(
                    job_id=args.job_id,
                    target_task_id=args.task_id,
                    reference=args.reference,
                )
            elif args.job_command == "authorize-read-only-continuation":
                if not args.confirm:
                    raise ValueError(
                        "Read-only partial continuation authorization requires --confirm"
                    )
                portfolio_path = path.with_name(f"{path.stem}.work-orders.db")
                with WorkOrderPortfolioStore(portfolio_path) as work_orders:
                    continuation_request = work_orders.continuation_request(args.job_id)
                    continuation_order = work_orders.work_order(
                        continuation_request.work_order_id
                    )
                    continuation = inspector.authorize_partial_read_only_continuation(
                        args.job_id,
                        request=continuation_request,
                        work_order=continuation_order,
                        source_references={
                            key: value
                            for key, value in {
                                "firm_admission_digest": continuation_request.firm_admission_digest,
                                "workspace_context_fingerprint": continuation_request.workflow_context_fingerprint,
                                "knowledge_pack_digest": (
                                    ""
                                    if continuation_request.execution_origin is None
                                    else continuation_request.execution_origin.pack_digest
                                ),
                            }.items()
                            if value
                        },
                    )
            elif args.job_command == "timeline":
                try:
                    timeline = inspector.timeline(
                        args.job_id,
                        from_at=parse_operator_timestamp(args.timeline_from),
                        to_at=parse_operator_timestamp(args.timeline_to),
                        limit=args.limit,
                    )
                except KeyError as exc:
                    raise ValueError(str(exc).strip("'")) from None
            elif args.job_command == "recovery":
                try:
                    recovery = inspector.recovery_advice(args.job_id)
                except KeyError as exc:
                    raise ValueError(str(exc).strip("'")) from None
            elif args.job_command == "graph":
                try:
                    graph_record = graph_run_record_from_active_job(
                        inspector.inspect(args.job_id)
                    )
                except KeyError as exc:
                    raise ValueError(str(exc).strip("'")) from None
            elif args.job_command == "checkpoints":
                try:
                    checkpoint_history = inspector.checkpoints(args.job_id)
                except KeyError as exc:
                    raise ValueError(str(exc).strip("'")) from None
            elif args.job_command == "summary":
                try:
                    inspection = inspector.inspect(args.job_id)
                except KeyError as exc:
                    raise ValueError(str(exc).strip("'")) from None
            else:
                try:
                    inspection = inspector.inspect(args.job_id)
                except KeyError as exc:
                    raise ValueError(str(exc).strip("'")) from None
        finally:
            store.close()

    if args.job_command == "list":
        if args.json:
            print(json.dumps(to_primitive(items), ensure_ascii=False, sort_keys=True), file=output)
            return JOB_COMMAND_OK
        if not items:
            print("No ACTIVE JOB audits yet.", file=output)
            return JOB_COMMAND_OK
        for item in items:
            terminal = item.job_status or "-"
            print(
                f"{item.job_id}  {item.audit_status.value:<11} {terminal:<16} "
                f"attempts={item.attempt_count} mutations={item.mutation_count} "
                f"graph=v{item.final_graph_version}",
                file=output,
            )
        return JOB_COMMAND_OK

    if args.job_command == "control":
        if args.json:
            print(json.dumps(to_primitive(lifecycle), ensure_ascii=False, sort_keys=True), file=output)
            return JOB_COMMAND_OK
        print(
            f"ACTIVE JOB {args.job_id} lifecycle → {lifecycle['state']} "
            f"(revision {lifecycle['revision']})",
            file=output,
        )
        return JOB_COMMAND_OK

    if args.job_command == "settle-unknown":
        payload = {
            "job_id": args.job_id,
            "forfeited_mutation_lease_count": settlement_count,
            "reusable_capacity": False,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(
                f"ACTIVE JOB {args.job_id}: forfeited {settlement_count} unknown mutation lease(s); capacity is not reusable.",
                file=output,
            )
        return JOB_COMMAND_OK

    if args.job_command == "frozen-run-seal":
        payload = {
            "job_id": args.job_id,
            "run_id": frozen_seal.run_id,
            "status": frozen_seal.status.value,
            "failure_code": "FROZEN_DISPATCHER_ABANDONED",
            "replay": "PROHIBITED",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(
                f"ACTIVE JOB {args.job_id}: frozen run {frozen_seal.run_id[:16]}… sealed without replay.",
                file=output,
            )
        return JOB_COMMAND_OK

    if args.job_command == "effect-resolve":
        if args.json:
            print(
                json.dumps(effect_resolution, ensure_ascii=False, sort_keys=True),
                file=output,
            )
        else:
            release = (
                "resource released for an explicit replacement"
                if effect_resolution["resource_released"]
                else "resource remains sealed; automatic retry is still prohibited"
            )
            print(
                f"ACTIVE JOB {args.job_id}: effect {args.action_id[:16]}… → "
                f"{effect_resolution['outcome']} · {release}.",
                file=output,
            )
        return JOB_COMMAND_OK

    if args.job_command == "correct":
        payload = {
            "job_id": args.job_id,
            "task_id": args.task_id,
            "signal_id": correction["signal_id"],
            "status": correction["status"],
            "delivery": "TASK_RESULT_BOUNDARY",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(
                f"ACTIVE JOB {args.job_id}: queued USER_CORRECTION for {args.task_id}; delivery occurs at that task result boundary.",
                file=output,
            )
        return JOB_COMMAND_OK

    if args.job_command == "authorize-read-only-continuation":
        payload = {
            "job_id": continuation.job_id,
            "request_id": continuation.request_id,
            "work_order_id": continuation.work_order_id,
            "graph_digest": continuation.graph_digest,
            "completed_task_ids": continuation.completed_task_ids,
            "continuation_authority": continuation.continuation_authority,
            "dispatch": "requires ReceiptBoundContinuationService with the selected runtime",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output)
        else:
            print(
                f"ACTIVE JOB {continuation.job_id}: receipt-bound read-only continuation authorized "
                f"for {len(continuation.completed_task_ids)} completed task(s).",
                file=output,
            )
            print(
                "The one-shot receipt is now owned by the explicit continuation runtime entry; "
                "it cannot be replayed through ordinary job submission.",
                file=output,
            )
        return JOB_COMMAND_OK

    if args.job_command == "timeline":
        if args.json:
            print(json.dumps(to_primitive(timeline), ensure_ascii=False, sort_keys=True), file=output)
            return JOB_COMMAND_OK
        cap = " · requested window capped" if timeline.window_capped else ""
        truncation = " · newest events only" if timeline.truncated else ""
        print(
            f"ACTIVE JOB timeline {timeline.job_id} · {timeline.audit_status.value}"
            f" · {timeline.window_from} → {timeline.window_to}{cap}{truncation}",
            file=output,
        )
        usage = timeline.job_usage
        print(
            f"runs={timeline.runtime_run_count} · events={timeline.event_count}/{timeline.event_limit} · "
            f"model_calls={usage.model_calls} · tool_calls={usage.tool_calls} · cost=${usage.cost_usd:.6f}",
            file=output,
        )
        if not timeline.events:
            print("No retained runtime events in this bounded window.", file=output)
        for event in timeline.events:
            usage_delta = ""
            if event.usage_delta is not None:
                usage_delta = (
                    f" · Δmodel={event.usage_delta.model_calls}"
                    f" Δtool={event.usage_delta.tool_calls}"
                    f" Δcost=${event.usage_delta.cost_usd:.6f}"
                )
            terminal = ""
            if event.terminal_summary:
                status = event.terminal_summary.get("status", "")
                failure = event.terminal_summary.get("failure_code", "")
                terminal = f" · terminal={status}" + (f"/{failure}" if failure else "")
            print(
                f"- {event.occurred_at} · {event.event_type} · {event.task_id} · "
                f"{event.employee_id} · run={event.run_id[:16]}{usage_delta}{terminal}",
                file=output,
            )
        return JOB_COMMAND_FAILED if timeline.audit_status.value == "INVALID" else JOB_COMMAND_OK

    if args.job_command == "recovery":
        if args.json:
            print(json.dumps(to_primitive(recovery), ensure_ascii=False, sort_keys=True), file=output)
            return JOB_COMMAND_FAILED if recovery.audit_status.value == "INVALID" else JOB_COMMAND_OK
        print(
            f"ACTIVE JOB recovery {recovery.job_id} · {recovery.recovery_state} "
            f"· {recovery.disposition.value}",
            file=output,
        )
        print(
            "New Kernel attempt required: "
            + ("yes" if recovery.requires_new_kernel_attempt else "no"),
            file=output,
        )
        if recovery.runtime_run_statuses:
            print("Retained runtime statuses: " + ", ".join(recovery.runtime_run_statuses), file=output)
        if recovery.interruption_evidence is not None:
            evidence = recovery.interruption_evidence
            print(
                "Interruption evidence: "
                f"provider-cancellations={evidence.provider_cancellation_receipt_count} · "
                f"incomplete-cancellations={evidence.malformed_provider_cancellation_event_count} · "
                f"timeouts={evidence.timeout_terminal_run_count} · "
                f"nonterminal-runs={evidence.nonterminal_runtime_run_count} · "
                f"causes={','.join(item.value for item in evidence.causes)}",
                file=output,
            )
        if recovery.effect_recovery is not None:
            effect = recovery.effect_recovery
            print(
                f"Effect recovery: {effect.disposition} · completed={len(effect.completed_task_ids)} "
                f"· pending={len(effect.pending_task_ids)}",
                file=output,
            )
            if effect.reason:
                print(f"Effect recovery reason: {effect.reason}", file=output)
        for case in recovery.effect_recovery_cases:
            print(
                f"Effect case: {str(case['action_id'])[:16]}… · {case['case_status']} · "
                f"{case['effect']} · cause={case['cause']} · lease={'held' if case['lease_held'] else 'absent'}",
                file=output,
            )
        for claim in recovery.remote_effect_resource_claims:
            print(
                f"Remote effect claim: {claim['action_id']} · {claim['case_status']} · "
                f"{claim['effect']} · next={claim['next_action']}",
                file=output,
            )
        if recovery.local_continuation_candidate is not None:
            checks = ", ".join(recovery.local_continuation_candidate["required_checks"])
            print("Local continuation envelope: verified metadata only; dispatch remains disabled.", file=output)
            print(f"Required revalidation: {checks}", file=output)
        print("Recommended:", file=output)
        for action in recovery.recommended_actions:
            print(f"- {action}", file=output)
        print("Never automatic:", file=output)
        for action in recovery.prohibited_actions:
            print(f"- {action}", file=output)
        print("Action semantics:", file=output)
        for preview in recovery.action_previews:
            state = "enabled" if preview.enabled else "disabled"
            print(
                f"- {preview.action}: {state} · {preview.expected_effect} {preview.reason}",
                file=output,
            )
        return JOB_COMMAND_FAILED if recovery.audit_status.value == "INVALID" else JOB_COMMAND_OK

    if args.job_command == "graph":
        if args.json:
            print(json.dumps(to_primitive(graph_record), ensure_ascii=False, sort_keys=True), file=output)
            return JOB_COMMAND_OK
        blueprint = (
            f"{graph_record.blueprint_ref.blueprint_id}@{graph_record.blueprint_ref.version}"
            if graph_record.blueprint_ref is not None
            else "unbound"
        )
        print(
            f"Graph Run Record {graph_record.job_id} · Blueprint={blueprint} · "
            f"initial={graph_record.initial_graph_digest[:12]}… · revisions={len(graph_record.revisions)}",
            file=output,
        )
        if not graph_record.revisions:
            print("No accepted topology revision; the initial Graph remained authoritative.", file=output)
        for revision in graph_record.revisions:
            print(
                f"- r{revision.sequence} {revision.operation} · "
                f"{revision.previous_graph_digest[:12]}… → {revision.next_graph_digest[:12]}… · "
                f"lease Δ${revision.budget_delta:.6f} · {revision.approval_policy.value}",
                file=output,
            )
        return JOB_COMMAND_OK

    if args.job_command == "checkpoints":
        if args.json:
            print(json.dumps(to_primitive(checkpoint_history), ensure_ascii=False, sort_keys=True), file=output)
            return JOB_COMMAND_OK
        print(
            f"ACTIVE JOB checkpoints {checkpoint_history.job_id} · "
            f"{checkpoint_history.audit_status.value} · count={checkpoint_history.checkpoint_count}",
            file=output,
        )
        for checkpoint in checkpoint_history.checkpoints:
            parent = checkpoint.parent_checkpoint_id[:18] if checkpoint.parent_checkpoint_id else "root"
            changed = ",".join(checkpoint.changed_task_ids) or "-"
            states = ", ".join(f"{task['task_id']}={task['status']}" for task in checkpoint.task_states)
            print(
                f"- #{checkpoint.ledger_sequence} {checkpoint.event_type} · graph=v{checkpoint.graph_version} · "
                f"changed={changed} · parent={parent} · {states}",
                file=output,
            )
        print("Execution resume from a checkpoint: disabled", file=output)
        return JOB_COMMAND_OK

    if args.job_command == "summary":
        from dynamic_firm.product.execution_summary import execution_summary

        work_order = read_work_order_read_only(
            path.with_name(f"{path.stem}.work-orders.db"),
            inspection.work_order_id,
        )
        summary = execution_summary(inspection, work_order=work_order)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True), file=output)
            return JOB_COMMAND_FAILED if inspection.audit_status.value == "INVALID" else JOB_COMMAND_OK
        result = summary["result"]
        approach = summary["approach"]
        print(
            f"Execution summary {summary['job_id']} · terminal={result['terminal_status']} · "
            f"mode={approach['company_work_mode']}",
            file=output,
        )
        print(f"Purpose: {result['requested_purpose']}", file=output)
        print("Verification:", file=output)
        for item in summary["verification"]:
            print(f"- {item['name']}: {item['status']}", file=output)
        print("Next safe action:", file=output)
        for item in summary["limitations_next"]:
            print(f"- {item['next_action']}", file=output)
        return JOB_COMMAND_FAILED if inspection.audit_status.value == "INVALID" else JOB_COMMAND_OK

    if args.json:
        print(json.dumps(to_primitive(inspection), ensure_ascii=False, sort_keys=True), file=output)
    else:
        # The terminal and optional TUI consume this one strict durable route
        # projection.  The normal inspection lines below remain useful legacy
        # audit detail, but never become a second route/provider authority.
        from dynamic_firm.application.modern_terminal_job_audit import job_audit_snapshot
        from dynamic_firm.product.route_operator_projection import RouteOperatorProjection

        route_surface = job_audit_snapshot(path, inspection.job_id).get(
            "route_operator_projections", ()
        )
        for payload in route_surface if isinstance(route_surface, tuple) else ():
            try:
                projection = RouteOperatorProjection.from_canonical_json(
                    json.dumps(payload, sort_keys=True, separators=(",", ":"))
                )
            except (TypeError, ValueError):
                continue
            print("ROUTE EXECUTION · READ ONLY", file=output)
            for line in projection.render_cli_lines():
                print(f"  {line}", file=output)
        print(
            f"ACTIVE JOB {inspection.job_id} · {inspection.audit_status.value}"
            + (f" · {inspection.job_status}" if inspection.job_status else ""),
            file=output,
        )
        print(
            f"Graph v{inspection.final_graph_version} · attempts={inspection.attempt_count} · "
            f"mutations={inspection.mutation_count} · replay={'match' if inspection.replay_matches else 'mismatch'}",
            file=output,
        )
        print(
            f"Frozen snapshot {inspection.frozen_snapshot_hash[:16]} · chain {inspection.chain_head[:16]}",
            file=output,
        )
        for attempt in inspection.attempts:
            source = attempt.get("source_attempt_id") or "initial"
            print(
                f"- attempt {attempt['sequence']} · {attempt['task_id']} · {attempt['employee_id']} · "
                f"{attempt['status']} · source={source}",
                file=output,
            )
        for mutation in inspection.mutations:
            print(
                f"- {mutation['mutation_type']} · {mutation['task_id']} · "
                f"{mutation['from_employee_id']} → {mutation['to_employee_id']} · "
                f"target attempt {mutation['target_attempt_sequence']}",
                file=output,
            )
        for runtime_run in inspection.runtime_runs:
            approval = f" · approvals={runtime_run.pending_approval_count} pending" if runtime_run.pending_approval_count else ""
            print(
                f"- runtime · {runtime_run.task_id} · {runtime_run.employee_id} · "
                f"{runtime_run.status} · run={runtime_run.run_id[:16]}{approval}",
                file=output,
            )
        for error in inspection.errors:
            print(f"! {error}", file=output)
        print("Automatic resume: disabled", file=output)
    return JOB_COMMAND_FAILED if inspection.audit_status.value == "INVALID" else JOB_COMMAND_OK
