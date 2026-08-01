"""Coding-evaluation command adapter bound to the CLI composition root.

The module deliberately receives its CLI dependencies from ``dynamic_firm.cli``
after the parser and shared helpers are initialized.  This keeps evaluation
execution separate from command routing without introducing a second state
authority.
"""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli


def _current_isatty(stream):
    from dynamic_firm.cli import _isatty

    return _isatty(stream)


def _run_tui_acceptance_evaluation(args, output):
    from dynamic_firm.evaluation.tui_acceptance import (
        TuiAcceptanceScenario,
        run_tui_acceptance,
    )

    scenarios = (
        tuple(TuiAcceptanceScenario)
        if args.scenario == "all"
        else (TuiAcceptanceScenario(args.scenario),)
    )
    records = tuple(
        run_tui_acceptance(
            scenario,
            width=args.width,
            plain=args.plain,
            color=_current_isatty(output) and not args.plain and not args.json,
        )
        for scenario in scenarios
    )
    if args.json:
        print(
            cli.json.dumps(cli.to_primitive(records), ensure_ascii=False, sort_keys=True, indent=2),
            file=output,
        )
    else:
        print(
            "Noruct TUI acceptance preview · offline · no provider call · no mutation",
            file=output,
        )
        for record in records:
            print(f"\n===== {record.scenario.value.upper()} =====\n", file=output)
            print(record.rendered, end="" if record.rendered.endswith("\n") else "\n", file=output)
            for check in record.checks:
                print(
                    f"[{'PASS' if check.passed else 'FAIL'}] {check.name} · {check.evidence}",
                    file=output,
                )
        print(
            "\nHuman review remains required: confirm hierarchy and readability in your terminal.",
            file=output,
        )
        print("Quota consumed: no", file=output)
    return cli.EXIT_OK if all(record.machine_passed for record in records) else cli.EXIT_JOB_FAILED


def _live_coding_config(args, settings):
    from dynamic_firm.evaluation.closed_loop import LiveCodingEvaluationConfig

    provider_settings = cli._table(settings, "provider")
    command = str(
        cli._first(
            args.codex_command,
            cli.os.environ.get("NORUCT_CODEX_COMMAND"),
            provider_settings.get("codex_command"),
            "codex",
        )
    ).strip()
    model_value = cli._first(
        args.model,
        cli.os.environ.get("NORUCT_MODEL"),
        provider_settings.get("model"),
    )
    model = (str(model_value).strip() or None) if model_value is not None else None
    timeout = float(
        cli._first(
            args.request_timeout,
            provider_settings.get("request_timeout"),
            120.0,
        )
    )
    source_revision = str(
        cli._first(
            args.source_revision,
            cli.os.environ.get("NORUCT_SOURCE_REVISION"),
            "uncommitted-or-unknown",
        )
    ).strip() or "uncommitted-or-unknown"
    distribution_sha256 = ""
    if args.wheel is not None:
        from dynamic_firm.evaluation.firm_value import wheel_distribution_sha256

        distribution_sha256 = wheel_distribution_sha256(args.wheel)
    return LiveCodingEvaluationConfig(
        command=command,
        model=model,
        timeout_seconds=timeout,
        source_revision=source_revision,
        max_total_model_calls=args.max_live_model_calls,
        max_wall_time_ms=int(args.max_live_wall_time * 1000),
        quota_confirmed=bool(args.confirm_live_quota),
        company_revision=args.company_revision,
        roster_revision=args.roster_revision,
        playbook_revision=args.playbook_revision,
        distribution_sha256=distribution_sha256,
    )


