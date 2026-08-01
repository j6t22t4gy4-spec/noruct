from __future__ import annotations

"""Application controller for the Modern interactive Company terminal.

The controller coordinates local terminal commands, session persistence,
settings mutation, and immutable Company snapshots.  It deliberately owns no
CLI parsing, renderer loop, or Company/Kernel state authority.  The CLI passes
a narrow :class:`ModernControllerPorts` adapter for legacy ingress helpers;
the product renderer consumes the controller protocol without importing this
module's dependencies.
"""

from dataclasses import dataclass
from dataclasses import replace
import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from dynamic_firm.application.modern_terminal_integrations import (
    execute_integration_command,
)
from dynamic_firm.application.modern_terminal_company_settings import (
    propose_settings_roster_revision,
    propose_settings_skill_patch,
)
from dynamic_firm.application.modern_terminal_graph import (
    apply_graph_blueprint_action as apply_graph_blueprint_action_command,
    apply_graph_control as apply_graph_control_command,
    execute_graph_command,
    graph_control_snapshot as graph_control_snapshot_projection,
)
from dynamic_firm.application.modern_terminal_knowledge import (
    execute_knowledge_command,
)
from dynamic_firm.application.modern_terminal_job_audit import (
    execute_job_audit_command,
    job_audit_catalog as job_audit_catalog_projection,
    job_audit_snapshot as job_audit_snapshot_projection,
)
from dynamic_firm.application.modern_terminal_settings import (
    execute_runtime_settings_command,
)
from dynamic_firm.application.modern_terminal_network import (
    execute_network_command,
)
from dynamic_firm.application.modern_terminal_operator_state import (
    latest_interrupted_job_id,
)
from dynamic_firm.application.modern_terminal_operator_snapshot import (
    assemble_modern_terminal_snapshot,
)
from dynamic_firm.company import company_final_report, CompanyStateStore, EvolutionAutonomyMode, RetentionReviewMode
from dynamic_firm.providers.codex_exec import CodexExecProvider
from dynamic_firm.product import CompanySessionStore, ProductEvent, route_interactive_input
from dynamic_firm.product.global_settings import GlobalRuntimeSettings, write_global_runtime_settings
from dynamic_firm.product.settings_dashboard import page_for_entry
from dynamic_firm.product.modern_tui import ModernTerminalCommandResult, ModernTerminalResult, ModernTerminalSnapshot
from dynamic_firm.product.tui import _usage_text
from dynamic_firm.product.session_bindings import session_cost_mode_binding, session_mcp_binding, session_provider_binding
from dynamic_firm.product.models import ModelOption, filter_model_options, model_options
from dynamic_firm.runtime.models import Usage
from dynamic_firm.runtime.ports import ApprovalPort

# Terminal service adapters preserve the CLI's conventional success contract
# without making this reusable controller import the CLI module.
EXIT_OK = 0


@dataclass(frozen=True, slots=True)
class ModernControllerPorts:
    """CLI-owned adapters used by the reusable Modern terminal controller.

    Keeping this port explicit prevents the controller from importing
    ``dynamic_firm.cli`` while preserving the long-standing CLI contracts.
    """
    activate_interactive_session: Callable[..., object]
    authority_display: Callable[..., object]
    company_settings_entries: Callable[..., object]
    handoff_read_only_partial: Callable[..., object]
    continue_read_only_partial: Callable[..., Awaitable[object]]
    continue_graph_proposal: Callable[..., Awaitable[object]]
    goal_execution_services: Callable[..., object]
    graph_preview_for_config: Callable[..., object]
    interactive_skill_messages: Callable[..., object]
    load_active_roster: Callable[..., object]
    load_config: Callable[..., object]
    plugin_root: Callable[..., object]
    provider_display: Callable[..., object]
    render_graph_control: Callable[..., object]
    run_capabilities_command: Callable[..., object]
    run_config: Callable[..., object]
    run_gateway_service_command: Callable[..., object]
    run_portfolio_command: Callable[..., tuple[str, ...]]
    run_schedule_service_command: Callable[..., object]
    session_browse_response: Callable[..., object]
    state_path_for: Callable[..., object]
    tui_company_facts: Callable[..., object]


