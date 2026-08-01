from __future__ import annotations

import asyncio
import io
import os
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dynamic_firm.product.approval import InteractiveApprovalController
from dynamic_firm.kernel.models import (
    AttemptBudgetEvidence,
    AttemptFailureKind,
    JobMutationEvent,
    TaskMutationType,
)
from dynamic_firm.product.events import (
    ProductEvent,
    ProductEventType,
    product_event_from_mutation,
    product_event_from_run,
)
from dynamic_firm.product.models import ModelOption
from dynamic_firm.product.terminal import display_width, strip_ansi
from dynamic_firm.product.tui_live_assessment import live_assessment_entries
from dynamic_firm.product.tui import (
    ALT_SCREEN_ENTER,
    ALT_SCREEN_EXIT,
    CLEAR_LINE,
    CLEAR_SCREEN,
    SHOW_CURSOR,
    InlineTerminalUI,
    LiveTerminalUI,
    _drop_last_typeahead_grapheme,
)
from dynamic_firm.runtime.models import (
    ApprovalDecision,
    ApprovalRequest,
    EventType,
    RunEvent,
    ToolEffect,
    ToolRisk,
    Usage,
    utc_now,
)
from dynamic_firm.runtime.ports import CancellationToken


_ASCII_WORDMARK_FIRST_LINE = "███╗   ██╗ ██████╗ ██████╗ ██╗   ██╗ ██████╗████████╗"
_GOLDEN_ROOT = Path(__file__).parent / "golden"
_EMPLOYEE_ROLES = ("Noruct Generalist", "Repository Analyst")
_CAPABILITIES = (
    "conversation",
    "evidence synthesis",
    "general reasoning",
    "repository analysis",
)
_TOOLS = ("list", "read", "apply change set*")


def approval_request(*, effect: ToolEffect, allow_session: bool) -> ApprovalRequest:
    return ApprovalRequest(
        action_id="action-1",
        run_id="run-1",
        job_id="job-1",
        task_id="task-1",
        employee_id="employee-1",
        tool_name="tool",
        effect=effect,
        risk=ToolRisk.MEDIUM,
        resource_key="workspace:test:file.py",
        preview="Edit file.py",
        allow_session=allow_session,
    )


def render_golden_surface(width: int) -> str:
    output = io.StringIO()
    ui = InlineTerminalUI(
        stdin=io.StringIO("1\n"),
        stdout=output,
        interactive=False,
        color=False,
        animations=False,
        terminal_width=width,
    )
    ui.banner(
        workspace="/workspace/noruct/product",
        session_id="session-golden-contract",
        model="gpt-contract",
        provider="openai-codex (external)",
        authority="ask · shadow-only worker",
        version="0.0.9",
        roster_revision=2,
        active_employee_count=2,
        employee_roles=_EMPLOYEE_ROLES,
        capabilities=_CAPABILITIES,
        tools=_TOOLS,
    )
    ui.begin_goal("검증 가능한 변경을 최소 조직으로 구현해줘")
    ui.handle_event(ProductEvent(ProductEventType.COMPILER_STARTED, "compile"))
    ui.handle_event(
        ProductEvent(
            ProductEventType.PLAN_ACCEPTED,
            "dynamic plan",
            data={"mode": "DYNAMIC", "task_count": 2},
        )
    )
    ui.handle_event(
        ProductEvent(
            ProductEventType.EMPLOYEE_STARTED,
            "research started",
            task_id="task-inspect-spec",
            employee_id="employee-researcher",
        )
    )
    ui.handle_event(
        ProductEvent(
            ProductEventType.EMPLOYEE_STARTED,
            "implementation started",
            task_id="task-implement-change",
            employee_id="employee-engineer",
        )
    )
    for attempt, passed, detail in (
        (1, False, "edge case failed"),
        (2, True, "all bounded checks passed"),
    ):
        ui.handle_event(
            ProductEvent(
                ProductEventType.VALIDATION_RECORDED,
                "validation",
                task_id="task-implement-change",
                employee_id="employee-engineer",
                data={
                    "attempt": attempt,
                    "name": "fixture-validation",
                    "passed": passed,
                    "detail": detail,
                },
            )
        )
    request = replace(
        approval_request(effect=ToolEffect.WRITE, allow_session=False),
        tool_name="apply_workspace_change_set",
        task_id="task-implement-change",
        employee_id="employee-engineer",
        preview=(
            "Apply one validated shadow change\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-result = unsafe(value)\n"
            "+result = validated(value)"
        ),
    )
    ui.ask_approval(request)
    ui.commit("")
    ui.handle_event(
        ProductEvent(
            ProductEventType.APPROVAL_RESOLVED,
            "Approval allow once",
            task_id="task-implement-change",
            employee_id="employee-engineer",
        )
    )
    ui.handle_event(
        ProductEvent(
            ProductEventType.TOOL_FINISHED,
            "apply_workspace_change_set completed",
            task_id="task-implement-change",
            employee_id="employee-engineer",
            data={"tool_name": "apply_workspace_change_set", "output_bytes": 96},
        )
    )
    ui.handle_event(
        ProductEvent(
            ProductEventType.EMPLOYEE_FINISHED,
            "employee-researcher succeeded: inspect-spec",
            task_id="task-inspect-spec",
            employee_id="employee-researcher",
        )
    )
    ui.handle_event(
        ProductEvent(
            ProductEventType.EMPLOYEE_FINISHED,
            "employee-engineer succeeded: implement-change",
            task_id="task-implement-change",
            employee_id="employee-engineer",
        )
    )
    ui.handle_event(
        ProductEvent(
            ProductEventType.JOB_FINISHED,
            "Company job succeeded",
            data={"status": "SUCCEEDED"},
        )
    )
    ui.answer("변경을 검증하고 승인된 결과를 적용했습니다.")
    ui.result_details(
        SimpleNamespace(
            status=SimpleNamespace(value="SUCCEEDED"),
            acceptance_evidence=("Applied shadow change: src/app.py",),
            unresolved_issues=(),
            metrics=SimpleNamespace(
                unique_employee_count=2,
                maximum_parallelism=2,
                usage=Usage(
                    model_calls=3,
                    tool_calls=1,
                    input_tokens=12_000,
                    output_tokens=640,
                ),
            ),
        )
    )
    return output.getvalue()


