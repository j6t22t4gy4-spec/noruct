"""Workflow Patch evaluation CLI adapters."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Callable, Mapping, TextIO

from dynamic_firm.runtime.models import to_primitive

from .evaluation_cli_support import config_table, first_present

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_JOB_FAILED = 4
ProviderFactory = Callable[..., object]


def _run_workflow_patch_cohort_evaluation(
    args: argparse.Namespace,
    output: TextIO,
    *,
    settings: dict | None = None,
    provider_factory: ProviderFactory,
) -> int:
    from dynamic_firm.evaluation.workflow_patch_campaign import (
        WorkflowPatchCohortState,
        apply_workflow_patch_cohort,
        approve_workflow_patch_cohort,
        compare_workflow_patch_cohort,
        prepare_workflow_patch_cohort,
        preview_workflow_patch_cohort,
        rollback_workflow_patch_cohort,
        run_next_workflow_patch_cohort_slot,
        workflow_patch_cohort_status,
    )

    command_name = args.workflow_patch_command
    if command_name == "prepare":
        provider_settings = config_table(settings or {}, "provider")
        command = str(
            first_present(
                args.codex_command,
                os.environ.get("NORUCT_CODEX_COMMAND"),
                provider_settings.get("codex_command"),
                "codex",
            )
        ).strip()
        timeout = float(
            first_present(
                args.request_timeout,
                provider_settings.get("request_timeout"),
                120.0,
            )
        )
        prepared = asyncio.run(
            prepare_workflow_patch_cohort(
                args.directory,
                wheel=args.wheel,
                source_root=args.source_root,
                model=args.model,
                command=command,
                max_model_calls_per_run=args.max_live_model_calls,
                max_model_calls_cohort=args.max_cohort_model_calls,
                max_wall_time_ms_per_run=int(args.max_live_wall_time * 1000),
                lifetime_hours=args.expires_in_hours,
                request_timeout_seconds=timeout,
            )
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(prepared),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch live cohort · "
                f"{'READY' if prepared.preflight.ready else 'BLOCKED'} · "
                f"id={prepared.status.campaign_id}",
                file=output,
            )
            for check in prepared.preflight.checks:
                print(
                    f"[{'PASS' if check.passed else 'FAIL'}] "
                    f"{check.name} · {check.evidence}",
                    file=output,
                )
            print("Provider calls: 0 · quota consumed: no", file=output)
        return EXIT_OK if prepared.preflight.ready else EXIT_INPUT

    if command_name == "status":
        status = workflow_patch_cohort_status(args.directory)
        if args.json:
            print(
                json.dumps(
                    to_primitive(status),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch live cohort · {status.state.value} · "
                f"sealed={status.completed_runs}/{status.expected_runs} · "
                f"calls={status.external_model_calls_recorded}",
                file=output,
            )
            if status.patch_id:
                print(
                    f"Patch: {status.patch_id} · {status.patch_status}",
                    file=output,
                )
            if status.stop_reason:
                print(f"Stopped: {status.stop_reason}", file=output)
            if status.next_strategy:
                print(
                    f"Next: {status.next_strategy} · "
                    f"max calls {status.max_model_calls_for_next_run} · "
                    f"max wall {status.max_wall_time_ms_for_next_run}ms",
                    file=output,
                )
            print(f"Operator action: {status.operator_action}", file=output)
        return (
            EXIT_OK
            if status.state
            not in {
                WorkflowPatchCohortState.BLOCKED,
                WorkflowPatchCohortState.INTERRUPTED,
                WorkflowPatchCohortState.PARTIAL_FAILED,
            }
            else EXIT_JOB_FAILED
        )

    if command_name == "run-next":
        if not args.confirm_live_quota:
            raise ValueError(
                "Workflow Patch cohort requires --confirm-live-quota "
                "for exactly one slot"
            )
        result = asyncio.run(
            run_next_workflow_patch_cohort_slot(
                args.directory,
                confirm_live_quota=True,
                provider_factory=provider_factory,
            )
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(result),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch slot {result.event.fixture} · "
                f"{result.event.kind.value} · next={result.status.state.value}",
                file=output,
            )
            print(
                f"Record: {result.record_path or 'failure envelope preserved'}",
                file=output,
            )
        return (
            EXIT_OK
            if result.record_path is not None and result.task_success
            else EXIT_JOB_FAILED
        )

    if command_name == "patch-preview":
        candidate = preview_workflow_patch_cohort(args.directory)
        if args.json:
            print(
                json.dumps(
                    to_primitive(candidate),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch candidate · {candidate.patch_id} · "
                f"status={candidate.status.value} · "
                f"evidence={candidate.pattern.evidence_count}",
                file=output,
            )
            print(
                f"Expected quality gain: {candidate.expected_quality_gain:+.4f}",
                file=output,
            )
        return EXIT_OK

    if command_name in {"patch-approve", "patch-apply", "rollback"}:
        if not args.confirm:
            raise ValueError(
                f"Workflow Patch {command_name} requires --confirm"
            )
        if command_name == "patch-approve":
            candidate = approve_workflow_patch_cohort(
                args.directory,
                confirm=True,
                actor=args.actor,
            )
        elif command_name == "patch-apply":
            candidate = apply_workflow_patch_cohort(
                args.directory,
                confirm=True,
                actor=args.actor,
            )
        else:
            candidate = rollback_workflow_patch_cohort(
                args.directory,
                confirm=True,
                actor=args.actor,
            )
        if args.json:
            print(
                json.dumps(
                    to_primitive(candidate),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch · {candidate.patch_id} · "
                f"status={candidate.status.value}",
                file=output,
            )
        return EXIT_OK

    report = compare_workflow_patch_cohort(
        args.directory,
        output_path=args.output,
    )
    if args.json:
        print(
            json.dumps(
                to_primitive(report),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=output,
        )
    else:
        print(
            f"Workflow Patch cohort comparison · {report.outcome} · "
            f"gain={report.artifact_quality_gain:+.4f} · "
            f"calls={report.baseline_model_calls}->{report.patched_model_calls}",
            file=output,
        )
        print(f"Next: {report.recommended_direction}", file=output)
        print("Aggregator provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if report.cohort_gate_passed else EXIT_JOB_FAILED


def _run_workflow_patch_extension_evaluation(
    args: argparse.Namespace,
    output: TextIO,
    *,
    settings: dict | None = None,
    provider_factory: ProviderFactory,
) -> int:
    from dynamic_firm.evaluation.workflow_patch_extension import (
        WorkflowPatchExtensionState,
        assess_workflow_patch_extension,
        compare_workflow_patch_extension,
        prepare_workflow_patch_extension,
        rollback_workflow_patch_extension,
        run_next_workflow_patch_extension_slot,
        workflow_patch_extension_status,
    )

    command_name = args.workflow_patch_extension_command
    if command_name == "prepare":
        provider_settings = config_table(settings or {}, "provider")
        command = str(
            first_present(
                args.codex_command,
                os.environ.get("NORUCT_CODEX_COMMAND"),
                provider_settings.get("codex_command"),
                "codex",
            )
        ).strip()
        timeout = float(
            first_present(
                args.request_timeout,
                provider_settings.get("request_timeout"),
                120.0,
            )
        )
        prepared = asyncio.run(
            prepare_workflow_patch_extension(
                args.parent_directory,
                args.directory,
                wheel=args.wheel,
                source_root=args.source_root,
                model=args.model,
                command=command,
                max_model_calls_per_run=args.max_live_model_calls,
                max_model_calls_extension=args.max_extension_model_calls,
                max_wall_time_ms_per_run=int(args.max_live_wall_time * 1000),
                lifetime_hours=args.expires_in_hours,
                request_timeout_seconds=timeout,
            )
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(prepared),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                "Workflow Patch post-apply extension · "
                f"{'READY' if prepared.preflight.ready else 'BLOCKED'} · "
                f"id={prepared.status.extension_id}",
                file=output,
            )
            for check in prepared.preflight.checks:
                print(
                    f"[{'PASS' if check.passed else 'FAIL'}] "
                    f"{check.name} · {check.evidence}",
                    file=output,
                )
            print("Provider calls: 0 · quota consumed: no", file=output)
        return EXIT_OK if prepared.preflight.ready else EXIT_INPUT

    if command_name == "status":
        status = workflow_patch_extension_status(args.directory)
        if args.json:
            print(
                json.dumps(
                    to_primitive(status),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch post-apply extension · {status.state.value} · "
                f"sealed={status.completed_runs}/{status.expected_runs} · "
                f"observations={status.post_apply_observations}/3 · "
                f"calls={status.external_model_calls_recorded}",
                file=output,
            )
            print(
                f"Patch: {status.patch_id} · {status.patch_status} · "
                f"parent immutable={'yes' if status.parent_immutable else 'no'}",
                file=output,
            )
            if status.assessment_decision:
                print(
                    f"Assessment: {status.assessment_decision} · "
                    f"{status.assessment_id}",
                    file=output,
                )
            if status.stop_reason:
                print(f"Stopped: {status.stop_reason}", file=output)
            if status.next_strategy:
                print(
                    f"Next: {status.next_strategy} · "
                    f"max calls {status.max_model_calls_for_next_run} · "
                    f"max wall {status.max_wall_time_ms_for_next_run}ms",
                    file=output,
                )
            print(f"Operator action: {status.operator_action}", file=output)
        return (
            EXIT_OK
            if status.state
            not in {
                WorkflowPatchExtensionState.BLOCKED,
                WorkflowPatchExtensionState.INTERRUPTED,
                WorkflowPatchExtensionState.PARTIAL_FAILED,
            }
            else EXIT_JOB_FAILED
        )

    if command_name == "run-next":
        if not args.confirm_live_quota:
            raise ValueError(
                "Workflow Patch extension requires --confirm-live-quota "
                "for exactly one slot"
            )
        result = asyncio.run(
            run_next_workflow_patch_extension_slot(
                args.directory,
                confirm_live_quota=True,
                provider_factory=provider_factory,
            )
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(result),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch extension slot {result.event.fixture} · "
                f"{result.event.kind.value} · next={result.status.state.value}",
                file=output,
            )
            print(
                f"Record: {result.record_path or 'failure envelope preserved'}",
                file=output,
            )
        return (
            EXIT_OK
            if result.record_path is not None and result.task_success
            else EXIT_JOB_FAILED
        )

    if command_name == "assess":
        assessment = assess_workflow_patch_extension(args.directory)
        if args.json:
            print(
                json.dumps(
                    to_primitive(assessment),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch assessment · {assessment.decision.value} · "
                f"observations={len(assessment.cohort_observation_ids)} · "
                f"quality-gain={assessment.mean_quality_gain} · "
                f"call-savings={assessment.mean_model_call_savings}",
                file=output,
            )
            print("Provider calls: 0 · automatic rollback: no", file=output)
        return (
            EXIT_OK
            if assessment.decision.value == "KEEP"
            else EXIT_JOB_FAILED
        )

    if command_name == "rollback":
        if not args.confirm:
            raise ValueError("Workflow Patch extension rollback requires --confirm")
        patch = rollback_workflow_patch_extension(
            args.directory,
            confirm=True,
            actor=args.actor,
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(patch),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch · {patch.patch_id} · status={patch.status.value}",
                file=output,
            )
        return EXIT_OK

    report = compare_workflow_patch_extension(
        args.directory,
        output_path=args.output,
    )
    if args.json:
        print(
            json.dumps(
                to_primitive(report),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=output,
        )
    else:
        print(
            f"Workflow Patch extension comparison · {report.outcome} · "
            f"quality mean/min={report.mean_artifact_quality:.4f}/"
            f"{report.minimum_artifact_quality:.4f} · "
            f"calls={report.parent_applied_model_calls}->"
            f"{report.extension_mean_model_calls:.4f}",
            file=output,
        )
        print(
            f"Assessment: {report.assessment_decision} · "
            f"repair-free={report.repair_free_count}/3",
            file=output,
        )
        print(f"Next: {report.recommended_direction}", file=output)
        print("Aggregator provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if report.extension_gate_passed else EXIT_JOB_FAILED


def _run_workflow_patch_efficiency_evaluation(
    args: argparse.Namespace,
    output: TextIO,
    *,
    settings: dict | None = None,
    provider_factory: ProviderFactory,
) -> int:
    from dynamic_firm.evaluation.workflow_patch_efficiency import (
        WorkflowPatchEfficiencyState,
        WORKFLOW_PATCH_NATURAL_GOAL,
        compare_workflow_patch_efficiency_pair,
        create_workflow_patch_exact_context_binding,
        evaluate_workflow_patch_natural_preflight,
        prepare_workflow_patch_exact_context_evaluation,
        prepare_workflow_patch_efficiency_pair,
        run_next_workflow_patch_efficiency_slot,
        workflow_patch_efficiency_status,
    )

    command_name = args.workflow_patch_efficiency_command
    if command_name == "bind-context":
        binding = create_workflow_patch_exact_context_binding(
            args.preflight,
            output_path=args.output,
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(binding),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                "Workflow Patch exact-context binding · VERIFIED · "
                f"id={binding.binding_id} · "
                f"context={binding.production_context_fingerprint}",
                file=output,
            )
            print("Provider calls: 0 · quota consumed: no", file=output)
        return EXIT_OK

    if command_name == "prepare-bound":
        preparation = prepare_workflow_patch_exact_context_evaluation(
            args.parent_directory,
            args.binding,
            source_root=args.source_root,
            goal=args.goal or WORKFLOW_PATCH_NATURAL_GOAL,
            output_path=args.output,
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(preparation),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                "Workflow Patch exact-context preparation · READY · "
                f"id={preparation.preparation_id} · "
                f"campaign={preparation.campaign_id}",
                file=output,
            )
            for check in preparation.checks:
                print(
                    f"[{'PASS' if check.passed else 'FAIL'}] "
                    f"{check.name} · {check.evidence}",
                    file=output,
                )
            print("Apply eligible: no · automatic approval: no", file=output)
            print("Provider calls: 0 · quota consumed: no", file=output)
        return EXIT_OK

    if command_name == "prepare":
        provider_settings = config_table(settings or {}, "provider")
        command = str(
            first_present(
                args.codex_command,
                os.environ.get("NORUCT_CODEX_COMMAND"),
                provider_settings.get("codex_command"),
                "codex",
            )
        ).strip()
        timeout = float(
            first_present(
                args.request_timeout,
                provider_settings.get("request_timeout"),
                120.0,
            )
        )
        prepared = asyncio.run(
            prepare_workflow_patch_efficiency_pair(
                args.parent_directory,
                args.directory,
                wheel=args.wheel,
                source_root=args.source_root,
                model=args.model,
                command=command,
                max_model_calls_per_run=args.max_live_model_calls,
                max_model_calls_pair=args.max_pair_model_calls,
                max_wall_time_ms_per_run=int(
                    args.max_live_wall_time * 1000
                ),
                lifetime_hours=args.expires_in_hours,
                request_timeout_seconds=timeout,
                completion_contract_revision=args.completion_contract,
            )
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(prepared),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                "Workflow Patch completion efficiency · "
                f"{'READY' if prepared.preflight.ready else 'BLOCKED'} · "
                f"id={prepared.status.pair_id}",
                file=output,
            )
            for check in prepared.preflight.checks:
                print(
                    f"[{'PASS' if check.passed else 'FAIL'}] "
                    f"{check.name} · {check.evidence}",
                    file=output,
                )
            print("Provider calls: 0 · quota consumed: no", file=output)
        return EXIT_OK if prepared.preflight.ready else EXIT_INPUT

    if command_name == "natural-preflight":
        report = asyncio.run(
            evaluate_workflow_patch_natural_preflight(
                args.parent_directory,
                args.workspace,
                source_root=args.source_root,
                goal=args.goal or WORKFLOW_PATCH_NATURAL_GOAL,
                output_path=args.output,
            )
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(report),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Workflow Patch natural preflight · {report.outcome} · "
                f"manifest={report.workspace_manifest_status} · "
                f"identity={report.workspace_identity_status} · "
                f"priors={len(report.selected_prior_ids)}",
                file=output,
            )
            for check in report.checks:
                print(
                    f"[{'PASS' if check.passed else 'FAIL'}] "
                    f"{check.name} · {check.evidence}",
                    file=output,
                )
            print(f"Next: {report.recommended_direction}", file=output)
            print("Provider calls: 0 · quota consumed: no", file=output)
        return EXIT_OK if report.ready_for_live_observation else EXIT_JOB_FAILED

    if command_name == "status":
        status = workflow_patch_efficiency_status(args.directory)
        if args.json:
            print(
                json.dumps(
                    to_primitive(status),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                "Workflow Patch completion efficiency · "
                f"{status.state.value} · "
                f"sealed={status.completed_runs}/{status.expected_runs} · "
                f"calls={status.external_model_calls_recorded}",
                file=output,
            )
            print(
                "Parent immutable="
                f"{'yes' if status.parent_immutable else 'no'}",
                file=output,
            )
            if status.stop_reason:
                print(f"Stopped: {status.stop_reason}", file=output)
            if status.next_strategy:
                print(
                    f"Next: {status.next_strategy} · "
                    f"max calls {status.max_model_calls_for_next_run} · "
                    f"max wall {status.max_wall_time_ms_for_next_run}ms",
                    file=output,
                )
            print(f"Operator action: {status.operator_action}", file=output)
        return (
            EXIT_OK
            if status.state
            not in {
                WorkflowPatchEfficiencyState.BLOCKED,
                WorkflowPatchEfficiencyState.INTERRUPTED,
                WorkflowPatchEfficiencyState.PARTIAL_FAILED,
            }
            else EXIT_JOB_FAILED
        )

    if command_name == "run-next":
        if not args.confirm_live_quota:
            raise ValueError(
                "Completion efficiency pair requires --confirm-live-quota "
                "for exactly one slot"
            )
        result = asyncio.run(
            run_next_workflow_patch_efficiency_slot(
                args.directory,
                confirm_live_quota=True,
                provider_factory=provider_factory,
            )
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(result),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Completion efficiency slot {result.event.fixture} · "
                f"{result.event.kind.value} · "
                f"next={result.status.state.value}",
                file=output,
            )
            print(
                f"Record: {result.record_path or 'failure envelope preserved'}",
                file=output,
            )
            print(
                "Diagnostic: "
                f"{result.diagnostic_path or 'failure envelope preserved'}",
                file=output,
            )
        return (
            EXIT_OK
            if result.record_path is not None and result.task_success
            else EXIT_JOB_FAILED
        )

    report = compare_workflow_patch_efficiency_pair(
        args.directory,
        output_path=args.output,
    )
    if args.json:
        print(
            json.dumps(
                to_primitive(report),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=output,
        )
    else:
        print(
            f"Completion efficiency comparison · {report.outcome} · "
            f"quality={report.control_quality:.1f}->"
            f"{report.candidate_quality:.1f} · "
            f"calls={report.control_model_calls}->"
            f"{report.candidate_model_calls} · "
            f"repairs={report.control_repairs}->"
            f"{report.candidate_repairs}",
            file=output,
        )
        print(
            f"Tokens: {report.control_total_tokens}->"
            f"{report.candidate_total_tokens} · "
            f"four-call target="
            f"{'yes' if report.target_call_bound_met else 'no'}",
            file=output,
        )
        print(f"Next: {report.recommended_direction}", file=output)
        print("Aggregator provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if report.pair_gate_passed else EXIT_JOB_FAILED
