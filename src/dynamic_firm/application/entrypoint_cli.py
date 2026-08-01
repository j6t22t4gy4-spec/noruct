"""Top-level command routing bound by the CLI composition root."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

import sys

def main(
    argv: Sequence[str] | None = None,
    *,
    provider_factory: ProviderFactory | None = None,
    coding_worker_factory: CodingWorkerFactory | None = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    if provider_factory is None:
        provider_factory = cli._default_provider
    if coding_worker_factory is None:
        coding_worker_factory = cli._default_coding_worker
    parser = cli.build_parser()
    try:
        normalized = cli._normalize_argv(argv)
        if not normalized and cli._isatty(stdin) and cli._isatty(stdout):
            normalized = ["chat"]
        args = parser.parse_args(normalized)
        if args.command is None:
            parser.print_help(file=stdout)
            return cli.EXIT_OK
        if args.command == "demo":
            return cli._run_demo(args, stdout)
        if args.command == "eval":
            if args.evaluation == "tui":
                return cli._run_tui_acceptance_evaluation(args, stdout)
            if args.evaluation == "manager-campaign":
                from dynamic_firm.evaluation.manager_value_campaign import (
                    create_manager_value_campaign_report,
                    manager_value_campaign_status,
                    preflight_manager_value_campaign,
                    prepare_manager_value_campaign,
                    run_next_manager_value_slot,
                    seal_next_manager_value_slot,
                )
                if args.manager_campaign_command == "prepare":
                    status = prepare_manager_value_campaign(
                        args.directory,
                        wheel=args.wheel,
                        source_root=args.source_root,
                        model_id=args.model,
                        company_revision=args.company_revision,
                        roster_revision=args.roster_revision,
                        playbook_revision=args.playbook_revision,
                        max_total_model_calls=args.max_live_model_calls,
                        max_wall_time_ms=int(args.max_live_wall_time * 1000),
                        codex_command=args.codex_command,
                        request_timeout_seconds=args.request_timeout,
                    )
                elif args.manager_campaign_command == "seal-next":
                    status = seal_next_manager_value_slot(
                        args.directory,
                        record_path=args.record,
                        confirm_live_quota=args.confirm_live_quota,
                        confirm_evaluator_risk=args.confirm_evaluator_risk,
                    )
                elif args.manager_campaign_command == "run-next":
                    status = cli.asyncio.run(
                        run_next_manager_value_slot(
                            args.directory,
                            confirm_live_quota=args.confirm_live_quota,
                            confirm_evaluator_risk=args.confirm_evaluator_risk,
                        )
                    )
                elif args.manager_campaign_command == "report":
                    report = create_manager_value_campaign_report(
                        args.directory,
                        output_path=args.output,
                    )
                    if args.json:
                        print(cli.json.dumps(cli.to_primitive(report), ensure_ascii=False, sort_keys=True, indent=2), file=stdout)
                    else:
                        print(
                            "Manager value campaign report · qualified evidence · no outcome claim",
                            file=stdout,
                        )
                        for outcome in report.outcomes:
                            print(
                                f"{outcome.arm}: lower-decile quality={outcome.lower_decile_quality:.3f} "
                                f"failure={outcome.complete_failure_rate:.3f} safety={outcome.safety_failure_rate:.3f} "
                                f"calls={outcome.mean_model_calls:.2f} "
                                f"approvals={outcome.mean_approvals_granted:.2f}/"
                                f"{outcome.mean_approvals_requested:.2f} "
                                + (
                                    f"cost=${outcome.mean_reported_cost_usd:.6f}"
                                    if outcome.cost_accounting_mode == "REPORTED_USD"
                                    else "cost=model-call-proxy"
                                ),
                                file=stdout,
                            )
                    return cli.EXIT_OK
                elif args.manager_campaign_command == "preflight":
                    preflight = preflight_manager_value_campaign(args.directory)
                    if args.json:
                        print(cli.json.dumps(cli.to_primitive(preflight), ensure_ascii=False, sort_keys=True, indent=2), file=stdout)
                    else:
                        label = "ready" if preflight.ready else "blocked"
                        print(
                            f"Manager value campaign preflight · {label} · "
                            f"state={preflight.state.value} · model={preflight.model_id}",
                            file=stdout,
                        )
                        for check in preflight.checks:
                            mark = "ok" if check.passed else "blocked"
                            print(f"{mark:7} {check.name}: {check.evidence}", file=stdout)
                        if preflight.ready and preflight.next_fixture and preflight.next_arm:
                            print(
                                f"Next: {preflight.next_fixture}/{preflight.next_arm} · "
                                "run-next still requires fresh quota and evaluator-risk confirmation",
                                file=stdout,
                            )
                    return cli.EXIT_OK
                elif args.manager_campaign_command == "rehearse":
                    from dynamic_firm.evaluation.manager_value_live import (
                        run_manager_value_offline_rehearsal,
                    )

                    rehearsal = cli.asyncio.run(run_manager_value_offline_rehearsal())
                    if args.json:
                        print(
                            cli.json.dumps(
                                cli.to_primitive(rehearsal),
                                ensure_ascii=False,
                                sort_keys=True,
                                indent=2,
                            ),
                            file=stdout,
                        )
                    else:
                        print(
                            "Manager value campaign rehearsal · "
                            f"{'passed' if rehearsal.passed else 'failed'} · "
                            f"slots={len(rehearsal.outcomes)} · quota=0",
                            file=stdout,
                        )
                    return cli.EXIT_OK
                else:
                    status = manager_value_campaign_status(args.directory)
                if args.json:
                    print(cli.json.dumps(cli.to_primitive(status), ensure_ascii=False, sort_keys=True, indent=2), file=stdout)
                else:
                    print(
                        f"Manager value campaign · {status.state.value} · "
                        f"sealed={status.completed_runs}/{status.expected_runs} · "
                        f"failed={status.failed_runs} · interrupted={status.interrupted_runs} · "
                        f"calls={status.external_model_calls_recorded}",
                        file=stdout,
                    )
                    if status.next_fixture and status.next_arm:
                        print(f"Next: {status.next_fixture}/{status.next_arm} · explicit quota and evaluator-risk confirmation required", file=stdout)
                return cli.EXIT_OK
            if args.evaluation == "manager-value-contract":
                from dynamic_firm.evaluation.manager_value_contract import (
                    manager_value_qualification_contract,
                )

                payload = cli.to_primitive(manager_value_qualification_contract())
                if args.json:
                    print(cli.json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stdout)
                else:
                    print(
                        "Manager value qualification · "
                        f"{len(payload['fixtures'])} fixtures × {len(payload['arms'])} arms = "
                        f"{len(payload['exact_slots'])} sealed slots",
                        file=stdout,
                    )
                    print("Arms: " + " · ".join(payload["arms"]), file=stdout)
                    print("Frozen: " + " · ".join(payload["frozen_dimensions"]), file=stdout)
                    print(
                        "Status: in-process executor available; no live outcome is claimed.",
                        file=stdout,
                    )
                return cli.EXIT_OK
            if args.evaluation == "company":
                return cli._run_company_learning_evaluation(args, stdout)
            if args.evaluation == "observation":
                return cli._run_patch_observation_evaluation(args, stdout)
            if args.evaluation == "roster":
                return cli._run_roster_patch_evaluation(args, stdout)
            if args.evaluation == "hiring":
                return cli._run_hiring_evaluation(args, stdout)
            if args.evaluation == "hire-observation":
                return cli._run_hire_observation_evaluation(args, stdout)
            if args.evaluation == "retention-review":
                return cli._run_retention_review_evaluation(args, stdout)
            if args.evaluation == "employee-skill":
                return cli._run_employee_skill_evaluation(args, stdout)
            if args.evaluation == "task-mutation":
                return cli._run_task_mutation_evaluation(args, stdout)
            if args.evaluation == "active-job-ledger":
                return cli._run_active_job_ledger_evaluation(args, stdout)
            if args.evaluation == "organization-admission":
                return cli._run_organization_admission_evaluation(args, stdout)
            if args.evaluation == "causal-workflow":
                return cli._run_causal_workflow_evaluation(args, stdout)
            if args.evaluation == "alpha-readiness":
                return cli._run_alpha_readiness_evaluation(args, stdout)
            if args.evaluation == "information-boundary":
                return cli._run_information_boundary_evaluation(args, stdout)
            if args.evaluation == "information-boundary-v4":
                return cli._run_information_boundary_v4_evaluation(args, stdout)
            if args.evaluation == "information-boundary-pair":
                settings = (
                    cli._load_config(args.config)
                    if args.pair_command == "prepare"
                    else {}
                )
                return cli._run_information_boundary_pair_evaluation(
                    args,
                    stdout,
                    settings=settings,
                    provider_factory=provider_factory,
                )
            if args.evaluation == "release-authorization-pair":
                settings = (
                    cli._load_config(args.config)
                    if args.release_pair_command == "prepare"
                    else {}
                )
                return cli._run_release_authorization_pair_evaluation(
                    args,
                    stdout,
                    settings=settings,
                    provider_factory=provider_factory,
                )
            if args.evaluation == "workflow-patch-cohort":
                settings = (
                    cli._load_config(args.config)
                    if args.workflow_patch_command == "prepare"
                    else {}
                )
                return cli._run_workflow_patch_cohort_evaluation(
                    args,
                    stdout,
                    settings=settings,
                    provider_factory=provider_factory,
                )
            if args.evaluation == "workflow-patch-extension":
                settings = (
                    cli._load_config(args.config)
                    if args.workflow_patch_extension_command == "prepare"
                    else {}
                )
                return cli._run_workflow_patch_extension_evaluation(
                    args,
                    stdout,
                    settings=settings,
                    provider_factory=provider_factory,
                )
            if args.evaluation == "workflow-patch-efficiency":
                settings = (
                    cli._load_config(args.config)
                    if args.workflow_patch_efficiency_command == "prepare"
                    else {}
                )
                return cli._run_workflow_patch_efficiency_evaluation(
                    args,
                    stdout,
                    settings=settings,
                    provider_factory=provider_factory,
                )
            if args.evaluation == "exact-context-live-pair":
                settings = (
                    cli._load_config(args.config)
                    if args.exact_context_live_command == "prepare"
                    else {}
                )
                return cli._run_exact_context_live_pair_evaluation(
                    args,
                    stdout,
                    settings=settings,
                    provider_factory=provider_factory,
                )
            if args.evaluation == "firm-value":
                return cli._run_firm_value_evaluation(args, stdout)
            if args.evaluation == "firm-value-v2":
                return cli._run_firm_value_v2_evaluation(args, stdout)
            if args.evaluation == "firm-campaign":
                settings = (
                    cli._load_config(args.config)
                    if args.campaign_command == "prepare"
                    else {}
                )
                return cli._run_firm_value_campaign_evaluation(
                    args,
                    stdout,
                    settings=settings,
                    provider_factory=provider_factory,
                    coding_worker_factory=coding_worker_factory,
                )
            if args.evaluation == "firm-campaign-v2":
                settings = (
                    cli._load_config(args.config)
                    if args.campaign_command == "prepare"
                    else {}
                )
                return cli._run_firm_value_campaign_v2_evaluation(
                    args,
                    stdout,
                    settings=settings,
                    provider_factory=provider_factory,
                    coding_worker_factory=coding_worker_factory,
                )
            settings = cli._load_config(args.config) if args.live or args.preflight_live else {}
            return cli._run_coding_evaluation(
                args,
                stdout,
                settings=settings,
                provider_factory=provider_factory,
                coding_worker_factory=coding_worker_factory,
            )
        if args.command == "foundation":
            return cli._run_foundation(args, stdout)
        settings = cli._load_config(args.config)
        if args.command == "acp":
            return cli._run_acp_command(
                args,
                settings,
                provider_factory=provider_factory,
                coding_worker_factory=coding_worker_factory,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
        if (
            cli._isatty(stdin)
            and cli._isatty(stdout)
            and cli._needs_first_run_onboarding(args, args.config)
        ):
            print("\nFirst run · connect your company before its first goal.", file=stdout)
            onboarding_exit = cli._run_setup(
                cli._first_run_setup_args(),
                settings,
                args.config,
                stdin=stdin,
                output=stdout,
            )
            if onboarding_exit != cli.EXIT_OK:
                return onboarding_exit
            settings = cli._load_config(args.config)
            if not cli._provider_is_ready_without_network(settings):
                print(
                    "Connection saved. Finish the external login or set the named API-key environment variable, then run `noruct` again.",
                    file=stdout,
                )
                return cli.EXIT_OK
        if args.command == "setup":
            return cli._run_setup(
                args,
                settings,
                args.config,
                stdin=stdin,
                output=stdout,
            )
        if args.command == "provider":
            return cli._run_provider_command(args, settings, stdout, stdin=stdin)
        if args.command == "update":
            return cli._run_update_command(args, stdout)
        if args.command == "mcp":
            return cli._run_mcp_command(
                args,
                settings,
                args.config,
                stdout,
                ports=cli.McpCliPorts(
                    load_settings=cli._load_config,
                    state_path=cli._state_path,
                ),
            )
        if args.command == "browser":
            return cli._run_browser_command(args, settings, args.config, stdout)
        if args.command == "computer-use":
            return cli._run_computer_use_command(args, settings, args.config, stdout)
        if args.command == "media":
            return cli._run_media_command(args, settings, args.config, stdout)
        if args.command == "web-search":
            return cli._run_web_search_command(args, settings, args.config, stdout)
        if args.command == "home-assistant":
            return cli._run_home_assistant_command(args, settings, args.config, stdout)
        if args.command == "plugin":
            return cli._run_plugin_command(args, settings, args.config, stdout)
        if args.command in {"capabilities", "tools"}:
            return cli._run_capabilities_command(args, settings, stdout)
        if args.command == "environment":
            return cli._run_environment_command(args, settings, args.config, stdout)
        if args.command == "channel":
            return cli._run_channel_command(
                args,
                settings,
                args.config,
                stdout,
                provider_factory=provider_factory,
            )
        if args.command == "sessions":
            return cli._run_sessions(args, settings, stdout)
        if args.command == "session":
            return cli._run_session_command(args, settings, stdout)
        if args.command == "skills":
            return cli._run_skills_command(args, settings, stdout)
        if args.command == "schedule":
            return cli._run_schedule_command(
                args, settings, stdout, provider_factory=provider_factory
            )
        if args.command == "gateway":
            return cli._run_gateway_command(
                args, settings, stdout, provider_factory=provider_factory
            )
        if args.command == "job":
            return cli._run_job(args, settings, stdout)
        if args.command == "portfolio":
            return cli._run_portfolio(
                args,
                settings,
                stdout,
                provider_factory=provider_factory,
                coding_worker_factory=coding_worker_factory,
            )
        if args.command == "graph":
            return cli._run_graph_command(
                args,
                settings,
                stdout,
                provider_factory=provider_factory,
            )
        if args.command == "data":
            return cli.run_data_command(
                args,
                settings,
                args.config,
                stdout,
                state_path_for=cli._state_path,
            )
        if args.command == "knowledge":
            return cli._run_knowledge(args, settings, stdout)
        if args.command == "intent":
            if args.intent_command == "run":
                cli._prepare_permission_mode(args, settings, stdin=stdin, stdout=stdout)
            return cli._run_intent(
                args,
                settings,
                stdout,
                provider_factory=provider_factory,
                coding_worker_factory=coding_worker_factory,
                stdin=stdin,
            )
        if args.command == "decision":
            return cli._run_decision(args, settings, stdout)
        if args.command == "question":
            return cli._run_question(args, settings, stdout)
        if args.command == "research":
            return cli._run_research(args, settings, stdout)
        if args.command == "evolution":
            return cli._run_evolution(args, settings, stdout)
        if args.command == "network":
            return cli._run_network(args, settings, stdout)
        if args.command == "company":
            return cli._run_company(args, settings, stdout)
        if args.command == "doctor":
            return cli._run_doctor(args, settings, args.config, stdout)
        cli._prepare_permission_mode(args, settings, stdin=stdin, stdout=stdout)
        if args.command == "handoff-read-only":
            return cli._run_read_only_partial_handoff(args, settings, stdout=stdout)
        if args.command == "continue-read-only":
            return cli._run_read_only_partial_continuation(
                args,
                settings,
                provider_factory=provider_factory,
                stdout=stdout,
            )
        if args.command == "continue-graph-proposal":
            return cli._run_graph_proposal_continuation(
                args,
                settings,
                provider_factory=provider_factory,
                stdin=stdin,
                stdout=stdout,
            )
        if args.command in {"chat", "resume"}:
            return cli._run_interactive(
                args,
                settings,
                provider_factory=provider_factory,
                coding_worker_factory=coding_worker_factory,
                stdin=stdin,
                stdout=stdout,
            )
        return cli._run_once(
            args,
            settings,
            provider_factory=provider_factory,
            coding_worker_factory=coding_worker_factory,
            stdin=stdin,
            stdout=stdout,
        )
    except (TypeError, ValueError, OSError) as exc:
        print(f"noruct: {exc}", file=stderr)
        return cli.EXIT_INPUT
    except KeyboardInterrupt:
        print("noruct: interrupted", file=stderr)
        return 130
    except Exception as exc:
        message = str(exc)
        if message.startswith("selected Noruct runtime Python lacks required PyYAML==6.0.3;"):
            # This is an installation contract remedy, not provider/model or
            # customer data.  Keep the otherwise fail-closed runtime boundary
            # while making the normal runtime installation remedy actionable.
            print(f"noruct: {message}", file=stderr)
            return cli.EXIT_RUNTIME
        print(f"noruct: runtime failed ({type(exc).__name__})", file=stderr)
        return cli.EXIT_RUNTIME
