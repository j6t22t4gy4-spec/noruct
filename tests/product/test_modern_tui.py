from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from dynamic_firm.product.events import ProductEvent, ProductEventType
from dynamic_firm.product.modern_tui import (
    ModernTerminalCommandResult,
    ModernTerminalResult,
    ModernTerminalSnapshot,
    SessionInputHistory,
    create_modern_terminal_app,
    modern_terminal_available,
)
from dynamic_firm.product.models import ModelOption
from dynamic_firm.product.terminal_diagnostics import record_modern_terminal_crash
from dynamic_firm.runtime.models import ApprovalDecision, ApprovalRequest, ToolEffect, ToolRisk


class ModernTerminalLazyImportTests(unittest.TestCase):
    """Optional Textual factories must stay importable without Textual installed."""

    def test_secondary_modal_components_keep_textual_as_a_lazy_dependency(self) -> None:
        from dynamic_firm.product.modern_tui_secondary_screens import (
            create_secondary_terminal_screens,
        )

        self.assertTrue(callable(create_secondary_terminal_screens))

    def test_settings_modal_component_keeps_textual_as_a_lazy_dependency(self) -> None:
        from dynamic_firm.product.modern_tui_settings_screen import (
            create_settings_screen,
        )

        self.assertTrue(callable(create_settings_screen))