class InlineTerminalUITests(unittest.TestCase):
    def test_direct_stream_and_final_result_have_one_transcript_owner(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
        )
        ui.begin_goal("Answer directly", echo=False)
        ui.handle_event(
            ProductEvent(
                ProductEventType.INPUT_ROUTED,
                "Direct conversation",
                data={"route": "CONVERSATION"},
            )
        )
        for delta in ("One ", "durable answer."):
            ui.handle_event(
                ProductEvent(
                    ProductEventType.MODEL_STREAMING,
                    delta,
                    data={"stream_kind": "text_delta", "text": delta},
                )
            )
        ui.answer("One durable answer.")

        rendered = output.getvalue()
        self.assertEqual(rendered.count("● Noruct"), 1)
        self.assertEqual(rendered.count("One durable answer."), 1)
        self.assertNotIn("canonical result", rendered)

    def test_company_employee_delta_is_status_only_and_content_hidden(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
        )
        ui.begin_goal("Run a company job", echo=False)
        ui.handle_event(
            ProductEvent(
                ProductEventType.INPUT_ROUTED,
                "Company goal",
                data={"route": "COMPANY_GOAL"},
            )
        )
        ui.handle_event(
            ProductEvent(
                ProductEventType.MODEL_STREAMING,
                "private intermediate employee answer",
                data={
                    "stream_kind": "text_delta",
                    "text": "private intermediate employee answer",
                },
            )
        )
        ui.clear_status()

        rendered = output.getvalue()
        self.assertNotIn("private intermediate employee answer", rendered)
        self.assertNotIn("● Noruct", rendered)

    def test_typed_organization_admission_is_visible_without_exposing_model_output(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
        )
        ui.begin_goal("Integrate specialist evidence")
        ui.handle_event(
            ProductEvent(
                ProductEventType.ORGANIZATION_ADMISSION,
                "Admitted typed capability gap",
                task_id="analyze_goal",
                data={
                    "admitted": True,
                    "capability": "security_review",
                    "reason": "TYPED_CAPABILITY_GAP",
                },
            )
        )

        rendered = output.getvalue()
        self.assertIn("Specialist need admitted", rendered)
        self.assertNotIn("Organization expanded", rendered)
        self.assertIn("security_review", rendered)
        self.assertNotIn("Admitted typed capability gap", rendered)

        ui.handle_event(
            ProductEvent(
                ProductEventType.TASK_ASSIGNED,
                "Security Reviewer assigned security-review · temporary",
                task_id="security-review",
                employee_id="temp-security",
                data={
                    "employee_role": "Security Reviewer",
                    "employee_tenure": "temporary",
                },
            )
        )
        ui.handle_event(
            ProductEvent(
                ProductEventType.GRAPH_PATCH_APPLIED,
                "Organization expanded · 1 task added",
                data={"semantic_operation": "INSERT"},
            )
        )
        rendered = output.getvalue()
        self.assertIn("Security Reviewer assigned", rendered)
        self.assertIn("temporary", rendered)
        self.assertIn("Organization expanded", rendered)

    def test_external_read_readiness_uses_only_the_normalized_product_identity(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
        )

        ui.handle_event(
            ProductEvent(
                ProductEventType.CAPABILITY_READY,
                "External read capability ready",
                data={"tool_name": "read_external_context", "trust": "untrusted"},
            )
        )

        rendered = output.getvalue()
        self.assertIn("External read capability ready", rendered)
        self.assertNotIn("MCP", rendered)
        self.assertNotIn("read_issue", rendered)

    def test_task_mutation_event_projects_attempt_reason_and_assignee_transition(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
            terminal_width=80,
        )
        ui.begin_goal("Recover a bounded read task")
        ui.handle_event(
            product_event_from_mutation(
                JobMutationEvent(
                    event_id="mutation-1",
                    sequence=1,
                    mutation_type=TaskMutationType.REROUTE,
                    task_id="analyze-repository",
                    source_attempt_id="attempt-1",
                    source_attempt_content_hash="attempt-hash",
                    target_attempt_id="attempt-2",
                    source_attempt_sequence=1,
                    target_attempt_sequence=2,
                    from_employee_id="employee-analyst-a",
                    to_employee_id="employee-analyst-b",
                    failure_kind=AttemptFailureKind.ASSIGNEE_MISMATCH,
                    rationale="Typed assignee mismatch.",
                    matched_capabilities=("analysis",),
                    downstream_task_ids=("final",),
                    mutation_budget_before=2,
                    mutation_budget_after=1,
                    next_attempt_reservation=AttemptBudgetEvidence(1, 1, 0.1, 5_000),
                    frozen_snapshot_hash="snapshot-hash",
                    content_hash="event-hash",
                )
            )
        )

        rendered = output.getvalue()
        self.assertIn("Reroute", rendered)
        self.assertIn("attempt 2", rendered)
        self.assertIn("analyst a", rendered)
        self.assertIn("analyst b", rendered)
        self.assertIn("assignee mismatch", rendered)

    def test_banner_answer_and_compact_details_form_a_conversation_surface(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=True,
            color=False,
            animations=False,
        )

        ui.banner(
            workspace="/workspace/project",
            session_id="session-contract",
            model="contract-model",
            provider="openai-api",
            authority="read-only",
            version="0.0.4",
            roster_revision=2,
            active_employee_count=2,
            employee_roles=_EMPLOYEE_ROLES,
            capabilities=_CAPABILITIES,
            tools=("list", "read"),
        )
        ui.handle_event(
            ProductEvent(
                ProductEventType.INPUT_ROUTED,
                "Direct conversation · compiler skipped",
                data={
                    "route": "CONVERSATION",
                    "roster_revision": 2,
                    "active_employee_count": 2,
                },
            )
        )
        ui.answer("Hello from Noruct.")
        ui.close()

        rendered = output.getvalue()
        self.assertIn("NORUCT 0.0.4", rendered)
        self.assertIn(_ASCII_WORDMARK_FIRST_LINE, rendered)
        self.assertIn("COMPANY ONLINE", rendered)
        self.assertNotIn("Welcome to Noruct", rendered)
        self.assertIn("company snapshot", rendered)
        self.assertIn("model         contract-model", rendered)
        self.assertIn("via           openai-api", rendered)
        self.assertIn("authority     read-only", rendered)
        self.assertIn("roster        r2 · 2 active", rendered)
        self.assertIn("employees     Noruct Generalist · Repository Analyst", rendered)
        self.assertIn("capabilities  conversation", rendered)
        self.assertIn("tools         list · read", rendered)
        self.assertIn("Dynamic Firm Runtime", rendered)
        self.assertIn("● Noruct", rendered)
        self.assertIn("Hello from Noruct.", rendered)
        self.assertNotIn("Direct conversation · compiler skipped", rendered)

    def test_full_wordmark_uses_a_colored_brand_gradient(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=True,
            color=True,
            animations=False,
            terminal_width=80,
        )

        ui.banner(
            workspace="/workspace/project",
            session_id="session-brand",
            model="contract-model",
            provider="provider",
            authority="read-only",
            version="0.0.18",
            employee_roles=_EMPLOYEE_ROLES,
            capabilities=_CAPABILITIES,
            tools=_TOOLS,
        )

        rendered = output.getvalue()
        self.assertIn("\x1b[38;2;167;139;250m", rendered)
        self.assertIn("\x1b[38;2;103;232;249m", rendered)
        self.assertIn(_ASCII_WORDMARK_FIRST_LINE, strip_ansi(rendered))
        self.assertIn("COMPANY ONLINE", strip_ansi(rendered))

    def test_multiline_input_uses_a_trailing_backslash(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO("first line\\\nsecond line\n"),
            stdout=output,
            interactive=True,
            color=False,
            animations=False,
        )

        self.assertEqual(ui.read_goal(), "first line\nsecond line")
        self.assertIn("/model", output.getvalue())
        self.assertIn("│ ❯ ", output.getvalue())
        self.assertIn("│ … ", output.getvalue())

    def test_goal_input_delegates_buffer_and_cursor_to_readline_on_a_real_tty(self) -> None:
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            interactive=True,
            color=False,
            animations=False,
        )
        with (
            patch.object(ui, "_uses_readline_editor", return_value=True),
            patch("builtins.input", return_value="edited value") as read_input,
        ):
            value = ui._read_goal_line("│ ❯ ")

        self.assertEqual(value, "edited value")
        read_input.assert_called_once_with("│ ❯ ")

    @unittest.skipUnless(os.name == "posix", "PTY regression requires a POSIX terminal")
    def test_readline_repaints_repeated_backspace_across_a_wrap_boundary(self) -> None:
        import fcntl
        import pty
        import select
        import struct
        import sys
        import termios
        import time

        process_id, master_fd = pty.fork()
        if process_id == 0:
            exit_code = 0
            try:
                child_ui = InlineTerminalUI(
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    interactive=True,
                    color=False,
                    animations=False,
                    terminal_width=40,
                )
                result = child_ui.read_goal()
                print(f"RESULT={result!r}", flush=True)
                child_ui.close()
            except BaseException:
                exit_code = 1
            finally:
                os._exit(exit_code)

        fcntl.ioctl(
            master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 12, 20, 0, 0),
        )
        captured = bytearray()
        sent = False
        completed = False
        deadline = time.monotonic() + 3.0
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    continue
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
                if not sent and "❯ ".encode() in captured:
                    os.write(
                        master_fd,
                        b"abcdefghijklmnopqrstuvwxyz" + b"\x7f" * 12 + b"XY\n",
                    )
                    sent = True
                if b"RESULT=" in captured:
                    completed = True
                    break
        finally:
            try:
                if not completed:
                    try:
                        os.kill(process_id, 15)
                    except ProcessLookupError:
                        pass
                _, status = os.waitpid(process_id, 0)
            finally:
                os.close(master_fd)

        rendered = captured.decode("utf-8", errors="replace")
        self.assertTrue(sent)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIn("RESULT='abcdefghijklmnXY'", rendered)
        # Crossing back over a soft-wrap boundary must trigger an editor
        # redisplay. Canonical stdin.readline() only emits \b-space-\b and
        # leaves the wrapped terminal row visually stale.
        self.assertGreaterEqual(rendered.count("│ ❯ "), 2)

    def test_model_picker_supports_discovered_and_custom_session_models(self) -> None:
        discovered_output = io.StringIO()
        discovered = InlineTerminalUI(
            stdin=io.StringIO("2\n"),
            stdout=discovered_output,
            interactive=False,
            color=False,
            animations=False,
        )
        options = (
            ModelOption("codex-default", "default"),
            ModelOption("gpt-contract", "cache", current=True),
        )

        self.assertEqual(
            discovered.choose_model(options, provider="openai-codex (external)"),
            "gpt-contract",
        )
        self.assertIn("SELECT MODEL", discovered_output.getvalue())
        self.assertIn("gpt-contract  current", discovered_output.getvalue())

        custom = InlineTerminalUI(
            stdin=io.StringIO("c\nmy-custom-model\n"),
            stdout=io.StringIO(),
            interactive=False,
            color=False,
            animations=False,
        )
        self.assertEqual(
            custom.choose_model(options, provider="openai-codex (external)"),
            "my-custom-model",
        )

        searched = InlineTerminalUI(
            stdin=io.StringIO("s gpt\n1\n"),
            stdout=io.StringIO(),
            interactive=False,
            color=False,
            animations=False,
        )
        self.assertEqual(
            searched.choose_model(options, provider="openai-codex (external)"),
            "gpt-contract",
        )

    def test_review_picker_exposes_three_scoped_modes_and_keeps_hard_gates_visible(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO("3\n"),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
        )

        selected = ui.choose_review_mode("approval")

        self.assertEqual(selected, "always-approve")
        rendered = output.getvalue()
        self.assertIn("approval", rendered)
        self.assertIn("auto-review", rendered)
        self.assertIn("always-approve", rendered)
        self.assertIn("hard gates", rendered)
        self.assertIn("employee skill", rendered)
        self.assertIn("approval only", rendered)

    def test_status_line_is_replaceable_but_committed_events_remain_in_scrollback(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=True,
            color=False,
            details=True,
        )

        ui.status("Thinking")
        ui.handle_event(
            ProductEvent(ProductEventType.PLAN_ACCEPTED, "Solo plan accepted")
        )
        ui.status("Running")
        ui.close()

        rendered = output.getvalue()
        self.assertIn(CLEAR_LINE, rendered)
        self.assertIn("◆ Plan · plan · 0 tasks\n", rendered)
        self.assertNotIn("\x1b[?1049h", rendered)

    def test_narrow_layout_is_width_bounded_and_keeps_priority_fields(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=True,
            color=False,
            animations=False,
            terminal_width=48,
        )

        ui.banner(
            workspace="/a/very/long/workspace/path/that/must/be/compacted",
            session_id="session-contract",
            model="a-long-model-identifier",
            provider="openai-codex (external)",
            authority="approval required",
            version="0.0.5",
            roster_revision=3,
            active_employee_count=1,
        )
        ui.show_status(session_id="session-contract", turn_count=3, usage=Usage(model_calls=2))

        rendered = strip_ansi(output.getvalue()).replace("\r", "")
        self.assertIn("NORUCT 0.0.5", rendered)
        self.assertIn("authority", rendered)
        self.assertIn("approval required", rendered)
        self.assertIn("roster", rendered)
        self.assertIn("r3 · 1 active", rendered)
        for line in rendered.splitlines():
            self.assertLessEqual(display_width(line), 48, line)

    def test_clear_repaints_the_same_roster_authority(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=True,
            color=False,
            animations=False,
        )
        ui.banner(
            workspace="/workspace",
            session_id="session-roster",
            model="model",
            provider="provider",
            authority="read-only",
            version="0.0.18",
            roster_revision=4,
            active_employee_count=3,
            employee_roles=_EMPLOYEE_ROLES,
            capabilities=_CAPABILITIES,
            tools=("list", "read", "write*"),
        )

        ui.clear_screen()

        rendered = output.getvalue()
        self.assertEqual(rendered.count("roster        r4 · 3 active"), 2)
        self.assertEqual(rendered.count("tools         list · read · write*"), 2)

    def test_approval_preserves_unified_diff_lines_and_defaults_to_deny(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO("\n"),
            stdout=output,
            interactive=True,
            color=False,
            animations=False,
            terminal_width=72,
        )
        request = replace(
            approval_request(effect=ToolEffect.WRITE, allow_session=False),
            tool_name="apply_workspace_change_set",
            preview=(
                "Apply 1 shadow-generated file change (24 bytes)\n"
                "--- a/file.py\n"
                "+++ b/file.py\n"
                "@@ -1 +1 @@\n"
                "-before value\n"
                "+after value " + "x" * 80
            ),
        )

        decision = ui.ask_approval(request)

        rendered = strip_ansi(output.getvalue())
        self.assertEqual(decision, ApprovalDecision.DENY)
        self.assertIn("APPROVAL · REQUIRED", rendered)
        self.assertIn("│ --- a/file.py", rendered)
        self.assertIn("│ +++ b/file.py", rendered)
        self.assertIn("│ @@ -1 +1 @@", rendered)
        self.assertIn("│ -before value", rendered)
        self.assertIn("↳", rendered)
        self.assertIn("Enter defaults to deny", rendered)

    def test_result_strip_surfaces_status_staffing_parallelism_usage_and_changes(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=True,
            color=False,
            animations=False,
        )
        ui.begin_goal("Update app.py", echo=False)
        result = SimpleNamespace(
            status=SimpleNamespace(value="SUCCEEDED"),
            acceptance_evidence=("Applied shadow change: app.py", "tests passed"),
            unresolved_issues=(),
            metrics=SimpleNamespace(
                unique_employee_count=2,
                maximum_parallelism=2,
                usage=Usage(model_calls=3, tool_calls=1, input_tokens=10, output_tokens=5),
            ),
        )

        ui.result_details(result)

        rendered = strip_ansi(output.getvalue())
        self.assertIn("✓ succeeded", rendered)
        self.assertIn("2 staff", rendered)
        self.assertIn("2 parallel", rendered)
        self.assertIn("3 model", rendered)
        self.assertIn("changed  app.py", rendered)

    def test_validation_event_and_tool_outcome_use_the_same_card_grammar(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
            terminal_width=80,
        )
        raw = RunEvent(
            event_id="event-1",
            run_id="run-1",
            seq=1,
            job_id="job-1",
            task_id="task-implement",
            employee_id="employee-engineer",
            type=EventType.VALIDATION_RECORDED,
            payload={
                "attempt": 2,
                "name": "bounded-tests",
                "passed": True,
                "detail": "4 checks passed",
            },
            usage_delta=None,
            occurred_at=utc_now(),
        )
        mapped = product_event_from_run(raw)
        assert mapped is not None

        ui.handle_event(mapped)
        ui.handle_event(
            ProductEvent(
                ProductEventType.TOOL_FINISHED,
                "run_workspace_command completed",
                task_id="task-implement",
                employee_id="employee-engineer",
                data={"tool_name": "run_workspace_command", "output_bytes": 48},
            )
        )

        rendered = output.getvalue()
        self.assertIn("╭─ VALIDATION · PASS", rendered)
        self.assertIn("bounded-tests", rendered)
        self.assertIn("4 checks passed", rendered)
        self.assertIn("╭─ TOOL · DONE", rendered)
        self.assertIn("run_workspace_command", rendered)

    def test_model_recovery_event_maps_to_live_working_status(self) -> None:
        raw = RunEvent(
            event_id="event-recovery",
            run_id="run-recovery",
            seq=4,
            job_id="job-recovery",
            task_id="task-answer",
            employee_id="employee-generalist",
            type=EventType.MODEL_RECOVERY_REQUESTED,
            payload={
                "reason": "EMPTY_RESPONSE",
                "attempt": 1,
                "max_consecutive_errors": 2,
            },
            usage_delta=None,
            occurred_at=utc_now(),
        )

        mapped = product_event_from_run(raw)

        self.assertIsNotNone(mapped)
        self.assertEqual(mapped.type, ProductEventType.MODEL_WORKING)
        self.assertEqual(mapped.message, "No model reply · recovery 1/2")

    def test_terminal_summary_is_available_to_the_live_task_without_exposing_transcript(self) -> None:
        output = io.StringIO()
        ui = LiveTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
            terminal_width=100,
            terminal_height=32,
            live_screen=True,
        )
        ui.begin_goal("summarize a bounded employee result")
        ui.handle_event(
            ProductEvent(
                ProductEventType.EMPLOYEE_STARTED,
                "employee started",
                task_id="task-summary",
                employee_id="employee-generalist",
            )
        )
        ui.handle_event(
            ProductEvent(
                ProductEventType.EMPLOYEE_FINISHED,
                "employee-generalist succeeded: task-summary",
                task_id="task-summary",
                employee_id="employee-generalist",
                data={
                    "terminal_summary": {
                        "summary": "Validated the change without exposing the transcript.",
                        "usage": {"model_calls": 1},
                    }
                },
            )
        )

        self.assertEqual(ui._live_tasks["task-summary"].status, "succeeded")
        self.assertEqual(
            ui._live_tasks["task-summary"].detail,
            "Validated the change without exposing the transcript.",
        )

    def test_text_delta_and_progress_events_keep_distinct_stream_kinds(self) -> None:
        delta = RunEvent(
            event_id="event-delta",
            run_id="run-stream",
            seq=4,
            job_id="job-stream",
            task_id="task-answer",
            employee_id="employee-generalist",
            type=EventType.MODEL_TEXT_DELTA,
            payload={"text": "streamed answer"},
            usage_delta=None,
            occurred_at=utc_now(),
        )
        progress = replace(
            delta,
            event_id="event-progress",
            seq=5,
            type=EventType.MODEL_STREAM_PROGRESS,
            payload={"received_chars": 15},
        )

        mapped_delta = product_event_from_run(delta)
        mapped_progress = product_event_from_run(progress)

        self.assertIsNotNone(mapped_delta)
        self.assertIsNotNone(mapped_progress)
        self.assertEqual(mapped_delta.data["stream_kind"], "text_delta")
        self.assertEqual(mapped_delta.data["text"], "streamed answer")
        self.assertEqual(mapped_progress.data["stream_kind"], "progress")
        self.assertEqual(mapped_progress.message, "Receiving model response · 15 chars")

    def test_responsive_product_hierarchy_is_bounded_at_four_contract_widths(self) -> None:
        for width in (120, 80, 60, 40):
            with self.subTest(width=width):
                output = io.StringIO()
                ui = InlineTerminalUI(
                    stdin=io.StringIO(),
                    stdout=output,
                    interactive=False,
                    color=False,
                    animations=False,
                    terminal_width=width,
                )
                ui.banner(
                    workspace="/a/long/한글/workspace/path/project",
                    session_id="session-responsive-contract",
                    model="contract-model-with-a-long-name",
                    provider="openai-codex (external)",
                    authority="ask · shadow-only worker",
                    version="0.0.9",
                    roster_revision=2,
                    active_employee_count=2,
                    employee_roles=_EMPLOYEE_ROLES,
                    capabilities=_CAPABILITIES,
                    tools=_TOOLS,
                )
                ui.begin_goal("검증 가능한 변경을 최소 조직으로 구현해줘")
                ui.handle_event(ProductEvent(ProductEventType.COMPILER_STARTED, "compile"))
                ui.handle_event(
                    ProductEvent(
                        ProductEventType.PLAN_ACCEPTED,
                        "dynamic plan",
                        data={"mode": "DYNAMIC", "task_count": 2},
                    )
                )
                ui.handle_event(
                    ProductEvent(
                        ProductEventType.EMPLOYEE_STARTED,
                        "employee started",
                        task_id="task-implementation",
                        employee_id="employee-engineer",
                    )
                )
                ui.answer("변경을 검증했고 결과를 하나로 통합했습니다.")
                ui.result_details(
                    SimpleNamespace(
                        status=SimpleNamespace(value="SUCCEEDED"),
                        acceptance_evidence=(),
                        unresolved_issues=(),
                        metrics=SimpleNamespace(
                            unique_employee_count=2,
                            maximum_parallelism=2,
                            usage=Usage(
                                model_calls=3,
                                tool_calls=1,
                                input_tokens=12_000,
                                output_tokens=640,
                            ),
                        ),
                    )
                )

                rendered = output.getvalue()
                self.assertIn(
                    "COMPANY ONLINE" if width >= 58 else "company snapshot",
                    rendered,
                )
                self.assertNotIn("Welcome to Noruct", rendered)
                self.assertIn("Repository Analyst", rendered)
                self.assertIn("tools", rendered)
                self.assertIn("change set*", rendered)
                self.assertIn("/model", rendered)
                self.assertIn("Company plan", rendered)
                self.assertIn("● Noruct", rendered)
                if width >= 58:
                    self.assertIn(_ASCII_WORDMARK_FIRST_LINE, rendered)
                else:
                    self.assertNotIn(_ASCII_WORDMARK_FIRST_LINE, rendered)
                for line in rendered.splitlines():
                    self.assertLessEqual(display_width(line), width, line)

    def test_plain_surface_contains_no_ansi_or_box_drawing(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=True,
            color=True,
            animations=False,
            plain=True,
        )
        ui.banner(
            workspace="/workspace",
            session_id="session-plain",
            model="model",
            provider="provider",
            authority="read-only",
            version="0.0.9",
        )
        ui.begin_goal("hello")
        ui.answer("plain answer")
        ui.close()

        rendered = output.getvalue()
        self.assertNotIn("\x1b", rendered)
        for character in "╭╮╰╯├┤│─":
            self.assertNotIn(character, rendered)
        self.assertIn("plain answer", rendered)

    def test_integrated_surface_matches_four_width_golden_files(self) -> None:
        for width in (120, 80, 60, 40):
            with self.subTest(width=width):
                expected = (_GOLDEN_ROOT / f"tui-v7-{width}.txt").read_text(encoding="utf-8")
                self.assertEqual(render_golden_surface(width), expected)

    def test_failure_surface_keeps_reason_and_unresolved_items_visible(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            terminal_width=60,
        )
        ui.begin_goal("Run a bounded task")
        ui.handle_event(ProductEvent(ProductEventType.COMPILER_STARTED, "compile"))
        ui.handle_event(
            ProductEvent(
                ProductEventType.JOB_FINISHED,
                "Company job failed",
                data={"status": "FAILED"},
            )
        )
        ui.answer("The company could not complete the requested task.")
        ui.result_details(
            SimpleNamespace(
                status=SimpleNamespace(value="FAILED"),
                acceptance_evidence=(),
                unresolved_issues=("Validation did not pass.",),
                metrics=SimpleNamespace(
                    unique_employee_count=1,
                    maximum_parallelism=1,
                    usage=Usage(model_calls=1),
                ),
            )
        )

        rendered = output.getvalue()
        self.assertIn("× Company job failed", rendered)
        self.assertIn("● Noruct", rendered)
        self.assertIn("UNRESOLVED", rendered)
        self.assertIn("Validation did not pass.", rendered)

    def test_workspace_edit_session_approval_is_cached_but_execute_is_not(self) -> None:
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO("2\n1\n"),
            stdout=output,
            interactive=True,
            color=False,
        )
        controller = InteractiveApprovalController(ui)
        cancellation = CancellationToken()

        first = asyncio.run(
            controller.request(
                approval_request(effect=ToolEffect.WRITE, allow_session=True),
                cancellation,
            )
        )
        cached = asyncio.run(
            controller.request(
                approval_request(effect=ToolEffect.WRITE, allow_session=True),
                cancellation,
            )
        )
        command = asyncio.run(
            controller.request(
                approval_request(effect=ToolEffect.EXECUTE, allow_session=False),
                cancellation,
            )
        )

        self.assertEqual(first, ApprovalDecision.ALLOW_SESSION)
        self.assertEqual(cached, ApprovalDecision.ALLOW_SESSION)
        self.assertEqual(command, ApprovalDecision.ALLOW_ONCE)
        self.assertEqual(output.getvalue().count("APPROVAL · REQUIRED"), 2)

    def test_approval_controller_retries_a_transient_terminal_failure(self) -> None:
        class FlakyUI:
            def __init__(self) -> None:
                self.calls = 0

            def ask_approval(self, _request):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("redraw race")
                return ApprovalDecision.ALLOW_ONCE

        ui = FlakyUI()
        decision = asyncio.run(
            InteractiveApprovalController(ui).request(
                approval_request(effect=ToolEffect.EXECUTE, allow_session=False),
                CancellationToken(),
            )
        )

        self.assertEqual(decision, ApprovalDecision.ALLOW_ONCE)
        self.assertEqual(ui.calls, 2)


