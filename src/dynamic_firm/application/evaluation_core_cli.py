"""Information-boundary and release-pair evaluation CLI adapters."""

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


def _run_active_job_ledger_evaluation(
    args: argparse.Namespace,
    output: TextIO,
) -> int:
    from dynamic_firm.evaluation.active_job_ledger import (
        run_active_job_ledger_evaluation,
    )

    record = run_active_job_ledger_evaluation()
    if args.json:
        print(
            json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True),
            file=output,
        )
    else:
        print(
            f"ACTIVE JOB ledger: {'PASS' if record.passed else 'FAIL'} · "
            f"retry={record.retry.audit_status.value} · "
            f"reroute={record.reroute.audit_status.value} · "
            f"interrupted={record.interrupted.audit_status.value}",
            file=output,
        )
        print(
            f"Runtime schema v{record.runtime_schema_version} · "
            f"Company schema v{record.company_schema_version} · "
            f"tamper={'detected' if record.tamper_detected else 'missed'}",
            file=output,
        )
        print("Provider calls: 0 · quota consumed: no · automatic resume: disabled", file=output)
        for check in record.checks:
            print(
                f"[{'PASS' if check.passed else 'FAIL'}] "
                f"{check.name} · {check.evidence}",
                file=output,
            )
    return EXIT_OK if record.passed else EXIT_JOB_FAILED