class _Controller:
    def __init__(
        self,
        *,
        requires_approval: bool = False,
        pending_graph_proposal: bool = False,
    ) -> None:
        self.goals: list[str] = []
        self.commands: list[str] = []
        self.graph_actions: list[dict[str, object]] = []
        self.graph_submissions: list[dict[str, object]] = []
        self.graph_preview_goals: list[str] = []
        self.graph_proposal_decisions: list[tuple[str, str, bool]] = []
        self.read_only_resumes: list[str] = []
        self.requires_approval = requires_approval
        self.pending_graph_proposal = pending_graph_proposal

    def snapshot(self) -> ModernTerminalSnapshot:
        return ModernTerminalSnapshot(
            workspace="/workspace/noruct",
            session_id="session-modern-contract",
            model="model-contract",
            provider="provider-contract",
            authority="ask",
            version="0.0.test",
            roster_revision=3,
            active_employee_count=2,
            employee_roles=("Generalist", "Reviewer"),
            tools=("read", "write*"),
            settings_entries=(
                {
                    "key": "provider.model",
                    "category": "Connection",
                    "title": "Default model",
                    "state": "configured",
                    "value": "model-contract",
                    "scope": "GLOBAL",
                    "effect": "connection",
                    "summary": "Used by future Company jobs.",
                    "agent_writable": True,
                },
            ),
        )

    def initial_messages(self) -> tuple[str, ...]:
        return ("Company surface ready",)

    def input_history(self) -> tuple[str, ...]:
        return ("First goal", "Second goal")

    def model_options(self) -> tuple[ModelOption, ...]:
        return (
            ModelOption("model-contract", "Current session model", current=True),
            ModelOption("model-next", "Discovered model"),
        )

    def provider_login(self) -> tuple[str, ...]:
        return ("Provider sign-in verified",)

    def graph_control_snapshot(self) -> dict[str, object]:
        return {
            "selection": {
                "blueprint_id": None,
                "version": None,
                "pinned_employee_ids": (),
                "excluded_employee_ids": (),
                "require_independent_review": False,
                "max_concurrency": None,
                "max_cost_usd": None,
                "max_wall_time_ms": None,
                "mutation_policy": "BOUNDED_AUTO",
            },
            "blueprints": (),
        }

    def apply_graph_control(self, submission: dict[str, object]) -> tuple[str, ...]:
        self.graph_submissions.append(submission)
        return ("Future Job Graph defaults saved",)

    def apply_graph_blueprint_action(
        self, submission: dict[str, object]
    ) -> tuple[str, ...]:
        self.graph_actions.append(submission)
        return ("Blueprint Draft saved · tui_draft@1",)

    def preview_graph(self, goal: str) -> tuple[str, ...]:
        self.graph_preview_goals.append(goal)
        return (f"Graph preview · {goal}", "No Job was started.")

    def job_audit_catalog(self) -> dict[str, object]:
        return {
            "schema": "noruct.job-audit-catalog.v1",
            "jobs": (
                {
                    "job_id": "job-modern-audit",
                    "audit_status": "TERMINAL",
                    "job_status": "SUCCEEDED",
                    "final_graph_version": 2,
                },
                {
                    "job_id": "job-prior-audit",
                    "audit_status": "TERMINAL",
                    "job_status": "FAILED",
                    "final_graph_version": 1,
                },
            ),
        }

    def job_audit_snapshot(self, job_id: str | None = None) -> dict[str, object]:
        if job_id not in {None, "", "job-modern-audit", "job-prior-audit"}:
            return {
                "schema": "noruct.job-audit-surface.v1",
                "job": None,
                "graph": {},
                "checkpoints": (),
                "requested_job_id": job_id,
                "error": "No retained ACTIVE JOB matches this identifier.",
            }
        snapshot = {
            "schema": "noruct.job-audit-surface.v1",
            "job": {
                "job_id": "job-modern-audit",
                "audit_status": "TERMINAL",
                "job_status": "SUCCEEDED",
                "company_work_mode": "TEAM_JOB",
                "coordination_policy": "PLAN_FIRST",
                "requested_effect": "READ",
                "replay_matches": True,
                "final_graph_version": 2,
                "attempt_count": 3,
                "mutation_count": 1,
            },
            "graph": {
                "blueprint": "review@1",
                "initial_digest": "a" * 64,
                "change_summary": {
                    "initial_graph_version": 1,
                    "final_graph_version": 2,
                    "initial_digest": "a" * 64,
                    "final_digest": "b" * 64,
                    "accepted_revision_count": 1,
                    "accepted_operations": {"INSERT": 1},
                    "total_reserved_cost_delta": 0.01,
                    "final_task_count": 2,
                    "final_task_status_counts": {"SUCCEEDED": 2},
                    "execution_replica_group_count": 0,
                },
                "revisions": (
                    {
                        "sequence": 1,
                        "operation": "INSERT",
                        "previous_digest": "a" * 64,
                        "next_digest": "b" * 64,
                        "budget_delta": 0.01,
                        "approval_policy": "BOUNDED_AUTO",
                        "expected_impact": "CAPABILITY_COVERAGE",
                        "validation_receipt": "KERNEL_GRAPH_AND_LEASE_VALIDATED",
                        "observed_terminal_outcome": "JOB_SUCCEEDED",
                    },
                ),
                "proposals": (
                    {
                        "proposal_id": "graph-proposal-1234567890abcdef12345678",
                        "sequence": 1,
                        "status": "PENDING" if self.pending_graph_proposal else "REJECTED",
                        "operation": "INSERT",
                        "base_graph_version": 1,
                        "proposed_lease": {
                            "model_calls": 1,
                            "tool_calls": 0,
                            "cost_usd": 0.02,
                        },
                    },
                ),
            },
            "checkpoints": (
                {
                    "ledger_sequence": 1,
                    "event_type": "ADMITTED",
                    "graph_version": 1,
                    "parent_checkpoint_id": "",
                    "changed_task_ids": ("research",),
                    "task_states": ({"task_id": "research", "status": "SUCCEEDED"},),
                },
            ),
            "route_admissions": (
                {
                    "employee_id": "employee-reviewer",
                    "task_id": "review",
                    "route_id": "local-review-route",
                    "selection_reasons": ("POLICY_ORDER",),
                    "binding_digest": "c" * 64,
                    "selection_receipt_digest": "d" * 64,
                    "selection_policy_digest": "e" * 64,
                    "intelligence_snapshot_digest": "a" * 64,
                    "compatibility_evidence_digest": "b" * 64,
                    "egress_policy_digest": "f" * 64,
                    "fallback_policy_digest": "0" * 64,
                    "selected_uncertainty": 0.0,
                },
            ),
            "model_invocations": (
                {
                    "employee_id": "employee-reviewer",
                    "task_id": "review",
                    "route_id": "local-review-route",
                    "binding_digest": "c" * 64,
                    "receipt_digest": "f" * 64,
                    "terminal_status": "SUCCEEDED",
                    "usage_availability": "UNAVAILABLE",
                    "cost_availability": "AVAILABLE",
                    "cost_usd": 0.0,
                    "latency_ms": 0.0,
                    # The screen must whitelist its mapping values rather
                    # than becoming a physical-call or content viewer.
                    "invocation_id": "private-call-id",
                    "provider": "private-provider",
                    "model": "private-model",
                    "output": "private-output",
                    "credential": "private-credential",
                    "context": "비공개 컨텍스트",
                },
            ),
        }
        if job_id == "job-prior-audit":
            snapshot["job"] = {  # type: ignore[index]
                **snapshot["job"],  # type: ignore[index]
                "job_id": "job-prior-audit",
                "job_status": "FAILED",
                "final_graph_version": 1,
            }
        return snapshot

    async def execute_command(self, command: str) -> ModernTerminalCommandResult:
        self.commands.append(command)
        if command == "/settings":
            return ModernTerminalCommandResult(open_settings=True)
        if command == "/graph":
            return ModernTerminalCommandResult(open_graph_controls=True)
        if command == "/job" or command.startswith("/job "):
            return ModernTerminalCommandResult(
                open_job_audit=True,
                job_audit_job_id=command.removeprefix("/job").strip(),
            )
        if command == "/clear":
            return ModernTerminalCommandResult(clear=True, messages=("Cleared",))
        return ModernTerminalCommandResult(messages=(f"Command handled · {command}",))

    async def decide_graph_proposal(
        self,
        *,
        job_id: str,
        proposal_id: str,
        approve: bool,
        approval_port,
    ) -> ModernTerminalResult:
        del approval_port
        self.graph_proposal_decisions.append((job_id, proposal_id, approve))
        self.pending_graph_proposal = False
        return ModernTerminalResult(
            summary="Graph proposal decision completed",
            status="SUCCEEDED",
            details=("Same Job resumed from its durable proposal receipt",),
            company_report_mode="MANAGER_OPERATIONAL_ENVELOPE",
            reporting_owner_employee_id="employee-manager",
            execution_owner_employee_id="employee-specialist",
            report_requires_attention=False,
        )

    async def resume_partial_read_only_job(self, *, job_id: str) -> ModernTerminalResult:
        self.read_only_resumes.append(job_id)
        return ModernTerminalResult(
            summary="Read-only prefix resumed",
            status="SUCCEEDED",
            details=("Exact Work Order and receipt were rechecked",),
            company_report_mode="MANAGER_OPERATIONAL_ENVELOPE",
            reporting_owner_employee_id="employee-manager",
            execution_owner_employee_id="employee-specialist",
            report_requires_attention=False,
        )

    async def execute_goal(self, goal, event_sink, approval_port) -> ModernTerminalResult:
        self.goals.append(goal)
        event_sink(ProductEvent(ProductEventType.COMPILER_STARTED, "Compiler started"))
        event_sink(
            ProductEvent(
                ProductEventType.MODEL_STREAMING,
                "first streamed ",
                data={"stream_kind": "text_delta"},
            )
        )
        event_sink(
            ProductEvent(
                ProductEventType.MODEL_STREAMING,
                "answer",
                data={"stream_kind": "text_delta"},
            )
        )
        if self.requires_approval and approval_port is not None:
            decision = await approval_port.request(
                ApprovalRequest(
                    action_id="action-modern",
                    run_id="run-modern",
                    job_id="job-modern",
                    task_id="task-modern",
                    employee_id="employee-modern",
                    tool_name="apply_change",
                    effect=ToolEffect.WRITE,
                    risk=ToolRisk.MEDIUM,
                    resource_key="workspace:modern",
                    preview="Apply the bounded change",
                    allow_session=True,
                ),
                _NeverCancelled(),
            )
            event_sink(ProductEvent(ProductEventType.APPROVAL_RESOLVED, decision.value))
        return ModernTerminalResult(
            summary="first streamed answer",
            status="SUCCEEDED",
            details=("Validated in the Noruct controller",),
            company_report_mode="MANAGER_OPERATIONAL_ENVELOPE",
            reporting_owner_employee_id="employee-manager",
            execution_owner_employee_id="employee-specialist",
            report_requires_attention=False,
        )


