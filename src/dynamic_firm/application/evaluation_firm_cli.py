"""Exact-context and Firm Value evaluation CLI adapters."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Callable, Mapping, TextIO

from dynamic_firm.runtime.models import to_primitive

from .evaluation_cli_support import config_table, first_present, write_evaluation_record

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_JOB_FAILED = 4
ProviderFactory = Callable[..., object]
CodingWorkerFactory = Callable[..., object]


def _run_exact_context_live_pair_evaluation(
    args: argparse.Namespace,
    output: TextIO,
    *,
    settings: dict | None = None,
    provider_factory: ProviderFactory,
) -> int:
    from dynamic_firm.evaluation.exact_context_live_pair import (
        ExactContextLivePairState,
        compare_exact_context_live_pair,
        exact_context_live_pair_status,
        prepare_exact_context_live_pair,
        run_next_exact_context_live_pair_slot,
    )

    command_name = args.exact_context_live_command
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
            prepare_exact_context_live_pair(
                args.parent_directory,
                args.directory,
                binding_path=args.binding,
                preparation_path=args.preparation,
                wheel=args.wheel,
                source_root=args.source_root,
                model=args.model,
                command=command,
                python_command=args.python_command,
                employee_runtime=args.employee_runtime,
                runtime_python=args.runtime_python,
                max_model_calls_per_run=args.max_live_model_calls,
                max_model_calls_pair=args.max_pair_model_calls,
                max_input_tokens_per_run=args.max_input_tokens,
                max_output_tokens_per_run=args.max_output_tokens,
                max_cost_usd_per_run=args.max_cost_usd,
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
                "Exact-context source-frozen live pair · "
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

    if command_name == "status":
        status = exact_context_live_pair_status(args.directory)
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
                "Exact-context source-frozen live pair · "
                f"{status.state.value} · "
                f"sealed={status.completed_runs}/{status.expected_runs} · "
                f"calls={status.external_model_calls_recorded}",
                file=output,
            )
            if status.stop_reason:
                print(f"Stopped: {status.stop_reason}", file=output)
            if status.next_strategy:
                print(
                    f"Next: {status.next_slot}/{status.next_strategy} · "
                    f"max calls {status.max_model_calls_for_next_run} · "
                    f"max wall {status.max_wall_time_ms_for_next_run}ms",
                    file=output,
                )
            print(
                "Quota confirmation: "
                f"{'required' if status.explicit_quota_confirmation_required else 'none'}",
                file=output,
            )
        return (
            EXIT_OK
            if status.state
            not in {
                ExactContextLivePairState.BLOCKED,
                ExactContextLivePairState.INTERRUPTED,
                ExactContextLivePairState.PARTIAL_FAILED,
            }
            else EXIT_JOB_FAILED
        )

    if command_name == "run-next":
        if not args.confirm_live_quota:
            raise ValueError(
                "Exact-context live pair requires --confirm-live-quota for "
                "exactly one slot"
            )
        result = asyncio.run(
            run_next_exact_context_live_pair_slot(
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
                f"Exact-context slot {result.event.fixture} · "
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

    report = compare_exact_context_live_pair(
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
            f"Exact-context pair comparison · {report.outcome} · "
            f"quality={report.control_quality:.1f}->"
            f"{report.candidate_quality:.1f} · "
            f"calls={report.control_model_calls}->"
            f"{report.candidate_model_calls} · "
            f"repairs={report.control_repairs}->"
            f"{report.candidate_repairs}",
            file=output,
        )
        print(
            f"Tokens: {report.control_tokens}->{report.candidate_tokens} · "
            f"proposal={'yes' if report.proposal_recommended else 'no'}",
            file=output,
        )
        print(f"Next: {report.recommended_direction}", file=output)
        print("Aggregator provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if report.pair_gate_passed else EXIT_JOB_FAILED


def _run_firm_value_evaluation(
    args: argparse.Namespace,
    output: TextIO,
) -> int:
    from dynamic_firm.evaluation.firm_value import (
        aggregate_firm_value_records,
        create_firm_value_manifest,
        firm_value_manifest_to_json,
        firm_value_report_to_json,
        run_firm_value_self_test,
        wheel_distribution_sha256,
    )

    if args.create_manifest is not None:
        if args.manifest is not None or args.record:
            raise ValueError("Firm value manifest creation cannot include --manifest or --record")
        if args.wheel is None or not args.source_revision or not args.model:
            raise ValueError(
                "Firm value manifest creation requires --wheel, --source-revision, and --model"
            )
        manifest = create_firm_value_manifest(
            distribution_sha256=wheel_distribution_sha256(args.wheel),
            source_revision=args.source_revision,
            model_id=args.model,
            company_revision=args.company_revision,
            roster_revision=args.roster_revision,
            playbook_revision=args.playbook_revision,
            max_total_model_calls=args.max_live_model_calls,
            max_wall_time_ms=int(args.max_live_wall_time * 1000),
            lifetime_hours=args.expires_in_hours,
        )
        payload = firm_value_manifest_to_json(manifest)
        target = write_evaluation_record(args.create_manifest, payload)
        if args.json:
            print(payload, file=output)
        else:
            print(
                f"Firm Value manifest created · {manifest.benchmark_id} · "
                f"runs={len(manifest.expected_runs)}",
                file=output,
            )
            print(f"Manifest: {target}", file=output)
            print("Provider calls: 0 · quota consumed: no", file=output)
        return EXIT_OK

    if args.manifest is not None:
        if args.wheel is not None or args.source_revision is not None or args.model is not None:
            raise ValueError(
                "Firm value aggregation takes provenance only from --manifest and --record"
            )
        report = aggregate_firm_value_records(args.manifest, args.record)
        payload = firm_value_report_to_json(report)
        if args.json:
            print(payload, file=output)
        else:
            print(
                f"Firm Value Gate · {report.overall_classification} · "
                f"next={report.recommended_direction}",
                file=output,
            )
            for pair in report.pairs:
                print(
                    f"{pair.fixture:<20} {pair.classification:<31} "
                    f"quality={pair.quality_delta:+.4f} "
                    f"calls={pair.external_model_call_delta:+d} "
                    f"elapsed={pair.elapsed_delta_ms:+d}ms",
                    file=output,
                )
            print("Aggregator provider calls: 0 · quota consumed: no", file=output)
        return (
            EXIT_OK
            if report.hard_safety_gate_passed
            and report.organization_gate_passed
            and report.no_validation_downgrade
            else EXIT_JOB_FAILED
        )

    if args.record or args.wheel is not None or args.source_revision is not None or args.model is not None:
        raise ValueError("Firm value records require --manifest")
    record = run_firm_value_self_test()
    if args.json:
        print(
            json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True),
            file=output,
        )
    else:
        print(
            f"Firm Value self-test: {'PASS' if record.passed else 'FAIL'} · "
            f"{record.report.overall_classification}",
            file=output,
        )
        for check in record.checks:
            print(
                f"[{'PASS' if check.passed else 'FAIL'}] "
                f"{check.name} · {check.evidence}",
                file=output,
            )
        print("Provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if record.passed else EXIT_JOB_FAILED


def _run_firm_value_v2_evaluation(
    args: argparse.Namespace,
    output: TextIO,
) -> int:
    from dynamic_firm.evaluation.firm_value_v2 import (
        firm_value_v2_to_json,
        run_firm_value_v2_self_test,
    )

    record = asyncio.run(run_firm_value_v2_self_test())
    if args.json:
        print(firm_value_v2_to_json(record), file=output)
    else:
        print(
            f"Firm Value v2: {'PASS' if record.passed else 'FAIL'} · "
            f"{record.report.overall_classification}",
            file=output,
        )
        for pair in record.report.pairs:
            denominator = "value" if pair.included_in_gain_denominator else "control"
            print(
                f"{pair.fixture.value:<24} {pair.classification:<20} "
                f"artifact={pair.artifact_quality_delta:+.4f} · {denominator}",
                file=output,
            )
        print(
            "Artifact quality, safety, organization, and cost are separate projections.",
            file=output,
        )
        print(
            "Provider calls: 0 · quota consumed: no · offline contract only, not live value evidence",
            file=output,
        )
    return EXIT_OK if record.passed else EXIT_JOB_FAILED


def _run_firm_value_campaign_evaluation(
    args: argparse.Namespace,
    output: TextIO,
    *,
    settings: dict | None = None,
    provider_factory: ProviderFactory,
    coding_worker_factory: CodingWorkerFactory,
) -> int:
    from dynamic_firm.evaluation.firm_value_campaign import (
        CampaignState,
        campaign_status,
        compare_campaign,
        prepare_firm_value_campaign,
        run_next_campaign_slot,
    )

    if args.campaign_command == "prepare":
        provider_settings = config_table(settings or {}, "provider")
        command = str(
            first_present(
                args.codex_command,
                os.environ.get("NORUCT_CODEX_COMMAND"),
                provider_settings.get("codex_command"),
                "codex",
            )
        ).strip()
        model_value = first_present(
            args.model,
            os.environ.get("NORUCT_MODEL"),
            provider_settings.get("model"),
        )
        model = str(model_value).strip() if model_value is not None else ""
        if not model:
            raise ValueError("Firm campaign preparation requires an explicit --model")
        timeout = float(
            first_present(
                args.request_timeout,
                provider_settings.get("request_timeout"),
                120.0,
            )
        )
        prepared = asyncio.run(
            prepare_firm_value_campaign(
                args.directory,
                wheel=args.wheel,
                source_root=args.source_root,
                command=command,
                model_id=model,
                company_revision=args.company_revision,
                roster_revision=args.roster_revision,
                playbook_revision=args.playbook_revision,
                max_total_model_calls=args.max_live_model_calls,
                max_wall_time_ms=int(args.max_live_wall_time * 1000),
                lifetime_hours=args.expires_in_hours,
                request_timeout_seconds=timeout,
            )
        )
        if args.json:
            print(
                json.dumps(to_primitive(prepared), ensure_ascii=False, sort_keys=True, indent=2),
                file=output,
            )
        else:
            print(
                f"Firm Value campaign prepared · {prepared.status.benchmark_id} · "
                f"{'READY' if prepared.preflight.ready else 'BLOCKED'}",
                file=output,
            )
            for check in prepared.preflight.checks:
                print(
                    f"[{'PASS' if check.passed else 'FAIL'}] {check.name} · {check.evidence}",
                    file=output,
                )
            print("Provider calls: 0 · quota consumed: no", file=output)
        return EXIT_OK if prepared.preflight.ready else EXIT_INPUT

    if args.campaign_command == "status":
        status = campaign_status(args.directory)
        if args.json:
            print(
                json.dumps(to_primitive(status), ensure_ascii=False, sort_keys=True, indent=2),
                file=output,
            )
        else:
            print(
                f"Firm Value campaign · {status.state.value} · "
                f"sealed={status.completed_runs}/{status.expected_runs} · "
                f"events={status.event_count}",
                file=output,
            )
            if status.next_fixture and status.next_strategy:
                print(
                    f"Next: {status.next_fixture}/{status.next_strategy} · "
                    f"max calls {status.max_model_calls_for_next_run} · "
                    f"max wall {status.max_wall_time_ms_for_next_run}ms",
                    file=output,
                )
                print("Run only with `--confirm-live-quota`; one confirmation covers one slot.", file=output)
        return EXIT_OK if status.state not in {
            CampaignState.BLOCKED,
            CampaignState.INTERRUPTED,
            CampaignState.PARTIAL_FAILED,
        } else EXIT_JOB_FAILED

    if args.campaign_command == "run-next":
        result = asyncio.run(
            run_next_campaign_slot(
                args.directory,
                confirm_live_quota=args.confirm_live_quota,
                provider_factory=provider_factory,
                coding_worker_factory=coding_worker_factory,
            )
        )
        if args.json:
            print(
                json.dumps(to_primitive(result), ensure_ascii=False, sort_keys=True, indent=2),
                file=output,
            )
        else:
            print(
                f"Campaign slot {result.event.fixture}/{result.event.strategy} · "
                f"{result.event.kind.value} · next={result.status.state.value}",
                file=output,
            )
            print(f"Record: {result.record_path or 'failure envelope preserved'}", file=output)
        return EXIT_OK if result.record_path is not None and result.task_success else EXIT_JOB_FAILED

    report = compare_campaign(args.directory, output_path=args.output)
    if args.json:
        print(
            json.dumps(to_primitive(report), ensure_ascii=False, sort_keys=True, indent=2),
            file=output,
        )
    else:
        print(
            f"Firm Value campaign comparison · {report.outcome} · "
            f"next={report.recommended_direction}",
            file=output,
        )
        print("Aggregator provider calls: 0 · quota consumed: no", file=output)
    return (
        EXIT_OK
        if report.campaign_gate_passed
        else EXIT_JOB_FAILED
    )


def _run_firm_value_campaign_v2_evaluation(
    args: argparse.Namespace,
    output: TextIO,
    *,
    settings: dict | None = None,
    provider_factory: ProviderFactory,
    coding_worker_factory: CodingWorkerFactory,
) -> int:
    from dynamic_firm.evaluation.firm_value_campaign import CampaignState
    from dynamic_firm.evaluation.firm_value_campaign_v2 import (
        campaign_v2_status,
        compare_campaign_v2,
        prepare_firm_value_campaign_v2,
        run_next_campaign_v2_slot,
    )

    if args.campaign_command == "prepare":
        provider_settings = config_table(settings or {}, "provider")
        command = str(
            first_present(
                args.codex_command,
                os.environ.get("NORUCT_CODEX_COMMAND"),
                provider_settings.get("codex_command"),
                "codex",
            )
        ).strip()
        model_value = first_present(
            args.model,
            os.environ.get("NORUCT_MODEL"),
            provider_settings.get("model"),
        )
        model = str(model_value).strip() if model_value is not None else ""
        if not model:
            raise ValueError("Firm campaign v2 preparation requires an explicit --model")
        timeout = float(
            first_present(
                args.request_timeout,
                provider_settings.get("request_timeout"),
                120.0,
            )
        )
        prepared = asyncio.run(
            prepare_firm_value_campaign_v2(
                args.directory,
                wheel=args.wheel,
                source_root=args.source_root,
                command=command,
                model_id=model,
                company_revision=args.company_revision,
                roster_revision=args.roster_revision,
                playbook_revision=args.playbook_revision,
                max_total_model_calls=args.max_live_model_calls,
                max_wall_time_ms=int(args.max_live_wall_time * 1000),
                lifetime_hours=args.expires_in_hours,
                request_timeout_seconds=timeout,
            )
        )
        if args.json:
            print(
                json.dumps(to_primitive(prepared), ensure_ascii=False, sort_keys=True, indent=2),
                file=output,
            )
        else:
            print(
                f"Firm Value campaign v2 prepared · {prepared.status.benchmark_id} · "
                f"{'READY' if prepared.preflight.ready else 'BLOCKED'}",
                file=output,
            )
            for check in prepared.preflight.checks:
                print(
                    f"[{'PASS' if check.passed else 'FAIL'}] {check.name} · {check.evidence}",
                    file=output,
                )
            print("Provider calls: 0 · quota consumed: no", file=output)
            print(
                "Evaluator: clean environment only; no OS sandbox and no network isolation. "
                "Every slot requires separate risk confirmation.",
                file=output,
            )
        return EXIT_OK if prepared.preflight.ready else EXIT_INPUT

    if args.campaign_command == "status":
        status = campaign_v2_status(args.directory)
        if args.json:
            print(
                json.dumps(to_primitive(status), ensure_ascii=False, sort_keys=True, indent=2),
                file=output,
            )
        else:
            print(
                f"Firm Value campaign v2 · {status.state.value} · "
                f"sealed={status.completed_runs}/{status.expected_runs} · "
                f"events={status.event_count}",
                file=output,
            )
            if status.stop_reason:
                print(f"Stopped: {status.stop_reason}", file=output)
            if status.next_fixture and status.next_strategy:
                print(
                    f"Next: {status.next_fixture}/{status.next_strategy} · "
                    f"max calls {status.max_model_calls_for_next_run} · "
                    f"max wall {status.max_wall_time_ms_for_next_run}ms",
                    file=output,
                )
                print(
                    "Run with both `--confirm-live-quota` and "
                    "`--confirm-evaluator-risk`; confirmations cover one slot only.",
                    file=output,
                )
                print(
                    "Risk: candidate Python has a clean environment, but no OS sandbox or "
                    "network isolation.",
                    file=output,
                )
        return EXIT_OK if status.state not in {
            CampaignState.BLOCKED,
            CampaignState.INTERRUPTED,
            CampaignState.PARTIAL_FAILED,
        } else EXIT_JOB_FAILED

    if args.campaign_command == "run-next":
        if not args.confirm_live_quota:
            raise ValueError(
                "Firm campaign v2 requires --confirm-live-quota for exactly one slot"
            )
        if not args.confirm_evaluator_risk:
            raise ValueError(
                "Firm campaign v2 requires --confirm-evaluator-risk because candidate "
                "execution has no OS sandbox or network isolation"
            )
        result = asyncio.run(
            run_next_campaign_v2_slot(
                args.directory,
                confirm_live_quota=True,
                confirm_evaluator_risk=True,
                provider_factory=provider_factory,
                coding_worker_factory=coding_worker_factory,
            )
        )
        if args.json:
            print(
                json.dumps(to_primitive(result), ensure_ascii=False, sort_keys=True, indent=2),
                file=output,
            )
        else:
            print(
                f"Campaign v2 slot {result.event.fixture}/{result.event.strategy} · "
                f"{result.event.kind.value} · next={result.status.state.value}",
                file=output,
            )
            print(f"Record: {result.record_path or 'failure envelope preserved'}", file=output)
        return EXIT_OK if result.record_path is not None and result.task_success else EXIT_JOB_FAILED

    report = compare_campaign_v2(args.directory, output_path=args.output)
    if args.json:
        print(
            json.dumps(to_primitive(report), ensure_ascii=False, sort_keys=True, indent=2),
            file=output,
        )
    else:
        print(
            f"Firm Value campaign v2 comparison · {report.outcome} · "
            f"next={report.recommended_direction}",
            file=output,
        )
        print("Aggregator provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if report.campaign_gate_passed else EXIT_JOB_FAILED