def _run_organization_admission_evaluation(
    args: argparse.Namespace,
    output: TextIO,
) -> int:
    from dynamic_firm.evaluation.organization_admission import (
        run_organization_admission_evaluation,
    )

    record = asyncio.run(run_organization_admission_evaluation())
    if args.json:
        print(
            json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True, indent=2),
            file=output,
        )
    else:
        print(
            f"Organization admission evaluation · "
            f"{sum(item.passed for item in record.records)}/{len(record.records)} passed",
            file=output,
        )
        for item in record.records:
            print(
                f"[{'PASS' if item.passed else 'FAIL'}] {item.fixture_id} · "
                f"compiler={item.compiler_model_calls} · employees={item.employee_count} · "
                f"admission={item.organization_admission_count} · graph=v{item.final_graph_version}",
                file=output,
            )
        print("Provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if record.passed else EXIT_JOB_FAILED


def _run_causal_workflow_evaluation(
    args: argparse.Namespace,
    output: TextIO,
) -> int:
    from dynamic_firm.evaluation.causal_workflow import (
        run_causal_workflow_evaluation,
    )

    record = asyncio.run(run_causal_workflow_evaluation())
    if args.json:
        print(
            json.dumps(
                to_primitive(record),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=output,
        )
    else:
        print(
            f"Causal Workflow Patch evaluation · "
            f"{'PASS' if record.passed else 'FAIL'} · "
            f"cohort={record.cohort_job_count}",
            file=output,
        )
        for check in record.checks:
            print(
                f"[{'PASS' if check.passed else 'FAIL'}] "
                f"{check.name} · {check.evidence}",
                file=output,
            )
        print(
            "Provider calls: 0 · quota consumed: no · "
            "isolated mechanism proof, not production value authorization",
            file=output,
        )
    return EXIT_OK if record.passed else EXIT_JOB_FAILED


def _run_alpha_readiness_evaluation(
    args: argparse.Namespace,
    output: TextIO,
) -> int:
    from dynamic_firm.evaluation.alpha_readiness import (
        run_alpha_readiness_evaluation,
    )

    record = asyncio.run(run_alpha_readiness_evaluation(args.source_root))
    if args.json:
        print(
            json.dumps(
                to_primitive(record),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=output,
        )
    else:
        print(
            f"0.1.0a1 readiness · {record.classification} · "
            f"blocking={len(record.blocking_checks)}",
            file=output,
        )
        for check in record.checks:
            suffix = " · operator required" if check.operator_required else ""
            print(
                f"[{'PASS' if check.passed else 'BLOCK'}] "
                f"{check.name} · {check.evidence}{suffix}",
                file=output,
            )
        print("Provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if record.ready else EXIT_JOB_FAILED


def _run_information_boundary_evaluation(
    args: argparse.Namespace,
    output: TextIO,
) -> int:
    from dynamic_firm.evaluation.information_boundary import (
        create_information_boundary_preflight,
        run_information_boundary_benchmark,
    )

    if args.create_preflight is not None:
        if args.wheel is None or not args.model:
            raise ValueError(
                "Information-boundary preflight requires --wheel and --model"
            )
        record = asyncio.run(
            create_information_boundary_preflight(
                args.create_preflight,
                wheel=args.wheel,
                source_root=args.source_root,
                reserved_model_profile=args.model,
                company_revision=args.company_revision,
                roster_revision=args.roster_revision,
                playbook_revision=args.playbook_revision,
            )
        )
        if args.json:
            print(
                json.dumps(
                    to_primitive(record),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                file=output,
            )
        else:
            print(
                f"Information-boundary preflight · "
                f"{'READY' if record.ready else 'NOT READY'} · "
                f"id={record.benchmark_id}",
                file=output,
            )
            print(
                f"Source={record.source_revision} · wheel={record.distribution_sha256}",
                file=output,
            )
            print(
                f"Provider calls={record.external_provider_calls} · "
                f"quota consumed={'yes' if record.quota_consumed else 'no'}",
                file=output,
            )
        return EXIT_OK if record.ready else EXIT_JOB_FAILED

    if args.wheel is not None or args.model is not None:
        raise ValueError(
            "--wheel and --model are valid only with --create-preflight"
        )
    record = asyncio.run(run_information_boundary_benchmark())
    if args.json:
        print(
            json.dumps(
                to_primitive(record),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=output,
        )
    else:
        print(
            f"Information-boundary benchmark v3 · "
            f"{sum(item.passed for item in record.records)}/{len(record.records)} passed · "
            f"quality gain={record.artifact_quality_gain:.4f}",
            file=output,
        )
        for item in record.records:
            print(
                f"[{'PASS' if item.passed else 'FAIL'}] {item.case.value} · "
                f"compiler={item.admission.compiler_model_calls} · "
                f"employees={item.admission.employee_count} · "
                f"admission={item.admission.organization_admission_count} · "
                f"graph=v{item.admission.final_graph_version}",
                file=output,
            )
        print("Provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if record.passed else EXIT_JOB_FAILED


def _run_information_boundary_v4_evaluation(
    args: argparse.Namespace,
    output: TextIO,
) -> int:
    from dynamic_firm.evaluation.information_boundary_v4 import (
        run_information_boundary_suite,
    )

    record = asyncio.run(
        run_information_boundary_suite(
            company_revision=args.company_revision,
            roster_revision=args.roster_revision,
            playbook_revision=args.playbook_revision,
        )
    )
    if args.json:
        print(
            json.dumps(
                to_primitive(record),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=output,
        )
    else:
        print(
            f"Information-boundary suite v4 · "
            f"{'PASS' if record.passed else 'FAIL'} · "
            f"fixtures={len(record.fixture_gains)}",
            file=output,
        )
        for item in record.fixture_gains:
            print(
                f"[{'PASS' if item.artifact_quality_gain >= 0.2 else 'FAIL'}] "
                f"{item.fixture_id} · "
                f"{item.solo_quality:.4f}→{item.admitted_quality:.4f} · "
                f"gain={item.artifact_quality_gain:.4f} · "
                f"capability={item.capability}",
                file=output,
            )
        print("Provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if record.passed else EXIT_JOB_FAILED


def _run_information_boundary_pair_evaluation(
    args: argparse.Namespace,
    output: TextIO,
    *,
    settings: dict | None = None,
    provider_factory: ProviderFactory,
) -> int:
    from dynamic_firm.evaluation.firm_value_campaign import CampaignState
    from dynamic_firm.evaluation.information_boundary_campaign import (
        compare_information_boundary_pair,
        information_boundary_pair_status,
        prepare_information_boundary_pair,
        run_next_information_boundary_pair_slot,
    )

    if args.pair_command == "prepare":
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
            prepare_information_boundary_pair(
                args.directory,
                preflight=args.preflight,
                wheel=args.wheel,
                source_root=args.source_root,
                command=command,
                max_model_calls_per_run=args.max_live_model_calls,
                max_model_calls_pair=args.max_pair_model_calls,
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
                f"Information-boundary live pair · "
                f"{'READY' if prepared.preflight.ready else 'BLOCKED'} · "
                f"id={prepared.status.benchmark_id}",
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

    if args.pair_command == "status":
        status = information_boundary_pair_status(args.directory)
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
                f"Information-boundary live pair · {status.state.value} · "
                f"sealed={status.completed_runs}/{status.expected_runs} · "
                f"calls={status.external_model_calls_recorded}",
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
                print(
                    "Run only with `--confirm-live-quota`; one confirmation covers one slot.",
                    file=output,
                )
        return (
            EXIT_OK
            if status.state
            not in {
                CampaignState.BLOCKED,
                CampaignState.INTERRUPTED,
                CampaignState.PARTIAL_FAILED,
            }
            else EXIT_JOB_FAILED
        )

    if args.pair_command == "run-next":
        if not args.confirm_live_quota:
            raise ValueError(
                "Information-boundary pair requires --confirm-live-quota for exactly one slot"
            )
        result = asyncio.run(
            run_next_information_boundary_pair_slot(
                args.directory,
                confirm_live_quota=args.confirm_live_quota,
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
                f"Information-boundary slot {result.event.strategy} · "
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

    report = compare_information_boundary_pair(
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
            f"Information-boundary pair comparison · {report.outcome} · "
            f"gain={report.artifact_quality_gain:+.4f} · "
            f"next={report.recommended_direction}",
            file=output,
        )
        print("Aggregator provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if report.pair_gate_passed else EXIT_JOB_FAILED


def _run_release_authorization_pair_evaluation(
    args: argparse.Namespace,
    output: TextIO,
    *,
    settings: dict | None = None,
    provider_factory: ProviderFactory,
) -> int:
    from dynamic_firm.evaluation.firm_value_campaign import CampaignState
    from dynamic_firm.evaluation.release_authorization_campaign import (
        compare_release_authorization_pair,
        prepare_release_authorization_pair,
        release_authorization_pair_status,
        run_next_release_authorization_pair_slot,
    )

    if args.release_pair_command == "prepare":
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
            prepare_release_authorization_pair(
                args.directory,
                wheel=args.wheel,
                source_root=args.source_root,
                model=args.model,
                command=command,
                company_revision=args.company_revision,
                roster_revision=args.roster_revision,
                playbook_revision=args.playbook_revision,
                max_model_calls_per_run=args.max_live_model_calls,
                max_model_calls_pair=args.max_pair_model_calls,
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
                f"Release-authorization live pair · "
                f"{'READY' if prepared.preflight.ready else 'BLOCKED'} · "
                f"id={prepared.status.benchmark_id}",
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

    if args.release_pair_command == "status":
        status = release_authorization_pair_status(args.directory)
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
                f"Release-authorization live pair · {status.state.value} · "
                f"sealed={status.completed_runs}/{status.expected_runs} · "
                f"calls={status.external_model_calls_recorded}",
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
                print(
                    "Run only with `--confirm-live-quota`; "
                    "one confirmation covers one slot.",
                    file=output,
                )
        return (
            EXIT_OK
            if status.state
            not in {
                CampaignState.BLOCKED,
                CampaignState.INTERRUPTED,
                CampaignState.PARTIAL_FAILED,
            }
            else EXIT_JOB_FAILED
        )

    if args.release_pair_command == "run-next":
        if not args.confirm_live_quota:
            raise ValueError(
                "Release-authorization pair requires --confirm-live-quota "
                "for exactly one slot"
            )
        result = asyncio.run(
            run_next_release_authorization_pair_slot(
                args.directory,
                confirm_live_quota=args.confirm_live_quota,
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
                f"Release-authorization slot {result.event.strategy} · "
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

    report = compare_release_authorization_pair(
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
            f"Release-authorization pair comparison · {report.outcome} · "
            f"gain={report.artifact_quality_gain:+.4f} · "
            f"next={report.recommended_direction}",
            file=output,
        )
        print("Aggregator provider calls: 0 · quota consumed: no", file=output)
    return EXIT_OK if report.pair_gate_passed else EXIT_JOB_FAILED