class ModernInteractiveController:
    """Noruct-owned bridge between the optional surface and the product loop."""

    def __init__(
        self,
        args: argparse.Namespace,
        settings: dict,
        *,
        provider_factory: ProviderFactory,
        coding_worker_factory: CodingWorkerFactory,
        ports: ModernControllerPorts,
    ) -> None:
        self.args = args
        self.settings = settings
        self.provider_factory = provider_factory
        self.coding_worker_factory = coding_worker_factory
        self.ports = ports
        self.state_path = self.ports.state_path_for(args, settings)
        self.sessions = CompanySessionStore(self.state_path)
        self._initial_messages: list[str] = []
        self.session = self.sessions.resolve(args.session) if args.command == "resume" else None
        if args.command == "resume" and self.session is None:
            reference = args.session or "latest"
            self.sessions.close()
            raise ValueError(f"Company session was not found: {reference}")
        if self.session is not None:
            self.config = self.ports.activate_interactive_session(args, settings, self.session)
        else:
            args.goal = "Validate the company interface"
            self.config = self.ports.run_config(args, settings)
        self.roster_snapshot = self.ports.load_active_roster(self.config)
        if self.session is None:
            self.session = self.sessions.create(
                workspace=self.config.workspace,
                model=self.config.model,
                **session_provider_binding(self.config),
                **session_mcp_binding(self.config),
                **session_cost_mode_binding(self.config),
            )
        self.session_usage = self.sessions.usage(self.session.session_id)
        self.turn_count = self.session.turn_count
        self._record_interrupted_job()

    def _record_interrupted_job(self) -> None:
        job_id = latest_interrupted_job_id(self.state_path)
        if job_id is not None:
            self._initial_messages.append(
                f"Interrupted job {job_id} · inspect with noruct job inspect {job_id}"
            )

    def close(self) -> None:
        self.sessions.close()

    def snapshot(self) -> ModernTerminalSnapshot:
        facts = self.ports.tui_company_facts(self.config, self.roster_snapshot)
        return assemble_modern_terminal_snapshot(
            config=self.config,
            state_path=self.state_path,
            roster_snapshot=self.roster_snapshot,
            session_id=self.session.session_id,
            facts=facts,
            provider=self.ports.provider_display(self.config),
            authority=self.ports.authority_display(self.config),
            company_settings_entries=self.ports.company_settings_entries,
        )

    def _persist_global_runtime_defaults(self, **changes: object) -> RunCommandConfig:
        """Persist future-job defaults, then rebuild this controller from them.

        Interactive slash commands used to modify only ``self.args``.  That
        made the Settings view look applied while the next terminal launch
        silently returned to an older configuration.  Provider and run
        defaults have one owner: the secret-free global TOML profile.
        """

        current = GlobalRuntimeSettings.from_mapping(self.settings)
        # A first save can happen before `setup` has made a TOML file.  Seed
        # that file from the *effective* interactive binding rather than
        # losing command-line connection details while changing one setting.
        if not self.settings.get("provider"):
            current = replace(
                current,
                provider_kind=self.config.provider_kind,
                base_url=self.config.base_url,
                model=self.config.model,
                api_key_env=self.config.api_key_env or "",
                no_auth=bool(getattr(self.args, "no_auth", False)),
                codex_command=self.config.codex_command,
                external_command=self.config.external_command,
                request_timeout=self.config.request_timeout_seconds,
                stale_timeout=self.config.stale_timeout_seconds,
                state_path=str(self.config.state_path),
                max_wall_time=self.config.run_limits.max_wall_time_ms / 1000,
                max_model_calls=self.config.run_limits.max_model_calls,
                max_tool_calls=self.config.run_limits.max_tool_calls,
                max_cost_usd=self.config.run_limits.max_cost_usd,
                cost_mode=self.config.run_limits.cost_efficiency_mode.value,
                permission_mode=self.config.permission_mode,
                capability_trust_mode=self.config.capability_trust_mode,
                external_read_mode=self.config.external_read_mode,
                external_state_mode=self.config.external_state_mode,
                agent_settings_mode=self.config.agent_settings_mode,
                employee_runtime=self.config.employee_runtime,
                runtime_python=self.config.runtime_python,
            )
        updated = replace(current, **changes)
        # The screen inventory and write path must be identical.  `args.config`
        # can be an older parsed default after session restoration, whereas the
        # active RunCommandConfig carries the authoritative selected profile.
        target = write_global_runtime_settings(self.config.config_path, updated)
        self.args.config = target
        self.settings = self.ports.load_config(target)
        # Command-line flags are one-shot overrides.  Once the operator saves
        # a global setting they must no longer mask the new config value.
        for field_name in (
            "provider_kind", "base_url", "model", "codex_command",
            "external_command", "api_key_env", "no_auth", "request_timeout",
            "stale_timeout",
            "max_wall_time", "max_model_calls", "max_tool_calls", "max_cost_usd",
            "cost_mode", "permission_mode", "capability_trust_mode", "external_read_mode", "external_state_mode",
            "agent_settings_mode", "employee_runtime", "runtime_python",
        ):
            setattr(self.args, field_name, None)
        candidate_args = argparse.Namespace(**vars(self.args))
        candidate_args.goal = "Validate the saved global runtime defaults"
        candidate = self.ports.run_config(candidate_args, self.settings)
        self.config = candidate
        self.roster_snapshot = self.ports.load_active_roster(candidate)
        return candidate

    def _propose_settings_roster_revision(
        self,
        payload: Mapping[str, object],
        *,
        manager_only: bool,
    ) -> ModernTerminalCommandResult:
        return propose_settings_roster_revision(
            self.state_path, payload, manager_only=manager_only
        )

    def _propose_settings_skill_patch(
        self,
        payload: Mapping[str, object],
    ) -> ModernTerminalCommandResult:
        return propose_settings_skill_patch(self.state_path, payload)

    def initial_messages(self) -> tuple[str, ...]:
        return tuple(self._initial_messages)

    def input_history(self) -> tuple[str, ...]:
        """Return only this Company session's locally retained goal history."""

        return self.sessions.input_history(self.session.session_id)

    def model_options(self) -> tuple[ModelOption, ...]:
        return model_options(self.config.provider_kind, self.config.model)

    def provider_login(self) -> tuple[str, ...]:
        """Run the user-managed Codex login while the TUI yields the terminal."""

        if self.config.provider_kind != "openai_codex":
            return ("Account sign-in is available for the ChatGPT subscription connection.",)
        executable = CodexExecProvider.resolve_executable(self.config.codex_command)
        if executable is None:
            return (
                "Configured Codex executable was not found. Install Codex CLI or set its absolute path in Settings.",
            )
        print(
            "Opening the user-managed OpenAI sign-in. Noruct does not receive, persist, or display credentials.",
            file=sys.__stdout__,
        )
        try:
            completed = subprocess.run(
                [executable, "login"],
                env=CodexExecProvider._child_environment(os.environ),
                stdin=sys.__stdin__,
                stdout=sys.__stdout__,
                stderr=sys.__stderr__,
                check=False,
            )
        except OSError:
            return ("Could not start the configured Codex login command.",)
        if completed.returncode != 0:
            return (f"Provider sign-in ended with exit code {completed.returncode}.",)
        status = CodexExecProvider.login_status(executable)
        return (
            "OpenAI account sign-in verified."
            if status.authenticated
            else "Sign-in command finished, but the external Codex session is not authenticated yet.",
        )

    def job_audit_snapshot(self, job_id: str | None = None) -> Mapping[str, object]:
        """Project one retained ACTIVE JOB without granting lifecycle control."""

        return job_audit_snapshot_projection(self.state_path, job_id)

    def job_audit_catalog(self) -> Mapping[str, object]:
        """Project bounded content-free retained Job choices for TUI and future GUI."""

        return job_audit_catalog_projection(self.state_path)

    async def decide_graph_proposal(
        self,
        *,
        job_id: str,
        proposal_id: str,
        approve: bool,
        approval_port: ApprovalPort | None,
    ) -> ModernTerminalResult:
        """Resolve a durable Graph proposal through the shared runtime path.

        The TUI supplies only an explicit human decision and opaque receipt
        identifiers.  The injected CLI composition rebuilds the local runtime
        and reopens the canonical Work Order itself; no audit projection or
        screen state becomes request, Graph, or budget authority.
        """

        execution = self.ports.goal_execution_services(
            provider_factory=self.provider_factory,
            coding_worker_factory=self.coding_worker_factory,
        )
        turn_args = argparse.Namespace(**vars(self.args))
        turn_args.goal = "Resume one retained Graph proposal"
        prepared = execution.prepare(turn_args, self.settings)
        self.config = prepared.config
        self.roster_snapshot = prepared.roster_snapshot
        result = await self.ports.continue_graph_proposal(
            config=prepared.config,
            provider=prepared.provider,
            job_id=job_id,
            proposal_id=proposal_id,
            approve=approve,
            approval_port=approval_port,
        )
        report = company_final_report(result)
        details = (
            ((report.operator_line(),) if report.manager_employee_id else ())
            + tuple(result.acceptance_evidence[:4])
            + tuple(f"Unresolved · {item}" for item in result.unresolved_issues[:3])
        )
        return ModernTerminalResult(
            summary=report.summary,
            status=result.status.value,
            details=details,
            company_report_mode=report.mode.value,
            reporting_owner_employee_id=report.reporting_owner_employee_id,
            execution_owner_employee_id=report.execution_owner_employee_id,
            report_requires_attention=report.requires_attention,
        )

    async def resume_partial_read_only_job(
        self,
        *,
        job_id: str,
    ) -> ModernTerminalResult:
        """Resume a candidate only through the shared receipt-bound runtime.

        The TUI supplies an opaque Job id and an explicit click.  It does not
        receive a Work Order, receipt body, ActionPolicy, or any authority to
        decide eligibility; the injected runtime revalidates all of those
        inputs before its one-shot claim.
        """

        execution = self.ports.goal_execution_services(
            provider_factory=self.provider_factory,
            coding_worker_factory=self.coding_worker_factory,
        )
        turn_args = argparse.Namespace(**vars(self.args))
        turn_args.goal = "Resume a retained read-only Job prefix"
        prepared = execution.prepare(turn_args, self.settings)
        self.config = prepared.config
        self.roster_snapshot = prepared.roster_snapshot
        result = await self.ports.continue_read_only_partial(
            config=prepared.config,
            provider=prepared.provider,
            job_id=job_id,
        )
        report = company_final_report(result)
        details = (
            ((report.operator_line(),) if report.manager_employee_id else ())
            + tuple(result.acceptance_evidence[:4])
            + tuple(f"Unresolved · {item}" for item in result.unresolved_issues[:3])
        )
        return ModernTerminalResult(
            summary=report.summary,
            status=result.status.value,
            details=details,
            company_report_mode=report.mode.value,
            reporting_owner_employee_id=report.reporting_owner_employee_id,
            execution_owner_employee_id=report.execution_owner_employee_id,
            report_requires_attention=report.requires_attention,
        )

    async def handoff_partial_read_only_job(
        self,
        *,
        job_id: str,
        target_device_id: str,
    ) -> ModernTerminalResult:
        """Transfer opaque authority without constructing a provider or Job."""

        turn_args = argparse.Namespace(**vars(self.args))
        turn_args.goal = "Transfer a retained read-only Job continuation"
        config = self.ports.run_config(turn_args, self.settings)
        self.config = config
        admission = self.ports.handoff_read_only_partial(
            config=config,
            job_id=job_id,
            target_device_id=target_device_id,
        )
        completed = tuple(getattr(admission, "completed_task_ids", ()))
        return ModernTerminalResult(
            summary=(
                f"Read-only continuation authority transferred to {target_device_id}."
            ),
            status="TRANSFERRED",
            details=(
                f"Job · {job_id}",
                f"Receipt-proven completed tasks · {len(completed)}",
                "No Job, prompt, file, or result content was transmitted.",
            ),
        )


    def graph_control_snapshot(self) -> Mapping[str, object]:
        """Project inert future-Job Graph preferences for the product surface."""

        return graph_control_snapshot_projection(self.state_path)


    def apply_graph_control(self, submission: Mapping[str, object]) -> tuple[str, ...]:
        """Save bounded future-Job Graph preferences through the shared adapter."""

        return apply_graph_control_command(self, submission)

    def apply_graph_blueprint_action(
        self, submission: Mapping[str, object]
    ) -> tuple[str, ...]:
        """Author an inert local Blueprint without changing execution authority."""

        return apply_graph_blueprint_action_command(self, submission)

    def preview_graph(self, goal: str) -> tuple[str, ...]:
        """Bind the saved future-Job selection read-only for one operator goal."""

        return execute_graph_command(self, f"preview {goal}").messages


    async def execute_goal(
        self,
        goal: str,
        event_sink: Callable[[ProductEvent], None],
        approval_port: ApprovalPort | None,
    ) -> ModernTerminalResult:
        turn_args = argparse.Namespace(**vars(self.args))
        turn_args.goal = goal
        execution = self.ports.goal_execution_services(
            provider_factory=self.provider_factory,
            coding_worker_factory=self.coding_worker_factory,
        )
        prepared = execution.prepare(turn_args, self.settings)
        self.config = prepared.config
        self.roster_snapshot = prepared.roster_snapshot
        routing = route_interactive_input(goal)
        result = await execution.execute(
            prepared,
            approval_port=approval_port,
            event_sink=event_sink,
            prior_context=self.sessions.recent_context(self.session.session_id),
            route=routing.route,
            session_key=self.session.session_id,
        )
        self.sessions.append_turn(
            session_id=self.session.session_id,
            goal=goal,
            job_id=result.job_id,
            status=result.status.value,
            summary=result.summary,
            usage=result.metrics.usage,
        )
        self.session_usage = self.session_usage.plus(result.metrics.usage)
        self.turn_count += 1
        report = company_final_report(result)
        details = (
            ((report.operator_line(),) if report.manager_employee_id else ())
            + tuple(result.acceptance_evidence[:4])
            + tuple(
                f"Unresolved · {item}" for item in result.unresolved_issues[:3]
            )
        )
        return ModernTerminalResult(
            summary=report.summary,
            status=result.status.value,
            details=details,
            company_report_mode=report.mode.value,
            reporting_owner_employee_id=report.reporting_owner_employee_id,
            execution_owner_employee_id=report.execution_owner_employee_id,
            report_requires_attention=report.requires_attention,
        )

    def _execute_integration_command(
        self,
        command: str,
        argument: str,
    ) -> ModernTerminalCommandResult | None:
        """Delegate bounded local integration commands to their application component."""

        return execute_integration_command(self, command, argument)


    def _execute_runtime_settings_command(
        self,
        command: str,
        argument: str,
    ) -> ModernTerminalCommandResult | None:
        """Delegate bounded global runtime settings to the application adapter."""

        return execute_runtime_settings_command(self, command, argument)

    def _execute_network_command(self, argument: str) -> ModernTerminalCommandResult:
        """Delegate Network operations to the shared operator projection."""

        return execute_network_command(self, argument)

    async def _execute_portfolio_command(
        self, argument: str
    ) -> ModernTerminalCommandResult:
        """Use the CLI-owned portfolio composition without a TUI state fork."""

        messages = await asyncio.to_thread(
            self.ports.run_portfolio_command, self, argument
        )
        return ModernTerminalCommandResult(messages=messages)


    async def execute_command(self, command_line: str) -> ModernTerminalCommandResult:
        command, _, command_arg = command_line.partition(" ")
        argument = command_arg.strip()
        if command in {"/help", "?", "/"}:
            return ModernTerminalCommandResult(
                messages=(
                    "Type / to open the command palette. Commands: /settings, /network [sources|search QUERY|updates|permissions|trust] or [source-add|stage|review|install|activate|rollback|update-mode] JSON, /portfolio [status|preview|submit --confirm GOAL|drain --confirm], /capabilities, /provider <kind>, /provider-login, /endpoint <url>, /auth-env <name>, /codex-command <path>, /external-command <path>, /request-timeout <seconds>, /routing-policy [QUALITY_FIRST|BALANCED|EFFICIENT|PRIVATE_LOCAL_FIRST], /routing-onboard [preview|confirm] SYNTHETIC_PROVIDER_FREE_JSON, /company-coordination <settings JSON>, /permission [ask|read-only], /trust [strict|trusted|autonomous], /external-read [blocked|ask|allow], /external-state [blocked|ask|user-authorized-auto], /agent-settings [blocked|ask], /tools, /remember <text>, /knowledge [query], /workbench [intent-id], /graph [preview <goal>], /intent [id], /decision [due|id], /question [open|id], /research [draft|id], /model [id|search query], /mode [standard|economy], /skills [goal], /usage, /evolution [never|propose|always-approve], /review [approval|auto-review|always-approve], /status, /sessions, /new, /clear, /quit.",
                    "In ask mode, coding goals use a disposable shadow workspace and request approval before a real workspace change is applied.",
                )
            )
        if command in {"/quit", "/exit"}:
            return ModernTerminalCommandResult(exit_requested=True)
        if command == "/clear":
            return ModernTerminalCommandResult(clear=True)
        if command == "/network":
            return self._execute_network_command(argument)
        if command == "/portfolio":
            return await self._execute_portfolio_command(argument)
        if command == "/settings":
            if argument:
                entries = [
                    item for item in self.snapshot().settings_entries
                    if isinstance(item, Mapping)
                    and page_for_entry(item).lower() == argument.lower()
                ]
                if entries:
                    return ModernTerminalCommandResult(
                        messages=tuple(
                            f"{item.get('title', 'Setting')} · {item.get('value') or item.get('state', 'unknown')} · {item.get('scope', 'GLOBAL')} · {item.get('effect', '')}"
                            + (
                                f" · Connect: {item.get('setup_hint')}"
                                if item.get("setup_hint") and item.get("state") != "ready"
                                else ""
                            )
                            for item in entries
                        ) + ("Credential values are never available to the agent or Settings Center. Company profile edits remain proposal-only until explicit approval and apply.",)
                    )
                return ModernTerminalCommandResult(
                    messages=("Unknown Settings category. Open /settings to inspect Connection, Execution, Integrations, Messaging, Environment, Automation, Company, Data, and Network.",)
                )
            return ModernTerminalCommandResult(
                open_settings=True,
                messages=("Settings Center · global connection/execution defaults plus Company profile, delegation, learning, and local data controls. Changes show their scope before Done.",),
            )
        if command == "/job":
            return execute_job_audit_command(argument)
        if command == "/graph":
            return execute_graph_command(self, argument)
        if command in {"/company-manager-revise", "/company-employee-revise"}:
            try:
                payload = json.loads(argument)
            except json.JSONDecodeError:
                return ModernTerminalCommandResult(
                    messages=("Company Settings revision payload is malformed.",)
                )
            if not isinstance(payload, Mapping):
                return ModernTerminalCommandResult(
                    messages=("Company Settings revision must be an object.",)
                )
            return self._propose_settings_roster_revision(
                payload,
                manager_only=command == "/company-manager-revise",
            )
        if command == "/company-skill-propose":
            try:
                payload = json.loads(argument)
            except json.JSONDecodeError:
                return ModernTerminalCommandResult(
                    messages=("Company Skill Patch payload is malformed.",)
                )
            if not isinstance(payload, Mapping):
                return ModernTerminalCommandResult(
                    messages=("Company Skill Patch must be an object.",)
                )
            return self._propose_settings_skill_patch(payload)
        integration_result = self._execute_integration_command(command, argument)
        if integration_result is not None:
            return integration_result
        runtime_settings_result = self._execute_runtime_settings_command(command, argument)
        if runtime_settings_result is not None:
            return runtime_settings_result
        knowledge_result = execute_knowledge_command(self.state_path, command, argument)
        if knowledge_result is not None:
            return knowledge_result
        if command == "/skills":
            return ModernTerminalCommandResult(
                messages=self.ports.interactive_skill_messages(self.config, argument)
            )
        if command == "/model":
            if not argument:
                return ModernTerminalCommandResult(
                    messages=(f"Current model · {self.config.model}",),
                    open_model_picker=True,
                )
            if argument.lower().startswith("search"):
                _, _, query = argument.partition(" ")
                try:
                    matches = filter_model_options(
                        model_options(self.config.provider_kind, self.config.model), query
                    )
                except ValueError as exc:
                    return ModernTerminalCommandResult(messages=(str(exc),))
                if not matches:
                    return ModernTerminalCommandResult(messages=("No local model id matches that search.",))
                return ModernTerminalCommandResult(
                    messages=(
                        "Matching local models · "
                        + ", ".join(option.model_id for option in matches)
                    )
                )
            previous = self.config.model
            candidate_config = self._persist_global_runtime_defaults(model=argument)
            self.sessions.update_model(self.session.session_id, candidate_config.model)
            return ModernTerminalCommandResult(
                messages=(f"Model switched · {previous} → {self.config.model}",)
            )
        if command == "/provider-login":
            if self.config.provider_kind != "openai_codex":
                return ModernTerminalCommandResult(
                    messages=("Select ChatGPT subscription in Settings before starting account sign-in.",)
                )
            return ModernTerminalCommandResult(
                messages=("Preparing the user-managed OpenAI sign-in…",),
                provider_login_requested=True,
            )
        if command == "/usage":
            return ModernTerminalCommandResult(messages=(_usage_text(self.session_usage),))
        if command == "/mode":
            if not argument:
                return ModernTerminalCommandResult(
                    messages=(
                        f"Cost mode · {self.config.run_limits.cost_efficiency_mode.value}",
                        "economy compacts only repeated/oversized successful tool output before a model call; it never changes raw receipts, failures, or approval previews.",
                    )
                )
            normalized = argument.lower().replace("_", "-")
            if normalized not in {"standard", "economy"}:
                return ModernTerminalCommandResult(
                    messages=("Cost mode must be standard or economy.",)
                )
            previous = self.config.run_limits.cost_efficiency_mode.value
            self._persist_global_runtime_defaults(cost_mode=normalized)
            self.sessions.update_cost_efficiency_mode(self.session.session_id, normalized)
            return ModernTerminalCommandResult(
                messages=(
                    f"Cost mode · {previous} → {normalized}",
                    "Economy is a context policy, not a price guarantee; provider receipts remain authoritative.",
                )
            )
        if command == "/review":
            with CompanyStateStore(self.state_path) as company_store:
                previous = company_store.retention_review_mode()
                if not argument:
                    return ModernTerminalCommandResult(
                        messages=(
                            f"Retention review · {previous.value}",
                            "Set with /review approval, /review auto-review, or /review always-approve.",
                        )
                    )
                aliases = {"auto_review": "auto-review", "always_approve": "always-approve"}
                selected = aliases.get(argument, argument)
                try:
                    mode = RetentionReviewMode(selected)
                except ValueError:
                    return ModernTerminalCommandResult(
                        messages=("Review mode must be approval, auto-review, or always-approve.",)
                    )
                company_store.set_retention_review_mode(mode, actor="user:modern-terminal")
                return ModernTerminalCommandResult(
                    messages=(f"Retention review · {previous.value} → {mode.value}",)
                )
        if command == "/evolution":
            with CompanyStateStore(self.state_path) as company_store:
                previous = company_store.evolution_autonomy_mode()
                if not argument:
                    return ModernTerminalCommandResult(
                        messages=(
                            f"Company evolution · {previous.value}",
                            "Set with /evolution never, /evolution propose, or /evolution always-approve.",
                            "Always-approve adopts qualifying improvements for future Jobs; running Jobs, authority, budget, signatures, and compatibility remain protected.",
                        )
                    )
                aliases = {"always_approve": "always-approve"}
                selected = aliases.get(argument, argument)
                try:
                    mode = EvolutionAutonomyMode(selected)
                except ValueError:
                    return ModernTerminalCommandResult(
                        messages=("Evolution mode must be never, propose, or always-approve.",)
                    )
                company_store.set_evolution_autonomy_mode(mode, actor="user:modern-terminal")
                return ModernTerminalCommandResult(
                    messages=(f"Company evolution · {previous.value} → {mode.value}",)
                )
        if command == "/status":
            with CompanyStateStore(self.state_path) as company_store:
                review_mode = company_store.retention_review_mode().value
                evolution_mode = company_store.evolution_autonomy_mode().value
            return ModernTerminalCommandResult(
                messages=(
                    f"Session {self.session.session_id[:12]} · {self.turn_count} turn(s) · evolution {evolution_mode} · review {review_mode}",
                    _usage_text(self.session_usage),
                )
            )
        if command == "/sessions":
            selected, messages = self.ports.session_browse_response(
                self.sessions,
                argument,
                current_session_id=self.session.session_id,
            )
            if selected is not None:
                self.session = selected
                self.config = self.ports.activate_interactive_session(self.args, self.settings, selected)
                self.roster_snapshot = self.ports.load_active_roster(self.config)
                self.session_usage = self.sessions.usage(selected.session_id)
                self.turn_count = selected.turn_count
                return ModernTerminalCommandResult(messages=messages, clear_answer=True)
            return ModernTerminalCommandResult(
                messages=messages
            )
        if command == "/new":
            self.session = self.sessions.create(
                workspace=self.config.workspace,
                model=self.config.model,
                **session_provider_binding(self.config),
            **session_mcp_binding(self.config),
            **session_cost_mode_binding(self.config),
            )
            self.roster_snapshot = self.ports.load_active_roster(self.config)
            self.session_usage = Usage()
            self.turn_count = 0
            return ModernTerminalCommandResult(
                messages=(f"New company session · {self.session.session_id[:12]}",)
            )
        if command in {"/view", "/details"}:
            return ModernTerminalCommandResult(
                messages=("Modern terminal keeps its company surface visible; this command is native-terminal only.",)
            )
        return ModernTerminalCommandResult(
            messages=(f"Unknown local command · {command}. Use /help.",)
        )