def _run_coding_evaluation(
    args,
    output,
    *,
    settings=None,
    provider_factory=None,
    coding_worker_factory=None,
):
    from dynamic_firm.evaluation.closed_loop import (
        CodingStrategyKind,
        closed_loop_records_to_json,
        live_coding_preflight_to_json,
        live_coding_record_to_json,
        run_closed_loop_evaluation,
        run_live_coding_preflight,
        run_live_coding_evaluation,
    )
    from dynamic_firm.evaluation.coding import CodingFixtureKind

    if provider_factory is None:
        provider_factory = cli._default_provider
    if coding_worker_factory is None:
        coding_worker_factory = cli._default_coding_worker
    if args.live and args.preflight_live:
        raise ValueError("Use either --live or --preflight-live, not both")
    if args.confirm_live_quota and not args.live:
        raise ValueError("--confirm-live-quota is valid only with --live")
    if args.preflight_live:
        if args.fixture != "parallel-evidence" or args.strategy != "dynamic":
            raise ValueError("Live preflight requires parallel-evidence with --strategy dynamic")
        if args.output is None:
            raise ValueError("Live preflight requires --output so the readiness record is preserved")
        preflight = cli.asyncio.run(
            run_live_coding_preflight(
                _live_coding_config(args, settings or {}),
                args.fixture,
                args.strategy,
            )
        )
        payload = live_coding_preflight_to_json(preflight)
        target = cli._write_evaluation_record(args.output, payload)
        if args.json:
            print(payload, file=output)
        else:
            print(
                f"parallel-evidence/dynamic preflight: "
                f"{'READY' if preflight.ready else 'BLOCKED'}",
                file=output,
            )
            for check in preflight.checks:
                print(
                    f"[{'PASS' if check.passed else 'FAIL'}] {check.name} · {check.evidence}",
                    file=output,
                )
            print(f"Record: {target}", file=output)
            print("Quota consumed: no · external model calls: 0", file=output)
            print("This readiness record is not live evidence.", file=output)
        return cli.EXIT_OK if preflight.ready else cli.EXIT_INPUT
    if args.live:
        if not args.confirm_live_quota:
            raise ValueError("Live evaluation requires --confirm-live-quota")
        if args.fixture == "all" or args.strategy == "all":
            raise ValueError("Live evaluation requires exactly one fixture and one strategy")
        if args.output is None:
            raise ValueError("Live evaluation requires --output so the evidence record is preserved")
        live_record = cli.asyncio.run(
            run_live_coding_evaluation(
                _live_coding_config(args, settings or {}),
                args.fixture,
                args.strategy,
                provider_factory=provider_factory,
                coding_worker_factory=coding_worker_factory,
            )
        )
        payload = live_coding_record_to_json(live_record)
        target = cli._write_evaluation_record(args.output, payload)
        if args.json:
            print(payload, file=output)
        else:
            result = live_record.result
            print(
                f"{result.fixture.value}/{result.strategy.value}: {result.status.value} "
                f"task_success={str(result.score.task_success).lower()} "
                f"employees={result.trajectory.employee_count} "
                f"parallelism={result.trajectory.maximum_parallelism} "
                f"external_calls={live_record.external_model_calls} "
                f"elapsed_ms={live_record.elapsed_ms}",
                file=output,
            )
            print(f"Record: {target}", file=output)
            print(f"Evaluation run: {live_record.evaluation_run_id}", file=output)
            print("Subscription USD cost: unavailable; no estimate was invented.", file=output)
        return (
            cli.EXIT_OK
            if live_record.result.status == cli.JobStatus.SUCCEEDED
            and live_record.result.score.task_success
            else cli.EXIT_JOB_FAILED
        )

    fixtures = tuple(CodingFixtureKind) if args.fixture == "all" else (CodingFixtureKind(args.fixture),)
    strategies = tuple(CodingStrategyKind) if args.strategy == "all" else (CodingStrategyKind(args.strategy),)

    async def evaluate():
        return tuple(
            [
                await run_closed_loop_evaluation(fixture, strategy)
                for fixture in fixtures
                for strategy in strategies
            ]
        )

    records = cli.asyncio.run(evaluate())
    payload = closed_loop_records_to_json(records)
    if args.output is not None:
        cli._write_evaluation_record(args.output, payload)
    if args.json:
        print(payload, file=output)
    else:
        print("fixture / strategy                  job       score  staff  parallel  approval  validation", file=output)
        for record in records:
            label = f"{record.fixture.value} / {record.strategy.value}"
            attempts = "→".join("pass" if item else "fail" for item in record.trajectory.validation_attempts)
            approval = f"{record.trajectory.approvals_granted}/{record.trajectory.approvals_requested}"
            print(
                f"{label:<35} {record.status.value:<9} "
                f"{record.score.quality_score:>5.4f}  "
                f"{record.trajectory.employee_count:>2}     "
                f"{record.trajectory.maximum_parallelism:>2}        "
                f"{approval:<8}  {attempts}",
                file=output,
            )
        print("Trajectory source: append-only runtime ledger; no model credentials or network used.", file=output)
    healthy = all(
        record.status == cli.JobStatus.SUCCEEDED
        and record.score.task_success
        and record.ledger_matches_kernel
        and record.workspace_unchanged_before_approval
        for record in records
    )
    return cli.EXIT_OK if healthy else cli.EXIT_JOB_FAILED
