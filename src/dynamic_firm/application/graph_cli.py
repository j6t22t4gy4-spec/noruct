"""Graph operator-control CLI adapter."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from dynamic_firm.evaluation.execution_replica_value import (
    assess_execution_replica_value,
    compare_execution_replica_trials,
    execution_replica_trial_from_payload,
)


@dataclass(frozen=True)
class GraphCliPorts:
    render: Callable[[object, bool, TextIO], int]


@dataclass(frozen=True)
class GraphCommunityPorts:
    evolution_state_path: Callable[[], Path]
    render: Callable[[object, bool, TextIO], int]


@dataclass(frozen=True)
class GraphRegistryPorts:
    preview: Callable[..., object]
    constraints_for_selection: Callable[..., object]
    render: Callable[[object, bool, TextIO], int]


def _require_confirm(args: cli.argparse.Namespace, label: str) -> None:
    if not args.confirm:
        raise ValueError(f"Graph {label} require --confirm")


def run_stateless_graph_command(
    args: cli.argparse.Namespace, *, ports: GraphCliPorts, output: TextIO
) -> int | None:
    """Handle no-state graph analysis commands before opening a registry."""

    if args.graph_command == "replica-evaluate":
        pairs = []
        for single_path, replica_path in args.pair:
            single = execution_replica_trial_from_payload(
                cli.json.loads(Path(single_path).read_text(encoding="utf-8"))
            )
            replica = execution_replica_trial_from_payload(
                cli.json.loads(Path(replica_path).read_text(encoding="utf-8"))
            )
            pairs.append(compare_execution_replica_trials(single, replica))
        return ports.render(
            {"execution_replica_assessment": assess_execution_replica_value(tuple(pairs))},
            args.json,
            output,
        )
    if args.graph_command == "community-inspect":
        payload = cli.json.loads(args.release_file.read_text(encoding="utf-8"))
        release = cli.community_release_from_payload(payload)
        return ports.render(
            {
                "community_publication": _community_publication(release, state="INSPECTED"),
                "runtime_effect": "NONE",
            },
            args.json,
            output,
        )
    if args.graph_command == "community-artifact-inspect":
        payload = cli.json.loads(args.artifact_file.read_text(encoding="utf-8"))
        release = cli.community_release_from_evolution_artifact(payload)
        return ports.render(
            {
                "artifact": payload,
                "community_publication": _community_publication(release, state="INSPECTED"),
                "runtime_effect": "NONE",
            },
            args.json,
            output,
        )
    if args.graph_command == "community-passport-build":
        _require_confirm(args, "passport build")
        if args.output.exists():
            raise ValueError("Community Passport output path already exists")
        observations = cli.json.loads(args.observations.read_text(encoding="utf-8"))
        passport = cli.build_qualified_blueprint_passport(observations)
        args.output.write_text(
            cli.json.dumps(passport.payload(), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return ports.render(
            {"passport_path": str(args.output.resolve()), "passport": passport},
            args.json,
            output,
        )
    return None


def _community_publication(release: object, *, state: str) -> dict[str, object]:
    """Expose only public release data; never leak the local source ref."""

    public_payload = release.public_payload()  # type: ignore[union-attr]
    return {"state": state, "release": public_payload}


def run_graph_registry_command(
    args: cli.argparse.Namespace,
    *,
    control: cli.GraphBlueprintControlService,
    ports: GraphRegistryPorts,
    output: TextIO,
) -> int | None:
    command = args.graph_command
    if command == "list":
        return ports.render(control.catalog(slot=args.slot), args.json, output)
    if command == "show":
        return ports.render(
            control.revision(args.blueprint_id, args.version), args.json, output
        )
    if command == "import":
        _require_confirm(args, "import")
        payload = cli.json.loads(args.payload_file.read_text(encoding="utf-8"))
        return ports.render(control.import_payload(payload), args.json, output)
    if command == "fork":
        _require_confirm(args, "fork")
        source = control.revision(args.source_blueprint_id, args.source_version)
        return ports.render(
            control.fork(source.ref, blueprint_id=args.blueprint_id, version=args.version),
            args.json,
            output,
        )
    if command == "revise":
        _require_confirm(args, "revision")
        source = control.revision(args.source_blueprint_id, args.source_version)
        candidate = control.parse_payload(
            cli.json.loads(args.payload_file.read_text(encoding="utf-8"))
        )
        blueprint, receipt = control.revise(source.ref, candidate, rationale=args.reason)
        return ports.render(
            {"blueprint": blueprint, "revision_receipt": receipt}, args.json, output
        )
    if command == "history":
        receipts = control.revision_receipts(args.blueprint_id)
        return ports.render(
            {
                "revision_receipts": receipts,
                "revision_diffs": tuple(
                    diff
                    for receipt in receipts
                    if (diff := control.revision_diff(receipt.candidate_ref)) is not None
                ),
            },
            args.json,
            output,
        )
    if command == "select":
        _require_confirm(args, "selection")
        blueprint = control.revision(args.blueprint_id, args.version)
        return ports.render(
            {"selection": control.select(
                blueprint.ref,
                slot=args.slot,
                constraints=ports.constraints_for_selection(args, control.selection(slot=args.slot).constraints),
            )},
            args.json,
            output,
        )
    if command == "clear":
        _require_confirm(args, "clear")
        constraints = None
        if args.clear_constraints:
            constraints = cli.GraphUserConstraints()
        return ports.render(
            {"selection": control.select(None, slot=args.slot, constraints=constraints)},
            args.json,
            output,
        )
    if command == "preview":
        selection = control.selection(slot=args.slot)
        if args.blueprint_id is None:
            reference = selection.blueprint_ref
            if reference is None:
                raise ValueError("Graph preview requires --blueprint-id or a selected Blueprint")
        else:
            if args.version is None:
                raise ValueError("Graph preview requires --version with --blueprint-id")
            reference = control.revision(args.blueprint_id, args.version).ref
        preview = ports.preview(args, control, reference, selection.constraints)
        return ports.render(preview, args.json, output)
    return None


def run_graph_community_command(
    args: cli.argparse.Namespace,
    *,
    control: cli.GraphBlueprintControlService,
    community_registry: cli.CommunityBlueprintRegistry,
    ports: GraphCommunityPorts,
    output: TextIO,
) -> int | None:
    command = args.graph_command
    if not command.startswith("community-"):
        return None
    if command == "community-list":
        return ports.render({"community_blueprints": community_registry.list()}, args.json, output)
    if command == "community-prepare":
        _require_confirm(args, "community preparation")
        passport = None if args.passport is None else cli.blueprint_passport_from_payload(
            cli.json.loads(args.passport.read_text(encoding="utf-8"))
        )
        draft = community_registry.prepare(
            control.revision(args.blueprint_id, args.version),
            draft_id=args.draft_id,
            artifact_id=args.artifact_id,
            passport=passport,
        )
        return ports.render(
            {"community_publication": _community_publication(draft.release, state=draft.state.value)},
            args.json,
            output,
        )
    if command in {"community-publish", "community-withdraw"}:
        _require_confirm(args, command.removeprefix("community-"))
        draft = (
            community_registry.publish(args.draft_id)
            if command == "community-publish"
            else community_registry.withdraw(args.draft_id)
        )
        return ports.render(
            {"community_publication": _community_publication(draft.release, state=draft.state.value)},
            args.json,
            output,
        )
    if command == "community-export":
        _require_confirm(args, "export")
        payload = community_registry.export_release(args.draft_id)
        if args.output.exists():
            raise ValueError("Community Graph export path already exists")
        args.output.write_text(cli.json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        return ports.render(
            {"release_path": str(args.output.resolve()), "release": payload}, args.json, output
        )
    if command == "community-artifact-export":
        _require_confirm(args, "artifact export")
        release = cli.community_release_from_payload(
            community_registry.export_release(args.draft_id)
        )
        artifact = cli.community_release_to_evolution_artifact(
            release, release_channel=args.channel
        )
        if args.output.exists():
            raise ValueError("Community Graph artifact export path already exists")
        args.output.write_text(
            cli.json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return ports.render(
            {"artifact_path": str(args.output.resolve()), "artifact": artifact}, args.json, output
        )
    if command == "community-stage":
        _require_confirm(args, "stage")
        release = cli.community_release_from_payload(
            cli.json.loads(args.release_file.read_text(encoding="utf-8"))
        )
        staged = cli.materialize_staged_blueprint(release)
        control.save(staged)
        community_registry.record_stage(release, staged.ref)
        return ports.render({"staged_blueprint": staged}, args.json, output)
    if command == "community-activate":
        _require_confirm(args, "activation")
        blueprint = control.revision(args.blueprint_id, args.version)
        community_registry.stage_for(blueprint.ref)
        return ports.render(
            {"selection": control.select(blueprint.ref, slot=args.slot)},
            args.json,
            output,
        )
    raise ValueError(f"Unsupported Community Graph command: {command}")


def run_natural_graph_edit_command(
    args: cli.argparse.Namespace,
    *,
    control: cli.GraphBlueprintControlService,
    provider: object,
    model_profile: str,
    timeout_seconds: float,
    render: Callable[[object, bool, TextIO], int],
    output: TextIO,
) -> int:
    _require_confirm(args, "natural edit")
    from dynamic_firm.application.natural_graph_editor import propose_natural_graph_edit

    proposal = propose_natural_graph_edit(
        control, provider, blueprint_id=args.blueprint_id, version=args.version,
        instruction=args.instruction, model_profile=model_profile, timeout_seconds=timeout_seconds,
    )
    candidate = proposal.payload()
    if args.output is not None:
        if args.output.exists():
            raise ValueError("Natural Graph candidate path already exists")
        args.output.write_text(
            cli.json.dumps(candidate, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
    payload = {
        "natural_graph_edit": {
            "candidate": candidate,
            "rationale": proposal.rationale,
            "output": None if args.output is None else str(args.output),
            "runtime_effect": "NONE_REQUIRES_EXPLICIT_GRAPH_REVISE_CONFIRM",
        }
    }
    return render(payload, args.json, output)

def _graph_preview_for_config(
    config: RunCommandConfig,
    *,
    control: cli.GraphBlueprintControlService,
    ref,
    slot: str = "default",
    constraints: GraphUserConstraints | None = None,
):
    """Bind one inert prospective Work Order to the current Company state.

    This path deliberately shares the Front Door's authority, ROSTER and hard
    limit projections while stopping before provider creation, Job admission
    persistence, tool grants, budget reservation or Employee dispatch.
    """

    if not config.state_path.exists():
        raise ValueError(
            "Graph preview requires an initialized local Company state. Start Noruct once to create the Company and ROSTER."
        )
    with cli.CompanyStateStore(config.state_path) as company_store:
        company_snapshot = company_store.company()
        try:
            roster_snapshot = cli.decode_active_roster(company_store.roster())
        except cli.RosterSnapshotError as exc:
            raise ValueError(
                "Graph preview requires an active persistent ROSTER. Start Noruct once to initialize it."
            ) from exc
        roster = roster_snapshot.resolve_execution_profiles(config.model)
        operating_decision = cli.classify_company_input(config.goal)
        authority_snapshot = cli.AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=company_snapshot.revision,
            roster_revision=roster_snapshot.revision,
            playbook_revision=company_store.playbook().revision,
            action_policy_digest=cli.kernel_content_digest(
                cli._action_policy(config, workspace_access=True)
            ),
        )
        budget_snapshot = cli.WorkOrderBudgetSnapshot(
            max_model_calls=config.run_limits.max_model_calls,
            max_tool_calls=config.run_limits.max_tool_calls,
            max_cost_usd=config.run_limits.max_cost_usd,
            max_wall_time_ms=config.run_limits.max_wall_time_ms,
        )
        preview_key = cli.hashlib.sha256(
            f"{config.goal}\x00{ref.blueprint_id}\x00{ref.version}\x00{ref.content_digest}".encode("utf-8")
        ).hexdigest()[:24]
        work_order = cli.normalize_work_order(
            config.goal,
            work_order_id=f"graph-preview-{preview_key}",
            requested_outcome=config.goal,
            constraints=(
                "Preview only: no provider, tool, budget lease, Job, or Employee action is authorized.",
            ),
            acceptance_criteria=(
                "Show the predicted execution structure and unresolved admission constraints.",
            ),
            workspace_ref=f"workspace:{cli.WORKSPACE_ID}",
            authority_snapshot=authority_snapshot,
            budget_snapshot=budget_snapshot,
            requested_at=cli.datetime.now(cli.timezone.utc),
            operating_decision=operating_decision,
        )
        return control.preview(
            ref=ref,
            work_order=work_order,
            roster=roster,
            limits=cli.JobLimits(
                max_tasks=6,
                max_concurrency=3,
                max_graph_patches=1,
                max_temporary_roles=2,
                max_total_model_calls=config.run_limits.max_model_calls,
                max_total_tool_calls=config.run_limits.max_tool_calls,
                max_total_cost_usd=config.run_limits.max_cost_usd,
                max_wall_time_ms=config.run_limits.max_wall_time_ms,
            ),
            slot=slot,
            constraints=constraints,
        )


def _render_graph_control(payload: object, *, as_json: bool, output: TextIO) -> int:
    primitive = cli.to_primitive(payload)
    if as_json:
        print(cli.json.dumps(primitive, ensure_ascii=False, sort_keys=True, indent=2), file=output)
        return cli.EXIT_OK
    if not isinstance(primitive, cli.Mapping):
        print(str(primitive), file=output)
        return cli.EXIT_OK
    selection = primitive.get("selection")
    if isinstance(selection, cli.Mapping):
        ref = selection.get("blueprint_ref")
        reference = (
            f"{ref.get('blueprint_id')}@{ref.get('version')}"
            if isinstance(ref, cli.Mapping)
            else "none"
        )
        constraints = selection.get("constraints", {})
        policy = (
            constraints.get("mutation_policy", "BOUNDED_AUTO")
            if isinstance(constraints, cli.Mapping)
            else "BOUNDED_AUTO"
        )
        print(f"Graph selection · {reference} · mutation={policy}", file=output)
    blueprints = primitive.get("blueprints")
    if isinstance(blueprints, list):
        if not blueprints:
            print("No local Graph Blueprints. Import a data-only draft or save a future Job preview.", file=output)
        for item in blueprints:
            if not isinstance(item, cli.Mapping):
                continue
            print(
                f"  {item.get('blueprint_id')}@{item.get('version')} · "
                f"{item.get('origin')} · {item.get('objective_class')} · "
                f"{len(item.get('tasks', ())) if isinstance(item.get('tasks'), list) else 0} task(s) · "
                f"{str(item.get('content_digest', ''))[:12]}…",
                file=output,
            )
    elif "blueprint_id" in primitive:
        print(
            f"Graph Blueprint · {primitive['blueprint_id']}@{primitive.get('version')} · "
            f"{primitive.get('origin')} · digest={str(primitive.get('content_digest', ''))[:16]}…",
            file=output,
        )
        replica_tasks = tuple(
            task
            for task in primitive.get("tasks", ())
            if isinstance(task, cli.Mapping)
            and isinstance(task.get("execution_replica"), cli.Mapping)
        )
        for task in replica_tasks:
            replica = task["execution_replica"]
            print(
                f"  replica · {replica.get('group_id')}/{replica.get('replica_id')} · "
                f"{replica.get('strategy')} · task={task.get('task_id')} · "
                f"scope={replica.get('scope_template')} · "
                f"aggregate={replica.get('aggregation_task_id')}:{replica.get('aggregation')}",
                file=output,
            )
    blueprint = primitive.get("blueprint")
    if isinstance(blueprint, cli.Mapping):
        print(
            f"Graph Blueprint · {blueprint.get('blueprint_id')}@{blueprint.get('version')} · "
            f"{blueprint.get('origin')} · digest={str(blueprint.get('content_digest', ''))[:16]}…",
            file=output,
        )
    receipt = primitive.get("revision_receipt")
    if isinstance(receipt, cli.Mapping):
        print(
            f"Revision · {receipt.get('status')} · {receipt.get('reason')} · "
            f"receipt={str(receipt.get('content_digest', ''))[:16]}…",
            file=output,
        )
    receipts = primitive.get("revision_receipts")
    if isinstance(receipts, list):
        if not receipts:
            print("No local Graph revision receipts.", file=output)
        for item in receipts:
            if not isinstance(item, cli.Mapping):
                continue
            source = item.get("source_ref", {})
            candidate = item.get("candidate_ref", {})
            print(
                f"  {source.get('blueprint_id')}@{source.get('version')} → "
                f"{candidate.get('blueprint_id')}@{candidate.get('version')} · "
                f"{item.get('status')} · {item.get('reason')}",
                file=output,
            )
    diffs = primitive.get("revision_diffs")
    if isinstance(diffs, list):
        for item in diffs:
            if not isinstance(item, cli.Mapping):
                continue
            source = item.get("source_ref", {})
            candidate = item.get("candidate_ref", {})
            changed = item.get("changed_tasks", ())
            changed_label = ", ".join(
                f"{entry[0]}[{','.join(str(value) for value in entry[1])}]"
                for entry in changed
                if isinstance(entry, (tuple, list))
                and len(entry) == 2
                and isinstance(entry[1], (tuple, list))
            )
            print(
                f"  diff · {source.get('blueprint_id')}@{source.get('version')} → "
                f"{candidate.get('blueprint_id')}@{candidate.get('version')} · "
                f"+{','.join(item.get('added_task_ids', ())) or '—'} "
                f"−{','.join(item.get('removed_task_ids', ())) or '—'} "
                f"Δ{changed_label or '—'} · "
                f"envelope={','.join(item.get('changed_envelope_fields', ())) or '—'}",
                file=output,
            )
    if "binding_digest" in primitive and "work_mode" in primitive:
        reference = primitive.get("blueprint_ref", {})
        print(
            "Future Job Graph preview · "
            f"{reference.get('blueprint_id')}@{reference.get('version')} · "
            f"{primitive.get('work_mode')} · admission={primitive.get('admission_status')} "
            f"({primitive.get('admission_reason')})",
            file=output,
        )
        print(
            "  limits · "
            f"effective=${float(primitive.get('effective_max_cost_usd', 0.0)):.2f}/"
            f"{primitive.get('effective_max_wall_time_ms')}ms · "
            f"hard=${float(primitive.get('hard_cap_cost_usd', 0.0)):.2f}/"
            f"{primitive.get('hard_cap_wall_time_ms')}ms · "
            f"dependency width={primitive.get('dependency_width')} · "
            f"staffing profiles={primitive.get('distinct_staffing_profile_count')}"
            f"[{','.join(primitive.get('staffing_difference_dimensions', ())) or 'none'}] · "
            f"review={'independent' if primitive.get('requires_independent_review') else 'none'} · "
            f"mutation={primitive.get('mutation_policy')}",
            file=output,
        )
        if primitive.get("execution_replica_count", 0):
            print(
                "  execution replicas · "
                f"{primitive.get('execution_replica_count')} run(s) in "
                f"{len(primitive.get('execution_replica_group_ids', ()))} group(s) · "
                "same persistent Employee, separate RUN_ONLY instances",
                file=output,
            )
        tasks = primitive.get("tasks", ())
        for task in tasks if isinstance(tasks, list) else ():
            if not isinstance(task, cli.Mapping):
                continue
            employee = task.get("proposed_employee_id") or "temporary role required"
            dependency = ",".join(task.get("depends_on", ())) or "start"
            capabilities = ",".join(task.get("required_capabilities", ()))
            print(
                f"  {task.get('task_id')} · after={dependency} · {employee} · {capabilities}",
                file=output,
            )
            if task.get("execution_replica_group_id"):
                print(
                    "    replica · "
                    f"{task.get('execution_replica_group_id')}/{task.get('execution_replica_id')} · "
                    f"{task.get('execution_replica_strategy')} · "
                    f"scope={task.get('execution_replica_scope')} · "
                    f"aggregate={task.get('execution_replica_aggregation_task_id')}:"
                    f"{task.get('execution_replica_aggregation')}",
                    file=output,
                )
        for warning in primitive.get("constraint_warnings", ()):
            print(f"  warning · {warning}", file=output)
        print(
            "Preview only · no Job, budget lease, provider call, tool effect, or Employee run was created.",
            file=output,
        )
    replica_assessment = primitive.get("execution_replica_assessment")
    if isinstance(replica_assessment, cli.Mapping):
        print(
            "Replica value · "
            f"{replica_assessment.get('replica_group_id')} · "
            f"{replica_assessment.get('replica_strategy')} · "
            f"{replica_assessment.get('decision')}",
            file=output,
        )
        print(
            "  no Blueprint revision or automatic topology change was created.",
            file=output,
        )
        for reason in replica_assessment.get("reasons", ()):
            print(f"  evidence · {reason}", file=output)
    return cli.EXIT_OK


def _run_graph_command(
    args: cli.argparse.Namespace,
    settings: dict,
    output: TextIO,
    *,
    provider_factory: ProviderFactory | None = None,
) -> int:
    if provider_factory is None:
        provider_factory = cli._default_provider
    if args.graph_command == "dashboard":
        from dynamic_firm.application.graph_dashboard import (
            GraphDashboardPorts,
            run_graph_dashboard,
        )
        from dynamic_firm.application.operator_surface_read_model import (
            read_operator_surface,
        )

        state_path = cli._state_path(args, settings)

        def resolve_proposal(
            job_id: str,
            proposal_id: str,
            approve: bool,
        ) -> cli.Mapping[str, object]:
            """Compose the only receipt-bound Graph continuation for a GUI click."""

            portfolio_path = state_path.with_name(f"{state_path.stem}.work-orders.db")
            try:
                with cli.WorkOrderPortfolioStore(portfolio_path) as work_orders:
                    request = work_orders.continuation_request(job_id)
            except KeyError as exc:
                raise ValueError("No retained Graph continuation request exists") from exc
            config_args = cli.argparse.Namespace(**{**vars(args), "goal": request.goal})
            config = cli._run_config(config_args, settings)
            if config.state_path != state_path:
                raise ValueError("Graph continuation state does not match dashboard state")
            result = cli.asyncio.run(
                cli._continue_graph_proposal_runtime(
                    config=config,
                    provider=provider_factory(cli._provider_config(config)),
                    job_id=job_id,
                    proposal_id=proposal_id,
                    approve=approve,
                    approval_port=None,
                )
            )
            return {"job_status": result.status.value}

        def save_future_constraints(payload: cli.Mapping[str, object]) -> cli.Mapping[str, object]:
            saved = cli.save_future_graph_constraints(
                state_path,
                max_concurrency=payload["max_concurrency"],  # type: ignore[arg-type]
                max_cost_usd=payload["max_cost_usd"],  # type: ignore[arg-type]
                max_wall_time_ms=payload["max_wall_time_ms"],  # type: ignore[arg-type]
                mutation_policy=cli.GraphMutationPolicy(str(payload["mutation_policy"])),
            )
            return {
                "blueprint_id": (
                    saved.blueprint_ref.blueprint_id
                    if saved.blueprint_ref is not None
                    else None
                ),
                "version": saved.blueprint_ref.version if saved.blueprint_ref is not None else None,
                "max_concurrency": saved.constraints.max_concurrency,
                "max_cost_usd": saved.constraints.max_cost_usd,
                "max_wall_time_ms": saved.constraints.max_wall_time_ms,
                "mutation_policy": saved.constraints.mutation_policy.value,
            }

        return run_graph_dashboard(
            args,
            ports=GraphDashboardPorts(
                graph_snapshot=lambda: cli.graph_control_snapshot(state_path),
                job_catalog=lambda: cli.job_audit_catalog(state_path),
                job_snapshot=lambda job_id: cli.job_audit_snapshot(state_path, job_id),
                operator_snapshot=lambda: read_operator_surface(
                    state_path
                ).operator_snapshot,
                resolve_proposal=resolve_proposal,
                save_future_constraints=save_future_constraints,
            ),
            output=output,
        )
    from dynamic_firm.application.graph_cli import GraphCliPorts, run_stateless_graph_command

    stateless_result = run_stateless_graph_command(
        args,
        ports=GraphCliPorts(
            render=lambda payload, as_json, target: _render_graph_control(
                payload, as_json=as_json, output=target
            )
        ),
        output=output,
    )
    if stateless_result is not None:
        return stateless_result
    state_path = cli._state_path(args, settings)
    registry = cli.SQLiteGraphBlueprintRegistry(cli.graph_registry_path(state_path))
    control = cli.GraphBlueprintControlService(registry)
    community_registry = cli.CommunityBlueprintRegistry(
        cli.community_blueprint_registry_path(state_path)
    )
    try:
        if args.graph_command == "natural-edit":
            from dynamic_firm.application.graph_cli import run_natural_graph_edit_command

            config_args = cli.argparse.Namespace(**vars(args))
            config_args.goal = args.instruction
            config = cli._run_config(config_args, settings, validate_runtime=False)
            return run_natural_graph_edit_command(
                args,
                control=control,
                provider=provider_factory(cli._provider_config(config)),
                model_profile=config.model,
                timeout_seconds=min(
                    config.request_timeout_seconds,
                    config.run_limits.max_wall_time_ms / 1000,
                ),
                render=lambda payload, as_json, target: _render_graph_control(
                    payload, as_json=as_json, output=target
                ),
                output=output,
            )
        from dynamic_firm.application.graph_cli import (
            GraphCommunityPorts,
            run_graph_community_command,
        )

        community_result = run_graph_community_command(
            args,
            control=control,
            community_registry=community_registry,
            ports=GraphCommunityPorts(
                evolution_state_path=lambda: cli._evolution_state_path(args, settings),
                render=lambda payload, as_json, target: _render_graph_control(
                    payload, as_json=as_json, output=target
                ),
            ),
            output=output,
        )
        if community_result is not None:
            return community_result
        from dynamic_firm.application.graph_cli import (
            GraphRegistryPorts,
            run_graph_registry_command,
        )

        def preview_graph(
            candidate: cli.argparse.Namespace,
            graph_control: cli.GraphBlueprintControlService,
            ref: object,
            existing_constraints: object,
        ) -> object:
            config = cli._run_config(candidate, settings, validate_runtime=False)
            return _graph_preview_for_config(
                config,
                control=graph_control,
                ref=ref,  # type: ignore[arg-type]
                slot=candidate.slot,
                constraints=cli.graph_constraints_from_args(
                    candidate,
                    existing=existing_constraints,  # type: ignore[arg-type]
                    include_budget=False,
                ),
            )

        registry_result = run_graph_registry_command(
            args,
            control=control,
            ports=GraphRegistryPorts(
                preview=preview_graph,
                constraints_for_selection=lambda candidate, existing: cli.graph_constraints_from_args(
                    candidate, existing=existing  # type: ignore[arg-type]
                ),
                render=lambda payload, as_json, target: _render_graph_control(
                    payload, as_json=as_json, output=target
                ),
            ),
            output=output,
        )
        if registry_result is not None:
            return registry_result
        raise ValueError(f"Unknown graph command: {args.graph_command}")
    finally:
        community_registry.close()
        registry.close()