class _NeverCancelled:
    cancelled = False

    def raise_if_cancelled(self) -> None:
        return None


class SessionInputHistoryTests(unittest.TestCase):
    def test_session_history_deduplicates_bounds_and_restores_unsent_draft(self) -> None:
        history = SessionInputHistory(("first", "repeat", "repeat", "x" * 8_001, "last"))

        self.assertEqual(history.entries, ("first", "repeat", "last"))
        first = history.move(-1, "unsent")
        assert first is not None
        self.assertEqual(first.value, "last")
        second = history.move(-1, "unsent")
        assert second is not None
        self.assertEqual(second.value, "repeat")
        back = history.move(1, "ignored current value")
        assert back is not None
        self.assertEqual(back.value, "last")
        restored = history.move(1, "ignored current value")
        assert restored is not None
        self.assertTrue(restored.restored_draft)
        self.assertEqual(restored.value, "unsent")

    def test_session_history_is_bounded_and_rejects_unknown_direction(self) -> None:
        history = SessionInputHistory(tuple(f"goal-{index}" for index in range(120)))

        self.assertEqual(len(history.entries), 100)
        self.assertEqual(history.entries[0], "goal-20")
        with self.assertRaisesRegex(ValueError, "direction"):
            history.move(0, "draft")


@unittest.skipUnless(modern_terminal_available(), "optional modern terminal profile is not installed")
class ModernTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_width_matrix_keeps_one_composer_and_compact_company_identity(self) -> None:
        for width in (40, 60, 80, 120):
            with self.subTest(width=width):
                controller = _Controller()
                app = create_modern_terminal_app(controller)

                async with app.run_test(size=(width, 30)) as pilot:
                    await pilot.pause(0.05)
                    header = str(app.query_one("#company-header").render())
                    self.assertIn("NORUCT", header)
                    self.assertEqual(len(app.query("#composer")), 1)
                    watch = app.query_one("#company-watch")
                    if width < 94:
                        self.assertNotIn("workspace  /workspace/noruct", header)
                        self.assertTrue(app.query_one("#company-header").has_class("compact"))
                    else:
                        self.assertIn("workspace  /workspace/noruct", header)
                        self.assertFalse(app.query_one("#company-header").has_class("compact"))
                    self.assertEqual(watch.display, width >= 94)
                    if width == 80:
                        self.assertGreaterEqual(app.query_one("#current-assessment").size.height, 4)
                    await pilot.press("/", "h", "e", "l", "p", "enter")
                    await pilot.pause(0.05)
                    self.assertEqual(controller.commands, ["/help"])

    async def test_slash_opens_a_visible_palette_and_tab_completes_a_command(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.press("/")
            await pilot.pause(0.05)
            menu = app.query_one("#command-menu")
            self.assertTrue(menu.display)
            self.assertIn("/settings", str(menu.render()))
            await pilot.press("s", "e", "t", "tab")
            composer = app.query_one("#composer")
            self.assertEqual(composer.value, "/settings ")

    async def test_settings_modal_keeps_selection_open_until_done(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen.query("#settings-card")), 1)
            self.assertGreaterEqual(len(app.screen.query(".settings-actions")), 2)
            await pilot.click("#settings-page-execution")
            await pilot.wait_for_scheduled_animations()
            for _ in range(20):
                await pilot.pause(0.05)
                if len(app.screen.query("#settings-permission-ask")) == 1:
                    break
            self.assertEqual(len(app.screen.query("#settings-permission-ask")), 1)
            await pilot.click("#settings-permission-ask")
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#settings-card")), 1)
            self.assertTrue(app.screen.query_one("#settings-permission-ask").has_class("settings-selected"))
            self.assertIn("/permission ask", str(app.screen.query_one("#settings-pending").render()))
            self.assertEqual(controller.commands, ["/settings"])
            await pilot.click("#settings-done")
            await worker.wait()
            self.assertEqual(controller.commands, ["/settings", "/permission ask"])

    async def test_settings_category_button_recomposes_without_closing(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.15)
            await pilot.click("#settings-page-messaging")
            await pilot.pause(0.15)
            self.assertEqual(len(app.screen.query("#settings-card")), 1)
            self.assertTrue(app.screen.query_one("#settings-page-messaging").has_class("settings-selected"))
            await app.screen.dismiss(None)
            await worker.wait()

    async def test_settings_reset_discards_staged_changes_without_closing_or_applying(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.15)
            await pilot.click("#settings-page-execution")
            await pilot.wait_for_scheduled_animations()
            for _ in range(20):
                await pilot.pause(0.05)
                if len(app.screen.query("#settings-permission-read-only")) == 1:
                    break
            self.assertEqual(
                len(app.screen.query("#settings-permission-read-only")), 1
            )
            await pilot.click("#settings-permission-read-only")
            await pilot.pause(0.05)
            self.assertIn("/permission read-only", str(app.screen.query_one("#settings-pending").render()))
            await pilot.click("#settings-reset")
            for _ in range(20):
                await pilot.pause(0.05)
                if len(app.screen.query("#settings-permission-ask")) == 1:
                    break
            self.assertEqual(len(app.screen.query("#settings-card")), 1)
            self.assertNotIn("/permission read-only", str(app.screen.query_one("#settings-pending").render()))
            self.assertTrue(app.screen.query_one("#settings-permission-ask").has_class("settings-selected"))
            self.assertEqual(controller.commands, ["/settings"])
            await app.screen.dismiss(None)
            await worker.wait()

    async def test_settings_registry_entries_are_selectable_dashboard_controls(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.1)
            # Connection has an inventory entry as well as the editable
            # provider form. Selecting it must not apply or close Settings.
            control = app.screen.query_one("#settings-entry-provider-model")
            control.press()
            await pilot.pause(0.05)
            self.assertIn("Default model", str(app.screen.query_one("#settings-detail").render()))
            self.assertEqual(controller.commands, ["/settings"])
            await app.screen.dismiss(None)
            await worker.wait()

    async def test_messaging_app_picker_changes_fields_without_closing_or_saving(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.1)
            await pilot.click("#settings-page-messaging")
            await pilot.pause(0.1)
            await pilot.click("#settings-channel-slack")
            await pilot.pause(0.1)
            self.assertTrue(app.screen.query_one("#settings-channel-slack").has_class("settings-selected"))
            self.assertEqual(len(app.screen.query("#settings-channel-field-one")), 1)
            self.assertEqual(len(app.screen.query("#settings-telegram-workspace")), 0)
            self.assertEqual(controller.commands, ["/settings"])
            await app.screen.dismiss(None)
            await worker.wait()

    async def test_settings_all_pages_use_live_pickers_and_stage_one_typed_change(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 40)) as pilot:
            worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.1)
            self.assertGreaterEqual(len(app.screen.query("#settings-provider-grid Button")), 30)

            app.screen.query_one("#settings-page-environment").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-environment-computer").press()
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#settings-computer-driver")), 1)
            self.assertEqual(len(app.screen.query("#settings-container-image")), 0)

            app.screen.query_one("#settings-page-integrations").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-integration-mcp-action").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-mcp-action-python").value = "/usr/bin/env"
            app.screen.query_one("#settings-mcp-action-server").value = "/usr/bin/env"
            app.screen.query_one("#settings-mcp-action-tool").value = "run_action"
            app.screen.query_one("#settings-mcp-action-stage").press()
            await pilot.pause(0.05)
            self.assertIn("/quick-mcp-action", str(app.screen.query_one("#settings-pending").render()))

            app.screen.query_one("#settings-page-messaging").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-channel-direction-outbound").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-channel-dingtalk").press()
            await pilot.pause(0.05)
            self.assertEqual(app.screen.query_one("#settings-channel-field-one").value, "DINGTALK_WEBHOOK_URL")

            app.screen.query_one("#settings-page-automation").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-automation-schedule-service").press()
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#settings-schedule-service-start")), 1)
            self.assertEqual(len(app.screen.query("#settings-schedule-goal")), 0)

            app.screen.query_one("#settings-page-company").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-company-employees").press()
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#settings-review-approval")), 0)
            self.assertEqual(len(app.screen.query("#settings-employee-id")), 1)

            app.screen.query_one("#settings-page-data").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-data-evolution").press()
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#settings-evolution-propose")), 1)

            app.screen.query_one("#settings-done").press()
            await worker.wait()

        self.assertEqual(controller.commands[0], "/settings")
        self.assertTrue(controller.commands[1].startswith("/quick-mcp-action "))

    async def test_company_settings_stage_manager_and_skill_proposals_without_applying_them(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 40)) as pilot:
            manager_worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.1)
            app.screen.query_one("#settings-page-company").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-manager-model-profile").value = "manager-contract"
            app.screen.query_one("#settings-manager-role").value = "Executive Manager"
            app.screen.query_one("#settings-manager-rationale").value = "Validate proposal-only Manager settings."
            app.screen.query_one("#settings-manager-stage").press()
            await pilot.pause(0.05)
            self.assertIn("ROSTER proposal", str(app.screen.query_one("#settings-pending").render()))
            self.assertEqual(controller.commands, ["/settings"])
            app.screen.query_one("#settings-done").press()
            await manager_worker.wait()
            self.assertTrue(controller.commands[-1].startswith("/company-manager-revise "))

            skill_worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.1)
            app.screen.query_one("#settings-page-company").press()
            await pilot.pause(0.05)
            app.screen.query_one("#settings-company-skills").press()
            await pilot.pause(0.05)
            values = {
                "#settings-skill-employee-id": "employee-contract",
                "#settings-skill-key": "settings-skill",
                "#settings-skill-context": "contract",
                "#settings-skill-purpose": "Validate proposal staging.",
                "#settings-skill-steps": "Inspect settings | Propose patch",
                "#settings-skill-verification": "Confirm no active change",
                "#settings-skill-correction": "contract-correction-1",
                "#settings-skill-rationale": "A confirmed correction requires a procedure.",
            }
            for selector, value in values.items():
                app.screen.query_one(selector).value = value
            app.screen.query_one("#settings-skill-stage").press()
            await pilot.pause(0.05)
            self.assertIn("Skill Patch proposal", str(app.screen.query_one("#settings-pending").render()))
            app.screen.query_one("#settings-done").press()
            await skill_worker.wait()

        self.assertTrue(controller.commands[-1].startswith("/company-skill-propose "))

    async def test_connection_settings_stage_one_atomic_non_secret_change(self) -> None:
        """A provider switch must not dismiss the modal or emit half a profile."""

        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.1)
            await pilot.click("#settings-page-connection")
            await pilot.pause(0.1)
            app.screen.query_one("#settings-provider-kind").value = "openrouter"
            app.screen.query_one("#settings-model-input").value = "openrouter/test"
            app.screen.query_one("#settings-connection-stage").press()
            await pilot.pause(0.05)
            pending = str(app.screen.query_one("#settings-pending").render())
            self.assertIn("/connection", pending)
            self.assertIn("openrouter", pending)
            self.assertEqual(len(app.screen.query("#settings-card")), 1)
            await pilot.click("#settings-done")
            await worker.wait()

        self.assertEqual(len(controller.commands), 2)
        self.assertEqual(controller.commands[0], "/settings")
        self.assertTrue(controller.commands[1].startswith("/connection {"))

    async def test_connection_settings_exposes_account_login_and_model_picker(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 40)) as pilot:
            worker = app.run_worker(app._process_submission("/settings"))
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen.query("#settings-auth-account")), 1)
            self.assertEqual(len(app.screen.query("#settings-auth-api")), 1)
            self.assertEqual(len(app.screen.query("#settings-model-picker")), 1)

            await pilot.click("#settings-auth-account")
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#settings-provider-login")), 1)
            app.screen.query_one("#settings-provider-login").press()
            await pilot.pause(0.05)
            pending = str(app.screen.query_one("#settings-pending").render())
            self.assertIn("/connection", pending)
            self.assertIn("/provider-login", pending)
            await pilot.click("#settings-done")
            await worker.wait()

        self.assertEqual(controller.commands[0], "/settings")
        self.assertTrue(controller.commands[1].startswith("/connection "))
        self.assertEqual(controller.commands[2], "/provider-login")

    async def test_model_command_opens_picker_and_applies_selected_model(self) -> None:
        controller = _Controller()
        original_execute = controller.execute_command

        async def execute(command: str) -> ModernTerminalCommandResult:
            if command == "/model":
                controller.commands.append(command)
                return ModernTerminalCommandResult(open_model_picker=True)
            return await original_execute(command)

        controller.execute_command = execute  # type: ignore[method-assign]
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            worker = app.run_worker(app._process_submission("/model"))
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen.query("#model-card")), 1)
            await pilot.click("#model-option-1")
            await worker.wait()

        self.assertEqual(controller.commands, ["/model", "/model model-next"])

    async def test_settings_and_command_shortcuts_keep_operator_controls_discoverable(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.press("f3")
            await pilot.pause(0.05)
            self.assertEqual(app.query_one("#composer").value, "/")
            self.assertTrue(app.query_one("#command-menu").display)

            await pilot.press("f2")
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#settings-card")), 1)
            await pilot.click("#settings-page-execution")
            await pilot.pause(0.05)
            await pilot.click("#settings-permission-read-only")
            await pilot.pause(0.1)
            self.assertEqual(controller.commands, [])
            await pilot.click("#settings-done")
            await pilot.pause(0.1)
            self.assertEqual(controller.commands, ["/permission read-only"])

    async def test_graph_workbench_authors_a_draft_then_reopens_for_explicit_selection(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(118, 48)) as pilot:
            worker = app.run_worker(app._process_submission("/graph"))
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen.query("#graph-control-card")), 1)
            initial_screen = app.screen
            app.screen.query_one("#graph-draft-id").value = "tui_draft"
            app.screen.query_one("#graph-draft-capabilities").value = "analysis"
            app.screen.query_one("#graph-create-draft").press()
            for _ in range(20):
                await pilot.pause(0.05)
                if app.screen is not initial_screen:
                    break
            self.assertIsNot(app.screen, initial_screen)
            self.assertEqual(len(app.screen.query("#graph-control-card")), 1)
            self.assertEqual(controller.graph_actions[0]["action"], "create_draft")
            self.assertEqual(controller.graph_actions[0]["blueprint_id"], "tui_draft")
            await app.screen.dismiss(None)
            await worker.wait()

    async def test_graph_workbench_requests_a_provider_free_saved_selection_preview(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(118, 48)) as pilot:
            worker = app.run_worker(app._process_submission("/graph"))
            await pilot.pause(0.1)
            app.screen.query_one("#graph-preview-goal").value = "Validate the release proposal"
            app.screen.query_one("#graph-preview-selected").press()
            await worker.wait()

        self.assertEqual(controller.graph_preview_goals, ["Validate the release proposal"])
        self.assertEqual(controller.graph_actions, [])
        self.assertEqual(controller.graph_submissions, [])

    async def test_graph_workbench_can_save_constraints_then_preview_in_one_step(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(118, 48)) as pilot:
            worker = app.run_worker(app._process_submission("/graph"))
            await pilot.pause(0.1)
            app.screen.query_one("#graph-preview-goal").value = "Check a bounded release plan"
            app.screen.query_one("#graph-save-preview").press()
            await worker.wait()

        self.assertEqual(len(controller.graph_submissions), 1)
        self.assertEqual(controller.graph_preview_goals, ["Check a bounded release plan"])
        self.assertEqual(controller.graph_actions, [])

    async def test_graph_workbench_submits_typed_multi_task_topology(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(118, 48)) as pilot:
            worker = app.run_worker(app._process_submission("/graph"))
            await pilot.pause(0.1)
            initial_screen = app.screen
            app.screen.query_one("#graph-draft-id").value = "tui_topology"
            app.screen.query_one("#graph-topology-0-id").value = "research"
            app.screen.query_one("#graph-topology-0-objective").value = "Research {{objective}}"
            app.screen.query_one("#graph-topology-0-capabilities").value = "analysis"
            app.screen.query_one("#graph-topology-0-acceptance").value = "Evidence"
            app.screen.query_one("#graph-topology-1-id").value = "final"
            app.screen.query_one("#graph-topology-1-objective").value = "Integrate {{objective}}"
            app.screen.query_one("#graph-topology-1-depends").value = "research"
            app.screen.query_one("#graph-topology-1-capabilities").value = "analysis"
            app.screen.query_one("#graph-topology-1-acceptance").value = "Decision"
            app.screen.query_one("#graph-topology-final").value = "final"
            app.screen.query_one("#graph-save-topology-draft").press()
            for _ in range(20):
                await pilot.pause(0.05)
                if app.screen is not initial_screen:
                    break
            self.assertIsNot(app.screen, initial_screen)
            self.assertEqual(controller.graph_actions[0]["action"], "save_topology_draft")
            topology = controller.graph_actions[0]["topology"]
            assert isinstance(topology, dict)
            self.assertEqual([task["task_id"] for task in topology["tasks"]], ["research", "final"])
            await app.screen.dismiss(None)
            await worker.wait()

    async def test_job_audit_is_read_only_and_displays_graph_lineage(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(118, 42)) as pilot:
            await pilot.press("f4")
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen.query("#job-audit-card")), 1)
            self.assertIn("job-modern-audit", str(app.screen.query_one("#job-audit-summary").render()))
            self.assertIn("Graph · v1 → v2", str(app.screen.query_one("#job-audit-change-summary").render()))
            self.assertIn("INSERT×1", str(app.screen.query_one("#job-audit-change-summary").render()))
            self.assertIn("Blueprint · review@1", str(app.screen.query_one("#job-audit-graph").render()))
            self.assertIn("terminal=JOB_SUCCEEDED", str(app.screen.query_one("#job-audit-graph").render()))
            self.assertIn("REJECTED · INSERT", str(app.screen.query_one("#job-audit-proposals").render()))
            self.assertIn("research=SUCCEEDED", str(app.screen.query_one("#job-audit-checkpoints").render()))
            self.assertEqual(controller.commands, [])
            await pilot.click("#job-audit-close")
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#job-audit-card")), 0)

    async def test_job_audit_renders_safe_frozen_route_admission_read_only(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(60, 42)) as pilot:
            await pilot.press("f4")
            await pilot.pause(0.1)
            route_admissions = str(
                app.screen.query_one("#job-audit-route-admissions").render()
            )
            sections = [
                str(section.render())
                for section in app.screen.query(".settings-section")
            ]
            self.assertIn("employee-reviewer", route_admissions)
            self.assertIn("review", route_admissions)
            self.assertIn("local-review-route", route_admissions)
            self.assertIn("POLICY_ORDER", route_admissions)
            self.assertIn("c" * 16, route_admissions)
            self.assertIn("d" * 16, route_admissions)
            self.assertIn("e" * 16, route_admissions)
            self.assertIn("a" * 16, route_admissions)
            self.assertIn("b" * 16, route_admissions)
            self.assertIn("f" * 16, route_admissions)
            self.assertIn("0" * 16, route_admissions)
            self.assertIn("uncertainty=0.000", route_admissions)
            self.assertNotIn("credential", route_admissions.lower())
            self.assertNotIn("run-", route_admissions)
            self.assertIn("FROZEN ROUTE ADMISSIONS · READ ONLY", sections)
            self.assertEqual(controller.commands, [])
            self.assertEqual(controller.graph_actions, [])
            self.assertEqual(controller.graph_submissions, [])
            await pilot.click("#job-audit-close")
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#job-audit-card")), 0)

    async def test_job_audit_omits_malformed_route_status_pins(self) -> None:
        controller = _Controller()
        snapshot = controller.job_audit_snapshot()
        snapshot["route_admissions"] = (
            {
                **snapshot["route_admissions"][0],
                "selected_uncertainty": float("nan"),
                "provider": "must-not-render",
            },
        )
        controller.job_audit_snapshot = lambda _job_id=None: snapshot  # type: ignore[method-assign]
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(60, 42)) as pilot:
            await pilot.press("f4")
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen.query("#job-audit-route-admissions")), 0)
            rendered = "\n".join(
                str(section.render()) for section in app.screen.query("Static")
            )
            self.assertNotIn("must-not-render", rendered)

    async def test_job_audit_renders_content_free_durable_invocations_at_narrow_width(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(40, 42)) as pilot:
            await pilot.press("f4")
            await pilot.pause(0.1)
            invocations = str(
                app.screen.query_one("#job-audit-model-invocations").render()
            )
            sections = [
                str(section.render())
                for section in app.screen.query(".settings-section")
            ]
            self.assertIn("employee-reviewer", invocations)
            self.assertIn("local-review-route", invocations)
            self.assertIn("recorded-terminal=SUCCEEDED", invocations)
            self.assertIn("usage=UNAVAILABLE", invocations)
            self.assertIn("$0.000000 (AVAILABLE)", invocations)
            self.assertIn("c" * 16, invocations)
            self.assertIn("f" * 16, invocations)
            self.assertNotIn("private-call-id", invocations)
            self.assertNotIn("private-provider", invocations)
            self.assertNotIn("private-model", invocations)
            self.assertNotIn("private-output", invocations)
            self.assertNotIn("private-credential", invocations)
            self.assertNotIn("비공개 컨텍스트", invocations)
            self.assertIn("DURABLE MODEL INVOCATIONS · READ ONLY", sections)
            self.assertEqual(controller.commands, [])
            self.assertEqual(controller.graph_actions, [])
            self.assertEqual(controller.graph_submissions, [])
            await pilot.click("#job-audit-close")
            await pilot.pause(0.05)
            self.assertEqual(len(app.screen.query("#job-audit-card")), 0)

    async def test_pending_graph_proposal_is_explicitly_resolved_from_job_audit(self) -> None:
        controller = _Controller(pending_graph_proposal=True)
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(118, 46)) as pilot:
            # A proposal decision re-enters the controller through the modal's
            # owning worker.  Wait for that worker rather than asserting while
            # Textual is still unwinding the screen-dismiss callback.
            worker = app.run_worker(app._open_job_audit())
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen.query("#job-audit-approve-0")), 1)
            await pilot.click("#job-audit-reject-0")
            await pilot.pause(0.2)
            self.assertEqual(
                controller.graph_proposal_decisions,
                [
                    (
                        "job-modern-audit",
                        "graph-proposal-1234567890abcdef12345678",
                        False,
                    )
                ],
            )
            # The audit deliberately reopens as a refreshed, read-only
            # projection after the one-shot decision; close that follow-up
            # view before awaiting the owning worker.
            await pilot.click("#job-audit-close")
            await worker.wait()

        self.assertEqual(
            controller.graph_proposal_decisions,
            [
                (
                    "job-modern-audit",
                    "graph-proposal-1234567890abcdef12345678",
                    False,
                )
            ],
        )

    async def test_job_command_opens_the_same_audit_after_command_dispatch(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(118, 42)) as pilot:
            worker = app.run_worker(app._process_submission("/job"))
            await pilot.pause(0.1)
            self.assertEqual(controller.commands, ["/job"])
            self.assertEqual(len(app.screen.query("#job-audit-card")), 1)
            await pilot.click("#job-audit-close")
            await worker.wait()

    async def test_job_audit_catalog_reopens_the_selected_retained_job(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(118, 42)) as pilot:
            await pilot.press("f4")
            await pilot.pause(0.1)
            await pilot.click("#job-audit-select-1")
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen.query("#job-audit-card")), 1)
            self.assertIn(
                "job-prior-audit",
                str(app.screen.query_one("#job-audit-summary").render()),
            )
            await pilot.click("#job-audit-close")
            await pilot.pause(0.05)

    async def test_specific_job_command_opens_the_requested_read_only_audit(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(118, 42)) as pilot:
            worker = app.run_worker(app._process_submission("/job job-prior-audit"))
            await pilot.pause(0.1)
            self.assertEqual(controller.commands, ["/job job-prior-audit"])
            self.assertIn(
                "job-prior-audit",
                str(app.screen.query_one("#job-audit-summary").render()),
            )
            self.assertIn("FAILED", str(app.screen.query_one("#job-audit-summary").render()))
            await pilot.click("#job-audit-close")
            await worker.wait()

    def test_terminal_crash_record_excludes_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "terminal.log"
            try:
                raise ValueError("do-not-store-user-secret")
            except ValueError as exc:
                saved = record_modern_terminal_crash(exc, phase="test", path=path)

            self.assertEqual(saved, path)
            content = path.read_text(encoding="utf-8")
        self.assertIn("exception_type=ValueError", content)
        self.assertIn("phase=test", content)
        self.assertNotIn("do-not-store-user-secret", content)

    async def test_streamed_answer_has_one_surface_and_commands_stay_controller_owned(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.press("h", "e", "l", "l", "o", "enter")
            await pilot.pause(0.2)
            answer = str(app.query_one("#answer").render())
            self.assertEqual(controller.goals, ["hello"])
            self.assertTrue(str(app.query_one("#company-status").render()).startswith("Company ready"))
            self.assertEqual(answer, "first streamed answer")

            await pilot.press("/", "h", "e", "l", "p", "enter")
            await pilot.pause(0.1)
            self.assertEqual(controller.commands, ["/help"])

    async def test_company_watch_projects_the_same_final_report_contract(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("r", "e", "p", "o", "r", "t", "enter")
            await pilot.pause(0.2)
            watch = str(app.query_one("#company-watch-body").render())
            self.assertIn("LAST COMPANY REPORT", watch)
            self.assertIn("MANAGER_OPERATIONAL_ENVELOPE", watch)
            self.assertIn("employee-manager", watch)
            self.assertIn("employee-specialist", watch)

    async def test_company_watch_and_run_pulse_follow_controller_events_without_owning_state(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.05)
            watch = str(app.query_one("#company-watch-body").render())
            self.assertIn("ROSTER  r3 · 2 active", watch)
            self.assertIn("Generalist", watch)
            self.assertIn("AUTHORITY   ask", watch)

            app.receive_event(ProductEvent(ProductEventType.COMPILER_STARTED, "Compiler started"))
            pulse = str(app.query_one("#company-pulse").render())
            assessment = str(app.query_one("#current-assessment").render())
            self.assertIn("STAGE      composing", pulse)
            self.assertIn("LATEST     Compiler started", pulse)
            self.assertIn("OBJECTIVE  No goal submitted yet", assessment)
            self.assertIn("OBSERVE    No new Manager outcome evidence is recorded.", assessment)
            self.assertIn("DECISION   No active execution decision.", assessment)
            self.assertIn("NEXT       Inspect Company state before taking action.", assessment)
            activity = str(app.query_one("#activity-feed").render())
            self.assertIn("COMPOSING", activity)
            self.assertIn("Company is scoping work", activity)

            app.receive_event(
                ProductEvent(
                    ProductEventType.TASK_ASSIGNED,
                    "Reviewer assigned final · persistent",
                    task_id="final",
                    employee_id="reviewer",
                    data={
                        "employee_role": "Reviewer",
                        "employee_tenure": "persistent",
                    },
                )
            )
            app.receive_event(
                ProductEvent(
                    ProductEventType.JOB_FINISHED,
                    "Company job succeeded",
                    data={
                        "status": "SUCCEEDED",
                        "unique_employee_count": 2,
                        "maximum_parallelism": 1,
                        "graph_patch_count": 0,
                    },
                )
            )
            activity = str(app.query_one("#activity-feed").render())
            self.assertIn("Reviewer assigned final · persistent", activity)
            self.assertIn("2 employees", activity)

            await pilot.press("ctrl+o")
            self.assertFalse(app.query_one("#company-watch").display)
            await pilot.press("ctrl+o")
            self.assertTrue(app.query_one("#company-watch").display)

    async def test_session_scoped_input_history_restores_draft_without_submitting(self) -> None:
        controller = _Controller()
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            composer = app.query_one("#composer")
            composer.value = "unsent draft"
            await pilot.press("up")
            self.assertEqual(composer.value, "Second goal")
            await pilot.press("up")
            self.assertEqual(composer.value, "First goal")
            await pilot.press("down")
            self.assertEqual(composer.value, "Second goal")
            await pilot.press("down")
            self.assertEqual(composer.value, "unsent draft")
            self.assertEqual(controller.goals, [])

    async def test_approval_is_requested_from_the_controller_and_resolved_in_modal(self) -> None:
        controller = _Controller(requires_approval=True)
        app = create_modern_terminal_app(controller)

        async with app.run_test(size=(110, 36)) as pilot:
            worker = app.run_worker(app._process_submission("go"))
            await pilot.pause(0.3)
            await pilot.click("#allow-once")
            await worker.wait()
            self.assertEqual(controller.goals, ["go"])
            self.assertIn("first streamed answer", str(app.query_one("#answer").render()))