class LiveTerminalUITests(unittest.TestCase):
    def test_assessment_component_is_content_free_and_uses_only_task_projection(self) -> None:
        entries = live_assessment_entries(
            stage="REVIEW",
            status="Waiting for user review",
            tasks=(
                SimpleNamespace(
                    status="working",
                    employee="Reviewer",
                    label="check-result",
                    hidden_reasoning="must not render",
                ),
            ),
        )

        rendered = "\n".join(text for text, _ in entries)
        self.assertIn("FOCUS   Reviewer · check-result", rendered)
        self.assertIn("GUARD   protected action remains unexecuted", rendered)
        self.assertNotIn("must not render", rendered)

    def live_ui(
        self,
        *,
        stdin: io.StringIO | None = None,
        width: int = 96,
        height: int = 24,
    ) -> tuple[LiveTerminalUI, io.StringIO]:
        output = io.StringIO()
        ui = LiveTerminalUI(
            stdin=stdin or io.StringIO(),
            stdout=output,
            interactive=True,
            color=False,
            animations=False,
            terminal_width=width,
            terminal_height=height,
            live_screen=True,
        )
        ui.banner(
            workspace="/workspace/project",
            session_id="session-live-contract",
            model="contract-model",
            provider="openai-codex (external)",
            authority="ask · shadow-only worker",
            version="0.0.32",
            roster_revision=2,
            active_employee_count=2,
        )
        return ui, output

    def test_live_turn_defaults_to_expanded_and_repaints_changed_rows(self) -> None:
        ui, output = self.live_ui()
        ui.begin_goal("Inspect the repository and report bounded evidence", echo=False)

        first_frame = ui._live_previous_lines
        before_event = len(output.getvalue())
        ui.handle_event(
            ProductEvent(
                ProductEventType.COMPILER_STARTED,
                "compiler started",
            )
        )
        event_render = output.getvalue()[before_event:]

        self.assertEqual(len(first_frame), 20)
        self.assertTrue(all(display_width(strip_ansi(line)) == 95 for line in first_frame))
        self.assertIn("ctrl+o collapse", strip_ansi("\n".join(first_frame)))
        self.assertIn("ACTIVE WORK", strip_ansi("\n".join(first_frame)))
        self.assertIn("CURRENT ASSESSMENT", strip_ansi("\n".join(first_frame)))
        self.assertNotIn(ALT_SCREEN_ENTER, output.getvalue())
        self.assertNotIn(CLEAR_SCREEN, output.getvalue())
        self.assertNotIn("\n", event_render)
        self.assertGreater(event_render.count("\x1b[2K"), 0)
        self.assertLessEqual(event_render.count("\x1b[2K"), 20)
        ui.close()

    def test_live_direct_stream_uses_one_transcript_lane_and_not_the_surface(self) -> None:
        ui, output = self.live_ui(width=80)
        ui.begin_goal("Answer directly", echo=False)
        ui.handle_event(
            ProductEvent(
                ProductEventType.INPUT_ROUTED,
                "Direct conversation",
                data={"route": "CONVERSATION"},
            )
        )
        for delta in ("Streaming ", "once."):
            ui.handle_event(
                ProductEvent(
                    ProductEventType.MODEL_STREAMING,
                    delta,
                    data={"stream_kind": "text_delta", "text": delta},
                )
            )
        ui.handle_event(
            ProductEvent(
                ProductEventType.JOB_FINISHED,
                "Company job succeeded",
                data={"status": "SUCCEEDED"},
            )
        )
        ui.answer("Streaming once.")

        rendered = output.getvalue()
        surface = strip_ansi("\n".join(ui._live_previous_lines))
        self.assertEqual(rendered.count("● Noruct"), 1)
        self.assertEqual(rendered.count("Streaming once."), 1)
        self.assertNotIn("Streaming once.", surface)
        self.assertNotIn("canonical result", rendered)
        ui.close()

    def test_compact_bottom_dock_remains_an_explicit_option(self) -> None:
        ui, output = self.live_ui()
        ui.toggle_live_view("collapse")
        ui.begin_goal("Inspect the repository and report bounded evidence", echo=False)

        frame = strip_ansi("\n".join(ui._live_previous_lines))

        self.assertEqual(len(ui._live_previous_lines), 6)
        self.assertNotIn("ACTIVE WORK", frame)
        self.assertIn("ctrl+o expand", frame)
        self.assertNotIn(ALT_SCREEN_ENTER, output.getvalue())
        ui.close()

    def test_input_footer_exposes_the_view_toggle_without_a_model_call(self) -> None:
        ui, _ = self.live_ui()
        _, expanded_footer = ui._input_rules()
        ui.toggle_live_view("collapse")
        _, compact_footer = ui._input_rules()

        self.assertIn("/view expand", compact_footer)
        self.assertIn("/view collapse", expanded_footer)

    def test_expanded_company_surface_remains_visible_while_waiting_for_input(self) -> None:
        ui, output = self.live_ui(stdin=io.StringIO("next company goal\n"))

        goal = ui.read_goal()
        frame = strip_ansi("\n".join(ui._live_previous_lines))

        self.assertEqual(goal, "next company goal")
        self.assertTrue(ui._live_active)
        self.assertTrue(ui._live_expanded)
        self.assertEqual(len(ui._live_previous_lines), 20)
        self.assertIn("ACTIVE WORK", frame)
        self.assertIn("CURRENT ASSESSMENT", frame)
        self.assertNotIn("COMPANY RESPONSE", frame)
        self.assertIn("Waiting for your next goal", frame)
        self.assertIn("/view collapse", frame)
        self.assertIn("\x1b[1;4r", output.getvalue())
        self.assertIn("╭─ ❯ ", output.getvalue())
        ui.close()

    def test_current_assessment_projects_next_step_without_a_timeline(self) -> None:
        ui, _ = self.live_ui()
        ui.begin_goal("Inspect the repository and report bounded evidence", echo=False)
        ui.handle_event(
            ProductEvent(
                ProductEventType.APPROVAL_REQUIRED,
                "Approval required before a protected action",
            )
        )

        frame = strip_ansi("\n".join(ui._live_previous_lines))

        self.assertIn("CURRENT ASSESSMENT", frame)
        self.assertIn("NOW     Waiting for user review", frame)
        self.assertIn("WHY     Pause a protected action", frame)
        self.assertIn("NEXT    Wait for the operator", frame)
        self.assertIn("GUARD   protected action remains", frame)
        self.assertNotIn("LIVE ACTIVITY", frame)
        ui.close()

    @unittest.skipUnless(os.name == "posix", "PTY composer regression requires POSIX")
    def test_persistent_surface_keeps_readline_correct_across_soft_wrap_backspace(self) -> None:
        import fcntl
        import pty
        import select
        import struct
        import sys
        import termios
        import time

        process_id, master_fd = pty.fork()
        if process_id == 0:
            exit_code = 0
            try:
                child_ui = LiveTerminalUI(
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    interactive=True,
                    color=False,
                    animations=False,
                    terminal_width=40,
                    terminal_height=24,
                    live_screen=True,
                )
                result = child_ui.read_goal()
                with child_ui._lock:
                    visible = child_ui._live_active and child_ui._live_expanded
                    child_ui._exit_live_locked()
                print(f"RESULT={result!r} VISIBLE={visible!r}", flush=True)
                child_ui.close()
            except BaseException:
                exit_code = 1
            finally:
                os._exit(exit_code)

        fcntl.ioctl(
            master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 24, 40, 0, 0),
        )
        captured = bytearray()
        sent = False
        completed = False
        deadline = time.monotonic() + 3.0
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    continue
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
                if not sent and "╭─ ❯ ".encode() in captured:
                    os.write(
                        master_fd,
                        b"abcdefghijklmnopqrstuvwxyz0123456789"
                        + b"\x7f" * 12
                        + b"XY\n",
                    )
                    sent = True
                if b"RESULT=" in captured:
                    completed = True
                    break
        finally:
            try:
                if not completed:
                    try:
                        os.kill(process_id, 15)
                    except ProcessLookupError:
                        pass
                _, status = os.waitpid(process_id, 0)
            finally:
                os.close(master_fd)

        rendered = captured.decode("utf-8", errors="replace")
        self.assertTrue(sent)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIn("COMPANY WORK", rendered)
        self.assertIn("\x1b[1;4r", rendered)
        self.assertIn("RESULT='abcdefghijklmnopqrstuvwxXY' VISIBLE=True", rendered)

    def test_live_hotkey_buffer_preserves_typeahead_and_unicode_backspace(self) -> None:
        ui, _ = self.live_ui()

        toggled = ui._buffer_live_input_locked(
            "가나".encode("utf-8") + b"\x7f" + b" next" + b"\x1b[D" + b"\x0f"
        )

        self.assertTrue(toggled)
        self.assertEqual(ui._live_typeahead.decode("utf-8"), "가 next")

    def test_live_typeahead_backspace_drops_common_extended_graphemes_as_one_unit(self) -> None:
        cases = (
            ("e\u0301", ""),
            ("👩🏽\u200d💻", ""),
            ("🇰🇷", ""),
            ("one 👩🏽\u200d💻", "one "),
            ("one 🇰🇷", "one "),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                buffer = bytearray(value.encode("utf-8"))
                _drop_last_typeahead_grapheme(buffer)
                self.assertEqual(buffer.decode("utf-8"), expected)

    def test_live_typeahead_incomplete_utf8_backspace_keeps_prior_complete_bytes(self) -> None:
        buffer = bytearray("가".encode("utf-8") + b"\xf0\x9f")

        _drop_last_typeahead_grapheme(buffer)

        self.assertEqual(buffer.decode("utf-8"), "가")

    @unittest.skipUnless(os.name == "posix", "PTY hotkey regression requires POSIX")
    def test_ctrl_o_collapses_the_default_expanded_dock_and_restores_terminal_mode(self) -> None:
        import fcntl
        import pty
        import select
        import struct
        import sys
        import termios
        import time

        process_id, master_fd = pty.fork()
        if process_id == 0:
            exit_code = 0
            try:
                original_mode = termios.tcgetattr(sys.stdin.fileno())
                child_ui = LiveTerminalUI(
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    interactive=True,
                    color=False,
                    animations=False,
                    terminal_width=96,
                    terminal_height=24,
                    live_screen=True,
                )
                child_ui.begin_goal("Collapse the expanded company dock", echo=False)
                time.sleep(0.6)
                expanded = child_ui._live_expanded
                with child_ui._lock:
                    child_ui._exit_live_locked()
                restored = termios.tcgetattr(sys.stdin.fileno()) == original_mode
                queued = child_ui.read_goal()
                child_ui.close()
                print(
                    f"EXPANDED={expanded!r} RESTORED={restored!r} QUEUED={queued!r}",
                    flush=True,
                )
            except BaseException:
                exit_code = 1
            finally:
                os._exit(exit_code)

        fcntl.ioctl(
            master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 24, 96, 0, 0),
        )
        captured = bytearray()
        sent = False
        completed = False
        deadline = time.monotonic() + 3.0
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    continue
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
                if not sent and b"ctrl+o collapse" in captured:
                    os.write(master_fd, b"\x0fnext goal\n")
                    sent = True
                if b"EXPANDED=" in captured:
                    completed = True
                    break
        finally:
            try:
                if not completed:
                    try:
                        os.kill(process_id, 15)
                    except ProcessLookupError:
                        pass
                _, status = os.waitpid(process_id, 0)
            finally:
                os.close(master_fd)

        rendered = captured.decode("utf-8", errors="replace")
        self.assertTrue(sent)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIn("ACTIVE WORK", rendered)
        self.assertIn("ctrl+o expand", rendered)
        self.assertIn("EXPANDED=False RESTORED=True QUEUED='next goal'", rendered)

    def test_live_screen_can_be_explicitly_disabled_without_disabling_styling(self) -> None:
        output = io.StringIO()
        ui = LiveTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=True,
            color=False,
            animations=False,
            live_screen=False,
        )
        ui.begin_goal("Use the inline fallback")
        ui.answer("Inline result")
        ui.close()

        self.assertNotIn(ALT_SCREEN_ENTER, output.getvalue())
        self.assertIn("╭", output.getvalue())
        self.assertIn("Inline result", output.getvalue())

    def test_undersized_terminal_falls_back_before_changing_terminal_modes(self) -> None:
        ui, output = self.live_ui(width=39, height=13)
        ui.begin_goal("Use the safe small-terminal fallback")
        ui.close()

        self.assertNotIn(ALT_SCREEN_ENTER, output.getvalue())
        self.assertIn("safe small-terminal", output.getvalue())
        self.assertIn("fallback", output.getvalue())

    def test_completed_answer_has_one_transcript_owner_and_never_enters_surface(self) -> None:
        ui, output = self.live_ui(width=80)
        ui.begin_goal("Answer with a durable result", echo=False)
        ui.handle_event(
            ProductEvent(
                ProductEventType.JOB_FINISHED,
                "Company job succeeded",
                data={"status": "SUCCEEDED"},
            )
        )
        answer = "A complete company answer remains available after the live viewport closes."
        ui.answer(answer)
        ui.result_details(
            SimpleNamespace(
                status=SimpleNamespace(value="SUCCEEDED"),
                acceptance_evidence=(),
                unresolved_issues=(),
                metrics=SimpleNamespace(
                    unique_employee_count=1,
                    maximum_parallelism=1,
                    usage=Usage(model_calls=1, input_tokens=10, output_tokens=8),
                ),
            )
        )

        rendered = output.getvalue()
        transcript = strip_ansi(rendered)
        self.assertEqual(transcript.count(answer), 1)
        self.assertNotIn(ALT_SCREEN_ENTER, rendered)
        self.assertNotIn(ALT_SCREEN_EXIT, rendered)
        self.assertIn("✓ succeeded", transcript)
        self.assertTrue(ui._live_active)
        self.assertEqual(len(ui._live_previous_lines), 20)
        surface = strip_ansi("\n".join(ui._live_previous_lines))
        self.assertNotIn(answer, surface)
        self.assertNotIn("COMPANY RESPONSE", surface)
        self.assertIn("Waiting for your next goal", surface)
        ui.close()

    def test_goal_transition_repaints_one_fixed_surface_without_allocating_again(self) -> None:
        ui, output = self.live_ui(stdin=io.StringIO("Inspect the fixed surface\n"))
        self.assertEqual(ui.read_goal(), "Inspect the fixed surface")
        before = len(output.getvalue())

        ui.begin_goal("Inspect the fixed surface", echo=False)

        transition = output.getvalue()[before:]
        self.assertNotIn("\n", transition)
        self.assertNotIn("\r\n", transition)
        self.assertEqual(transition.count("ACTIVE WORK"), 1)
        self.assertEqual(ui._live_reserved_rows, 20)
        self.assertTrue(ui._live_active)
        ui.close()

    def test_local_command_output_uses_transcript_region_while_surface_stays_active(self) -> None:
        ui, output = self.live_ui(stdin=io.StringIO("/help\nnext goal\n"))

        self.assertEqual(ui.read_goal(), "/help")
        self.assertTrue(ui._live_transcript_mode)
        ui.show_help()
        self.assertEqual(ui.read_goal(), "next goal")

        rendered = strip_ansi(output.getvalue())
        self.assertIn("Commands", rendered)
        self.assertTrue(ui._live_active)
        self.assertFalse(ui._live_transcript_mode)
        self.assertEqual(ui._live_reserved_rows, 20)
        ui.close()

    def test_terminal_height_change_clears_old_geometry_and_moves_same_surface(self) -> None:
        ui, output = self.live_ui(height=24)
        ui.begin_goal("Follow terminal geometry", echo=False)
        before = len(output.getvalue())

        ui._terminal_height = 28
        with ui._lock:
            ui._render_live_locked()

        resize = output.getvalue()[before:]
        self.assertNotIn("\n", resize)
        self.assertIn("\x1b[5;1H\x1b[2K", resize)
        self.assertIn("\x1b[9;1H\x1b[2K", resize)
        self.assertEqual(ui._live_physical_height, 28)
        self.assertEqual(ui._live_reserved_rows, 20)
        ui.close()

    def test_live_approval_uses_full_scrollback_and_defaults_to_deny(self) -> None:
        ui, output = self.live_ui(stdin=io.StringIO("\n"), height=28)
        ui.begin_goal("Apply a bounded change", echo=False)
        decision = ui.ask_approval(
            replace(
                approval_request(effect=ToolEffect.WRITE, allow_session=False),
                preview="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-before\n+after",
            )
        )

        rendered = strip_ansi(output.getvalue())
        self.assertEqual(decision, ApprovalDecision.DENY)
        self.assertIn("APPROVAL · REQUIRED", rendered)
        self.assertIn("--- a/file.py", rendered)
        self.assertIn("Enter defaults to deny", rendered)
        self.assertNotIn(ALT_SCREEN_ENTER, output.getvalue())
        self.assertTrue(ui._live_active)
        ui.close()

    def test_oversized_approval_temporarily_uses_full_scrollback_preview(self) -> None:
        ui, output = self.live_ui(stdin=io.StringIO("\n"), height=16)
        ui.begin_goal("Review a long change", echo=False)
        long_preview = "\n".join(f"line {index}: changed value" for index in range(30))

        decision = ui.ask_approval(
            replace(
                approval_request(effect=ToolEffect.WRITE, allow_session=False),
                preview=long_preview,
            )
        )

        self.assertEqual(decision, ApprovalDecision.DENY)
        self.assertNotIn(ALT_SCREEN_ENTER, output.getvalue())
        self.assertNotIn(ALT_SCREEN_EXIT, output.getvalue())
        self.assertIn("line 29: changed value", strip_ansi(output.getvalue()))
        self.assertTrue(ui._live_active)
        ui.close()


if __name__ == "__main__":
    unittest.main()
