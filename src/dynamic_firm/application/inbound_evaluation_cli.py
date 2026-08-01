"""Inbound bridge configuration and organization-evaluation commands."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

def _provider_preflight_config(settings: dict, *, timeout_seconds: float = 10.0) -> cli.ProviderPreflightConfig:
    provider = cli._table(settings, "provider")
    kind = cli._provider_kind(cli._first(cli.os.environ.get("NORUCT_PROVIDER"), provider.get("kind"), "openai_api"))
    profile = cli.provider_profile(kind) if kind not in {"openai_codex", "external_exec"} else None
    base_url = str(cli._first(cli.os.environ.get("NORUCT_BASE_URL"), provider.get("base_url"), profile.base_url if profile else ""))
    model = str(cli._first(cli.os.environ.get("NORUCT_MODEL"), provider.get("model"), ""))
    default_api_key_env = profile.api_key_env if profile else None
    api_key_env = str(cli._first(provider.get("api_key_env"), default_api_key_env, "")).strip() or None
    no_auth = bool(cli._first(provider.get("no_auth"), default_api_key_env is None))
    return cli.ProviderPreflightConfig(
        kind=kind,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        no_auth=no_auth,
        timeout_seconds=timeout_seconds,
    )

def _remote_worker_programs(values: list[str]) -> dict[str, str]:
    programs: dict[str, str] = {}
    for value in values:
        name, separator, program = value.partition("=")
        if not separator or not name or not program or name in programs:
            raise ValueError("Each --program must be a unique ID=/absolute/program entry")
        programs[name] = program
    if not programs:
        raise ValueError("At least one --program ID=/absolute/program is required")
    return programs

def _container_programs(values: list[str]) -> dict[str, tuple[str, ...]]:
    programs: dict[str, tuple[str, ...]] = {}
    for value in values:
        name, separator, command = value.partition("=")
        if not separator or not name or not command:
            raise ValueError("Container --program values must use ID=COMMAND")
        if name in programs:
            raise ValueError(f"Container program id is repeated: {name}")
        programs[name] = (command,)
    if not programs:
        raise ValueError("Configure at least one --program ID=COMMAND")
    return programs

def _inbound_run_config(
    message: InboundMessage,
    channel: InboundChannelConfig,
    args: cli.argparse.Namespace,
    settings: dict,
) -> RunCommandConfig:
    """Materialize one accepted bridge line as an ordinary read-only Job."""

    synthetic = cli.argparse.Namespace(
        goal=f"[Message from configured external source {channel.source_id}]\n\n{message.text}",
        workspace=channel.workspace,
        state=args.state,
        provider_kind=None,
        base_url=None,
        model=None,
        codex_command=None,
        api_key_env=None,
        no_auth=None,
        request_timeout=None,
        max_wall_time=None,
        max_model_calls=None,
        max_tool_calls=None,
        max_cost_usd=None,
        cost_mode=None,
        permission_mode="read-only",
        employee_runtime=None,
        runtime_python=None,
        skills_dir=None,
    )
    return cli._run_config(synthetic, settings)

def _telegram_run_config(
    message: TelegramInboundMessage,
    channel: TelegramChannelConfig,
    args: cli.argparse.Namespace,
    settings: dict,
) -> RunCommandConfig:
    """Materialize one allowlisted Telegram text as an ordinary read-only Job."""

    synthetic = cli.argparse.Namespace(
        goal=f"[Message from configured Telegram sender {message.sender_id}]\n\n{message.text}",
        workspace=channel.workspace,
        state=args.state,
        provider_kind=None,
        base_url=None,
        model=None,
        codex_command=None,
        external_command=None,
        api_key_env=None,
        no_auth=None,
        request_timeout=None,
        max_wall_time=None,
        max_model_calls=None,
        max_tool_calls=None,
        max_cost_usd=None,
        cost_mode=None,
        permission_mode="read-only",
        employee_runtime=None,
        runtime_python=None,
        skills_dir=None,
    )
    return cli._run_config(synthetic, settings)

def _ntfy_inbound_run_config(message: NtfyInboundMessage, channel: NtfyInboundConfig, args: cli.argparse.Namespace, settings: dict) -> RunCommandConfig:
    synthetic = cli.argparse.Namespace(
        goal=f"[Message from configured ntfy topic {channel.topic}]\n\n{message.text}", workspace=channel.workspace,
        state=args.state, provider_kind=None, base_url=None, model=None, codex_command=None, external_command=None,
        api_key_env=None, no_auth=None, request_timeout=None, max_wall_time=None, max_model_calls=None,
        max_tool_calls=None, max_cost_usd=None, cost_mode=None, permission_mode="read-only", employee_runtime=None,
        runtime_python=None, skills_dir=None,
    )
    return cli._run_config(synthetic, settings)

def _matrix_inbound_run_config(message: MatrixInboundMessage, channel: MatrixInboundConfig, args: cli.argparse.Namespace, settings: dict) -> RunCommandConfig:
    synthetic = cli.argparse.Namespace(
        goal=f"[Message from configured Matrix sender {message.sender_id} in {message.room_id}]\n\n{message.text}", workspace=channel.workspace,
        state=args.state, provider_kind=None, base_url=None, model=None, codex_command=None, external_command=None,
        api_key_env=None, no_auth=None, request_timeout=None, max_wall_time=None, max_model_calls=None,
        max_tool_calls=None, max_cost_usd=None, cost_mode=None, permission_mode="read-only", employee_runtime=None,
        runtime_python=None, skills_dir=None,
    )
    return cli._run_config(synthetic, settings)

def _mattermost_inbound_run_config(message: MattermostInboundMessage, channel: MattermostInboundConfig, args: cli.argparse.Namespace, settings: dict) -> RunCommandConfig:
    synthetic = cli.argparse.Namespace(
        goal=f"[Message from configured Mattermost sender {message.sender_id} in {message.channel_id}]\n\n{message.text}", workspace=channel.workspace,
        state=args.state, provider_kind=None, base_url=None, model=None, codex_command=None, external_command=None,
        api_key_env=None, no_auth=None, request_timeout=None, max_wall_time=None, max_model_calls=None,
        max_tool_calls=None, max_cost_usd=None, cost_mode=None, permission_mode="read-only", employee_runtime=None,
        runtime_python=None, skills_dir=None,
    )
    return cli._run_config(synthetic, settings)

def _email_inbound_run_config(message: EmailInboundMessage, channel: EmailInboundConfig, args: cli.argparse.Namespace, settings: dict) -> RunCommandConfig:
    synthetic = cli.argparse.Namespace(
        goal=f"[Message from configured email sender {message.sender}]\n\n{message.text}", workspace=channel.workspace,
        state=args.state, provider_kind=None, base_url=None, model=None, codex_command=None, external_command=None,
        api_key_env=None, no_auth=None, request_timeout=None, max_wall_time=None, max_model_calls=None,
        max_tool_calls=None, max_cost_usd=None, cost_mode=None, permission_mode="read-only", employee_runtime=None,
        runtime_python=None, skills_dir=None,
    )
    return cli._run_config(synthetic, settings)

def _slack_inbound_run_config(
    message: SlackInboundMessage,
    channel: SlackInboundConfig,
    args: cli.argparse.Namespace,
    settings: dict,
) -> RunCommandConfig:
    """Materialize one verified Slack text event as an ordinary read-only Job."""

    synthetic = cli.argparse.Namespace(
        goal=(
            f"[Message from configured Slack sender {message.sender_id} "
            f"in channel {message.channel_id}]\n\n{message.text}"
        ),
        workspace=channel.workspace,
        state=args.state,
        provider_kind=None,
        base_url=None,
        model=None,
        codex_command=None,
        external_command=None,
        api_key_env=None,
        no_auth=None,
        request_timeout=None,
        max_wall_time=None,
        max_model_calls=None,
        max_tool_calls=None,
        max_cost_usd=None,
        cost_mode=None,
        permission_mode="read-only",
        employee_runtime=None,
        runtime_python=None,
        skills_dir=None,
    )
    return cli._run_config(synthetic, settings)

def _discord_inbound_run_config(
    message: DiscordInboundMessage,
    channel: DiscordInboundConfig,
    args: cli.argparse.Namespace,
    settings: dict,
) -> RunCommandConfig:
    """Materialize one allowlisted Discord text as an ordinary read-only Job."""

    synthetic = cli.argparse.Namespace(
        goal=(
            f"[Message from configured Discord sender {message.sender_id} "
            f"in channel {message.channel_id}]\n\n{message.text}"
        ),
        workspace=channel.workspace,
        state=args.state,
        provider_kind=None,
        base_url=None,
        model=None,
        codex_command=None,
        external_command=None,
        api_key_env=None,
        no_auth=None,
        request_timeout=None,
        max_wall_time=None,
        max_model_calls=None,
        max_tool_calls=None,
        max_cost_usd=None,
        cost_mode=None,
        permission_mode="read-only",
        employee_runtime=None,
        runtime_python=None,
        skills_dir=None,
    )
    return cli._run_config(synthetic, settings)

def _run_company_learning_evaluation(args: cli.argparse.Namespace, output: TextIO) -> int:
    from dynamic_firm.evaluation.company_learning import run_company_learning_evaluation

    record = cli.asyncio.run(run_company_learning_evaluation())
    primitive = cli.to_primitive(record)
    if args.json:
        print(cli.json.dumps(primitive, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            f"Company learning: {'PASS' if record.passed else 'FAIL'} · "
            f"{record.first_decision} → {record.second_decision}",
            file=output,
        )
        print(
            f"Patch: {record.candidate_id} · preview-only · "
            f"replay={'match' if record.replay_matches else 'mismatch'}",
            file=output,
        )
        print(
            f"PLAYBOOK r{record.final_playbook_revision} · "
            f"patterns={record.final_pattern_count} · automatic apply disabled",
            file=output,
        )
        for check in record.checks:
            print(f"- {'pass' if check.passed else 'fail'} {check.name}: {check.evidence}", file=output)
    return cli.EXIT_OK if record.passed else cli.EXIT_JOB_FAILED

def _run_patch_observation_evaluation(args: cli.argparse.Namespace, output: TextIO) -> int:
    from dynamic_firm.evaluation.patch_observation import (
        run_patch_observation_evaluation,
    )

    record = run_patch_observation_evaluation()
    primitive = cli.to_primitive(record)
    if args.json:
        print(cli.json.dumps(primitive, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            f"Patch observation: {'PASS' if record.passed else 'FAIL'} · "
            f"{record.two_observation_decision} → {record.three_observation_decision} "
            f"→ {record.safety_decision}",
            file=output,
        )
        print(
            f"Patch: {record.final_patch_status} · PLAYBOOK r{record.final_playbook_revision} · "
            "automatic rollback disabled",
            file=output,
        )
        for check in record.checks:
            print(
                f"- {'pass' if check.passed else 'fail'} {check.name}: {check.evidence}",
                file=output,
            )
    return cli.EXIT_OK if record.passed else cli.EXIT_JOB_FAILED

def _run_roster_patch_evaluation(args: cli.argparse.Namespace, output: TextIO) -> int:
    from dynamic_firm.evaluation.roster_patch import run_roster_patch_evaluation

    record = run_roster_patch_evaluation()
    primitive = cli.to_primitive(record)
    if args.json:
        print(cli.json.dumps(primitive, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            f"Roster governance: {'PASS' if record.passed else 'FAIL'} · "
            f"ROSTER r{record.initial_roster_revision} → "
            f"r{record.applied_roster_revision}",
            file=output,
        )
        print(
            "Lifecycle: " + " → ".join(record.lifecycle),
            file=output,
        )
        print(
            f"Running snapshot r{record.running_job_roster_revision} · "
            f"next/restart r{record.next_job_roster_revision} · "
            f"stale rejected={str(record.stale_apply_rejected).lower()}",
            file=output,
        )
        print("Provider calls: 0 · quota consumed: no · automatic apply: disabled", file=output)
        for check in record.checks:
            print(
                f"[{'PASS' if check.passed else 'FAIL'}] {check.name} · {check.evidence}",
                file=output,
            )
    return cli.EXIT_OK if record.passed else cli.EXIT_JOB_FAILED

def _run_hiring_evaluation(args: cli.argparse.Namespace, output: TextIO) -> int:
    from dynamic_firm.evaluation.hiring import run_hiring_recommendation_evaluation

    record = run_hiring_recommendation_evaluation()
    primitive = cli.to_primitive(record)
    if args.json:
        print(cli.json.dumps(primitive, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            f"Hiring recommendation: {'PASS' if record.passed else 'FAIL'} · "
            f"{record.first_decision} → {record.second_decision}",
            file=output,
        )
        print(
            f"Candidate: {record.candidate_id} · evidence=2 · "
            f"ROSTER r{record.initial_roster_revision} → "
            f"r{record.applied_roster_revision}",
            file=output,
        )
        print(
            "Provider calls: 0 · quota consumed: no · "
            "automatic approve/apply: disabled",
            file=output,
        )
        for check in record.checks:
            print(
                f"[{'PASS' if check.passed else 'FAIL'}] "
                f"{check.name} · {check.evidence}",
                file=output,
            )
    return cli.EXIT_OK if record.passed else cli.EXIT_JOB_FAILED

def _run_hire_observation_evaluation(args: cli.argparse.Namespace, output: TextIO) -> int:
    from dynamic_firm.evaluation.hire_observation import (
        run_hire_observation_evaluation,
    )

    record = run_hire_observation_evaluation()
    primitive = cli.to_primitive(record)
    if args.json:
        print(cli.json.dumps(primitive, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            f"Hire observation: {'PASS' if record.passed else 'FAIL'} · "
            f"{record.two_observation_decision} → "
            f"{record.three_observation_decision} → {record.safety_decision}",
            file=output,
        )
        print(
            f"Contract: {record.patch_id} · ROSTER r{record.applied_roster_revision} · "
            f"cohort={record.cohort_count}",
            file=output,
        )
        print(
            "Provider calls: 0 · quota consumed: no · "
            "automatic dormancy/ROSTER patch: disabled",
            file=output,
        )
        for check in record.checks:
            print(
                f"[{'PASS' if check.passed else 'FAIL'}] "
                f"{check.name} · {check.evidence}",
                file=output,
            )
    return cli.EXIT_OK if record.passed else cli.EXIT_JOB_FAILED

def _run_retention_review_evaluation(args: cli.argparse.Namespace, output: TextIO) -> int:
    from dynamic_firm.evaluation.retention_review import (
        run_retention_review_evaluation,
    )

    record = run_retention_review_evaluation()
    primitive = cli.to_primitive(record)
    if args.json:
        print(cli.json.dumps(primitive, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            f"Retention review: {'PASS' if record.passed else 'FAIL'} · "
            f"approval={record.manual_decision} · "
            f"auto={record.auto_review_decision} · "
            f"always={record.always_approve_decision}",
            file=output,
        )
        print(
            f"Safety in auto-review: {record.auto_review_safety_decision} · "
            f"stale rejected={str(record.stale_apply_rejected).lower()}",
            file=output,
        )
        print("Provider calls: 0 · quota consumed: no · hard invariants: always on", file=output)
        for check in record.checks:
            print(
                f"[{'PASS' if check.passed else 'FAIL'}] "
                f"{check.name} · {check.evidence}",
                file=output,
            )
    return cli.EXIT_OK if record.passed else cli.EXIT_JOB_FAILED

def _run_employee_skill_evaluation(args: cli.argparse.Namespace, output: TextIO) -> int:
    from dynamic_firm.evaluation.employee_skill import run_employee_skill_evaluation

    record = run_employee_skill_evaluation()
    primitive = cli.to_primitive(record)
    if args.json:
        print(cli.json.dumps(primitive, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            f"Employee Skill governance: {'PASS' if record.passed else 'FAIL'} · "
            f"r{record.applied_revision} → rollback r{record.rolled_back_revision}",
            file=output,
        )
        print("Lifecycle: " + " → ".join(record.lifecycle), file=output)
        print(
            f"Assessment: {record.first_assessment} → {record.keep_assessment} "
            f"→ {record.safety_assessment}",
            file=output,
        )
        print(
            "Provider calls: 0 · quota consumed: no · "
            "review: approval only · automatic apply/rollback: disabled",
            file=output,
        )
        for check in record.checks:
            print(
                f"[{'PASS' if check.passed else 'FAIL'}] "
                f"{check.name} · {check.evidence}",
                file=output,
            )
    return cli.EXIT_OK if record.passed else cli.EXIT_JOB_FAILED

def _run_task_mutation_evaluation(args: cli.argparse.Namespace, output: TextIO) -> int:
    from dynamic_firm.evaluation.task_mutation import run_task_mutation_evaluation

    record = run_task_mutation_evaluation()
    primitive = cli.to_primitive(record)
    if args.json:
        print(cli.json.dumps(primitive, ensure_ascii=False, sort_keys=True), file=output)
    else:
        print(
            f"Task mutation: {'PASS' if record.passed else 'FAIL'} · "
            f"retry={'→'.join(record.retry.employees)} · "
            f"reroute={'→'.join(record.reroute.employees)}",
            file=output,
        )
        print(
            f"Exhaustion attempts={len(record.retry_exhaustion.task_attempts)} · "
            f"cycle assignees={len(record.reroute_cycle.employees)} · "
            f"replay={'match' if record.deterministic_replay else 'mismatch'}",
            file=output,
        )
        print(
            "Provider calls: 0 · quota consumed: no · "
            "ROSTER/PLAYBOOK/Skill mutation: none",
            file=output,
        )
        for check in record.checks:
            print(
                f"[{'PASS' if check.passed else 'FAIL'}] "
                f"{check.name} · {check.evidence}",
                file=output,
            )
    return cli.EXIT_OK if record.passed else cli.EXIT_JOB_FAILED
