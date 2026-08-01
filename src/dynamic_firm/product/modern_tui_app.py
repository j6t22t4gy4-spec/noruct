"""Optional Textual app composition for the Product terminal surface."""

from __future__ import annotations

import importlib.util
import time
from typing import Callable, Mapping, Sequence

from dynamic_firm.product.events import ProductEvent, ProductEventType
from dynamic_firm.product.modern_tui_screens import create_terminal_modal_screens
from dynamic_firm.product.operator_surface import assessment_projection
from dynamic_firm.product.terminal_activity import TerminalFlowProjector
from dynamic_firm.product.terminal_diagnostics import record_modern_terminal_crash
from dynamic_firm.runtime.models import ApprovalDecision, ApprovalRequest
from dynamic_firm.runtime.ports import CancellationToken

from .modern_tui_contracts import (
    ModernTerminalController,
    ModernTerminalSnapshot,
    ModernTerminalUnavailable,
    SessionInputHistory,
)


def modern_terminal_available() -> bool:
    """Return whether the explicitly selected optional surface is installed."""

    return importlib.util.find_spec("textual") is not None


def modern_terminal_install_hint() -> str:
    return "Install the audited optional UI profile: pip install 'noruct[modern-tui]'."


def create_modern_terminal_app(controller: ModernTerminalController):
    """Create the optional app for an interactive terminal or an isolated test.

    This is intentionally a narrow adapter. The controller owns all mutable
    Company/session state and supplies the only goal/command execution paths.
    """

    if not modern_terminal_available():
        raise ModernTerminalUnavailable(modern_terminal_install_hint())

    from textual.app import App, ComposeResult
    from textual.css.query import NoMatches
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Input, Static

    ApprovalScreen, SettingsScreen, ModelScreen, GraphControlScreen, JobAuditScreen = create_terminal_modal_screens()

    _COMMAND_HINTS: tuple[tuple[str, str], ...] = (
        ("/settings", "runtime settings"),
        ("/company-coordination", "configure multi-device opaque lease coordination"),
        ("/capabilities", "show every configured and connectable capability"),
        ("/provider-login", "sign in with the selected account provider"),
        ("/model", "choose or switch model"),
        ("/permission", "allow or restrict workspace changes"),
        ("/external-state", "set external action approval posture"),
        ("/tools", "show effective tool surface"),
        ("/review", "approval policy"),
        ("/mode", "context economy mode"),
        ("/skills", "inspect available skills"),
        ("/knowledge", "search the knowledge runtime"),
        ("/workbench", "show Knowledge · Intent · Decision · Job relations"),
        ("/workbench ready", "check evidence before an explicit Intent run"),
        ("/graph", "set future-job execution Blueprint controls"),
        ("/portfolio", "inspect, queue, or explicitly drain local Work Orders"),
        ("/job", "inspect the latest immutable Job graph and checkpoints"),
        ("/status", "company and session status"),
        ("/help", "all local commands"),
    )

    class _ApprovalController:
        def __init__(self, app: "_ModernTerminalApp") -> None:
            self._app = app
            self._workspace_edits_allowed = False

        async def request(
            self,
            request: ApprovalRequest,
            cancellation: CancellationToken,
        ) -> ApprovalDecision:
            cancellation.raise_if_cancelled()
            if self._workspace_edits_allowed and request.allow_session:
                return ApprovalDecision.ALLOW_SESSION
            decision = await self._app.push_screen_wait(ApprovalScreen(request))
            cancellation.raise_if_cancelled()
            if decision == ApprovalDecision.ALLOW_SESSION and request.allow_session:
                self._workspace_edits_allowed = True
            return decision

    class _ModernTerminalApp(App[None]):
        CSS = """
        Screen { background: #10131a; color: #e6edf3; }
        #company-header { height: auto; border: heavy #6f8cff; background: #161b26;
          color: #dbe5ff; padding: 1 2; }
        #company-header.compact { border: solid #6f8cff; padding: 0 1; }
        #company-status { height: auto; color: #9fb4d8; padding: 0 2; background: #131925; }
        #company-shell { height: 1fr; margin: 0 1; }
        #company-watch { width: 34; min-width: 29; max-width: 38; border: round #2f5d78;
          background: #0d1520; padding: 0 1; }
        #company-watch-label, #pulse-label, #assessment-label, #activity-label { height: auto; color: #72c9fa;
          padding-top: 1; text-style: bold; }
        #company-watch-body, #company-pulse { height: auto; color: #aebcd2; padding-bottom: 1; }
        #company-pulse { color: #d8e1ee; border-top: solid #263950; padding-top: 1; }
        #assessment-wrap { width: 1fr; margin-left: 1; }
        #current-assessment { height: auto; min-height: 6; max-height: 8; border: round #303a54; padding: 0 1; background: #0c0f15;
          color: #c3d0e5; }
        #activity-feed { height: 1fr; min-height: 6; border: round #263950; padding: 0 1; background: #0b111a;
          color: #b8c8dc; overflow-y: auto; }
        #answer-label { height: auto; color: #92f7c4; padding: 0 2; }
        #answer { height: auto; min-height: 3; max-height: 10; overflow-y: auto;
          border: round #315b48; margin: 0 1; padding: 0 1; background: #101915; }
        #composer-wrap { height: auto; border-top: solid #35405a; padding: 1 1 0 1;
          background: #151925; }
        #composer { border: tall #6f8cff; background: #0c0f15; }
        #command-menu { display: none; height: auto; max-height: 7; overflow-y: auto;
          border: round #405a89; background: #0b1320; color: #b8cced; margin: 0 1; padding: 0 1; }
        #shortcuts { height: auto; color: #8190ad; padding: 0 1 1 1; }
        .modal-title { color: #ffcf7b; text-style: bold; }
        """
        TITLE = "Noruct"
        SUB_TITLE = "Dynamic Firm Runtime"
        BINDINGS = [
            ("f2", "open_settings", "Settings"),
            ("f3", "show_commands", "Commands"),
            ("f4", "open_job_audit", "Job audit"),
            ("ctrl+l", "reset_assessment", "Reset assessment"),
            ("ctrl+o", "toggle_operator_surface", "Toggle company view"),
            ("ctrl+c", "quit", "Quit"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._answer_parts: list[str] = []
            self._working = False
            self._approval = _ApprovalController(self)
            self._input_history = SessionInputHistory()
            self._turn_started_at: float | None = None
            self._turn_stage = "READY"
            self._last_activity = "Company ready"
            self._current_status = "Company ready"
            self._active_goal = "No goal submitted yet"
            self._flow = TerminalFlowProjector()
            self._operator_surface_visible = True
            self._operator_snapshot: Mapping[str, object] = {}
            self._last_company_report: Mapping[str, object] = {}
            self._pulse_timer = None

        def compose(self) -> ComposeResult:
            yield Static(id="company-header", markup=False)
            yield Static("Company ready", id="company-status", markup=False)
            with Horizontal(id="company-shell"):
                with Vertical(id="company-watch"):
                    yield Static("COMPANY WATCH", id="company-watch-label")
                    yield Static(id="company-watch-body", markup=False)
                    yield Static("RUN PULSE", id="pulse-label")
                    yield Static(id="company-pulse", markup=False)
                with Vertical(id="assessment-wrap"):
                    yield Static("CURRENT ASSESSMENT", id="assessment-label")
                    yield Static(id="current-assessment", markup=False)
                    yield Static("ACTIVITY TIMELINE", id="activity-label")
                    yield Static(id="activity-feed", markup=False)
            yield Static("LATEST COMPANY ANSWER", id="answer-label")
            yield Static("No completed answer yet.", id="answer", markup=False)
            with Vertical(id="composer-wrap"):
                yield Input(
                    placeholder="Give the company a goal, or type / for commands",
                    id="composer",
                )
                yield Static(id="command-menu", markup=False)
                yield Static(
                    "Type / for commands · Tab completes · F2 settings · F3 commands · F4 Job audit · Ctrl+L clear",
                    id="shortcuts",
                    markup=False,
                )

        def on_mount(self) -> None:
            self._replace_history(controller.input_history())
            self._render_snapshot()
            for message in controller.initial_messages():
                self._write_activity(message)
            self._pulse_timer = self.set_interval(0.5, self._tick_operator_surface)
            self.query_one(Input).focus()

        def on_unmount(self) -> None:
            """Stop the pulse before a closed screen can receive a late timer tick."""

            if self._pulse_timer is not None:
                self._pulse_timer.stop()
                self._pulse_timer = None

        def _replace_history(self, values: Sequence[str]) -> None:
            """Load one Company session's complete, bounded composer history."""

            self._input_history.replace(values)

        def _remember_history(self, value: str) -> None:
            self._input_history.remember(value)

        def _move_history(self, direction: int) -> bool:
            if self._working:
                return False
            composer = self.query_one(Input)
            if self.focused is not composer:
                return False
            selection = self._input_history.move(direction, composer.value)
            if selection is None:
                return False
            composer.value = selection.value
            composer.cursor_position = len(composer.value)
            if selection.restored_draft:
                self._set_status("Composer draft restored")
            else:
                assert selection.index is not None
                self._set_status(f"Session input history {selection.index + 1}/{selection.total}")
            return True

        def _command_matches(self, value: str) -> tuple[tuple[str, str], ...]:
            query = value.strip().lower()
            if not query.startswith("/") or " " in query:
                return ()
            return tuple(item for item in _COMMAND_HINTS if item[0].startswith(query))[:7]

        def _render_command_menu(self, value: str) -> None:
            menu = self.query_one("#command-menu", Static)
            matches = self._command_matches(value)
            menu.display = bool(matches) and not self._working
            menu.update("\n".join(f"{command:<14} {description}" for command, description in matches))

        def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id == "composer":
                self._render_command_menu(event.value)

        def _render_snapshot(self) -> None:
            snapshot = controller.snapshot()
            self._operator_snapshot = snapshot.operator_snapshot
            compact = self.size.width < 94
            if compact:
                header = (
                    "NORUCT  /  DYNAMIC FIRM RUNTIME\n"
                    f"{snapshot.model} · {snapshot.provider} · {snapshot.authority}\n"
                    f"session {snapshot.session_id[:12]} · roster r{snapshot.roster_revision} · "
                    f"{snapshot.active_employee_count} active"
                )
            else:
                header = (
                    "NORUCT  /  DYNAMIC FIRM RUNTIME\n"
                    f"workspace  {snapshot.workspace}\n"
                    f"session    {snapshot.session_id[:12]}  ·  {snapshot.model}\n"
                    f"authority  {snapshot.authority}  ·  {snapshot.provider}\n"
                    f"company    roster r{snapshot.roster_revision}  ·  {snapshot.active_employee_count} active  ·  v{snapshot.version}"
                )
            header_widget = self.query_one("#company-header", Static)
            header_widget.set_class(compact, "compact")
            header_widget.update(header)
            self._render_company_watch(snapshot)
            self._render_current_assessment()
            self._render_activity_feed()
            self._sync_operator_surface_visibility()

        def _render_company_watch(self, snapshot: ModernTerminalSnapshot) -> None:
            """Project bounded Company facts without making UI state authoritative."""

            employees = "\n".join(f"  • {role}" for role in snapshot.employee_roles[:4])
            if not employees:
                employees = "  • no active employee detail"
            capabilities = ", ".join(snapshot.capabilities[:4]) or "conversation"
            tools = ", ".join(snapshot.tools[:5]) or "no tool projection"
            watch = (
                f"ROSTER  r{snapshot.roster_revision} · {snapshot.active_employee_count} active\n"
                f"{employees}\n\n"
                f"CAPABILITY  {capabilities}\n"
                f"TOOLS       {tools}\n"
                f"AUTHORITY   {snapshot.authority}"
            )
            operator = snapshot.operator_snapshot
            if operator:
                manager = operator.get("manager", {})
                execution = operator.get("execution", {})
                hold = operator.get("hold", {})
                approval = operator.get("approval", {})
                budget = operator.get("budget", {})
                if not all(
                    isinstance(item, Mapping)
                    for item in (manager, execution, hold, approval, budget)
                ):
                    operator = {}
                else:
                    skill_heads = manager.get("skill_heads", ())
                    if not isinstance(skill_heads, (tuple, list)):
                        skill_heads = ()
                    skill_summary = ", ".join(
                        str(item.get("skill_key", ""))
                        for item in skill_heads[:3]
                        if isinstance(item, Mapping) and item.get("skill_key")
                    ) or "none"
                    watch += (
                        "\n\nOPERATOR STATE\n"
                        f"MANAGER    {manager.get('status', 'not configured')}\n"
                        f"SKILLS     {skill_summary}\n"
                        f"GRAPH      {execution.get('decision', 'no active job')}\n"
                        f"HOLD       {hold.get('reason', 'none')}\n"
                        f"APPROVAL   {approval.get('status', 'none pending')}\n"
                        f"BUDGET     {budget.get('summary', 'not available')}\n"
                        f"ATTENTION  {operator.get('attention', {}).get('summary', 'not scanned')}"
                    )
            if not operator and snapshot.operating_report:
                watch += "\n\nOPERATIONS\n" + "\n".join(
                    f"{item}" for item in snapshot.operating_report[:5]
                )
            report = self._last_company_report
            if report:
                watch += (
                    "\n\nLAST COMPANY REPORT\n"
                    f"MODE       {report.get('mode', 'unavailable')}\n"
                    f"REPORTER   {report.get('reporting_owner', 'unavailable')}\n"
                    f"EXECUTOR   {report.get('execution_owner', 'unavailable')}\n"
                    f"FOLLOW-UP  {'required' if report.get('requires_attention') else 'none'}"
                )
            self.query_one("#company-watch-body", Static).update(watch)
            self._render_run_pulse()

        def _render_run_pulse(self) -> None:
            elapsed = self._elapsed_label()
            work_state = "active" if self._working else "idle"
            pulse = (
                f"STATE      {work_state}\n"
                f"STAGE      {self._turn_stage.lower()}\n"
                f"ELAPSED    {elapsed}\n"
                f"LATEST     {self._last_activity[:96]}"
            )
            self.query_one("#company-pulse", Static).update(pulse)

        def _render_current_assessment(self) -> None:
            """Show runtime-derived intent, never hidden model reasoning."""
            flow = self._flow.snapshot()
            objective, observation, decision, next_action = assessment_projection(
                self._operator_snapshot,
                current_objective=self._active_goal,
            )
            assessment = (
                f"OBJECTIVE  {objective[:160]}\n"
                f"OBSERVE    {observation[:160]}\n"
                f"DECISION   {decision[:160]}\n"
                f"NEXT       {next_action[:160]}\n"
                f"GUARD      {flow.guard}"
            )
            self.query_one("#current-assessment", Static).update(assessment)

        def _render_activity_feed(self) -> None:
            """Render the bounded lifecycle projection, not hidden model reasoning."""

            items = self._flow.snapshot().items
            if not items:
                rendered = "No Company activity yet. Submit a goal or open settings."
            else:
                rendered = "\n".join(
                    f"{item.sequence:02d}  {item.stage:<15} {item.label}\n"
                    f"    {item.detail}"
                    for item in items
                )
            self.query_one("#activity-feed", Static).update(rendered)

        def _elapsed_label(self) -> str:
            if self._turn_started_at is None:
                return "--:--"
            elapsed = max(0, int(time.monotonic() - self._turn_started_at))
            return f"{elapsed // 60:02d}:{elapsed % 60:02d}"

        def _sync_operator_surface_visibility(self) -> None:
            watch = self.query_one("#company-watch")
            watch.display = self._operator_surface_visible and self.size.width >= 94

        def _tick_operator_surface(self) -> None:
            if not self.is_mounted or not self.is_running:
                return
            try:
                self._render_run_pulse()
            except NoMatches:
                # A queued timer tick can arrive after Textual has removed the
                # screen during shutdown. The controller has no UI state here.
                return

        def on_resize(self, _event: object) -> None:
            """Keep the fixed Company surface useful on narrow terminals."""

            if self.is_mounted:
                self._render_snapshot()

        def _write_activity(self, message: str) -> None:
            clean = " ".join(str(message).replace("\x00", "").split())
            if clean:
                self._last_activity = clean[:320]
                self._current_status = clean[:320]
                self._flow.record_system(clean)
                self._turn_stage = self._flow.stage
                self._render_run_pulse()
                self._render_current_assessment()
                self._render_activity_feed()

        def _set_status(self, message: str) -> None:
            self._current_status = " ".join(str(message).replace("\x00", "").split())[:320]
            self.query_one("#company-status", Static).update(
                f"{message[:240]}  ·  {self._turn_stage.lower()}  ·  {self._elapsed_label()}"
            )
            self._render_run_pulse()
            self._render_current_assessment()
            self._render_activity_feed()

        def _set_answer(self, value: str) -> None:
            self.query_one("#answer", Static).update(value[:12_000] or "No completed answer yet.")

        def receive_event(self, event: ProductEvent) -> None:
            self._flow.record_event(event)
            self._turn_stage = self._flow.stage
            if event.type == ProductEventType.MODEL_STREAMING and event.data.get("stream_kind") == "text_delta":
                self._answer_parts.append(event.message)
                self._set_answer("".join(self._answer_parts))
                self._set_status("Receiving company answer…")
                self._render_activity_feed()
                return
            # Use the bounded runtime projection so assignment tenure and
            # terminal Job metrics remain visible without duplicating state.
            self._set_status(self._flow.latest)
            self._last_activity = self._flow.latest
            self._render_run_pulse()
            self._render_current_assessment()
            self._render_activity_feed()

        async def _open_settings(self, *, from_command: bool = False) -> None:
            """Open bounded future-job controls without giving UI state execution authority."""

            if self._working and not from_command:
                self._set_status("Settings apply between Company jobs")
                return
            selected = await self.push_screen_wait(SettingsScreen(controller.snapshot()))
            if selected is None:
                self._write_activity("Settings closed without a change")
                return
            if not selected:
                self._write_activity("Settings closed without a change")
                return
            for command in selected:
                applied = await controller.execute_command(command)
                for message in applied.messages:
                    self._write_activity(message)
                if applied.open_model_picker:
                    await self._open_model_picker()
                if applied.open_graph_controls:
                    await self._open_graph_controls(from_command=True)
                if applied.open_job_audit:
                    await self._open_job_audit(
                        from_command=True,
                        job_id=applied.job_audit_job_id,
                    )
                if applied.provider_login_requested:
                    self._run_provider_login()
            self._replace_history(controller.input_history())
            self._render_snapshot()

        async def _open_model_picker(self) -> None:
            """Let the operator select one locally discovered model ID."""

            selected = await self.push_screen_wait(
                ModelScreen(tuple(controller.model_options()), controller.snapshot().provider)
            )
            if selected is None:
                self._write_activity("Model selection closed without a change")
                return
            applied = await controller.execute_command(f"/model {selected}")
            for message in applied.messages:
                self._write_activity(message)
            self._replace_history(controller.input_history())
            self._render_snapshot()

        async def _open_graph_controls(self, *, from_command: bool = False) -> None:
            """Edit inert future-Job defaults through the typed controller boundary."""

            if self._working and not from_command:
                self._set_status("Graph controls apply between Company jobs")
                return
            while True:
                try:
                    selected = await self.push_screen_wait(
                        GraphControlScreen(controller.graph_control_snapshot())
                    )
                except Exception as exc:
                    diagnostic_path = record_modern_terminal_crash(exc, phase="graph-controls-open")
                    self._write_activity(f"Graph controls failed safely · {type(exc).__name__}")
                    if diagnostic_path is not None:
                        self._write_activity(f"Terminal diagnostic saved · {diagnostic_path}")
                    return
                if selected is None:
                    self._write_activity("Graph controls closed without a change")
                    return
                intent = str(selected.pop("intent", "selection"))
                try:
                    if intent == "blueprint-action":
                        messages = tuple(controller.apply_graph_blueprint_action(selected))
                    elif intent == "preview":
                        messages = tuple(controller.preview_graph(str(selected.get("goal") or "").strip()))
                    elif intent == "selection-preview":
                        preview_goal = str(selected.pop("preview_goal", "")).strip()
                        messages = tuple(controller.apply_graph_control(selected))
                        messages += tuple(controller.preview_graph(preview_goal))
                    else:
                        messages = tuple(controller.apply_graph_control(selected))
                except Exception as exc:
                    diagnostic_path = record_modern_terminal_crash(exc, phase="graph-controls-apply")
                    messages = (f"Graph controls were not saved · {type(exc).__name__}",)
                    if diagnostic_path is not None:
                        messages += (f"Terminal diagnostic saved · {diagnostic_path}",)
                for message in messages:
                    self._write_activity(message)
                self._render_snapshot()
                if intent != "blueprint-action":
                    return

        async def _open_job_audit(
            self,
            *,
            from_command: bool = False,
            job_id: str = "",
        ) -> None:
            """Inspect retained lineage and explicitly resolve one safe continuation.

            Checkpoints and all non-proposal lineage stay read-only. Actions
            are limited to an explicit approve/reject decision for an exact
            ``PENDING`` Graph receipt, or a rechecked read-only prefix resume.
            The controller then enters the shared receipt-bound same-Job path.
            """

            if self._working and not from_command:
                self._set_status("Job audit opens after the current Company job")
                return
            try:
                selected_job_id = job_id
                while True:
                    catalog = controller.job_audit_catalog()
                    selected = await self.push_screen_wait(
                        JobAuditScreen(
                            controller.job_audit_snapshot(selected_job_id or None),
                            catalog=catalog,
                        )
                    )
                    if selected is None:
                        break
                    if isinstance(selected, str):
                        if selected == selected_job_id:
                            break
                        selected_job_id = selected
                        continue
                    if not isinstance(selected, Mapping):
                        break
                    intent = str(selected.get("intent", ""))
                    if intent == "read-only-partial-continuation":
                        continuation_job_id = str(selected.get("job_id", "")).strip()
                        if not continuation_job_id:
                            self._write_activity("Read-only continuation request was malformed and was not applied")
                            break
                        composer = self.query_one(Input)
                        self._working = True
                        composer.disabled = True
                        self._set_status("Rechecking and resuming read-only Job prefix…")
                        try:
                            result = await controller.resume_partial_read_only_job(
                                job_id=continuation_job_id,
                            )
                        except Exception as exc:
                            diagnostic_path = record_modern_terminal_crash(
                                exc,
                                phase="read-only-partial-continuation",
                            )
                            self._write_activity(
                                f"Read-only continuation failed safely · {type(exc).__name__}"
                            )
                            if diagnostic_path is not None:
                                self._write_activity(
                                    f"Terminal diagnostic saved · {diagnostic_path}"
                                )
                        else:
                            self._set_answer(result.summary)
                            self._write_activity(
                                f"Read-only continuation · Job {result.status}"
                            )
                            for detail in result.details:
                                self._write_activity(detail)
                        finally:
                            self._working = False
                            composer.disabled = False
                        selected_job_id = continuation_job_id
                        self._render_snapshot()
                        continue
                    if intent == "read-only-partial-handoff":
                        continuation_job_id = str(selected.get("job_id", "")).strip()
                        target_device_id = str(selected.get("target_device_id", "")).strip()
                        if not continuation_job_id or not target_device_id:
                            self._write_activity("Read-only handoff request was malformed and was not applied")
                            break
                        composer = self.query_one(Input)
                        self._working = True
                        composer.disabled = True
                        self._set_status("Rechecking and transferring read-only continuation authority…")
                        try:
                            result = await controller.handoff_partial_read_only_job(
                                job_id=continuation_job_id,
                                target_device_id=target_device_id,
                            )
                        except Exception as exc:
                            diagnostic_path = record_modern_terminal_crash(
                                exc,
                                phase="read-only-partial-handoff",
                            )
                            self._write_activity(
                                f"Read-only handoff failed safely · {type(exc).__name__}"
                            )
                            if diagnostic_path is not None:
                                self._write_activity(
                                    f"Terminal diagnostic saved · {diagnostic_path}"
                                )
                        else:
                            self._set_answer(result.summary)
                            self._write_activity(
                                f"Read-only continuation authority · Job {result.status}"
                            )
                            for detail in result.details:
                                self._write_activity(detail)
                        finally:
                            self._working = False
                            composer.disabled = False
                        selected_job_id = continuation_job_id
                        self._render_snapshot()
                        continue
                    if intent != "graph-proposal-decision":
                        break
                    decision_job_id = str(selected.get("job_id", "")).strip()
                    proposal_id = str(selected.get("proposal_id", "")).strip()
                    decision = str(selected.get("decision", "")).strip()
                    if not decision_job_id or not proposal_id or decision not in {"approve", "reject"}:
                        self._write_activity("Graph proposal decision was malformed and was not applied")
                        break
                    composer = self.query_one(Input)
                    self._working = True
                    composer.disabled = True
                    self._set_status(
                        "Applying approved Graph proposal…"
                        if decision == "approve"
                        else "Resuming the prior Graph after proposal rejection…"
                    )
                    try:
                        result = await controller.decide_graph_proposal(
                            job_id=decision_job_id,
                            proposal_id=proposal_id,
                            approve=decision == "approve",
                            approval_port=self._approval,
                        )
                    except Exception as exc:
                        diagnostic_path = record_modern_terminal_crash(
                            exc,
                            phase="graph-proposal-decision",
                        )
                        self._write_activity(
                            f"Graph proposal decision failed safely · {type(exc).__name__}"
                        )
                        if diagnostic_path is not None:
                            self._write_activity(
                                f"Terminal diagnostic saved · {diagnostic_path}"
                            )
                    else:
                        self._set_answer(result.summary)
                        self._write_activity(
                            f"Graph proposal {decision} · Job {result.status}"
                        )
                        for detail in result.details:
                            self._write_activity(detail)
                    finally:
                        self._working = False
                        composer.disabled = False
                    selected_job_id = decision_job_id
                    self._render_snapshot()
            except Exception as exc:
                diagnostic_path = record_modern_terminal_crash(exc, phase="job-audit-open")
                self._write_activity(f"Job audit failed safely · {type(exc).__name__}")
                if diagnostic_path is not None:
                    self._write_activity(f"Terminal diagnostic saved · {diagnostic_path}")
                return
            self._render_snapshot()

        def _run_provider_login(self) -> None:
            """Hand the real terminal to the user-managed provider login."""

            try:
                with self.suspend():
                    messages = tuple(controller.provider_login())
            except Exception as exc:
                diagnostic_path = record_modern_terminal_crash(exc, phase="provider-login")
                messages = (f"Provider sign-in failed safely · {type(exc).__name__}",)
                if diagnostic_path is not None:
                    messages += (f"Terminal diagnostic saved · {diagnostic_path}",)
            for message in messages:
                self._write_activity(message)
            self._render_snapshot()

        async def _process_submission(self, value: str) -> None:
            composer = self.query_one(Input)
            clean = value.strip()
            composer.value = ""
            self._render_command_menu("")
            if not clean or self._working:
                return
            self._working = True
            composer.disabled = True
            self._remember_history(clean)
            self._answer_parts = []
            self._active_goal = clean
            self._turn_started_at = time.monotonic()
            self._turn_stage = "COMMAND" if clean.startswith(("/", "?")) else "COMPILING"
            self._flow.record_system(
                "Local command selected" if self._turn_stage == "COMMAND" else f"Goal accepted · {clean}",
                stage=self._turn_stage,
            )
            self._render_current_assessment()
            self._render_activity_feed()
            try:
                if clean.startswith(("/", "?")):
                    command = await controller.execute_command(clean)
                    if command.clear:
                        self._set_answer("")
                    elif command.clear_answer:
                        self._answer_parts = []
                        self._set_answer("")
                    for message in command.messages:
                        self._write_activity(message)
                    if command.open_settings:
                        await self._open_settings(from_command=True)
                    if command.open_model_picker:
                        await self._open_model_picker()
                    if command.open_graph_controls:
                        await self._open_graph_controls(from_command=True)
                    if command.open_job_audit:
                        await self._open_job_audit(
                            from_command=True,
                            job_id=command.job_audit_job_id,
                        )
                    if command.provider_login_requested:
                        self._run_provider_login()
                    self._replace_history(controller.input_history())
                    self._render_snapshot()
                    self._turn_stage = "READY"
                    self._turn_started_at = None
                    self._flow.record_system("Company ready", stage="READY")
                    self._set_status("Company ready")
                    if command.exit_requested:
                        self.exit()
                    return
                self._set_status("Compiling the smallest useful company…")
                result = await controller.execute_goal(clean, self.receive_event, self._approval)
                self._last_company_report = {
                    "mode": result.company_report_mode,
                    "reporting_owner": result.reporting_owner_employee_id,
                    "execution_owner": result.execution_owner_employee_id,
                    "requires_attention": result.report_requires_attention,
                }
                current = "".join(self._answer_parts).strip()
                if not current or current != result.summary.strip():
                    self._set_answer(result.summary)
                self._write_activity(f"Company job {result.status.lower()}")
                for detail in result.details:
                    self._write_activity(detail)
                self._turn_stage = "READY"
                self._turn_started_at = None
                self._flow.record_system("Company ready", stage="READY")
                self._set_status("Company ready")
                self._render_snapshot()
            except Exception as exc:  # safe boundary: runtime owns detailed persistence
                diagnostic_path = record_modern_terminal_crash(
                    exc,
                    phase="submission",
                )
                self._turn_stage = "SAFE FAILURE"
                self._flow.record_system(
                    f"Company run failed safely · {type(exc).__name__}",
                    stage="SAFE FAILURE",
                )
                self._set_status(f"Company run failed safely · {type(exc).__name__}")
                self._write_activity(f"Safe terminal error · {type(exc).__name__}")
                if diagnostic_path is not None:
                    self._write_activity(f"Terminal diagnostic saved · {diagnostic_path}")
            finally:
                self._working = False
                composer.disabled = False
                composer.focus()

        def on_key(self, event: object) -> None:
            """Provide shell-style Up/Down recall without framework state leakage."""

            key = str(getattr(event, "key", "")).lower()
            direction = -1 if key == "up" else 1 if key == "down" else 0
            if direction and self._move_history(direction):
                prevent_default = getattr(event, "prevent_default", None)
                stop = getattr(event, "stop", None)
                if callable(prevent_default):
                    prevent_default()
                if callable(stop):
                    stop()
                return
            if key == "tab" and not self._working:
                composer = self.query_one(Input)
                matches = self._command_matches(composer.value)
                if matches:
                    composer.value = matches[0][0] + " "
                    composer.cursor_position = len(composer.value)
                    self._render_command_menu(composer.value)
                    prevent_default = getattr(event, "prevent_default", None)
                    stop = getattr(event, "stop", None)
                    if callable(prevent_default):
                        prevent_default()
                    if callable(stop):
                        stop()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.run_worker(
                self._process_submission(event.value),
                group="noruct-turn",
                exclusive=True,
                description="Noruct company turn",
            )

        def action_reset_assessment(self) -> None:
            self._current_status = "Company state is unchanged"
            self._set_status("Current assessment reset; Company state is unchanged")

        def action_open_settings(self) -> None:
            # Textual requires a worker for a modal that waits for dismissal.
            self.run_worker(
                self._open_settings(),
                group="noruct-settings",
                exclusive=True,
                description="Noruct settings",
            )

        def action_open_job_audit(self) -> None:
            self.run_worker(
                self._open_job_audit(),
                group="noruct-job-audit",
                exclusive=True,
                description="Noruct Job audit",
            )

        def action_show_commands(self) -> None:
            if self._working:
                self._set_status("Command controls return when the Company job finishes")
                return
            composer = self.query_one(Input)
            if not composer.value:
                composer.value = "/"
                composer.cursor_position = 1
            composer.focus()
            self._render_command_menu(composer.value)

        def action_toggle_operator_surface(self) -> None:
            if self.size.width < 94:
                self._set_status("Company view needs a terminal width of at least 94 columns")
                return
            self._operator_surface_visible = not self._operator_surface_visible
            self._sync_operator_surface_visibility()
            self._set_status(
                "Company view expanded" if self._operator_surface_visible else "Company view collapsed"
            )

    return _ModernTerminalApp()


def run_modern_terminal(controller: ModernTerminalController) -> None:
    """Run the opt-in terminal surface without leaking framework ownership."""

    try:
        create_modern_terminal_app(controller).run()
    except Exception as exc:
        record_modern_terminal_crash(exc, phase="application")
        raise
