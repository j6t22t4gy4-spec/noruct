"""Knowledge, intent, decision, research, and schedule command adapter."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False

def _normalize_argv(argv: Sequence[str] | None) -> list[str]:
    raw = list(cli.sys.argv[1:] if argv is None else argv)
    if not raw:
        return raw
    known = {
        "run",
        "continue-read-only",
        "handoff-read-only",
        "continue-graph-proposal",
        "ask",
        "chat",
        "resume",
        "sessions",
        "session",
        "skills",
        "schedule",
        "gateway",
        "job",
        "portfolio",
        "graph",
        "data",
        "knowledge",
        "intent",
        "decision",
        "question",
        "research",
        "evolution",
        "network",
        "foundation",
        "company",
        "setup",
        "provider",
        "update",
        "acp",
        "mcp",
        "browser",
        "computer-use",
        "media",
        "web-search",
        "home-assistant",
        "plugin",
        "capabilities",
        "tools",
        "environment",
        "channel",
        "demo",
        "eval",
        "doctor",
    }
    prefix: list[str] = []
    cursor = 0
    while cursor < len(raw):
        item = raw[cursor]
        if item == "--config" and cursor + 1 < len(raw):
            prefix.extend(raw[cursor : cursor + 2])
            cursor += 2
            continue
        if item.startswith("--config="):
            prefix.append(item)
            cursor += 1
            continue
        break
    remaining = raw[cursor:]
    if not remaining or remaining[0] in known or remaining[0].startswith("-"):
        return raw
    goal_tokens: list[str] = []
    while remaining and not remaining[0].startswith("-"):
        goal_tokens.append(remaining.pop(0))
    return [*prefix, "ask", " ".join(goal_tokens), *remaining]

def _state_path(args: cli.argparse.Namespace, settings: dict) -> cli.Path:
    run = cli._table(settings, "run")
    return cli.Path(cli._first(getattr(args, "state", None), run.get("state"), cli.DEFAULT_STATE_PATH)).expanduser().resolve()

def _evolution_state_path(args: cli.argparse.Namespace, settings: dict) -> cli.Path:
    explicit = getattr(args, "evolution_state", None)
    if explicit is not None:
        return cli.Path(explicit).expanduser().resolve()
    state_path = _state_path(args, settings)
    return state_path.with_name(f"{state_path.stem}.evolution.db")

def _evolution_human_summary(command: str, payload: object) -> str:
    if command == "status":
        assert isinstance(payload, cli.Mapping)
        return (
            "Evolution Network local boundary · default transport off · "
            f"{payload['active_consents']} active consent(s) · "
            f"{payload['blueprints']} Blueprint(s)"
        )
    if command == "export":
        assert isinstance(payload, cli.Mapping)
        return f"Evolution Network local export · {payload['destination']}"
    if command == "delete":
        return "Evolution Network local state deleted"
    if command == "evaluate" and isinstance(payload, cli.Mapping):
        return (
            "Blueprint admission screen · "
            f"{payload['decision']} · promotion={'allowed' if payload['promotion_allowed'] else 'blocked'}"
        )
    if command == "delta" and isinstance(payload, cli.Mapping):
        if "delta" in payload:
            return "Blueprint Delta preview accepted · public synthetic holdout only"
        return (
            "Blueprint Delta holdout · "
            f"{payload['decision']} · manual review={'eligible' if payload['manual_review_eligible'] else 'blocked'}"
        )
    if command == "release-candidate" and isinstance(payload, cli.Mapping):
        if "release_candidates" in payload:
            return f"Local release candidates · {len(payload['release_candidates'])} record(s)"
        return f"Local release candidate pending review · {payload['candidate_id']}"
    if command == "registry" and isinstance(payload, cli.Mapping):
        if "destination" in payload:
            return f"Public read-only registry bundle written · {payload['destination']}"
        if "snapshot_id" in payload:
            return f"Verified registry bundle staged without adoption · {payload['snapshot_id']}"
        if "snapshots" in payload:
            return f"Verified registry staging area · {len(payload['snapshots'])} snapshot(s)"
        if "bundle_digest" in payload:
            return f"Public read-only registry bundle verified · {payload['registry_id']}"
        return "Public registry signing payload prepared"
    if command == "capsule" and isinstance(payload, cli.Mapping):
        if "destination" in payload and "preview" in payload:
            return (
                "Learning Capsule built from verified ACTIVE JOB · local file only · "
                f"{payload['destination']}"
            )
        if "sanitized_capsule" in payload:
            return "Learning Capsule preview accepted · no local write · no network transport"
        return f"Learning Capsule {payload['status'].lower()} · {payload['capsule_id']}"
    if command == "blueprint" and isinstance(payload, cli.Mapping):
        if "blueprint" in payload:
            return "Employee Blueprint preview accepted · catalog-only"
        if "selection_id" in payload:
            return f"Blueprint selection active · {payload['blueprint_id']}@{payload['version']}"
        if "blueprint_id" in payload:
            return f"Employee Blueprint cataloged · {payload['blueprint_id']}@{payload['version']}"
    if command == "artifact" and isinstance(payload, cli.Mapping):
        if "artifact" in payload:
            return "Evolution Artifact preview accepted · catalog-only · update mode pinned by default"
        if "artifacts" in payload:
            return f"Evolution Artifact catalog · {len(payload['artifacts'])} version(s)"
        if "active_artifacts" in payload:
            return f"Active local Artifact snapshot · {len(payload['active_artifacts'])} item(s)"
        if "updates" in payload:
            return f"Local Artifact update cycle · {len(payload['updates'])} subscription(s) evaluated"
        if "pins" in payload:
            return f"Job Artifact snapshot pinned · {payload['job_id']} · {len(payload['pins'])} item(s)"
        if "activation_id" in payload:
            return (
                f"Evolution Artifact active · {payload['artifact_id']}@{payload['version']}"
                f" · origin={payload['artifact']['origin_kind']}"
            )
        if "installation_id" in payload:
            return f"Evolution Artifact {payload['status'].lower()} · {payload['artifact_id']}@{payload['version']}"
        if "subscription_id" in payload:
            return f"Evolution Artifact update mode · {payload['mode']}"
        if "artifact_id" in payload:
            return (
                f"Evolution Artifact cataloged · {payload['artifact_id']}@{payload['version']}"
                f" · origin={payload['origin_kind']}"
            )
    if command == "artifact-registry" and isinstance(payload, cli.Mapping):
        if "registries" in payload:
            return f"Public Artifact registry discovery · {len(payload['registries'])} active pointer(s) · no bundle trusted or staged"
        if "destination" in payload:
            return f"Public read-only Artifact registry bundle written · {payload['destination']}"
        if "bundle_digest" in payload:
            return f"Public read-only Artifact registry bundle verified · {payload['registry_id']}"
        return "Artifact registry signing payload prepared"
    if command == "consent" and isinstance(payload, cli.Mapping):
        return f"Evolution consent {payload['status'].lower()} · {payload['consent_id']}"
    return "Evolution Network local action complete"

def _run_evolution(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    from dynamic_firm.application.evolution_cli import run_evolution_command

    return run_evolution_command(
        args,
        settings,
        output,
        evolution_state_path=_evolution_state_path,
        runtime_state_path=_state_path,
        human_summary=_evolution_human_summary,
        exit_ok=cli.EXIT_OK,
    )

def _run_network(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    """Keep CLI ingress while delegating the Network catalog lifecycle."""

    from dynamic_firm.application.network_cli import run_network_command

    return run_network_command(
        args,
        state_path=_evolution_state_path(args, settings),
        output=output,
    )

def _run_foundation(args: cli.argparse.Namespace, output: TextIO) -> int:
    """Delegate Foundation dispatch while retaining the CLI error boundary."""

    from dynamic_firm.application.foundation_cli.command import run_foundation_command

    return run_foundation_command(args, output, exit_ok=cli.EXIT_OK)

def _knowledge_paths(
    args: cli.argparse.Namespace,
    settings: dict,
) -> tuple[cli.Path, cli.Path]:
    return cli.knowledge_runtime_paths(_state_path(args, settings))

def _knowledge_json(payload: object, output: TextIO) -> None:
    print(
        cli.json.dumps(
            cli.to_primitive(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        file=output,
    )

def _knowledge_display(value: object) -> str:
    """Render stored/user/model text without terminal control authority."""

    return cli.strip_terminal_escapes(str(value))

def _run_knowledge(
    args: cli.argparse.Namespace,
    settings: dict,
    output: TextIO,
) -> int:
    """Compose parsed Knowledge ingress with the surface-neutral command adapter."""

    from dynamic_firm.application.knowledge_cli import run_knowledge_command

    database, vault_path = _knowledge_paths(args, settings)
    return run_knowledge_command(
        args,
        database=database,
        vault_path=vault_path,
        render_json=_knowledge_json,
        render_human=cli._render_knowledge_human,
        output=output,
    )

def _render_intent_human(command: str, payload: object, output: TextIO) -> None:
    primitive = cli.to_primitive(payload)
    if command == "list":
        assert isinstance(primitive, list)
        if not primitive:
            print("No Intents yet.", file=output)
        for intent in primitive:
            print(
                f"{intent['intent_id']} · {intent['status']} · p{intent['priority']} · "
                f"r{intent['revision']} · {_knowledge_display(intent['goal'])}",
                file=output,
            )
        return
    if command in {"create", "status"}:
        assert isinstance(primitive, cli.Mapping)
        print(
            f"{primitive['intent_id']} · {primitive['status']} · "
            f"p{primitive['priority']} · r{primitive['revision']} · "
            f"{_knowledge_display(primitive['goal'])}",
            file=output,
        )
        return
    if command == "run":
        assert isinstance(primitive, cli.Mapping)
        result = primitive["job"]
        knowledge = primitive["knowledge"]
        print(
            _knowledge_display(
                result.get("summary") or f"Job ended with {result['status']}."
            ),
            file=output,
        )
        print(
            f"Intent execution · {result['job_id']} · {result['status']} · "
            f"binding={knowledge['binding']['binding_id']} · "
            f"candidate={knowledge.get('candidate', {}).get('candidate_id', 'none') if knowledge.get('candidate') else 'none'}",
            file=output,
        )
        return
    if command == "bindings":
        assert isinstance(primitive, list)
        if not primitive:
            print("No Intent execution bindings.", file=output)
        for binding in primitive:
            print(
                f"{binding['binding_id']} · {binding['status']} · "
                f"job={binding['job_id']} · result={binding['job_status'] or 'pending'}",
                file=output,
            )
        return
    if command == "interrupt":
        assert isinstance(primitive, cli.Mapping)
        print(
            f"{primitive['binding_id']} · {primitive['status']} · "
            f"result={primitive['job_status']}",
            file=output,
        )
        return
    _knowledge_json(payload, output)

def _run_intent(
    args: cli.argparse.Namespace,
    settings: dict,
    output: TextIO,
    *,
    provider_factory: ProviderFactory,
    coding_worker_factory: CodingWorkerFactory,
    stdin: TextIO,
) -> int:
    database, vault_path = _knowledge_paths(args, settings)
    command = args.intent_command
    mutating = command in {"create", "interrupt"}
    if not database.is_file() and not mutating:
        if command in {"list", "bindings"}:
            payload: object = ()
            if args.json:
                _knowledge_json(payload, output)
            else:
                _render_intent_human(command, payload, output)
            return cli.EXIT_OK
        raise ValueError("Knowledge DB has not been created; create an Intent first")
    store = cli.KnowledgeStore(database)
    try:
        if command == "create":
            payload = store.create_intent(
                goal=args.goal,
                priority=args.priority,
                status=cli.IntentStatus(args.status),
                constraints=tuple(args.constraint),
                acceptance_criteria=tuple(args.acceptance_criteria),
                knowledge_query=args.knowledge_query,
            )
            exit_code = cli.EXIT_OK
        elif command == "list":
            payload = store.list_intents(
                status=(cli.IntentStatus(args.status) if args.status else None),
                limit=cli.knowledge_limit(args.limit, label="Intent list limit"),
            )
            exit_code = cli.EXIT_OK
        elif command == "show":
            verified = store.verified_intent(args.intent_id)
            if verified is None:
                raise ValueError(f"Intent was not found: {args.intent_id}")
            intent, _ = verified
            payload = {"intent": intent, "history": store.intent_history(args.intent_id)}
            exit_code = cli.EXIT_OK
        elif command == "status":
            payload = store.set_intent_status(args.intent_id, cli.IntentStatus(args.status))
            exit_code = cli.EXIT_OK
        elif command == "run":
            verified = store.verified_intent(args.intent_id)
            if verified is None:
                raise ValueError(f"Intent was not found: {args.intent_id}")
            intent, _ = verified
            config_args = cli.argparse.Namespace(**vars(args))
            config_args.goal = intent.goal
            config = cli._run_config(config_args, settings)
            roster_snapshot = cli._load_active_roster(config)
            provider_config = cli._provider_config(config)
            request_id = f"request-{cli.uuid.uuid4()}"
            job_id = f"job-{cli.uuid.uuid4()}"
            service = cli.UserKnowledgeService(store, cli.KnowledgeVault(vault_path))
            bridge = cli.KnowledgeFirmBridge(service)
            prepared = bridge.prepare(
                args.intent_id,
                request_id=request_id,
                job_id=job_id,
                access_scope=args.access_scope,
                evidence_limit=args.evidence_limit,
                evidence_max_bytes=args.evidence_max_bytes,
            )
            ui = None
            try:
                provider = provider_factory(provider_config)
                coding_worker = (
                    coding_worker_factory(provider_config)
                    if isinstance(provider_config, cli.CodexExecProviderConfig)
                    and config.permission_mode == "ask"
                    else None
                )
                ui = (
                    cli.InlineTerminalUI(stdin=stdin, stdout=output, plain=args.plain)
                    if config.permission_mode == "ask"
                    else None
                )
                approval_port = cli.InteractiveApprovalController(ui) if ui is not None else None
                result = cli.asyncio.run(
                    cli.run_goal(
                        config,
                        provider,
                        approval_port=approval_port,
                        coding_worker=coding_worker,
                        route=cli.InputRoute.COMPANY_GOAL,
                        roster_snapshot=roster_snapshot,
                        request_id=request_id,
                        job_id=job_id,
                        task_evidence=prepared.task_evidence,
                        execution_origin=prepared.execution_origin,
                    )
                )
                if result.request_id != request_id or result.job_id != job_id:
                    raise ValueError(
                        "Firm Job result identity does not match its prepared Knowledge binding"
                    )
                try:
                    completed = bridge.complete(
                        prepared,
                        cli.KnowledgeExecutionOutcome(
                            job_id=result.job_id,
                            status=result.status.value,
                            summary=result.summary,
                        ),
                    )
                except BaseException:
                    bridge.interrupt(prepared)
                    raise
            except BaseException:
                current = store.execution_binding(prepared.binding.binding_id)
                if current is not None and current.status == "PREPARED":
                    bridge.interrupt(prepared)
                raise
            finally:
                if ui is not None:
                    ui.close()
            payload = {"job": result, "knowledge": completed}
            exit_code = cli.EXIT_OK if result.status == cli.JobStatus.SUCCEEDED else cli.EXIT_JOB_FAILED
        elif command == "bindings":
            payload = store.list_execution_bindings(
                status=args.status,
                limit=cli.knowledge_limit(args.limit, label="Intent binding list limit"),
            )
            exit_code = cli.EXIT_OK
        elif command == "interrupt":
            if not args.confirm:
                raise ValueError("Interrupting a prepared Intent execution requires --confirm")
            payload = store.complete_execution_binding(
                args.binding_id,
                job_status="INTERRUPTED",
            )
            exit_code = cli.EXIT_OK
        else:
            raise ValueError(f"Unknown intent command: {command}")
    finally:
        store.close()
    if args.json:
        _knowledge_json(payload, output)
    else:
        _render_intent_human(command, payload, output)
    return exit_code

def _render_decision_human(command: str, payload: object, output: TextIO) -> None:
    primitive = cli.to_primitive(payload)
    if command in {"list", "due"}:
        assert isinstance(primitive, list)
        if not primitive:
            print("No Decisions due." if command == "due" else "No Decisions yet.", file=output)
        for decision in primitive:
            review = f" · review={decision['review_at']}" if decision.get("review_at") else ""
            print(
                f"{decision['decision_id']} · {decision['status']} · r{decision['revision']}"
                f"{review} · {_knowledge_display(decision['statement'])}",
                file=output,
            )
        return
    if command in {"record", "status"}:
        assert isinstance(primitive, cli.Mapping)
        print(
            f"{primitive['decision_id']} · {primitive['status']} · "
            f"r{primitive['revision']} · {_knowledge_display(primitive['statement'])}",
            file=output,
        )
        return
    _knowledge_json(payload, output)

def _run_decision(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    database, _ = _knowledge_paths(args, settings)
    command = args.decision_command
    mutating = command == "record"
    if not database.is_file() and not mutating:
        if command in {"list", "due"}:
            payload: object = ()
            if args.json:
                _knowledge_json(payload, output)
            else:
                _render_decision_human(command, payload, output)
            return cli.EXIT_OK
        raise ValueError("Knowledge DB has not been created; record a Decision first")
    store = cli.KnowledgeStore(database)
    try:
        if command == "record":
            payload = store.create_decision(
                statement=args.statement,
                rationale=args.rationale,
                status=cli.DecisionStatus(args.status),
                intent_id=args.intent_id,
                evidence_pack_id=args.evidence_pack_id,
                supersedes_decision_id=args.supersedes,
                review_at=args.review_at,
                actor=args.actor,
            )
        elif command == "list":
            payload = store.list_decisions(
                limit=cli.knowledge_limit(args.limit, label="Decision list limit")
            )
        elif command == "show":
            verified = store.verified_decision(args.decision_id)
            if verified is None:
                raise ValueError(f"Decision was not found: {args.decision_id}")
            decision, _ = verified
            payload = {
                "decision": decision,
                "history": store.decision_history(args.decision_id),
            }
        elif command == "status":
            payload = store.set_decision_status(
                args.decision_id,
                cli.DecisionStatus(args.status),
            )
        elif command == "due":
            payload = store.due_decisions(
                as_of=args.as_of,
                limit=cli.knowledge_limit(args.limit, label="Decision due limit"),
            )
        else:
            raise ValueError(f"Unknown decision command: {command}")
    finally:
        store.close()
    if args.json:
        _knowledge_json(payload, output)
    else:
        _render_decision_human(command, payload, output)
    return cli.EXIT_OK

def _render_question_human(command: str, payload: object, output: TextIO) -> None:
    primitive = cli.to_primitive(payload)
    if command == "list":
        assert isinstance(primitive, list)
        if not primitive:
            print("No Questions yet.", file=output)
        for question in primitive:
            print(f"{question['question_id']} · {question['status']} · r{question['revision']} · {_knowledge_display(question['prompt'])}", file=output)
        return
    if command in {"create", "status"}:
        assert isinstance(primitive, cli.Mapping)
        print(f"{primitive['question_id']} · {primitive['status']} · r{primitive['revision']} · {_knowledge_display(primitive['prompt'])}", file=output)
        return
    _knowledge_json(payload, output)

def _run_question(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    database, _ = _knowledge_paths(args, settings)
    command = args.question_command
    mutating = command == "create"
    if not database.is_file() and not mutating:
        if command == "list":
            payload: object = ()
            if args.json: _knowledge_json(payload, output)
            else: _render_question_human(command, payload, output)
            return cli.EXIT_OK
        raise ValueError("Knowledge DB has not been created; create a Question first")
    store = cli.KnowledgeStore(database)
    try:
        if command == "create":
            payload = store.create_question(
                prompt=args.prompt, owner=args.owner, status=cli.QuestionStatus(args.status), intent_id=args.intent_id,
                decision_id=args.decision_id, evidence_pack_id=args.evidence_pack_id,
                answer_criteria=tuple(args.answer_criterion), knowledge_query=args.knowledge_query, review_at=args.review_at,
            )
        elif command == "list":
            payload = store.list_questions(status=(cli.QuestionStatus(args.status) if args.status else None), limit=cli.knowledge_limit(args.limit, label="Question list limit"))
        elif command == "show":
            verified = store.verified_question(args.question_id)
            if verified is None: raise ValueError(f"Question was not found: {args.question_id}")
            payload = {"question": verified[0], "history": store.question_history(args.question_id)}
        elif command == "status":
            payload = store.set_question_status(args.question_id, cli.QuestionStatus(args.status))
        else:
            raise ValueError(f"Unknown Question command: {command}")
    finally:
        store.close()
    if args.json: _knowledge_json(payload, output)
    else: _render_question_human(command, payload, output)
    return cli.EXIT_OK

def _render_research_human(command: str, payload: object, output: TextIO) -> None:
    primitive = cli.to_primitive(payload)
    if command == "list":
        assert isinstance(primitive, list)
        if not primitive: print("No Research Requests yet.", file=output)
        for request in primitive:
            print(f"{request['request_id']} · {request['status']} · r{request['revision']} · {_knowledge_display(request['title'])}", file=output)
        return
    if command in {"create", "status"}:
        assert isinstance(primitive, cli.Mapping)
        print(f"{primitive['request_id']} · {primitive['status']} · r{primitive['revision']} · {_knowledge_display(primitive['title'])}", file=output)
        return
    if command == "review-propose":
        assert isinstance(primitive, cli.Mapping)
        print(f"Review proposal · question={primitive['question']['question_id']} · research={primitive['research_request']['request_id']} · DRAFT only", file=output)
        return
    if command == "accept":
        assert isinstance(primitive, cli.Mapping)
        print(f"Research accepted · {primitive['research_request']['request_id']} · compiled Intent={primitive['intent']['intent_id']} · no Job started", file=output)
        return
    _knowledge_json(payload, output)

def _run_research(args: cli.argparse.Namespace, settings: dict, output: TextIO) -> int:
    database, _ = _knowledge_paths(args, settings)
    command = args.research_command
    mutating = command in {"create", "review-propose", "accept"}
    if not database.is_file() and not mutating:
        if command == "list":
            payload: object = ()
            if args.json: _knowledge_json(payload, output)
            else: _render_research_human(command, payload, output)
            return cli.EXIT_OK
        raise ValueError("Knowledge DB has not been created; create a Research Request first")
    store = cli.KnowledgeStore(database)
    try:
        if command == "create":
            payload = store.create_research_request(
                title=args.title, objective=args.objective, owner=args.owner, question_id=args.question_id,
                intent_id=args.intent_id, decision_id=args.decision_id, evidence_pack_id=args.evidence_pack_id,
                knowledge_query=args.knowledge_query, required_evidence=tuple(args.required_evidence), freshness_at=args.freshness_at,
                counterargument_required=args.counterargument_required, max_cost_units=args.max_cost_units,
                max_duration_minutes=args.max_duration_minutes,
            )
        elif command == "list":
            payload = store.list_research_requests(status=(cli.ResearchRequestStatus(args.status) if args.status else None), limit=cli.knowledge_limit(args.limit, label="Research Request list limit"))
        elif command == "show":
            request = store.research_request(args.request_id)
            if request is None: raise ValueError(f"Research Request was not found: {args.request_id}")
            payload = {"research_request": request, "history": store.research_history(args.request_id)}
        elif command == "review-propose":
            question, request = store.propose_review_research(args.decision_id, owner=args.owner)
            payload = {"question": question, "research_request": request}
        elif command == "accept":
            request, intent = store.accept_research_request(args.request_id, priority=args.priority)
            payload = {"research_request": request, "intent": intent}
        elif command == "status":
            payload = store.set_research_status(args.request_id, cli.ResearchRequestStatus(args.status))
        else:
            raise ValueError(f"Unknown Research Request command: {command}")
    finally:
        store.close()
    if args.json: _knowledge_json(payload, output)
    else: _render_research_human(command, payload, output)
    return cli.EXIT_OK

def _run_skills_command(
    args: cli.argparse.Namespace,
    settings: dict,
    output: TextIO,
) -> int:
    """Delegate the complete Skill family behind the global CLI boundary."""

    from dynamic_firm.application.skills_cli import run_skills_command

    return run_skills_command(
        args,
        settings,
        output,
        state_path_for=_state_path,
        exit_ok=cli.EXIT_OK,
        exit_runtime=cli.EXIT_RUNTIME,
    )

def _schedule_ports():
    """Bind CLI-owned ingress callbacks without giving Schedule code the CLI."""

    from dynamic_firm.application.schedule_cli import SchedulePorts

    return SchedulePorts(
        state_path_for=_state_path,
        run_config_for=cli._run_config,
        provider_config_for=cli._provider_config,
        run_goal_for=cli.run_goal,
        roster_for=cli._load_active_roster,
        log_tail=cli._gateway_service_log_tail,
        company_goal_route=cli.InputRoute.COMPANY_GOAL,
        exit_ok=cli.EXIT_OK,
    )

def _run_schedule_service_command(
    args: cli.argparse.Namespace, settings: dict, output: TextIO
) -> int:
    """Compatibility callback for the Modern terminal controller."""

    from dynamic_firm.application.schedule_cli import _run_schedule_service_command

    return _run_schedule_service_command(args, settings, output, ports=_schedule_ports())

def _run_schedule_command(
    args: cli.argparse.Namespace,
    settings: dict,
    output: TextIO,
    *,
    provider_factory: ProviderFactory,
) -> int:
    """Delegate Schedule lifecycle dispatch behind the global CLI boundary."""

    from dynamic_firm.application.schedule_cli import run_schedule_command

    return run_schedule_command(
        args,
        settings,
        output,
        provider_factory=provider_factory,
        ports=_schedule_ports(),
    )
