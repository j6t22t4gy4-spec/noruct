from __future__ import annotations

"""Secondary Textual modals for the Modern terminal surface.

These screens are pure presentation adapters. They return model ids, inert
future-Job Graph submissions, or no value for a read-only audit; the terminal
controller remains the only caller that applies a returned command or state.
"""

import json
import math
from typing import Any, Mapping

from dynamic_firm.runtime.models import ApprovalDecision, ApprovalRequest

from .route_operator_projection import RouteOperatorProjection

from .modern_tui_graph_screen import create_graph_control_screen


def create_secondary_terminal_screens(
    *,
    ComposeResult: Any,
    Container: Any,
    Grid: Any,
    Horizontal: Any,
    ModalScreen: Any,
    Button: Any,
    Input: Any,
    Static: Any,
) -> tuple[type[Any], type[Any], type[Any], type[Any]]:
    """Create optional-framework modal classes after Textual is available."""

    class ApprovalScreen(ModalScreen[ApprovalDecision]):
        CSS = """
        ApprovalScreen { align: center middle; }
        #approval-card { width: 88; max-width: 96%; height: auto; border: heavy $warning;
          background: $surface; padding: 1 2; }
        .approval-actions { height: auto; margin-top: 1; }
        #approval-preview { height: auto; max-height: 14; overflow-y: auto; }
        """

        def __init__(self, request: ApprovalRequest) -> None:
            super().__init__()
            self._request = request

        def compose(self) -> ComposeResult:
            preview = self._request.preview.strip()[:2_000] or "Action approval required"
            with Container(id="approval-card"):
                yield Static(
                    f"Approval required · {self._request.tool_name}",
                    classes="modal-title",
                )
                yield Static(
                    "Effect: {effect}  ·  Risk: {risk}\nTarget: {target}".format(
                        effect=self._request.effect.value,
                        risk=self._request.risk.value,
                        target=self._request.resource_key[:240],
                    ),
                    classes="settings-entry",
                    markup=False,
                )
                yield Static(preview, id="approval-preview", markup=False)
                with Horizontal(classes="approval-actions"):
                    yield Button("Allow once", id="allow-once", variant="success")
                    if self._request.allow_session:
                        yield Button("Allow session", id="allow-session", variant="primary")
                    yield Button("Deny", id="deny", variant="error")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            decisions = {
                "allow-once": ApprovalDecision.ALLOW_ONCE,
                "allow-session": ApprovalDecision.ALLOW_SESSION,
                "deny": ApprovalDecision.DENY,
            }
            self.dismiss(decisions.get(event.button.id or "", ApprovalDecision.DENY))

    class ModelScreen(ModalScreen[str | None]):
        """A bounded model chooser; the controller remains configuration owner."""

        CSS = """
        ModelScreen { align: center middle; }
        #model-card { width: 88; max-width: 96%; height: auto; max-height: 88%;
          border: heavy $accent; background: $surface; padding: 1 2; overflow-y: auto; }
        #model-grid { grid-size: 2 10; grid-columns: 1fr 1fr;
          grid-rows: 3 3 3 3 3 3 3 3 3 3; height: auto; margin-top: 1; }
        .model-option { background: $panel; color: $text; }
        .model-current { background: #315fbe; color: #ffffff; text-style: bold; }
        """

        def __init__(self, options: tuple[object, ...], provider: str) -> None:
            super().__init__()
            self._options = options[:20]
            self._provider = provider

        def compose(self) -> ComposeResult:
            with Container(id="model-card"):
                yield Static("CHOOSE MODEL", classes="modal-title")
                yield Static(
                    f"Provider · {self._provider}\nOnly locally configured or discovered model IDs are shown.",
                    classes="settings-entry",
                    markup=False,
                )
                with Grid(id="model-grid"):
                    for index, option in enumerate(self._options):
                        model_id = str(getattr(option, "model_id", ""))
                        detail = str(getattr(option, "detail", ""))
                        current = bool(getattr(option, "current", False))
                        label = f"{'● ' if current else ''}{model_id}\n{detail}"
                        yield Button(
                            label,
                            id=f"model-option-{index}",
                            classes="model-option model-current" if current else "model-option",
                        )
                with Horizontal(classes="settings-actions"):
                    yield Button("Cancel", id="model-close")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id or ""
            if button_id == "model-close":
                self.dismiss(None)
                return
            if not button_id.startswith("model-option-"):
                return
            try:
                option = self._options[int(button_id.removeprefix("model-option-"))]
            except (IndexError, ValueError):
                return
            model_id = str(getattr(option, "model_id", "")).strip()
            if model_id:
                self.dismiss(model_id)
    class JobAuditScreen(ModalScreen[object]):
        """ACTIVE JOB graph inspection with narrowly receipt-bound decisions.

        The controller supplies a strict, content-free audit projection.  This
        modal keeps lineage, checkpoint, revision and budget data read-only.
        It may return only an explicit Graph proposal decision or an opaque
        read-only continuation request. The controller owns all authority.
        """

        CSS = """
        JobAuditScreen { align: center middle; }
        #job-audit-card { width: 108; max-width: 98%; height: 88%; border: heavy $accent;
          background: $surface; padding: 1 2; overflow-y: auto; }
        #job-audit-summary, #job-audit-recovery, #job-audit-change-summary, #job-audit-observed-execution, #job-audit-route-admissions, #job-audit-model-invocations, #job-audit-graph, #job-audit-proposals, #job-audit-checkpoints { height: auto;
          margin-top: 1; color: $text-muted; }
        #job-audit-checkpoints { max-height: 22; overflow-y: auto; }
        #job-audit-catalog { height: auto; max-height: 10; overflow-y: auto; margin-top: 1; }
        """

        def __init__(
            self,
            snapshot: Mapping[str, object],
            *,
            catalog: Mapping[str, object],
        ) -> None:
            super().__init__()
            self._snapshot = snapshot
            raw_jobs = catalog.get("jobs", ()) if isinstance(catalog, Mapping) else ()
            self._catalog = tuple(
                item for item in raw_jobs if isinstance(item, Mapping)
            ) if isinstance(raw_jobs, (tuple, list)) else ()
            graph = self._mapping(snapshot.get("graph"))
            job = self._mapping(snapshot.get("job"))
            self._pending_proposals = tuple(
                str(item.get("proposal_id", "")).strip()
                for item in self._items(graph.get("proposals"))
                if str(item.get("status", "")) == "PENDING"
                and str(item.get("proposal_id", "")).strip()
                and str(job.get("job_id", "")).strip()
            )

        @staticmethod
        def _mapping(value: object) -> Mapping[str, object]:
            return value if isinstance(value, Mapping) else {}

        @staticmethod
        def _items(value: object) -> tuple[Mapping[str, object], ...]:
            if not isinstance(value, (tuple, list)):
                return ()
            return tuple(item for item in value if isinstance(item, Mapping))

        def compose(self) -> ComposeResult:
            job = self._mapping(self._snapshot.get("job"))
            graph = self._mapping(self._snapshot.get("graph"))
            checkpoints = self._items(self._snapshot.get("checkpoints"))
            with Container(id="job-audit-card"):
                yield Static("RETAINED JOB AUDIT", classes="modal-title")
                with Horizontal(classes="settings-actions"):
                    yield Button("Close", id="job-audit-close", variant="primary")
                current_job_id = str(job.get("job_id", ""))
                if self._catalog:
                    yield Static("RECENT RETAINED JOBS", classes="settings-section")
                    with Container(id="job-audit-catalog"):
                        for index, candidate in enumerate(self._catalog[:20]):
                            candidate_id = str(candidate.get("job_id", ""))
                            if not candidate_id:
                                continue
                            label = (
                                f"{candidate_id[:44]} · {candidate.get('audit_status', 'unknown')} / "
                                f"{candidate.get('job_status', 'unknown')} · "
                                f"v{candidate.get('final_graph_version', 0)}"
                            )
                            classes = "settings-capability" + (
                                " settings-focused" if candidate_id == current_job_id else ""
                            )
                            yield Button(
                                label,
                                id=f"job-audit-select-{index}",
                                classes=classes,
                            )
                if not job:
                    error = str(self._snapshot.get("error", "")).strip()
                    requested = str(self._snapshot.get("requested_job_id", "")).strip()
                    yield Static(
                        error or (
                            "No retained ACTIVE JOB audit is available yet. This view never starts a Job or creates a graph."
                        ) + (f" Requested: {requested}" if requested else ""),
                        id="job-audit-summary",
                        markup=False,
                    )
                else:
                    summary = (
                        f"Job · {job.get('job_id', 'unknown')}\n"
                        f"State · {job.get('audit_status', 'unknown')} / {job.get('job_status', 'unknown')}\n"
                        f"Mode · {job.get('company_work_mode', 'unknown')} · "
                        f"Policy · {job.get('coordination_policy', 'unknown')} · "
                        f"Effect · {job.get('requested_effect', 'unknown')}\n"
                        f"Replay · {'verified' if job.get('replay_matches') else 'not verified'} · "
                        f"Graph v{job.get('final_graph_version', 0)} · "
                        f"attempts={job.get('attempt_count', 0)} · mutations={job.get('mutation_count', 0)}"
                    )
                    yield Static(summary, id="job-audit-summary", markup=False)
                    recovery = self._mapping(self._snapshot.get("recovery"))
                    if recovery:
                        cancellation_count = int(
                            recovery.get("provider_cancellation_receipt_count", 0) or 0
                        )
                        incomplete_count = int(
                            recovery.get("incomplete_cancellation_event_count", 0) or 0
                        )
                        timeout_count = int(
                            recovery.get("timeout_terminal_run_count", 0) or 0
                        )
                        effect = str(recovery.get("effect_recovery_disposition", ""))
                        recovery_text = (
                            "RECOVERY BOUNDARY · {state}\n"
                            "Provider cancellations={cancellations} · incomplete cancellation events={incomplete} · timeouts={timeouts}"
                        ).format(
                            state=recovery.get("state", "unknown"),
                            cancellations=cancellation_count,
                            incomplete=incomplete_count,
                            timeouts=timeout_count,
                        )
                        if effect:
                            recovery_text += f" · effect={effect}"
                        recovery_text += "\nThis screen never resumes interrupted work; use an explicit replacement path after review."
                        yield Static(recovery_text, id="job-audit-recovery", markup=False)
                    if self._pending_proposals:
                        # A pending proposal is the only mutable control in
                        # this otherwise read-only audit. Keep it near the
                        # retained Job identity rather than below the long
                        # lineage/checkpoint projection, where it can fall
                        # outside a compact terminal's reliable mouse area.
                        yield Static(
                            "PENDING PROPOSAL DECISION",
                            classes="settings-section",
                        )
                        yield Static(
                            "Approve applies the exact validated patch. Reject resumes the exact prior Graph. Either choice is one-shot and revalidates the retained Work Order and receipt.",
                            classes="settings-entry",
                            markup=False,
                        )
                        for index, _proposal_id in enumerate(self._pending_proposals):
                            with Horizontal(classes="settings-actions"):
                                yield Button(
                                    f"Approve proposal #{index + 1}",
                                    id=f"job-audit-approve-{index}",
                                    variant="success",
                                )
                                yield Button(
                                    f"Reject proposal #{index + 1}",
                                    id=f"job-audit-reject-{index}",
                                    variant="error",
                                )
                    continuation = self._mapping(self._snapshot.get("read_only_continuation"))
                    if continuation.get("candidate") is True:
                        yield Static("READ-ONLY PREFIX CONTINUATION", classes="settings-section")
                        yield Static(
                            "A successful read-only prefix may be resumable. Resume reopens the local Work Order, rechecks the frozen request and policy, then consumes a one-shot receipt. It never resumes in-flight or effectful work.",
                            classes="settings-entry",
                            markup=False,
                        )
                        yield Button(
                            "Recheck and resume read-only prefix",
                            id="job-audit-resume-read-only",
                            variant="warning",
                        )
                        yield Static(
                            "Transfer authority only · the other enrolled device must already retain this exact Work Order and receipt prefix.",
                            classes="settings-entry",
                            markup=False,
                        )
                        yield Input(
                            placeholder="Target enrolled device id (for example: device-laptop-b)",
                            id="job-audit-handoff-device",
                        )
                        yield Button(
                            "Transfer read-only continuation authority",
                            id="job-audit-handoff-read-only",
                            variant="error",
                        )
                    yield Static("INITIAL → FINAL SUMMARY", classes="settings-section")
                    revisions = self._items(graph.get("revisions"))
                    change_summary = self._mapping(graph.get("change_summary"))
                    operations = self._mapping(change_summary.get("accepted_operations"))
                    operation_text = ", ".join(
                        f"{name}×{count}"
                        for name, count in sorted(operations.items())
                    ) or "none"
                    statuses = self._mapping(change_summary.get("final_task_status_counts"))
                    status_text = ", ".join(
                        f"{name}={count}"
                        for name, count in sorted(statuses.items())
                    ) or "not retained"
                    yield Static(
                        "Graph · v{initial} → v{final} · accepted revisions={revisions}\n"
                        "Digest · {before} → {after}\n"
                        "Accepted operations · {operations} · reserved lease Δ=${cost:.6f}\n"
                        "Final task states · total={tasks} · {statuses} · replica groups={replicas}\n"
                        "Meaning · structural change summary only; it does not attribute quality or cost outcomes.".format(
                            initial=change_summary.get("initial_graph_version", 1),
                            final=change_summary.get("final_graph_version", job.get("final_graph_version", 0)),
                            revisions=change_summary.get("accepted_revision_count", len(revisions)),
                            before=str(change_summary.get("initial_digest", graph.get("initial_digest", "")))[:16] or "unavailable",
                            after=str(change_summary.get("final_digest", graph.get("initial_digest", "")))[:16] or "unavailable",
                            operations=operation_text,
                            cost=float(change_summary.get("total_reserved_cost_delta", 0.0) or 0.0),
                            tasks=change_summary.get("final_task_count", 0),
                            statuses=status_text,
                            replicas=change_summary.get("execution_replica_group_count", 0),
                        ),
                        id="job-audit-change-summary",
                        markup=False,
                    )
                    observed = self._mapping(self._snapshot.get("observed_execution"))
                    observed_tasks = self._mapping(observed.get("task_status_counts"))
                    observed_validations = self._mapping(
                        observed.get("coding_validation_status_counts")
                    )
                    task_text = ", ".join(
                        f"{name}={count}" for name, count in sorted(observed_tasks.items())
                    ) or "not retained"
                    validation_text = ", ".join(
                        f"{name}={count}"
                        for name, count in sorted(observed_validations.items())
                    ) or "NOT_RUN"
                    yield Static("OBSERVED EXECUTION", classes="settings-section")
                    yield Static(
                        "Terminal · {terminal}\n"
                        "Task states · {tasks}\n"
                        "Coding validation receipts · {validations}\n"
                        "Effect receipt lifecycle · {effect} · work outcome · {outcome}\n"
                        "Meaning · recorded execution facts only; no causal Graph-impact claim.".format(
                            terminal=observed.get("terminal_status", "NOT_RECORDED"),
                            tasks=task_text,
                            validations=validation_text,
                            effect=observed.get("effect_receipt_status", "NOT_RUN"),
                            outcome=observed.get("work_outcome_status", "NOT_VERIFIED"),
                        ),
                        id="job-audit-observed-execution",
                        markup=False,
                    )
                    common_routes = self._items(
                        self._snapshot.get("route_operator_projections")
                    )
                    if common_routes:
                        common_lines: list[str] = []
                        for item in common_routes[:64]:
                            try:
                                projection = RouteOperatorProjection.from_canonical_json(
                                    json.dumps(
                                        item,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    )
                                )
                            except (TypeError, ValueError):
                                continue
                            rows = dict(projection.render_tui_rows())
                            common_lines.append(
                                "Employee · {employee} · Task · {task}\n"
                                "Route · {route} · terminal={terminal}\n"
                                "Selection={reasons} · uncertainty={uncertainty}\n"
                                "Compatibility={compatibility} · egress={egress} · fallback={fallback}".format(
                                    employee=rows["employee_id"],
                                    task=rows["task_id"],
                                    route=rows["route_id"],
                                    terminal=rows["terminal_status"],
                                    reasons=rows["selection_reasons"],
                                    uncertainty=rows["selected_uncertainty"],
                                    compatibility=rows["compatibility"],
                                    egress=rows["egress_policy_state"],
                                    fallback=rows["fallback_state"],
                                )
                            )
                        if common_lines:
                            yield Static(
                                "ROUTE EXECUTION · READ ONLY",
                                classes="settings-section",
                            )
                            yield Static(
                                "\n".join(common_lines)
                                + "\nUnverified egress and unclassified fan-out are explicit; this surface grants no authority.",
                                id="job-audit-route-operator-projections",
                                markup=False,
                            )
                    route_admissions = self._items(
                        self._snapshot.get("route_admissions")
                    )
                    if route_admissions:
                        yield Static(
                            "FROZEN ROUTE ADMISSIONS · READ ONLY",
                            classes="settings-section",
                        )
                        route_lines: list[str] = []
                        for item in route_admissions[:64]:
                            employee = item.get("employee_id")
                            task = item.get("task_id")
                            route = item.get("route_id")
                            reasons = item.get("selection_reasons", ())
                            binding = item.get("binding_digest")
                            selection = item.get("selection_receipt_digest")
                            policy = item.get("selection_policy_digest")
                            snapshot = item.get("intelligence_snapshot_digest")
                            compatibility = item.get("compatibility_evidence_digest")
                            egress = item.get("egress_policy_digest")
                            fallback = item.get("fallback_policy_digest")
                            uncertainty = item.get("selected_uncertainty")
                            if (
                                not all(
                                    isinstance(value, str)
                                    and value
                                    and len(value) <= 192
                                    and value[0].isalnum()
                                    and all(character.isalnum() or character in "._:-" for character in value)
                                    for value in (employee, task, route)
                                )
                                or not isinstance(reasons, (tuple, list))
                                or not reasons
                                or any(
                                    not isinstance(reason, str)
                                    or reason not in {
                                        "HARD_CONSTRAINTS_SATISFIED",
                                        "SIMPLE_ROUTE_TIE_PREFERENCE",
                                        "POLICY_ORDER",
                                    }
                                    for reason in reasons
                                )
                                or not all(
                                    isinstance(value, str)
                                    and len(value) == 64
                                    and all(character in "0123456789abcdef" for character in value)
                                    for value in (
                                        binding,
                                        selection,
                                        policy,
                                        snapshot,
                                        compatibility,
                                        egress,
                                        fallback,
                                    )
                                )
                                or isinstance(uncertainty, bool)
                                or not isinstance(uncertainty, (int, float))
                                or not math.isfinite(float(uncertainty))
                                or not 0 <= float(uncertainty) <= 1
                            ):
                                continue
                            reason_text = ", ".join(reasons)
                            route_lines.append(
                                "Employee · {employee} · Task · {task}\n"
                                "Route · {route} · reasons={reasons}\n"
                                "Binding={binding} · selection={selection} · policy={policy}\n"
                                "Snapshot={snapshot} · compatibility={compatibility}\n"
                                "Egress-policy={egress} · fallback-policy={fallback} · uncertainty={uncertainty:.3f}".format(
                                    employee=employee,
                                    task=task,
                                    route=route,
                                    reasons=reason_text,
                                    binding=binding[:16],
                                    selection=selection[:16],
                                    policy=policy[:16],
                                    snapshot=snapshot[:16],
                                    compatibility=compatibility[:16],
                                    egress=egress[:16],
                                    fallback=fallback[:16],
                                    uncertainty=float(uncertainty),
                                )
                            )
                        if route_lines:
                            yield Static(
                                "\n".join(route_lines)
                                + "\nFrozen status pins only; not an egress/fallback permission or evidence of fallback use.",
                                id="job-audit-route-admissions",
                                markup=False,
                            )
                    invocations = self._items(
                        self._snapshot.get("model_invocations")
                    )
                    invocation_lines: list[str] = []
                    for item in invocations[:128]:
                        # The application projection already whitelists this
                        # data.  Repeat bounded presentation checks here so a
                        # malformed controller snapshot cannot turn this
                        # read-only modal into a raw receipt viewer.
                        terminal = item.get("terminal_status")
                        usage = item.get("usage_availability")
                        cost_state = item.get("cost_availability")
                        latency = item.get("latency_ms")
                        if (
                            not isinstance(terminal, str)
                            or terminal not in {"SUCCEEDED", "FAILED", "CANCELLED", "INDETERMINATE"}
                            or not isinstance(usage, str)
                            or usage not in {"AVAILABLE", "UNAVAILABLE"}
                            or not isinstance(cost_state, str)
                            or cost_state not in {"AVAILABLE", "UNAVAILABLE"}
                            or isinstance(latency, bool)
                            or not isinstance(latency, (int, float))
                            or not math.isfinite(float(latency))
                            or float(latency) < 0
                        ):
                            continue
                        cost_text = "unavailable"
                        if cost_state == "AVAILABLE":
                            cost = item.get("cost_usd")
                            if (
                                isinstance(cost, bool)
                                or not isinstance(cost, (int, float))
                                or not math.isfinite(float(cost))
                                or float(cost) < 0
                            ):
                                continue
                            # Preserve observed zero instead of collapsing it
                            # into unavailable or a quality/cost claim.
                            cost_text = f"${float(cost):.6f}"
                        elif item.get("cost_usd") is not None:
                            continue
                        employee = item.get("employee_id")
                        task = item.get("task_id")
                        route = item.get("route_id")
                        binding = item.get("binding_digest")
                        receipt = item.get("receipt_digest")
                        if not all(
                            isinstance(value, str)
                            and value
                            and len(value) <= 192
                            and value[0].isalnum()
                            and all(character.isalnum() or character in "._:-" for character in value)
                            for value in (employee, task, route)
                        ) or not all(
                            isinstance(value, str)
                            and len(value) == 64
                            and all(character in "0123456789abcdef" for character in value)
                            for value in (binding, receipt)
                        ):
                            continue
                        invocation_lines.append(
                            "Employee · {employee} · Task · {task}\n"
                            "Route · {route} · recorded-terminal={terminal} · usage={usage}\n"
                            "Cost · {cost} ({cost_state}) · latency={latency:.1f}ms\n"
                            "Binding={binding} · receipt={receipt}".format(
                                employee=employee,
                                task=task,
                                route=route,
                                terminal=terminal,
                                usage=usage,
                                cost=cost_text,
                                cost_state=cost_state,
                                latency=float(latency),
                                binding=binding[:16],
                                receipt=receipt[:16],
                            )
                        )
                    if invocation_lines:
                        yield Static(
                            "DURABLE MODEL INVOCATIONS · READ ONLY",
                            classes="settings-section",
                        )
                        yield Static(
                            "\n".join(invocation_lines)
                            + "\nRetained receipt facts only; not a live provider state or permission to resume, reroute, or send data.",
                            id="job-audit-model-invocations",
                            markup=False,
                        )
                    yield Static("GRAPH LINEAGE", classes="settings-section")
                    blueprint = str(graph.get("blueprint", "unbound"))
                    initial = str(graph.get("initial_digest", ""))[:16] or "unavailable"
                    graph_lines = [
                        f"Blueprint · {blueprint}",
                        f"Initial digest · {initial}",
                    ]
                    if not revisions:
                        graph_lines.append("No accepted topology revision; the initial Graph remained authoritative.")
                    for revision in revisions[:32]:
                        graph_lines.append(
                            "r{sequence} {operation} · {before} → {after} · lease Δ${cost:.6f} · {policy}\n"
                            "  expected={expected} · validation={validation} · terminal={terminal}".format(
                                sequence=revision.get("sequence", 0),
                                operation=revision.get("operation", "unknown"),
                                before=str(revision.get("previous_digest", ""))[:12] or "-",
                                after=str(revision.get("next_digest", ""))[:12] or "-",
                                cost=float(revision.get("budget_delta", 0.0) or 0.0),
                                policy=revision.get("approval_policy", "unknown"),
                                expected=revision.get("expected_impact", "unknown"),
                                validation=revision.get("validation_receipt", "unknown"),
                                terminal=revision.get("observed_terminal_outcome", "NOT_OBSERVED"),
                            )
                        )
                    yield Static("\n".join(graph_lines), id="job-audit-graph", markup=False)
                    yield Static("GRAPH PROPOSALS", classes="settings-section")
                    proposals = self._items(graph.get("proposals"))
                    proposal_lines: list[str] = []
                    if not proposals:
                        proposal_lines.append(
                            "No PROPOSE decision was recorded; accepted lineage above remains authoritative."
                        )
                    for proposal in proposals[:32]:
                        lease = self._mapping(proposal.get("proposed_lease"))
                        proposal_id = str(proposal.get("proposal_id", "")).strip()
                        proposal_lines.append(
                            "#{sequence} @ledger {ledger} {status} · {operation} at v{base} · lease Δ calls={calls} tools={tools} cost=${cost:.6f}".format(
                                sequence=proposal.get("sequence", 0),
                                ledger=proposal.get("ledger_sequence", "terminal"),
                                status=proposal.get("status", "unknown"),
                                operation=proposal.get("operation", "unknown"),
                                base=proposal.get("base_graph_version", 0),
                                calls=lease.get("model_calls", 0),
                                tools=lease.get("tool_calls", 0),
                                cost=float(lease.get("cost_usd", 0.0) or 0.0),
                            )
                        )
                        if proposal.get("status") == "PENDING" and proposal_id:
                            proposal_lines.append(
                                "  Decision · noruct continue-graph-proposal {job_id} {proposal_id} approve|reject --confirm".format(
                                    job_id=job.get("job_id", "<job-id>"),
                                    proposal_id=proposal_id,
                                )
                            )
                    yield Static(
                        "\n".join(proposal_lines),
                        id="job-audit-proposals",
                        markup=False,
                    )
                    yield Static("STATE CHECKPOINTS", classes="settings-section")
                    checkpoint_lines: list[str] = []
                    for checkpoint in checkpoints[:48]:
                        task_states = self._items(checkpoint.get("task_states"))
                        states = ", ".join(
                            f"{item.get('task_id', '?')}={item.get('status', '?')}"
                            for item in task_states[:16]
                        ) or "-"
                        changed = ",".join(
                            str(item) for item in checkpoint.get("changed_task_ids", ())
                        ) or "-"
                        parent = str(checkpoint.get("parent_checkpoint_id", ""))[:18] or "root"
                        checkpoint_lines.append(
                            "#{sequence} {event_type} · graph=v{version} · changed={changed} · parent={parent}\n  {states}".format(
                                sequence=checkpoint.get("ledger_sequence", 0),
                                event_type=checkpoint.get("event_type", "unknown"),
                                version=checkpoint.get("graph_version", 0),
                                changed=changed,
                                parent=parent,
                                states=states,
                            )
                        )
                    yield Static(
                        "\n".join(checkpoint_lines) or "No retained checkpoints.",
                        id="job-audit-checkpoints",
                        markup=False,
                    )
                yield Static(
                    "Read-only lineage · checkpoints never resume directly · only explicit receipt-bound actions may re-enter a Job.",
                    classes="settings-entry",
                    markup=False,
                )

        def on_button_pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id or ""
            if button_id.startswith("job-audit-select-"):
                try:
                    index = int(button_id.removeprefix("job-audit-select-"))
                    candidate = self._catalog[index]
                    selected_job_id = str(candidate.get("job_id", ""))
                except (IndexError, ValueError):
                    selected_job_id = ""
                if selected_job_id:
                    self.dismiss(selected_job_id)
                return
            if button_id == "job-audit-close":
                self.dismiss(None)
                return
            if button_id == "job-audit-resume-read-only":
                job_id = str(self._mapping(self._snapshot.get("job")).get("job_id", "")).strip()
                if job_id:
                    self.dismiss(
                        {
                            "intent": "read-only-partial-continuation",
                            "job_id": job_id,
                        }
                    )
                return
            if button_id == "job-audit-handoff-read-only":
                job_id = str(self._mapping(self._snapshot.get("job")).get("job_id", "")).strip()
                try:
                    target_device_id = self.query_one("#job-audit-handoff-device", Input).value.strip()
                except Exception:
                    target_device_id = ""
                if job_id and target_device_id:
                    self.dismiss(
                        {
                            "intent": "read-only-partial-handoff",
                            "job_id": job_id,
                            "target_device_id": target_device_id,
                        }
                    )
                return
            for prefix, approve in (
                ("job-audit-approve-", True),
                ("job-audit-reject-", False),
            ):
                if not button_id.startswith(prefix):
                    continue
                try:
                    index = int(button_id.removeprefix(prefix))
                    proposal_id = self._pending_proposals[index]
                    job_id = str(self._mapping(self._snapshot.get("job")).get("job_id", ""))
                except (IndexError, ValueError):
                    return
                if job_id and proposal_id:
                    self.dismiss(
                        {
                            "intent": "graph-proposal-decision",
                            "job_id": job_id,
                            "proposal_id": proposal_id,
                            "decision": "approve" if approve else "reject",
                        }
                    )
                return

    GraphControlScreen = create_graph_control_screen(
        ComposeResult=ComposeResult,
        Container=Container,
        Grid=Grid,
        Horizontal=Horizontal,
        ModalScreen=ModalScreen,
        Button=Button,
        Input=Input,
        Static=Static,
    )
    return ApprovalScreen, ModelScreen, GraphControlScreen, JobAuditScreen
